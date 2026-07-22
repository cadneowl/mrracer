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
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import CommandConfig

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
)


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
            token = token.replace("{" + key + "}", str(ctx.get(key, "")))
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


def _fail(job: CommandJob, message: str) -> None:
    """Move a job to a terminal error state (error text set before status)."""
    job.error = message[:8000]
    job.status = "error"


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

    def start(
        self,
        ctx: dict,
        on_success: Callable[[CommandJob], None] | None = None,
        stdin_provider: Callable[[], str] | None = None,
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
            argv = build_argv(self.config.command, ctx)
        except CommandError as exc:
            job.status, job.error = "error", str(exc)
            return job
        threading.Thread(
            target=self._run, args=(job, argv, on_success, stdin_provider), daemon=True
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

    def _run(self, job: CommandJob, argv: list[str], on_success, stdin_provider=None) -> None:
        # Catch-all guarantees a terminal state; a worker crash must never leave
        # the job "running" (the UI would tail it forever).
        try:
            self._execute(job, argv, on_success, stdin_provider)
        except Exception as exc:  # noqa: BLE001 - last-resort terminal state
            log.exception("%s worker crashed", self.kind)
            _fail(job, f"unexpected error: {exc}")

    def _execute(self, job: CommandJob, argv: list[str], on_success, stdin_provider=None) -> None:
        if not argv:
            _fail(job, f"{self.kind}.command is empty")
            return

        # Fetch backend context (MR diff / Jira ticket) to pipe on stdin. Runs in
        # this worker thread; a failure here surfaces as a job error.
        stdin_text: str | None = None
        if stdin_provider is not None:
            self._add(job, "log", "fetching context…")
            stdin_text = stdin_provider()

        try:
            proc = subprocess.Popen(
                argv,
                cwd=self.config.working_dir or None,
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
        timer = threading.Timer(self.config.timeout_seconds, lambda: (timed_out.set(), proc.kill()))
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
