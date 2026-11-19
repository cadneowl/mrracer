"""GitLab data sources.

Two implementations of the same small surface:

* ``GitLabSource`` — talks to a real GitLab via python-gitlab.
* ``FixtureSource`` — serves recorded/synthetic JSON, used by tests so they
  never hit the network.

Both return raw GitLab REST-shaped dicts; ``normalize_mr`` turns those into the
flat shape the poller stores. The personal access token is read from the
environment by the caller and passed in; it is never logged here.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Protocol

log = logging.getLogger("radar.gitlab")


class MRSource(Protocol):
    def list_merge_requests(
        self, project: str, updated_after: str | None, state: str
    ) -> list[dict]: ...

    def list_discussions(self, project_id: int, mr_iid: int) -> list[dict]: ...


def normalize_mr(raw: dict) -> dict:
    """Flatten a GitLab MR dict into the fields radar stores."""
    author = raw.get("author") or {}
    reviewers = raw.get("reviewers") or []
    return {
        "project_id": raw.get("project_id"),
        "mr_iid": raw["iid"],
        "title": raw.get("title", ""),
        "author": author.get("username"),
        "web_url": raw.get("web_url"),
        "source_branch": raw.get("source_branch"),
        "target_branch": raw.get("target_branch"),
        # The MR's head commit. Present on the list endpoint, so it costs no
        # extra call; it is what a skill checks out to read the proposed code.
        "head_sha": raw.get("sha"),
        "description": raw.get("description") or "",
        "labels": list(raw.get("labels", []) or []),
        "draft": bool(raw.get("draft", raw.get("work_in_progress", False))),
        "state": raw.get("state", "opened"),
        "reviewers": [r.get("username") for r in reviewers if r.get("username")],
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
    }


# How much diff text radar inlines. Raw diffs come without GitLab's own limits,
# so one regenerated lockfile or vendored bundle would otherwise ride along in
# full and push the review past what the model can be sent. A file over the
# per-file cap, or one that no longer fits the total, is left out and listed —
# the same treatment as a file GitLab blanked.
MAX_FILE_DIFF_CHARS = 200_000
MAX_DIFF_CHARS = 600_000


def _extended_header(c: dict) -> list[str]:
    """Git's own lines for what a change is, besides its content."""
    old, new = c.get("old_path"), c.get("new_path")
    a_mode, b_mode = c.get("a_mode"), c.get("b_mode")
    if c.get("new_file"):
        return [f"new file mode {b_mode}"]
    if c.get("deleted_file"):
        return [f"deleted file mode {a_mode}"]
    lines = []
    if a_mode != b_mode:
        lines += [f"old mode {a_mode}", f"new mode {b_mode}"]
    if c.get("renamed_file") and old != new:
        lines += [f"rename from {old}", f"rename to {new}"]
    return lines


def changes_to_diff(changes: list[dict]) -> tuple[str, list[dict]]:
    """Join an MR's per-file changes into one unified diff, saying where it has holes.

    A file GitLab lists with no diff text is not evidence the file is
    unchanged: GitLab blanks a file that is over its size limits, and a bare
    ``diff --git`` header is indistinguishable from a no-op touch. So each file
    carries git's extended header (new, deleted, renamed, mode) — which is what
    an empty diff legitimately means — and a file whose content is missing
    gets a line saying so and is returned as ``{path, why, at}``, so the
    context can lead with it. ``at`` is the commit the content lives at:
    ``base`` for a deleted file, ``head`` for everything else.
    """
    blocks, omitted = [], []
    budget = MAX_DIFF_CHARS
    for c in changes:
        old, new = c.get("old_path"), c.get("new_path")
        lines = [f"diff --git a/{old} b/{new}", *_extended_header(c)]
        body = c.get("diff") or ""
        why = None
        if c.get("too_large") or c.get("collapsed"):
            flag = "too large" if c.get("too_large") else "collapsed"
            why = f"GitLab omitted its diff ({flag})"
        elif len(body) > MAX_FILE_DIFF_CHARS:
            why = (f"its diff is {len(body):,} characters, over radar's "
                   f"{MAX_FILE_DIFF_CHARS:,} per file")
        elif len(body) > budget:
            why = f"radar's {MAX_DIFF_CHARS:,}-character diff budget was spent on earlier files"
        elif not body and len(lines) == 1:
            # Not new, deleted, renamed or re-moded: an empty diff explains nothing.
            why = "GitLab returned no diff text for it"
        if why:
            at = "base" if c.get("deleted_file") else "head"
            path = old if at == "base" else new
            omitted.append({"path": path, "why": why, "at": at})
            lines.append(f"# radar: {why}; its changes are NOT shown here")
        elif body:
            budget -= len(body)
            lines.append(body)
        blocks.append("\n".join(lines))
    return "\n".join(blocks), omitted


class GitLabSource:
    """Live GitLab source backed by python-gitlab."""

    # Per-request ceiling. Without one, python-gitlab (requests) waits forever:
    # a server that accepts the connection and never answers would hang the
    # poller's scheduler thread, or a review job, with nothing to time it out.
    HTTP_TIMEOUT_S = 60

    # A connection that dies mid-request is not an answer, and reads here are
    # idempotent — so they are worth attempting again. Sized small: three tries
    # over a few seconds covers a reset peer or a proxy recycling a connection,
    # and anything that survives that is a fault worth reporting rather than
    # hammering. GitLab's own rate limiting (429) is included, with the backoff
    # that makes retrying it polite instead of rude.
    HTTP_RETRIES = 3
    RETRY_BACKOFF_S = 1.0
    RETRY_ON_STATUS = (429, 500, 502, 503, 504)

    def __init__(self, url: str, token: str, timeout: int | None = None):
        import gitlab  # imported lazily so tests need no network stack

        self._gl = gitlab.Gitlab(
            url, private_token=token, timeout=timeout or self.HTTP_TIMEOUT_S
        )
        self._install_retries()
        self._project_cache: dict[str, object] = {}

    def _install_retries(self) -> None:
        """Retry transient failures on the session python-gitlab talks over.

        Done at the adapter rather than around each call: urllib3 retries the
        one request that failed, instead of radar re-running a fetch that had
        already downloaded a large diff. Best effort — a python-gitlab that
        stops exposing a requests session costs the retries, not the poller.
        """
        try:
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry

            retry = Retry(
                total=self.HTTP_RETRIES,
                connect=self.HTTP_RETRIES,
                read=self.HTTP_RETRIES,
                status=self.HTTP_RETRIES,
                backoff_factor=self.RETRY_BACKOFF_S,
                status_forcelist=self.RETRY_ON_STATUS,
                allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
                raise_on_status=False,
                # Radar's backoff, not the server's. urllib3 honours a
                # `Retry-After` header by default and sleeps exactly as long as
                # it says — up to six hours, per attempt — which no timeout here
                # bounds, because `HTTP_TIMEOUT_S` is a socket timeout and this
                # is a sleep. A GitLab or a proxy that answers a throttled read
                # with `Retry-After: 600` would park whichever thread asked: the
                # poller's, whose pass then stops for ten minutes, or a skill's
                # context fetch, which radar has its own deadline for and would
                # have given up on long before the sleep ended. Three tries a
                # few seconds apart is the policy; a header cannot extend it.
                respect_retry_after_header=False,
            )
            adapter = HTTPAdapter(max_retries=retry)
            session = getattr(self._gl, "session", None)
            if session is None:
                return
            session.mount("https://", adapter)
            session.mount("http://", adapter)
        except Exception:  # noqa: BLE001 - retries are an improvement, not a requirement
            log.warning("could not install HTTP retries; transient failures will not "
                        "be retried", exc_info=True)

    def _project(self, project: str):
        if project not in self._project_cache:
            self._project_cache[project] = self._gl.projects.get(project)
        return self._project_cache[project]

    def list_merge_requests(
        self, project: str, updated_after: str | None, state: str
    ) -> list[dict]:
        proj = self._project(project)
        kwargs = {
            "state": state,
            "order_by": "updated_at",
            "sort": "asc",
            "iterator": True,
        }
        if updated_after:
            kwargs["updated_after"] = updated_after
        result = []
        for mr in proj.mergerequests.list(**kwargs):
            data = dict(mr.attributes)
            data.setdefault("project_id", proj.id)
            result.append(data)
        return result

    def list_discussions(self, project_id: int, mr_iid: int) -> list[dict]:
        # project_id here is the numeric id we already resolved.
        proj = self._gl.projects.get(project_id, lazy=True)
        mr = proj.mergerequests.get(mr_iid, lazy=True)
        return [dict(d.attributes) for d in mr.discussions.list(iterator=True)]

    def get_mr_context(self, project_id: int, mr_iid: int) -> dict:
        """Title, description, refs, and unified diff of an MR (backend fetch)."""
        proj = self._gl.projects.get(project_id, lazy=True)
        mr = proj.mergerequests.get(mr_iid)
        # `access_raw_diffs` reads the diffs from Gitaly instead of the copy
        # GitLab stores, which is cut to its collapse limits: without it, a
        # large file — typically the test class that grew the most — comes
        # back listed with an empty diff, and a reviewer reads "touched, no
        # changes" where the MR's tests actually are.
        # Still `/changes`, not the paginated `/diffs` that replaces it: only
        # `/changes` takes `access_raw_diffs`, and `/diffs` blanks large files.
        resp = mr.changes(access_raw_diffs="true")
        changes = resp.get("changes", [])
        diff, omitted = changes_to_diff(changes)
        # Past GitLab's file limit the list itself is cut, and those files are
        # not blanked but absent. `changes_count` is the real total ("1000+").
        truncated = None
        if resp.get("overflow"):
            truncated = {"shown": len(changes), "total": str(resp.get("changes_count") or "more")}
        # diff_refs pins the diff to exact commits, so a skill with a checkout
        # can read the same snapshot the diff was computed against rather than
        # whatever the branch has moved on to.
        refs = getattr(mr, "diff_refs", None) or {}
        return {
            "title": mr.title,
            "description": mr.description or "",
            "diff": diff,
            "omitted": omitted,
            "truncated": truncated,
            "base_sha": refs.get("base_sha"),
            "head_sha": refs.get("head_sha") or getattr(mr, "sha", None),
            "start_sha": refs.get("start_sha"),
        }


class FixtureSource:
    """In-memory / on-disk fixture source for tests and demos."""

    def __init__(
        self,
        mrs_by_project: dict[str, list[dict]],
        discussions_by_mr: dict[tuple[int, int], list[dict]],
        mr_context_by_mr: dict[tuple[int, int], dict] | None = None,
    ):
        self._mrs = mrs_by_project
        self._discussions = discussions_by_mr
        self._mr_context = mr_context_by_mr or {}

    @classmethod
    def from_dir(cls, path: str | Path) -> FixtureSource:
        """Load fixtures from a directory.

        Layout:
            <dir>/projects/<project-key>.json   -> list of MR dicts
            <dir>/discussions/<pid>-<iid>.json  -> list of discussion dicts
        """
        path = Path(path)
        mrs: dict[str, list[dict]] = {}
        proj_dir = path / "projects"
        if proj_dir.is_dir():
            for f in sorted(proj_dir.glob("*.json")):
                mrs[f.stem.replace("__", "/")] = json.loads(f.read_text(encoding="utf-8"))
        disc: dict[tuple[int, int], list[dict]] = {}
        disc_dir = path / "discussions"
        if disc_dir.is_dir():
            for f in sorted(disc_dir.glob("*.json")):
                pid, iid = (int(x) for x in f.stem.split("-"))
                disc[(pid, iid)] = json.loads(f.read_text(encoding="utf-8"))
        return cls(mrs, disc)

    def list_merge_requests(
        self, project: str, updated_after: str | None, state: str
    ) -> list[dict]:
        mrs = list(self._mrs.get(project, []))
        if state != "all":
            mrs = [m for m in mrs if m.get("state", "opened") == state]
        if updated_after:
            mrs = [m for m in mrs if (m.get("updated_at") or "") > updated_after]
        return sorted(mrs, key=lambda m: m.get("updated_at") or "")

    def list_discussions(self, project_id: int, mr_iid: int) -> list[dict]:
        return self._discussions.get((project_id, mr_iid), [])

    def get_mr_context(self, project_id: int, mr_iid: int) -> dict:
        return self._mr_context.get(
            (project_id, mr_iid), {"title": "", "description": "", "diff": ""}
        )
