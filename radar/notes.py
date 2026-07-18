"""GitLab note & discussion parsing — the single place that understands
GitLab's note wording.

Review-request timing and every lifecycle transition are recovered from
system notes (the immutable audit trail), which is why this module exists in
one auditable spot: exact wording varies slightly by GitLab version, so the
patterns are centralized and easy to adjust. Human comments are turned into
note_added events, flagged with whether they opened a resolvable diff thread
(which is what resolves a first-response obligation).

Every event's dedup_key is derived from the note's stable integer id, so
re-polling the same note never produces a duplicate event.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from .events import Event, EventType

# --- system-note body patterns (case-insensitive, tolerant of markdown) ----

_MENTION = re.compile(r"@([A-Za-z0-9][A-Za-z0-9._-]*)")

_PATTERNS: dict[str, re.Pattern[str]] = {
    "review_requested": re.compile(r"^\s*requested review from\b", re.IGNORECASE),
    "reviewer_removed": re.compile(r"^\s*removed review request for\b", re.IGNORECASE),
    "approval_added": re.compile(r"^\s*approved this merge request\s*$", re.IGNORECASE),
    "changes_requested": re.compile(r"^\s*requested changes\s*$", re.IGNORECASE),
    "commits_pushed": re.compile(r"^\s*added \d+ commit", re.IGNORECASE),
    "draft_on": re.compile(r"marked this merge request as \**draft", re.IGNORECASE),
    "draft_off": re.compile(r"marked this merge request as \**ready", re.IGNORECASE),
    "mr_merged": re.compile(r"^\s*merged\s*$", re.IGNORECASE),
    "mr_closed": re.compile(r"^\s*closed\s*$", re.IGNORECASE),
}


def parse_gitlab_time(value: str) -> datetime:
    """Parse a GitLab ISO-8601 timestamp into an aware UTC datetime."""
    # GitLab returns e.g. '2026-07-01T09:30:00.000Z' or with a +00:00 offset.
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _key(project_id: int, mr_iid: int, note_id: object, suffix: str = "") -> str:
    base = f"{project_id}:{mr_iid}:n{note_id}"
    return f"{base}:{suffix}" if suffix else base


def classify_system_note(project_id: int, mr_iid: int, note: dict) -> list[Event]:
    """Turn one system note into zero or more events."""
    body = str(note.get("body", ""))
    note_id = note.get("id")
    author = (note.get("author") or {}).get("username")
    occurred_at = parse_gitlab_time(note["created_at"])
    events: list[Event] = []

    def make(event_type: str, reviewer: str | None = None, suffix: str = "", **payload) -> Event:
        return Event(
            project_id=project_id,
            mr_iid=mr_iid,
            event_type=event_type,
            occurred_at=occurred_at,
            dedup_key=_key(project_id, mr_iid, note_id, suffix),
            actor=author,
            reviewer=reviewer,
            payload=payload,
        )

    if _PATTERNS["review_requested"].search(body):
        for user in _MENTION.findall(body):
            events.append(make(EventType.REVIEW_REQUESTED, reviewer=user, suffix=user))
        return events
    if _PATTERNS["reviewer_removed"].search(body):
        for user in _MENTION.findall(body):
            events.append(make(EventType.REVIEWER_REMOVED, reviewer=user, suffix=user))
        return events
    if _PATTERNS["approval_added"].search(body):
        return [make(EventType.APPROVAL_ADDED, reviewer=author)]
    if _PATTERNS["changes_requested"].search(body):
        return [make(EventType.CHANGES_REQUESTED, reviewer=author)]
    if _PATTERNS["commits_pushed"].search(body):
        return [make(EventType.COMMITS_PUSHED)]
    if _PATTERNS["draft_on"].search(body):
        return [make(EventType.DRAFT_TOGGLED, draft=True)]
    if _PATTERNS["draft_off"].search(body):
        return [make(EventType.DRAFT_TOGGLED, draft=False)]
    if _PATTERNS["mr_merged"].search(body):
        return [make(EventType.MR_MERGED)]
    if _PATTERNS["mr_closed"].search(body):
        return [make(EventType.MR_CLOSED)]
    return []


def classify_discussion(project_id: int, mr_iid: int, discussion: dict) -> list[Event]:
    """Turn one discussion (a group of notes) into events.

    Handles both system notes and human comments. A human note is flagged as
    thread-opening when it is the first note of a resolvable (non-individual)
    discussion, and as on-diff when it carries a diff position.
    """
    notes = discussion.get("notes") or []
    individual = bool(discussion.get("individual_note", False))
    discussion_id = discussion.get("id")
    events: list[Event] = []

    for index, note in enumerate(notes):
        if note.get("system"):
            events.extend(classify_system_note(project_id, mr_iid, note))
            continue

        note_id = note.get("id")
        author = (note.get("author") or {}).get("username")
        occurred_at = parse_gitlab_time(note["created_at"])
        on_diff = note.get("position") is not None
        # A resolvable thread is a non-individual discussion; the first note
        # opens it. Individual notes are standalone comments.
        opens_thread = (not individual) and index == 0
        events.append(
            Event(
                project_id=project_id,
                mr_iid=mr_iid,
                event_type=EventType.NOTE_ADDED,
                occurred_at=occurred_at,
                dedup_key=_key(project_id, mr_iid, note_id),
                actor=author,
                reviewer=author,
                payload={
                    "discussion_id": discussion_id,
                    "opens_thread": opens_thread,
                    "on_diff": on_diff,
                    "resolvable": bool(note.get("resolvable", False)),
                },
            )
        )
    return events


def events_from_discussions(
    project_id: int, mr_iid: int, discussions: list[dict]
) -> list[Event]:
    """Flatten all events from an MR's discussions."""
    events: list[Event] = []
    for discussion in discussions:
        events.extend(classify_discussion(project_id, mr_iid, discussion))
    return events
