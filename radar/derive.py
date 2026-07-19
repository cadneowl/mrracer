"""Derivation: replay events into current review-obligation states.

Everything the dashboard shows is computed here from the append-only event
log plus the current MR snapshot and config. Nothing is read from a cache of
prior states, so changing SLA definitions in config and re-deriving yields
correct historical results (that is what ``recompute`` does).

Model
-----
The tracked unit is a *review obligation*: (project, mr_iid, reviewer, round).
Each ``review_requested`` opens a new round with its own clock. An obligation
moves through two phases against two budgets:

* first-response phase — clock runs from ``requested_at`` until the reviewer's
  first qualifying response (a diff thread, changes_requested, or approval).
* approval phase — clock runs until approval, but PAUSES whenever the ball is
  in the author's court (reviewer asked for changes / opened a thread and the
  author has not pushed or replied since). This is the fairness rule.

The single chip shows whichever clock is currently live (most-urgent,
auto-switching), colored by fraction of its budget consumed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from .business_time import business_hours_between
from .config import Config
from .events import Event, EventType
from .slas import is_waived_by_mr, match_sla

# Chip color buckets (the 5 dashboard colors).
CHIP_WAIVED = "WAIVED"  # blue
CHIP_PENDING = "PENDING"  # grey  — paused (author's court) or resolved-awaiting
CHIP_IN_SLA = "IN_SLA"  # green  — clock running, < 75% of budget
CHIP_AT_RISK = "AT_RISK"  # amber — clock running, >= 75% of budget
CHIP_BREACHED = "BREACHED"  # red   — clock running, over budget

_RESOLVING_RESPONSES = {EventType.APPROVAL_ADDED, EventType.CHANGES_REQUESTED}


@dataclass
class Obligation:
    project_id: int
    mr_iid: int
    reviewer: str
    round: int
    requested_at: datetime
    first_response_at: datetime | None = None
    first_response_type: str | None = None
    approved_at: datetime | None = None
    terminal_at: datetime | None = None
    terminal_reason: str | None = None  # reviewer_removed / merged / closed
    thread_count: int = 0
    # Ordered (timestamp, kind) transitions used to rebuild phase-B segments.
    transitions: list[tuple[datetime, str]] = field(default_factory=list)


@dataclass
class ObligationState:
    """Derived, display-ready state for one obligation."""

    project_id: int
    mr_iid: int
    reviewer: str
    round: int
    requested_at: datetime
    chip_state: str
    phase: str  # first_response / approval / resolved / terminal
    status_text: str
    budget_hours: float
    elapsed_hours: float
    remaining_hours: float
    fraction: float
    paused: bool
    reviewer_tz: str
    first_response_at: datetime | None
    resolved_at: datetime | None
    resolution_type: str | None
    within_sla: bool | None
    thread_count: int
    urgency: float  # ascending sort key; most-overdue (most negative) first
    first_response_hours: float | None = None  # business hours requested -> first response

    def to_record(self) -> dict:
        return {
            "project_id": self.project_id,
            "mr_iid": self.mr_iid,
            "reviewer": self.reviewer,
            "round": self.round,
            "requested_at": self.requested_at.isoformat(),
            "state": self.chip_state,
            "phase": self.phase,
            "first_response_at": self.first_response_at.isoformat()
            if self.first_response_at
            else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolution_type": self.resolution_type,
            "within_sla": self.within_sla,
            "elapsed_business_hours": round(self.elapsed_hours, 3),
            "thread_count": self.thread_count,
        }


def _note_is_thread(event: Event) -> bool:
    p = event.payload
    return bool(p.get("opens_thread")) and bool(p.get("on_diff"))


def _build_obligations(events: list[Event], author: str | None) -> list[Obligation]:
    """Partition an MR's events into per-reviewer, per-round obligations."""
    # Group review_requested times per reviewer to establish rounds.
    by_reviewer_requests: dict[str, list[datetime]] = {}
    for e in events:
        if e.event_type == EventType.REVIEW_REQUESTED and e.reviewer:
            by_reviewer_requests.setdefault(e.reviewer, []).append(e.occurred_at)

    obligations: list[Obligation] = []
    for reviewer, requests in by_reviewer_requests.items():
        requests = sorted(requests)
        for i, req_at in enumerate(requests):
            next_req = requests[i + 1] if i + 1 < len(requests) else None
            obl = Obligation(
                project_id=events[0].project_id,
                mr_iid=events[0].mr_iid,
                reviewer=reviewer,
                round=i + 1,
                requested_at=req_at,
            )
            _populate_obligation(obl, events, req_at, next_req, author)
            obligations.append(obl)
    return obligations


def _populate_obligation(
    obl: Obligation,
    events: list[Event],
    start: datetime,
    end: datetime | None,
    author: str | None,
) -> None:
    """Walk the window [start, end) and fill in an obligation's lifecycle."""
    for e in events:
        if e.occurred_at < start:
            continue
        if end is not None and e.occurred_at >= end:
            break
        if obl.terminal_at is not None or obl.approved_at is not None:
            break

        et = e.event_type
        by_reviewer = e.reviewer == obl.reviewer or e.actor == obl.reviewer
        by_author = author is not None and (e.actor == author)

        if et == EventType.REVIEWER_REMOVED and e.reviewer == obl.reviewer:
            obl.terminal_at, obl.terminal_reason = e.occurred_at, "reviewer_removed"
        elif et == EventType.MR_MERGED:
            obl.terminal_at, obl.terminal_reason = e.occurred_at, "merged"
        elif et == EventType.MR_CLOSED:
            obl.terminal_at, obl.terminal_reason = e.occurred_at, "closed"
        elif et == EventType.APPROVAL_ADDED and by_reviewer:
            if obl.first_response_at is None:
                obl.first_response_at, obl.first_response_type = e.occurred_at, "approval"
            obl.approved_at = e.occurred_at
        elif et == EventType.CHANGES_REQUESTED and by_reviewer:
            if obl.first_response_at is None:
                obl.first_response_at = e.occurred_at
                obl.first_response_type = "changes_requested"
            obl.transitions.append((e.occurred_at, "reviewer_ball_to_author"))
        elif et == EventType.NOTE_ADDED and by_reviewer and _note_is_thread(e):
            obl.thread_count += 1
            if obl.first_response_at is None:
                obl.first_response_at, obl.first_response_type = e.occurred_at, "comment"
            obl.transitions.append((e.occurred_at, "reviewer_ball_to_author"))
        elif by_author and et in (EventType.COMMITS_PUSHED, EventType.NOTE_ADDED):
            # Author pushed fixes or replied -> ball returns to the reviewer.
            obl.transitions.append((e.occurred_at, "author_ball_to_reviewer"))


def _reviewer_owed_intervals(
    obl: Obligation, phase_end: datetime
) -> list[tuple[datetime, datetime]]:
    """Phase-B intervals during which the clock runs (ball with reviewer).

    Phase B begins at first_response_at with the ball in the author's court
    (the reviewer just asked for changes / opened a thread), so the clock is
    initially paused until the author pushes or replies.
    """
    if obl.first_response_at is None:
        return []
    segments: list[tuple[datetime, datetime]] = []
    ball = "author"
    seg_start = obl.first_response_at
    for ts, kind in sorted(obl.transitions):
        if ts <= obl.first_response_at:
            continue
        if ts >= phase_end:
            break
        if kind == "author_ball_to_reviewer" and ball == "author":
            ball, seg_start = "reviewer", ts
        elif kind == "reviewer_ball_to_author" and ball == "reviewer":
            segments.append((seg_start, ts))
            ball = "author"
    if ball == "reviewer" and seg_start < phase_end:
        segments.append((seg_start, phase_end))
    return segments


def _currently_paused(obl: Obligation, now: datetime) -> bool:
    """Whether the ball is currently in the author's court (phase B, running)."""
    if obl.first_response_at is None or obl.approved_at is not None:
        return False
    ball = "author"
    for ts, kind in sorted(obl.transitions):
        if ts <= obl.first_response_at:
            continue
        if kind == "author_ball_to_reviewer":
            ball = "reviewer"
        elif kind == "reviewer_ball_to_author":
            ball = "author"
    return ball == "author"


def _color_for_fraction(fraction: float) -> str:
    if fraction >= 1.0:
        return CHIP_BREACHED
    if fraction >= 0.75:
        return CHIP_AT_RISK
    return CHIP_IN_SLA


def _derive_one(
    obl: Obligation,
    config: Config,
    snapshot: dict,
    mr_waiver: str | None,
    now: datetime,
) -> ObligationState:
    state = _compute_state(obl, config, snapshot, mr_waiver, now)
    if obl.first_response_at is not None:
        state.first_response_hours = business_hours_between(
            obl.requested_at,
            obl.first_response_at,
            config.calendar.calendar,
            config.calendar.tz_for(obl.reviewer),
        )
    return state


def _compute_state(
    obl: Obligation,
    config: Config,
    snapshot: dict,
    mr_waiver: str | None,
    now: datetime,
) -> ObligationState:
    tz = config.calendar.tz_for(obl.reviewer)
    cal = config.calendar.calendar
    rule = match_sla(config, snapshot.get("target_branch"), snapshot.get("labels", []))
    fr_budget = rule.first_response_business_hours
    ap_budget = rule.approval_business_hours

    resolved_at = obl.approved_at or obl.terminal_at
    resolution_type = "approval" if obl.approved_at else obl.terminal_reason

    # --- terminal / waived states -----------------------------------------
    if obl.terminal_at is not None and obl.approved_at is None:
        # reviewer removed / MR merged or closed without this reviewer approving
        return _state(
            obl, config, CHIP_WAIVED, "terminal",
            f"waived ({obl.terminal_reason})", 0.0, 0.0, 0.0, 0.0,
            paused=False, tz=tz, resolved_at=resolved_at,
            resolution_type=resolution_type, within_sla=None,
            urgency=math.inf,
        )
    if mr_waiver is not None and obl.approved_at is None:
        phase = "first_response" if obl.first_response_at is None else "approval"
        return _state(
            obl, config, CHIP_WAIVED, phase,
            f"waived ({mr_waiver})", 0.0, 0.0, 0.0, 0.0,
            paused=False, tz=tz, resolved_at=None, resolution_type=None,
            within_sla=None, urgency=math.inf,
        )

    # --- fully resolved (approved) ----------------------------------------
    if obl.approved_at is not None:
        fr_elapsed = (
            business_hours_between(obl.requested_at, obl.first_response_at, cal, tz)
            if obl.first_response_at
            else 0.0
        )
        # Approval-phase elapsed = reviewer-owed business hours up to approval
        # (pauses excluded). within_sla folds in BOTH budgets.
        ap_intervals = _reviewer_owed_intervals(obl, obl.approved_at)
        ap_elapsed = sum(business_hours_between(a, b, cal, tz) for a, b in ap_intervals)
        within = fr_elapsed <= fr_budget and ap_elapsed <= ap_budget
        fraction = ap_elapsed / ap_budget if ap_budget > 0 else math.inf
        return _state(
            obl, config, CHIP_PENDING, "resolved", "approved",
            ap_budget, ap_elapsed, ap_budget - ap_elapsed, fraction, paused=False, tz=tz,
            resolved_at=obl.approved_at, resolution_type="approval",
            within_sla=within, urgency=math.inf,
        )

    # --- phase A: awaiting first response ---------------------------------
    if obl.first_response_at is None:
        elapsed = business_hours_between(obl.requested_at, now, cal, tz)
        fraction = elapsed / fr_budget if fr_budget > 0 else math.inf
        remaining = fr_budget - elapsed
        chip = _color_for_fraction(fraction)
        return _state(
            obl, config, chip, "first_response", "awaiting first response",
            fr_budget, elapsed, remaining, fraction, paused=False, tz=tz,
            resolved_at=None, resolution_type=None, within_sla=None,
            urgency=remaining,
        )

    # --- phase B: awaiting approval (clock pauses in author's court) -------
    intervals = _reviewer_owed_intervals(obl, now)
    elapsed = sum(business_hours_between(a, b, cal, tz) for a, b in intervals)
    fraction = elapsed / ap_budget if ap_budget > 0 else math.inf
    remaining = ap_budget - elapsed
    paused = _currently_paused(obl, now)
    if paused:
        return _state(
            obl, config, CHIP_PENDING, "approval", "waiting on author",
            ap_budget, elapsed, remaining, fraction, paused=True, tz=tz,
            resolved_at=None, resolution_type=None, within_sla=None,
            urgency=math.inf,
        )
    chip = _color_for_fraction(fraction)
    return _state(
        obl, config, chip, "approval", "awaiting approval",
        ap_budget, elapsed, remaining, fraction, paused=False, tz=tz,
        resolved_at=None, resolution_type=None, within_sla=None,
        urgency=remaining,
    )


def _state(obl, config, chip, phase, status_text, budget, elapsed, remaining,
           fraction, *, paused, tz, resolved_at, resolution_type, within_sla,
           urgency) -> ObligationState:
    return ObligationState(
        project_id=obl.project_id,
        mr_iid=obl.mr_iid,
        reviewer=obl.reviewer,
        round=obl.round,
        requested_at=obl.requested_at,
        chip_state=chip,
        phase=phase,
        status_text=status_text,
        budget_hours=round(budget, 3),
        elapsed_hours=round(elapsed, 3),
        remaining_hours=round(remaining, 3),
        fraction=round(fraction, 4),
        paused=paused,
        reviewer_tz=str(tz),
        first_response_at=obl.first_response_at,
        resolved_at=resolved_at,
        resolution_type=resolution_type,
        within_sla=within_sla,
        thread_count=obl.thread_count,
        urgency=urgency,
    )


def derive_mr(
    events: list[Event],
    snapshot: dict,
    config: Config,
    now: datetime,
) -> list[ObligationState]:
    """Derive obligation states for a single MR."""
    if not events:
        return []
    events = sorted(events, key=lambda e: (e.occurred_at, e.event_type))
    mr_waiver = is_waived_by_mr(
        config.waive, snapshot.get("draft", False), snapshot.get("labels", [])
    )
    obligations = _build_obligations(events, snapshot.get("author"))
    return [_derive_one(o, config, snapshot, mr_waiver, now) for o in obligations]
