"""Launch an external command for an MR, stream its progress, and track the job.

Shared by the code-review and QA-test-plan features: both take a command
template from config (e.g. ``claude -p "/code-review {web_url}"``), fill in the
MR's context, and run it as a background subprocess.

The child's stdout is read line-by-line as it runs so the dashboard can show
live progress (an SSE endpoint tails ``job.progress``). If the command speaks
Claude Code's ``--output-format stream-json`` (line-delimited JSON events), we
turn tool_use / assistant events into friendly progress lines and take the final
answer from the ``result`` event. A run can report more than one of those, so the
answer is every result event in order — which is also why the child is launched
with background work turned off (see ``_DEFAULT_CHILD_ENV``): an agent that
defers work to a background subagent reports a placeholder first and its actual
findings only later, if at all. Any other command works too: its stdout lines
become the progress log and the accumulated text becomes the output.

Before launching, the skill's declared context bag is resolved for this MR (see
``skillcontext``): ``{source_root}`` becomes a placeholder like any other, and
— unless ``working_dir`` overrides it — the child runs *in* that checkout, so an
agent's own file tools land on the right tree. A required input that is unset,
or a root that is not a directory, refuses the job instead of launching one that
would review nothing.

Safety: the template is split into argv with ``shlex`` *before* substitution and
run with ``shell=False``, so an MR field can't inject shell metacharacters or
extra arguments; a substituted value that would make a token start with ``-`` is
refused. The child never inherits our GitLab PAT (see ``_ENV_DENYLIST``).
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import SECRET_ENV_NAMES, CommandConfig
from .skillcontext import SkillContextError, job_context
from .worktree import WorktreeError, create_mr_worktree

log = logging.getLogger("radar.commands")

# Env vars never exported to the skill subprocess. The child is an LLM agent fed
# attacker-influenceable MR content; it must not inherit radar's GitLab PAT or
# Jira credentials. When a skill needs context, radar fetches it and pipes it on
# stdin (see context.py); a skill that fetches on its own must carry its own
# credentials (e.g. an MCP server), not borrow radar's.
_ENV_DENYLIST = SECRET_ENV_NAMES

# Exported to every skill, on top of radar's own environment and before the
# skill's `env:` block, which can override or drop any of it.
#
# CLAUDE_CODE_DISABLE_BACKGROUND_TASKS: a headless `claude -p` that spawns a
# background subagent answers the turn immediately with a placeholder ("I'll
# report the findings when it completes") and emits that as a result, landing
# the real answer in a later one — if it is waited for at all. Radar captures
# every result event, so at best the placeholder is glued to the front of the
# output and at worst it is the whole of it. Forcing subagents to run inline
# makes the run's last word its actual work. A skill that would rather keep its
# parallelism can drop the variable with `env_unset:`.
#
# CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS: how long `claude -p` waits for still-
# running background agents before killing them and exiting with whatever it
# has (0 = wait forever; the CLI's default gives up after 10 minutes). Inert
# while the variable above keeps everything inline, but a skill can always
# escape to the background anyway — by opting out, or by shelling out to
# another `claude` itself — and then this is the difference between findings
# and a "still running" note. With the ceiling gone, the only clock left is
# radar's own `timeout_seconds`, which the operator sizes to the skill.
_DEFAULT_CHILD_ENV = {
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0",
}


def _is_secret_env(name: str) -> bool:
    """Whether a variable name is one of radar's own credentials.

    Case-insensitive, because Windows resolves environment variables that way:
    a child handed ``gitlab_token`` reads it back as ``GITLAB_TOKEN``.
    """
    return name.upper() in _ENV_DENYLIST


def _unset_env(env: dict, name: str) -> None:
    """Drop a variable from a child environment, matching the platform's own
    name rules (Windows ignores case, so the uppercase twin must go too)."""
    if os.name == "nt":
        for key in [k for k in env if k.upper() == name.upper()]:
            del env[key]
    else:
        env.pop(name, None)

_MAX_OUTPUT = 200_000    # cap captured output to bound memory / stored plan size
_MAX_JOBS = 256          # bound the in-memory job registry (evict oldest)
_MAX_PROGRESS = 500      # cap the per-job progress log

PLACEHOLDER_KEYS = (
    "web_url",
    "mr_iid",
    "project_id",
    "source_branch",
    "target_branch",
    "title",
    "author",
    "jira_keys",       # space-separated, e.g. "PROJ-1 PROJ-2"
    "jira_keys_csv",   # comma-separated, e.g. "PROJ-1,PROJ-2"
    "head_sha",        # the MR's head commit, as of the last poll
    "source_root",     # the checkout for this job (see skillcontext / worktree)
)

# Placeholders filled from the MR snapshot; the rest are computed per job.
SNAPSHOT_KEYS = tuple(k for k in PLACEHOLDER_KEYS if k != "source_root")


class CommandError(ValueError):
    """The command template + MR context can't be turned into a safe argv."""


# The line `claude -p` prints on stderr when its background-agent wait ceiling
# elapses and it kills agents that were still working. Radar's default ceiling
# is 0 (wait forever), so seeing this means something overrode it — and that the
# run's output is a deferral note, not the findings.
#
# Deliberately specific: a false positive throws away a finished review (the
# branch skips `on_success`, so nothing is stored), while a false negative only
# leaves today's behaviour of storing the note. So this matches the CLI's own
# sentence at the start of a line, not the phrase wherever it appears — a review
# that happens to discuss background tasks, or an MCP server logging about them,
# must not cost the operator their result. The tail is left loose because the
# sentence is undocumented and a release may reword it.
_BG_SWEPT_RE = re.compile(r"(?im)^\s*background tasks still running after\b.*?\bterminat")

# The status the Agent tool reports for a subagent dispatched to the background.
_ASYNC_LAUNCH_STATUS = "async_launched"

# How long to keep reading a pipe after the child itself has exited. Only in
# play when something the child spawned inherited the pipe and outlived it.
_DRAIN_GRACE = 5.0

# Every live skill process, so radar can take them down when it exits: with the
# wait ceiling at 0 the CLI never reaps its own background agents, and a job's
# timeout dies with the process running it. Registered while a child is alive,
# swept by the atexit hook below (which covers a clean exit and Ctrl-C; a
# SIGKILLed radar can't run code at all).
_LIVE: dict[int, tuple[subprocess.Popen, int | None]] = {}
_LIVE_LOCK = threading.Lock()


def _kill_tree(proc: subprocess.Popen, pgid: int | None) -> None:
    """Kill the child and everything it spawned, not just the child.

    ``proc.kill()`` alone ends the direct child while a backgrounded agent it
    launched keeps running (and keeps spending API tokens) — and if that agent
    inherited our stdout, keeps the pipe's write end open. On POSIX the child
    leads its own process group (``start_new_session`` at launch) so the group
    can be signalled as a unit; on Windows ``taskkill /T`` walks the tree.

    Both are best effort, and neither reaches a descendant that put *itself* in
    a new session or was re-parented after its own parent died. The direct
    ``kill()`` stays as a backstop, which is why every step here is guarded: a
    tree kill that raises must not skip it.

    ``pgid`` is captured at spawn rather than read from ``proc.pid`` here,
    because a caller may signal after the child was reaped and its pid could
    then belong to somebody else. Callers only reach that path while a group
    member is provably still alive, which keeps the id allocated.
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            pass  # missing from PATH, or wedged — fall through to kill()
    elif pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:  # group already empty
            pass
    try:
        proc.kill()
    except OSError:  # pragma: no cover - already reaped
        pass


def _join_all(threads: list[threading.Thread], grace: float) -> bool:
    """Wait up to ``grace`` for all of them together; True if any is still
    running. One shared deadline, not one each: the point is to bound how long
    a job can be held up by a pipe nobody is going to close."""
    deadline = time.monotonic() + grace
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    return any(thread.is_alive() for thread in threads)


def _sweep_live_children() -> None:
    """Kill any skill still running when radar exits, so nothing outlives the
    only supervisor it had."""
    with _LIVE_LOCK:
        live = list(_LIVE.values())
    for proc, pgid in live:
        if proc.poll() is None:
            log.warning("radar is exiting; stopping skill process %s", proc.pid)
            _kill_tree(proc, pgid)


atexit.register(_sweep_live_children)


def _content_blocks(obj: dict) -> list:
    """The content blocks of a stream-json message event, or nothing.

    Every shape here is the child's to choose and radar's to survive, so a
    message that is a string, or content that is not a list, reads as empty
    rather than raising on a reader thread.
    """
    message = obj.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    return content if isinstance(content, list) else []


def _async_agent_launched(obj: dict) -> bool:
    """Whether this stream-json event reports a subagent sent to the background.

    Structural on purpose: the marker is advisory (it only sharpens a timeout
    message), so matching the word anywhere in the line would be all cost and
    no benefit — a diff, a tool result, or a review discussing this very code
    would trip it and misdirect the operator.
    """
    if obj.get("status") == _ASYNC_LAUNCH_STATUS:
        return True
    for block in _content_blocks(obj):
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        content = block.get("content")
        for part in content if isinstance(content, list) else [content]:
            if isinstance(part, dict):
                part = part.get("text")
            if not isinstance(part, str) or _ASYNC_LAUNCH_STATUS not in part:
                continue
            try:
                payload = json.loads(part)
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict) and payload.get("status") == _ASYNC_LAUNCH_STATUS:
                return True
    return False


# Which of a tool's inputs actually says what the agent is doing. "Bash" alone,
# forty times over, tells an operator watching a long review nothing; the command
# it ran tells them where the review has got to. First key present wins, so the
# human-written description beats the machinery when a tool offers both.
_TOOL_DETAIL_KEYS = (
    "description", "command", "file_path", "notebook_path", "pattern",
    "query", "url", "prompt", "path", "skill", "subagent_type",
)


def _tool_detail(name: str, tool_input: object) -> str:
    """One line naming what this tool call is doing, e.g. ``Bash: git diff``."""
    if not isinstance(tool_input, dict):
        return name
    for key in _TOOL_DETAIL_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            detail = _short(value, 90)
            # Grep/Glob read as a pattern applied somewhere; the path is the
            # half that says which part of the tree is being looked at.
            where = tool_input.get("path") or tool_input.get("glob")
            if key == "pattern" and isinstance(where, str) and where.strip():
                detail = f"{detail} in {_short(where, 40)}"
            return f"{name}: {detail}"
    return name


def build_argv(command: str, ctx: dict) -> list[str]:
    """Split a command template into argv, then substitute placeholders into
    each token (never re-splitting substituted values).

    On Windows we split with ``posix=False`` so backslash paths survive, then
    strip the quotes shlex leaves on quoted tokens. Substituting after the
    split means an MR field can never inject extra args (run with shell=False).

    Argument-injection guard: if substitution makes a token *start* with ``-``
    when its template didn't (e.g. template ``tool {title}`` with a title of
    ``--upload-file``), the injected value would be read as a flag by the target
    tool. We refuse rather than smuggle a flag.
    """
    posix = os.name != "nt"
    tokens = shlex.split(command, posix=posix)
    argv = []
    for token in tokens:
        if not posix and len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
            token = token[1:-1]
        template_token = token
        for key in PLACEHOLDER_KEYS:
            # A snapshot column is nullable (an MR polled before `head_sha`
            # existed, an MR with no author), and `str(None)` is the four-letter
            # string "None" — which a skill would take for a real ref, branch or
            # URL. An absent value is an empty one.
            value = ctx.get(key)
            token = token.replace("{" + key + "}", "" if value is None else str(value))
        if token.startswith("-") and not template_token.startswith("-"):
            raise CommandError(
                "refusing to run: a substituted MR value would start with '-' and "
                f"be read as a flag (token {template_token!r} -> {token!r}). "
                "Embed the placeholder after a fixed prefix, e.g. --arg={placeholder}."
            )
        argv.append(token)
    return argv


@dataclass
class CommandJob:
    id: str
    kind: str  # "review" or "qa"
    project_id: int
    mr_iid: int
    title: str = ""
    status: str = "running"  # running / done / error
    output: str = ""
    error: str = ""
    persist_error: str = ""  # set if the result was produced but couldn't be saved
    returncode: int | None = None
    progress: list[dict] = field(default_factory=list)  # live log: {kind, text}
    # The panel's countdown, measured on the same clock the worker enforces the
    # budget with. Wall clock would drift from it across a host suspend or an
    # NTP step and show time the run does not actually have.
    started_mono: float = 0.0
    budget_s: int = 0
    # Progress items carry a stable `id` (identity, so the browser can update a
    # line it already drew) and a `rev` from this counter (bumped on append AND
    # on a collapse, so "what changed since?" has one answer covering both).
    progress_next_id: int = 0
    progress_rev: int = 0
    # Where the child runs. Per job, not per skill: the source root is resolved
    # from the MR's project, so two projects reviewed by one skill run in their
    # own checkouts (see `CommandRunner.start`).
    cwd: str | None = None


def _fail(job: CommandJob, message: str) -> None:
    """Move a job to a terminal error state (error text set before status)."""
    job.error = message[:8000]
    job.status = "error"


def _with_deadline(fn: Callable[[], object], seconds: float, what: str) -> object:
    """Run ``fn`` on a helper thread and give up on it after ``seconds``.

    Preparing a job means calling out to GitLab or Jira, and a socket with no
    answer coming back has no timeout of its own — the job would sit in
    "running" forever and the panel would tail it just as long. There is no way
    to interrupt a blocking read from outside, so the thread is abandoned
    (daemon, so it cannot hold up shutdown) and the job is failed. The work it
    was doing is a read; nothing is left half-written.
    """
    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=max(0.0, seconds))
    if worker.is_alive():
        raise TimeoutError(what)
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box.get("value")


def _feed_stdin(proc, text: str) -> None:
    try:
        proc.stdin.write(text)
        proc.stdin.close()
    except (OSError, ValueError):  # child exited / pipe closed early
        pass


def _short(text: str, limit: int = 140) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class CommandRunner:
    """Owns background command jobs for the process lifetime (in-memory)."""

    def __init__(self, config: CommandConfig, kind: str):
        self.config = config
        self.kind = kind
        self._jobs: dict[str, CommandJob] = {}
        self._lock = threading.Lock()

    @property
    def checkout(self) -> str:
        """"none" (run in the configured source) or "worktree" (one per job)."""
        return getattr(self.config, "checkout", "none")

    def start(
        self,
        ctx: dict,
        on_success: Callable[[CommandJob], None] | None = None,
        stdin_provider: Callable[[str], str] | None = None,
    ) -> CommandJob:
        job = CommandJob(
            id=uuid.uuid4().hex[:12],
            kind=self.kind,
            project_id=int(ctx["project_id"]),
            mr_iid=int(ctx["mr_iid"]),
            title=str(ctx.get("title", "")),
            started_mono=time.monotonic(),
            budget_s=self.config.timeout_seconds,
        )
        with self._lock:
            self._jobs[job.id] = job
            while len(self._jobs) > _MAX_JOBS:  # evict oldest so serve doesn't leak
                self._jobs.pop(next(iter(self._jobs)))
        try:
            # Resolve the skill's declared context first: a required var that is
            # unset, or a source root that is not a directory, refuses the job
            # here rather than launching an agent that would review nothing.
            # Cheap (env reads), so it stays on the request thread and the button
            # reports a misconfiguration immediately.
            resolved = job_context(self.config, job.project_id, str(ctx.get("web_url") or ""))
            resolved.raise_for_problems()
            if self.checkout == "worktree" and not resolved.source_root:
                raise SkillContextError(
                    "checkout: worktree needs a 'source:' that resolves to this project's "
                    "checkout — there is nothing to make a worktree of"
                )
        except SkillContextError as exc:
            job.status, job.error = "error", str(exc)
            return job
        threading.Thread(
            target=self._run,
            args=(job, ctx, resolved, on_success, stdin_provider),
            daemon=True,
        ).start()
        return job

    def get(self, job_id: str) -> CommandJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def progress_since(self, job_id: str, after_rev: int) -> tuple[list[dict], str] | None:
        """Progress items changed since ``after_rev``, plus the job's status, or
        None if the job is unknown.

        Filtering on the revision rather than on the id covers both things that
        can happen to the log: a new line appended, and an existing line's count
        going up when its event repeats. A reader tracking ids alone would never
        learn about the second, and the line it drew would sit there stale.

        ``id`` identifies the line across those updates — the log is capped by
        dropping from the front, so list positions shift and ids don't. The
        bookkeeping a collapse needs stays server-side; a reader gets only what
        it draws.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            items = [
                {"id": item["id"], "rev": item["rev"], "kind": item["kind"], "text": item["text"]}
                for item in job.progress
                if item["rev"] > after_rev
            ]
            return items, job.status

    def _add(self, job: CommandJob, kind: str, text: str) -> None:
        with self._lock:
            # Collapse an immediate repeat into a count on the line already
            # there. A run that reads twenty files says twenty different things
            # now, so a true repeat is a loop worth seeing as one line rather
            # than as twenty identical ones scrolling the rest off the panel.
            job.progress_rev += 1
            last = job.progress[-1] if job.progress else None
            if last is not None and last["kind"] == kind and last["base"] == text:
                last["count"] += 1
                last["text"] = f"{text} (×{last['count']})"
                last["rev"] = job.progress_rev
                return
            job.progress.append({
                "id": job.progress_next_id,
                "rev": job.progress_rev,
                "kind": kind,
                "text": text,
                "base": text,   # what to compare a repeat against
                "count": 1,
            })
            job.progress_next_id += 1
            overflow = len(job.progress) - _MAX_PROGRESS
            if overflow > 0:
                del job.progress[:overflow]

    # --- execution ---------------------------------------------------------

    def _run(self, job: CommandJob, ctx: dict, resolved, on_success, stdin_provider=None) -> None:
        # Catch-all guarantees a terminal state; a worker crash must never leave
        # the job "running" (the UI would tail it forever).
        #
        # `timeout_seconds` is the budget for the whole job, not just for the
        # command: fetching a checkout and fetching the MR's context happen
        # before the command starts, and both talk to the network. Bounding only
        # the child would leave the two phases most likely to hang unbounded.
        deadline = time.monotonic() + self.config.timeout_seconds
        worktree = None
        try:
            if self.checkout == "worktree":
                self._add(job, "log", "preparing this merge request's worktree…")
                worktree = create_mr_worktree(
                    resolved.source_root,
                    job.mr_iid,
                    str(ctx.get("head_sha") or "") or None,
                    remote=getattr(self.config, "remote", "origin"),
                    timeout_s=deadline - time.monotonic(),
                )
            source_root = str(worktree.path) if worktree else (resolved.source_root or "")
            self._execute(job, ctx, source_root, resolved, deadline, on_success, stdin_provider)
        except WorktreeError as exc:
            _fail(job, str(exc))
        except TimeoutError as exc:
            _fail(job, f"{self.kind} timed out after {self.config.timeout_seconds}s ({exc})")
        except Exception as exc:  # noqa: BLE001 - last-resort terminal state
            log.exception("%s worker crashed", self.kind)
            _fail(job, f"unexpected error: {exc}")
        finally:
            if worktree is not None:
                worktree.cleanup()

    def _execute(
        self,
        job: CommandJob,
        ctx: dict,
        source_root: str,
        resolved,
        deadline: float,
        on_success,
        stdin_provider=None,
    ) -> None:
        # Built here, not in `start`, because a worktree's path is only known
        # once the worker has made it — and `{source_root}` must name the tree
        # the skill will actually read.
        try:
            argv = build_argv(self.config.command, {**ctx, "source_root": source_root})
        except CommandError as exc:
            _fail(job, str(exc))
            return
        if not argv:
            _fail(job, f"{self.kind}.command is empty")
            return
        # An explicit working_dir wins; otherwise the checkout is the natural
        # place to run, so the agent's own file tools land on the right tree.
        job.cwd = self.config.working_dir or source_root or None

        # Fetch backend context (MR diff / Jira ticket) to pipe on stdin. Runs in
        # this worker thread; a failure here surfaces as a job error.
        stdin_text: str | None = None
        if stdin_provider is not None:
            self._add(job, "log", "fetching context…")
            stdin_text = _with_deadline(
                lambda: stdin_provider(source_root, resolved.inputs.shown),
                deadline - time.monotonic(),
                "fetching context",
            )

        try:
            proc = subprocess.Popen(
                argv,
                cwd=job.cwd,
                stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",   # decode as UTF-8 regardless of OS locale
                errors="replace",
                bufsize=1,          # line-buffered, for live streaming
                env=self._child_env(),
                # Group leader on POSIX so a timeout can kill the whole tree
                # (see _kill_tree); Windows gets the tree via taskkill instead.
                start_new_session=(os.name != "nt"),
            )
        except FileNotFoundError:
            _fail(job, f"command not found: {argv[0]!r} (is it on PATH?)")
            return
        except OSError as exc:  # pragma: no cover - defensive
            _fail(job, f"failed to launch {self.kind}: {exc}")
            return

        # With start_new_session the child leads a new group whose id is its pid.
        # Captured now, while the pid is certainly still the child's.
        pgid = proc.pid if os.name != "nt" else None
        with _LIVE_LOCK:
            _LIVE[proc.pid] = (proc, pgid)

        if stdin_text is not None:
            # Write on a thread so a large bundle can't deadlock against stdout.
            threading.Thread(
                target=_feed_stdin, args=(proc, stdin_text), daemon=True
            ).start()

        result_parts: list[str] = []  # final answer from stream-json 'result'
        raw_parts: list[str] = []     # accumulated plain-text output
        stats: dict = {}              # facts the drains gleaned about the run
        stderr_box: list[str] = []
        # Both pipes are drained on their own threads. EOF needs every write end
        # closed, and anything the child spawned that inherited a pipe can hold
        # it open past the child's death — so the worker waits for the CHILD,
        # which is the event the job actually depends on, and never blocks on a
        # read that a survivor could stall forever.
        drains = [
            threading.Thread(
                target=lambda: self._drain(
                    job, proc.stdout, stats,
                    lambda line: self._ingest(job, line, result_parts, raw_parts, stats),
                ),
                daemon=True,
            ),
            threading.Thread(
                target=lambda: self._drain(job, proc.stderr, stats, stderr_box.append),
                daemon=True,
            ),
        ]
        for drain in drains:
            drain.start()

        # What is left of the job's budget after preparing — never less than a
        # second, so a command that only just made the deadline still gets to
        # report something rather than being killed on the starting line.
        #
        # The wait and the kill live on this one thread: a timer thread killing
        # by pid races the reap here, and could signal a pid the OS had already
        # handed to somebody else. Here the child is provably unreaped when the
        # kill goes out, so its group id is still its own.
        remaining = max(1.0, deadline - time.monotonic())
        timed_out = False
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            timed_out = True
            # Kills the tree: with the wait ceiling defaulted to 0 the CLI never
            # reaps its own background agents, so this is the only thing that
            # stops one from running (and spending) past the job.
            _kill_tree(proc, pgid)
            proc.wait()

        if _join_all(drains, _DRAIN_GRACE):
            # The child is gone but our pipes are still held: it left something
            # behind. That survivor is both the reason a read can't finish and
            # a process running on radar's behalf with nothing supervising it,
            # so take the tree down and collect what the drains can still read.
            # Safe to signal by group here — a live member keeps the id from
            # being reused, and a live member is exactly what we just proved.
            log.warning("%s left a process holding its pipes; stopping them", self.kind)
            _kill_tree(proc, pgid)
            still_held = _join_all(drains, _DRAIN_GRACE)
            if still_held:
                # Best effort ran out: a descendant that re-parented or put
                # itself in a new session is beyond both kill paths. Say so in
                # the log the panel tails rather than in the result, because the
                # output collected so far is usually the whole of what the child
                # wrote — the survivor is holding the pipe, not still filling it.
                self._add(job, "log", (
                    "a process this run left behind is still holding its output "
                    "pipe; anything it writes from here on is not captured"
                ))
        with _LIVE_LOCK:
            _LIVE.pop(proc.pid, None)

        job.returncode = proc.returncode
        stderr_text = "".join(stderr_box)

        # Blank line between results: a run can report more than one, and gluing
        # them together swallows the heading or list the next one opens with.
        output = ("\n\n".join(result_parts) if result_parts else "".join(raw_parts))[:_MAX_OUTPUT]

        if timed_out:
            # Keep what the run did manage to say. A long review that ran out of
            # clock is more use half-written than replaced by the word "timeout".
            job.output = output
            detail = f"{self.kind} timed out after {self.config.timeout_seconds}s"
            if stats.get("async_agent"):
                detail += (
                    " while a background agent it launched was still working "
                    "(the whole process tree was stopped). Raise timeout_seconds "
                    "to outlast the work, or let radar's default env keep "
                    "subagents inline."
                )
            _fail(job, detail)
            return

        # A drain that raised took the rest of that pipe with it, so whatever
        # was captured is a fragment of the run. Report the fault instead of
        # publishing the fragment as the result.
        if stats.get("drain_error"):
            job.output = output
            _fail(job, f"could not read the {self.kind}'s output: {stats['drain_error']}")
            return

        if proc.returncode == 0 and output.strip():
            if _BG_SWEPT_RE.search(stderr_text):
                # The CLI killed background agents it was still waiting for and
                # exited with a deferral note instead of the findings. Radar's
                # default ceiling (0 = wait forever) makes this impossible, so
                # something overrides it. Keep the note visible under the error,
                # but never store it as the result.
                job.output = output
                _fail(job, (
                    "the CLI stopped waiting for a background agent the skill "
                    "launched and exited before it reported — the output below "
                    "is the run's deferral note, not the findings. Something "
                    "overrides radar's default CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0 "
                    "(wait forever): check this skill's env: and env_unset:, the "
                    "shell radar runs in, and .env."
                ))
                return
            # Publish output BEFORE flipping status so a reader that sees "done"
            # always sees the output too.
            job.output = output
            if on_success is not None:
                try:
                    on_success(job)
                except Exception as exc:  # noqa: BLE001 - report, don't crash
                    log.exception("%s result produced but not saved", self.kind)
                    job.persist_error = f"result was generated but could not be saved: {exc}"
            job.status = "done"
        else:
            detail = stderr_text.strip() or output or f"exited with code {proc.returncode}"
            _fail(job, detail.strip())

    def _drain(self, job: CommandJob, pipe, stats: dict, consume: Callable[[str], object]) -> None:
        """Read one of the child's pipes to EOF, handing each line to ``consume``.

        Runs on its own thread, so an exception here would otherwise vanish and
        take the rest of the pipe with it — leaving a fragment of the run to be
        published as if it were the whole. Anything unexpected is recorded for
        the worker to report; a torn-down pipe (the normal end of a killed run)
        is not an error.
        """
        try:
            for line in pipe:
                consume(line)
        except (OSError, ValueError):  # pipe torn down under us after a kill
            pass
        except Exception as exc:  # noqa: BLE001 - surfaced by the worker
            log.exception("%s output reader failed", self.kind)
            stats.setdefault("drain_error", f"{type(exc).__name__}: {exc}")
        finally:
            # Whoever read the pipe closes it. The worker cannot: closing takes
            # the buffer's lock, which this thread holds while blocked on a read
            # a survivor is keeping open — the worker would block behind it for
            # exactly as long as it was trying to avoid waiting.
            try:
                pipe.close()
            except (OSError, ValueError):  # pragma: no cover - already closed
                pass

    def _ingest(
        self,
        job: CommandJob,
        line: str,
        result_parts: list[str],
        raw_parts: list[str],
        stats: dict,
    ) -> None:
        """Handle one line of the child's stdout: parse Claude stream-json into
        progress + final result, or treat it as plain output."""
        line = line.rstrip("\r\n")
        if not line.strip():
            return
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            raw_parts.append(line + "\n")
            self._add(job, "log", _short(line))
            return
        if not isinstance(obj, dict):
            raw_parts.append(line + "\n")
            return

        # A subagent dispatched to the background: the findings then depend on
        # that agent finishing, which is worth naming if the budget cuts it short.
        if _async_agent_launched(obj):
            stats["async_agent"] = True

        event_type = obj.get("type")
        if event_type == "assistant":
            for block in _content_blocks(obj):
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "tool_use":
                    name = block.get("name")
                    self._add(job, "tool", _tool_detail(
                        name if isinstance(name, str) and name else "tool",
                        block.get("input"),
                    ))
                elif block_type == "text" and str(block.get("text", "")).strip():
                    self._add(job, "text", _short(block["text"]))
        elif event_type == "result":
            res = obj.get("result")
            if isinstance(res, str):
                result_parts.append(res)
            if obj.get("is_error"):
                self._add(job, "log", "run reported an error")
        elif event_type == "system":
            # Only the one that means a session actually started says so. A run
            # emits other system events as it goes (context compaction, and
            # whatever a later CLI adds), and logging them all as "session
            # started" reads like the run keeps restarting. An event with no
            # subtype keeps the old wording rather than vanishing: a CLI that
            # stops labelling its init should still show a session line.
            subtype = obj.get("subtype")
            if not isinstance(subtype, str) or not subtype.strip() or subtype == "init":
                self._add(job, "log", "session started")
            else:
                # Child-controlled text, so bounded like every other line here.
                self._add(job, "log", _short(subtype.replace("_", " "), 90))
        # other event types (tool results, partial deltas) are ignored in the log

    def _child_env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if not _is_secret_env(k)}
        env["PYTHONIOENCODING"] = "utf-8"  # nudge Python skills to emit UTF-8
        # setdefault, not update: an operator who exported one of these before
        # starting radar has said what they want, and radar is filling a gap
        # rather than overruling them.
        for name, value in _DEFAULT_CHILD_ENV.items():
            env.setdefault(name, value)
        # The skill's own env has the last word — except over the denylist. The
        # config parser refuses those names too, but the promise that the child
        # never sees radar's credentials belongs to the function that builds the
        # child's environment, not to whoever happened to construct the config.
        for name, value in dict(getattr(self.config, "env", ()) or ()).items():
            if not _is_secret_env(name):
                env[name] = value
        for name in getattr(self.config, "env_unset", ()) or ():
            _unset_env(env, name)
        return env
