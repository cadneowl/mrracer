"""Launch an external AI code-review command for an MR and track the job.

The dashboard's "review" button calls this. The command is a template from
config (e.g. ``claude -p "/code-review {web_url}"``); radar fills in the MR's
context and runs it as a background subprocess, capturing stdout as the review.

Safety: the template is split into argv with ``shlex`` *first*, then
placeholders are substituted into the resulting tokens, and the process is run
with ``shell=False``. So an MR field like a branch name can never inject extra
arguments or shell metacharacters — it stays inside one argv token.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
import uuid
from dataclasses import dataclass

from .config import ReviewConfig

# Placeholders a command template may reference, filled from the MR snapshot.
PLACEHOLDER_KEYS = (
    "web_url",
    "mr_iid",
    "project_id",
    "source_branch",
    "target_branch",
    "title",
    "author",
)


def build_argv(command: str, ctx: dict) -> list[str]:
    """Split a command template into argv, then substitute placeholders into
    each token (never re-splitting substituted values).

    On Windows we split with ``posix=False`` so backslash paths survive, then
    strip the quotes shlex leaves on quoted tokens. Substituting after the
    split means an MR field can never inject extra args (run with shell=False).
    """
    posix = os.name != "nt"
    tokens = shlex.split(command, posix=posix)
    argv = []
    for token in tokens:
        if not posix and len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
            token = token[1:-1]
        for key in PLACEHOLDER_KEYS:
            token = token.replace("{" + key + "}", str(ctx.get(key, "")))
        argv.append(token)
    return argv


@dataclass
class ReviewJob:
    id: str
    project_id: int
    mr_iid: int
    title: str = ""
    status: str = "running"  # running / done / error
    output: str = ""
    error: str = ""
    returncode: int | None = None


class ReviewRunner:
    """Owns review jobs for the process lifetime (in-memory, single-worker)."""

    def __init__(self, config: ReviewConfig):
        self.config = config
        self._jobs: dict[str, ReviewJob] = {}
        self._lock = threading.Lock()

    def start(self, ctx: dict) -> ReviewJob:
        job = ReviewJob(
            id=uuid.uuid4().hex[:12],
            project_id=int(ctx["project_id"]),
            mr_iid=int(ctx["mr_iid"]),
            title=str(ctx.get("title", "")),
        )
        with self._lock:
            self._jobs[job.id] = job
        argv = build_argv(self.config.command, ctx)
        threading.Thread(target=self._run, args=(job, argv), daemon=True).start()
        return job

    def _run(self, job: ReviewJob, argv: list[str]) -> None:
        if not argv:
            job.status, job.error = "error", "review.command is empty"
            return
        try:
            proc = subprocess.run(
                argv,
                cwd=self.config.working_dir or None,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            job.status = "error"
            job.error = f"review timed out after {self.config.timeout_seconds}s"
            return
        except FileNotFoundError:
            job.status = "error"
            job.error = f"command not found: {argv[0]!r} (is it on PATH?)"
            return
        except OSError as exc:  # pragma: no cover - defensive
            job.status, job.error = "error", f"failed to launch review: {exc}"
            return

        job.returncode = proc.returncode
        if proc.returncode == 0 and proc.stdout.strip():
            job.status, job.output = "done", proc.stdout
        else:
            detail = proc.stderr or proc.stdout or f"exited with code {proc.returncode}"
            job.status = "error"
            job.error = detail.strip()[:8000]

    def get(self, job_id: str) -> ReviewJob | None:
        with self._lock:
            return self._jobs.get(job_id)
