"""Event model.

The poller writes an append-only stream of events. Every SLA state and every
statistic is DERIVED by replaying this stream (see derive.py), so the events
here are the single source of truth.

Idempotency: each event carries a deterministic ``dedup_key``. Because almost
every event originates from a GitLab note (which has a stable integer id),
re-polling the same note produces the same key and the store ignores the
duplicate. See notes.py for how raw GitLab payloads become events.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime


class EventType:
    """Canonical event type strings (stored verbatim in the DB)."""

    REVIEW_REQUESTED = "review_requested"
    REVIEWER_REMOVED = "reviewer_removed"
    NOTE_ADDED = "note_added"
    APPROVAL_ADDED = "approval_added"
    CHANGES_REQUESTED = "changes_requested"
    COMMITS_PUSHED = "commits_pushed"  # author pushed; needed for clock fairness
    DRAFT_TOGGLED = "draft_toggled"
    MR_MERGED = "mr_merged"
    MR_CLOSED = "mr_closed"


# Event types that name a specific reviewer as the obligation subject.
REVIEWER_SCOPED = frozenset(
    {
        EventType.REVIEW_REQUESTED,
        EventType.REVIEWER_REMOVED,
        EventType.APPROVAL_ADDED,
        EventType.CHANGES_REQUESTED,
    }
)


@dataclass(frozen=True)
class Event:
    """A single fact about a merge request at a point in time.

    ``reviewer`` is the obligation subject when the event is reviewer-scoped
    (e.g. who was requested / who approved). ``actor`` is whoever caused the
    event (e.g. the note author). ``occurred_at`` is timezone-aware UTC.
    """

    project_id: int
    mr_iid: int
    event_type: str
    occurred_at: datetime
    dedup_key: str
    actor: str | None = None
    reviewer: str | None = None
    payload: dict = field(default_factory=dict)

    def payload_json(self) -> str:
        return json.dumps(self.payload, sort_keys=True, separators=(",", ":"))
