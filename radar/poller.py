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

from dataclasses import dataclass

from .config import Config
from .db import Database
from .events import Event, EventType
from .gitlab_client import MRSource, normalize_mr
from .notes import events_from_discussions, parse_gitlab_time


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


def poll_once(db: Database, config: Config, source: MRSource) -> PollResult:
    """Run one polling pass over all configured projects."""
    total_new = 0
    mrs_seen = 0

    for project in config.gitlab.projects:
        key = str(project)
        pstate = db.get_poll_state(key)
        updated_after = pstate["last_updated_after"] if pstate else None

        raw_mrs = source.list_open_merge_requests(project, updated_after)
        max_updated = updated_after
        project_id_for_state = pstate["project_id"] if pstate else None

        for raw in raw_mrs:
            mr = normalize_mr(raw)
            pid, iid = mr["project_id"], mr["mr_iid"]
            mrs_seen += 1
            project_id_for_state = pid

            db.upsert_mr_snapshot(
                project_id=pid,
                mr_iid=iid,
                title=mr["title"],
                author=mr["author"],
                web_url=mr["web_url"],
                source_branch=mr["source_branch"],
                target_branch=mr["target_branch"],
                labels=mr["labels"],
                draft=mr["draft"],
                state=mr["state"],
                reviewers=mr["reviewers"],
                created_at=mr["created_at"],
                updated_at=mr["updated_at"],
            )

            discussions = source.list_discussions(pid, iid)
            events = events_from_discussions(pid, iid, discussions)
            events += _reconcile_reviewers(db, mr, events)
            total_new += db.insert_events(events)

            updated = mr.get("updated_at")
            if updated and (max_updated is None or updated > max_updated):
                max_updated = updated

        db.set_poll_state(key, project_id_for_state, max_updated)

    return PollResult(projects=len(config.gitlab.projects), mrs_seen=mrs_seen, new_events=total_new)
