"""Launch an external command for an MR, stream its progress, and track the job.

Shared by the code-review and QA-test-plan features: both take a command
template from config (e.g. ``claude -p "/code-review {web_url}"``), fill in the
MR's context, and run it as a background subprocess.

The child's stdout is read line-by-line as it runs so the dashboard can show
live progress (an SSE endpoint tails ``job.progress``). If the command speaks
Claude Code's ``--output-format stream-json`` (line-delimited JSON events), we
turn tool_use / assistant events into friendly progress lines and take the final
answer from the ``result`` event. Any other command works too: its stdout lines
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

import json
import logging
import os
import shlex
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import CommandConfig
from .skillcontext import SkillContextError, job_context
from .worktree import WorktreeError, create_mr_worktree

log = logging.getLogger("radar.commands")

# Env vars never exported to the skill subprocess. The child is an LLM agent fed
# attacker-influenceable MR content; it must not inherit radar's GitLab PAT or
# Jira credentials. When a skill needs context, radar fetches it and pipes it on
# stdin (see context.py); a skill that fetches on its own must carry its own
# credentials (e.g. an MCP server), not borrow radar's.
_ENV_DENYLIST = frozenset(
    {"GITLAB_TOKEN", "GITLAB_URL", "JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"}
)

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

    def progress_since(self, job_id: str, since: int) -> tuple[list[dict], str] | None:
        """New progress items from index ``since`` plus the job's status, or
        None if the job is unknown."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return list(job.progress[since:]), job.status

    def _add(self, job: CommandJob, kind: str, text: str) -> None:
        with self._lock:
            job.progress.append({"kind": kind, "text": text})
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
            )
        except FileNotFoundError:
            _fail(job, f"command not found: {argv[0]!r} (is it on PATH?)")
            return
        except OSError as exc:  # pragma: no cover - defensive
            _fail(job, f"failed to launch {self.kind}: {exc}")
            return

        if stdin_text is not None:
            # Write on a thread so a large bundle can't deadlock against stdout.
            threading.Thread(
                target=_feed_stdin, args=(proc, stdin_text), daemon=True
            ).start()

        # Drain stderr concurrently so a chatty child can't deadlock on a full pipe.
        stderr_box: list[str] = []
        stderr_thread = threading.Thread(
            target=lambda: stderr_box.append(proc.stderr.read() or ""), daemon=True
        )
        stderr_thread.start()

        timed_out = threading.Event()
        # What is left of the job's budget after preparing — never less than a
        # second, so a command that only just made the deadline still gets to
        # report something rather than being killed on the starting line.
        remaining = max(1.0, deadline - time.monotonic())
        timer = threading.Timer(remaining, lambda: (timed_out.set(), proc.kill()))
        timer.start()

        result_parts: list[str] = []  # final answer from stream-json 'result'
        raw_parts: list[str] = []     # accumulated plain-text output
        try:
            for line in proc.stdout:
                self._ingest(job, line, result_parts, raw_parts)
        finally:
            timer.cancel()
        proc.wait()
        stderr_thread.join(timeout=1.0)
        job.returncode = proc.returncode

        if timed_out.is_set():
            _fail(job, f"{self.kind} timed out after {self.config.timeout_seconds}s")
            return

        output = ("".join(result_parts) if result_parts else "".join(raw_parts))[:_MAX_OUTPUT]
        if proc.returncode == 0 and output.strip():
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
            detail = "".join(stderr_box).strip() or output or f"exited with code {proc.returncode}"
            _fail(job, detail.strip())

    def _ingest(
        self, job: CommandJob, line: str, result_parts: list[str], raw_parts: list[str]
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

        event_type = obj.get("type")
        if event_type == "assistant":
            for block in (obj.get("message") or {}).get("content") or []:
                block_type = block.get("type")
                if block_type == "tool_use":
                    self._add(job, "tool", f"using {block.get('name', 'tool')}")
                elif block_type == "text" and str(block.get("text", "")).strip():
                    self._add(job, "text", _short(block["text"]))
        elif event_type == "result":
            res = obj.get("result")
            if isinstance(res, str):
                result_parts.append(res)
            if obj.get("is_error"):
                self._add(job, "log", "run reported an error")
        elif event_type == "system":
            self._add(job, "log", "session started")
        # other event types (tool results, partial deltas) are ignored in the log

    def _child_env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in _ENV_DENYLIST}
        env["PYTHONIOENCODING"] = "utf-8"  # nudge Python skills to emit UTF-8
        return env
