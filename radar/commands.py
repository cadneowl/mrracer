"""Launch an external command for an MR and track the job.

Shared by the code-review and QA-test-plan features: both take a command
template from config (e.g. ``claude -p "/code-review {web_url}"`` or
``claude -p "/qa-testplan {jira_keys}"``), fill in the MR's context, and run it
as a background subprocess, capturing stdout.

Safety: the template is split into argv with ``shlex`` *first*, then
placeholders are substituted into the resulting tokens, and the process runs
with ``shell=False``. So an MR field can never inject shell metacharacters or
extra arguments. A substituted value that would make a token *start with* ``-``
(argument/flag smuggling) is refused.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from .config import CommandConfig

log = logging.getLogger("radar.commands")

# Env vars never exported to the skill subprocess. The child is an LLM agent
# fed attacker-influenceable MR content; it must not inherit our GitLab PAT.
_ENV_DENYLIST = frozenset({"GITLAB_TOKEN", "GITLAB_URL"})

_MAX_OUTPUT = 200_000   # cap captured stdout to bound memory / stored plan size
_MAX_JOBS = 256         # bound the in-memory job registry (evict oldest)

# Placeholders a command template may reference, filled from the MR context.
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
    tool. We refuse rather than smuggle a flag. Templates that legitimately
    start a token with a dash keep the literal dash in the template, so they're
    unaffected — embedding a placeholder after a fixed prefix is the safe way to
    pass such values.
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


def _fail(job: CommandJob, message: str) -> None:
    """Move a job to a terminal error state (error text set before status)."""
    job.error = message[:8000]
    job.status = "error"


class CommandRunner:
    """Owns background command jobs for the process lifetime (in-memory)."""

    def __init__(self, config: CommandConfig, kind: str):
        self.config = config
        self.kind = kind
        self._jobs: dict[str, CommandJob] = {}
        self._lock = threading.Lock()

    def start(
        self, ctx: dict, on_success: Callable[[CommandJob], None] | None = None
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
            # Evict oldest terminal jobs so long-running `serve` doesn't leak.
            while len(self._jobs) > _MAX_JOBS:
                self._jobs.pop(next(iter(self._jobs)))
        try:
            argv = build_argv(self.config.command, ctx)
        except CommandError as exc:
            job.status, job.error = "error", str(exc)
            return job
        threading.Thread(target=self._run, args=(job, argv, on_success), daemon=True).start()
        return job

    def _run(self, job: CommandJob, argv: list[str], on_success) -> None:
        # Catch-all guarantees every job reaches a terminal state; a crash in
        # the worker thread must never strand the job in "running" (the UI would
        # poll it forever).
        try:
            self._execute(job, argv, on_success)
        except Exception as exc:  # noqa: BLE001 - last-resort terminal state
            log.exception("%s worker crashed", self.kind)
            _fail(job, f"unexpected error: {exc}")

    def _execute(self, job: CommandJob, argv: list[str], on_success) -> None:
        if not argv:
            _fail(job, f"{self.kind}.command is empty")
            return
        try:
            proc = subprocess.run(
                argv,
                cwd=self.config.working_dir or None,
                capture_output=True,
                text=True,
                # Decode as UTF-8 regardless of the OS locale (Windows defaults
                # to cp1252, which mangles the arrows/em-dashes/emoji a markdown
                # plan contains). errors='replace' keeps a decode hiccup from
                # crashing the job.
                encoding="utf-8",
                errors="replace",
                env=self._child_env(),
                timeout=self.config.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            _fail(job, f"{self.kind} timed out after {self.config.timeout_seconds}s")
            return
        except FileNotFoundError:
            _fail(job, f"command not found: {argv[0]!r} (is it on PATH?)")
            return
        except OSError as exc:  # pragma: no cover - defensive
            _fail(job, f"failed to launch {self.kind}: {exc}")
            return

        if proc.returncode == 0 and proc.stdout.strip():
            # Publish payload BEFORE flipping status, so a reader that sees
            # status=="done" always sees the output too (htmx stops polling on
            # the first "done", so a torn read would strand an empty panel).
            job.output = proc.stdout[:_MAX_OUTPUT]
            job.returncode = 0
            if on_success is not None:
                try:
                    on_success(job)
                except Exception as exc:  # noqa: BLE001 - report, don't crash
                    log.exception("%s result produced but not saved", self.kind)
                    job.persist_error = f"result was generated but could not be saved: {exc}"
            job.status = "done"
        else:
            detail = proc.stderr or proc.stdout or f"exited with code {proc.returncode}"
            job.returncode = proc.returncode
            _fail(job, detail.strip())

    def _child_env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in _ENV_DENYLIST}
        env["PYTHONIOENCODING"] = "utf-8"  # nudge Python skills to emit UTF-8
        return env

    def get(self, job_id: str) -> CommandJob | None:
        with self._lock:
            return self._jobs.get(job_id)
