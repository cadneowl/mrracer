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
from pathlib import Path
from typing import Protocol


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


class GitLabSource:
    """Live GitLab source backed by python-gitlab."""

    # Per-request ceiling. Without one, python-gitlab (requests) waits forever:
    # a server that accepts the connection and never answers would hang the
    # poller's scheduler thread, or a review job, with nothing to time it out.
    HTTP_TIMEOUT_S = 60

    def __init__(self, url: str, token: str, timeout: int | None = None):
        import gitlab  # imported lazily so tests need no network stack

        self._gl = gitlab.Gitlab(
            url, private_token=token, timeout=timeout or self.HTTP_TIMEOUT_S
        )
        self._project_cache: dict[str, object] = {}

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
        changes = mr.changes().get("changes", [])
        diff = "\n".join(
            f"diff --git a/{c.get('old_path')} b/{c.get('new_path')}\n{c.get('diff', '')}"
            for c in changes
        )
        # diff_refs pins the diff to exact commits, so a skill with a checkout
        # can read the same snapshot the diff was computed against rather than
        # whatever the branch has moved on to.
        refs = getattr(mr, "diff_refs", None) or {}
        return {
            "title": mr.title,
            "description": mr.description or "",
            "diff": diff,
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
