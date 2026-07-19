"""The poller: fetch open MRs, turn GitLab state into events, store idempotently.

For each configured project it fetches open MRs (using an updated_after
high-water mark to skip unchanged ones), refreshes each MR's snapshot, and
appends events parsed from the MR's discussions. Re-running is safe: events
carry deterministic dedup keys, so nothing is duplicated.

Reviewer reconciliation: system notes are the source of truth for review
requests, but a reviewer can be present on an MR without a matching note (e.g.
assigned at creation). For any current reviewer lacking a review_requested
event, a synthetic one is emitted (dedup-keyed, so idempotent) dated to the
MR's creation, so the obligation is still tracked.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Config
from .db import Database
from .events import Event, EventType
from .gitlab_client import MRSource, normalize_mr
from .notes import events_from_discussions, parse_gitlab_time

log = logging.getLogger("radar.poller")

_TERMINAL_EVENT = {"merged": EventType.MR_MERGED, "closed": EventType.MR_CLOSED}


@dataclass
class PollResult:
    projects: int
    mrs_seen: int
    new_events: int


def _reviewers_with_requests(db: Database, pid: int, iid: int, parsed: list[Event]) -> set[str]:
    reviewers = {
        e.reviewer
        for e in parsed
        if e.event_type == EventType.REVIEW_REQUESTED and e.reviewer
    }
    for e in db.iter_events(pid, iid):
        if e.event_type == EventType.REVIEW_REQUESTED and e.reviewer:
            reviewers.add(e.reviewer)
    return reviewers


def _reconcile_reviewers(db: Database, mr: dict, parsed: list[Event]) -> list[Event]:
    """Emit synthetic review_requested events for current reviewers that have
    no request event yet (from notes or a prior poll)."""
    pid, iid = mr["project_id"], mr["mr_iid"]
    have = _reviewers_with_requests(db, pid, iid, parsed)
    created = mr.get("created_at")
    if not created:
        return []
    requested_at = parse_gitlab_time(created)
    extra: list[Event] = []
    for reviewer in mr["reviewers"]:
        if reviewer in have:
            continue
        extra.append(
            Event(
                project_id=pid,
                mr_iid=iid,
                event_type=EventType.REVIEW_REQUESTED,
                occurred_at=requested_at,
                dedup_key=f"{pid}:{iid}:reconcile-rev:{reviewer}",
                actor=mr.get("author"),
                reviewer=reviewer,
                payload={"source": "reviewer_snapshot"},
            )
        )
    return extra


def _terminal_events(mr: dict) -> list[Event]:
    """Emit a terminal event straight from a merged/closed MR's state, so an MR
    that leaves the open set still resolves its obligations (GitLab doesn't
    always leave a parseable 'merged'/'closed' system note)."""
    event_type = _TERMINAL_EVENT.get(mr["state"])
    if event_type is None:
        return []
    pid, iid = mr["project_id"], mr["mr_iid"]
    when = mr.get("updated_at") or mr.get("created_at")
    if not when:
        return []
    return [
        Event(
            project_id=pid,
            mr_iid=iid,
            event_type=event_type,
            occurred_at=parse_gitlab_time(when),
            dedup_key=f"{pid}:{iid}:state-{mr['state']}",
            actor=mr.get("author"),
            payload={"source": "mr_state"},
        )
    ]


def poll_once(db: Database, config: Config, source: MRSource) -> PollResult:
    """Run one polling pass over all configured projects.

    Each project is isolated: a failure fetching one project is logged and the
    remaining projects still run.
    """
    total_new = 0
    mrs_seen = 0

    for project in config.gitlab.projects:
        try:
            seen, new = _poll_project(db, config, source, str(project))
            mrs_seen += seen
            total_new += new
        except Exception:  # noqa: BLE001 - isolate a bad project from the rest
            log.exception("polling project %s failed", project)

    return PollResult(projects=len(config.gitlab.projects), mrs_seen=mrs_seen, new_events=total_new)


def _poll_project(db: Database, config: Config, source: MRSource, key: str) -> tuple[int, int]:
    pstate = db.get_poll_state(key)
    updated_after = pstate["last_updated_after"] if pstate else None
    # First poll (no high-water mark): only open MRs, to avoid pulling all
    # history. After that, fetch all states so merge/close transitions since the
    # mark are recorded and their obligations resolve.
    state = "all" if updated_after else "opened"

    raw_mrs = source.list_merge_requests(key, updated_after, state)
    max_updated = updated_after
    project_id_for_state = pstate["project_id"] if pstate else None
    seen = 0
    new = 0

    for raw in raw_mrs:
        mr = normalize_mr(raw)
        pid, iid = mr["project_id"], mr["mr_iid"]
        seen += 1
        project_id_for_state = pid

        db.upsert_mr_snapshot(
            project_id=pid,
            mr_iid=iid,
            title=mr["title"],
            author=mr["author"],
            web_url=mr["web_url"],
            source_branch=mr["source_branch"],
            target_branch=mr["target_branch"],
            description=mr["description"],
            labels=mr["labels"],
            draft=mr["draft"],
            state=mr["state"],
            reviewers=mr["reviewers"],
            created_at=mr["created_at"],
            updated_at=mr["updated_at"],
        )

        discussions = source.list_discussions(pid, iid)
        events = events_from_discussions(pid, iid, discussions)
        if mr["state"] == "opened":
            events += _reconcile_reviewers(db, mr, events)
        else:
            events += _terminal_events(mr)
        new += db.insert_events(events)

        updated = mr.get("updated_at")
        if updated and (max_updated is None or updated > max_updated):
            max_updated = updated

    db.set_poll_state(key, project_id_for_state, max_updated)
    return seen, new
