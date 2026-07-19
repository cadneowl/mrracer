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

import contextlib
import os
import shlex
import subprocess
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from .config import CommandConfig

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
    returncode: int | None = None


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
        try:
            argv = build_argv(self.config.command, ctx)
        except CommandError as exc:
            job.status, job.error = "error", str(exc)
            return job
        threading.Thread(target=self._run, args=(job, argv, on_success), daemon=True).start()
        return job

    def _run(self, job: CommandJob, argv: list[str], on_success) -> None:
        if not argv:
            job.status, job.error = "error", f"{self.kind}.command is empty"
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
                # Nudge Python-based skills to emit UTF-8 too (no effect on others).
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                timeout=self.config.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            job.status = "error"
            job.error = f"{self.kind} timed out after {self.config.timeout_seconds}s"
            return
        except FileNotFoundError:
            job.status = "error"
            job.error = f"command not found: {argv[0]!r} (is it on PATH?)"
            return
        except OSError as exc:  # pragma: no cover - defensive
            job.status, job.error = "error", f"failed to launch {self.kind}: {exc}"
            return

        job.returncode = proc.returncode
        if proc.returncode == 0 and proc.stdout.strip():
            job.status, job.output = "done", proc.stdout
            if on_success is not None:
                # Persistence must never crash the worker thread.
                with contextlib.suppress(Exception):
                    on_success(job)
        else:
            detail = proc.stderr or proc.stdout or f"exited with code {proc.returncode}"
            job.status = "error"
            job.error = detail.strip()[:8000]

    def get(self, job_id: str) -> CommandJob | None:
        with self._lock:
            return self._jobs.get(job_id)
