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

One obligation names no reviewer, because its whole point is that nobody is on
the hook: an open MR with an empty reviewer list carries an *assignment*
obligation, owed by its author, against ``assignment_business_hours``. Without
it such an MR is invisible here — no review was ever requested, so none of the
above has anything to say about it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from .business_time import business_hours_between
from .config import Config
from .events import Event, EventType
from .notes import parse_gitlab_time
from .slas import is_waived_by_mr, match_sla

# Chip color buckets (the 5 dashboard colors).
CHIP_WAIVED = "WAIVED"  # blue
CHIP_PENDING = "PENDING"  # grey  — paused (author's court) or resolved-awaiting
CHIP_IN_SLA = "IN_SLA"  # green  — clock running, < 75% of budget
CHIP_AT_RISK = "AT_RISK"  # amber — clock running, >= 75% of budget
CHIP_BREACHED = "BREACHED"  # red   — clock running, over budget

# What an obligation is owed for. Everything on the board is a review owed by a
# named reviewer, except the one owed by an author who has named none.
KIND_REVIEW = "review"
KIND_ASSIGNMENT = "assignment"

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
    # KIND_REVIEW unless this is the "nobody is reviewing this" obligation, in
    # which case ``reviewer`` is the author who owes an assignment, not a review.
    kind: str = KIND_REVIEW

    def to_record(self) -> dict:
        return {
            "project_id": self.project_id,
            "mr_iid": self.mr_iid,
            # Recorded so a reader of the table can tell an assignment owed by
            # an author from a review owed by a reviewer. Both put a username in
            # `reviewer`, and only one of them is a review record.
            "kind": self.kind,
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
           urgency, kind=KIND_REVIEW) -> ObligationState:
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
        kind=kind,
    )


def _snapshot_time(value: str | None) -> datetime | None:
    """Parse a snapshot's GitLab timestamp. None if absent or unparseable —
    a timestamp radar cannot read is not worth failing a whole board over."""
    if not value:
        return None
    try:
        return parse_gitlab_time(value)
    except ValueError:
        return None


def _restarts_assignment_clock(event: Event) -> bool:
    """Whether this event left the MR newly in need of a reviewer."""
    if event.event_type == EventType.REVIEWER_REMOVED:
        return True
    # Marked ready: what came before was a draft, which owes nobody anything.
    return event.event_type == EventType.DRAFT_TOGGLED and not event.payload.get("draft")


def _unassigned_since(events: list[Event], snapshot: dict) -> datetime | None:
    """When this MR last became something somebody could have been asked to review.

    Usually that is when it was opened, but two things restart the clock:

    * losing its last reviewer — an MR left orphaned should not be billed for
      the days somebody was on it;
    * being marked ready — a draft is waived while it is one, so anchoring at
      creation would bill the whole draft window the moment it clears and put
      every draft-first MR on the board already breached.
    """
    candidates = [e.occurred_at for e in events if _restarts_assignment_clock(e)]
    created = _snapshot_time(snapshot.get("created_at"))
    if created is not None:
        candidates.append(created)
    return max(candidates, default=None)


def _assignment_state(
    events: list[Event],
    snapshot: dict,
    config: Config,
    mr_waiver: str | None,
    now: datetime,
) -> ObligationState | None:
    """The obligation to put *someone* on an open MR that has no reviewers.

    None when the MR has reviewers, is no longer open (a merged MR nobody
    reviewed is history, not a task), has already been approved, or the
    matching SLA rule sets no assignment budget.
    """
    if snapshot.get("state", "opened") != "opened" or snapshot.get("reviewers"):
        return None
    if any(e.event_type == EventType.APPROVAL_ADDED for e in events):
        # Somebody did review this and approved it. It is waiting to merge, not
        # waiting for a reviewer, even if the approver has since been removed.
        return None
    rule = match_sla(config, snapshot.get("target_branch"), snapshot.get("labels", []))
    budget = rule.assignment_business_hours
    if budget is None:
        return None
    since = _unassigned_since(events, snapshot)
    if since is None:
        return None

    author = snapshot.get("author") or ""
    # Round 0: no review was ever requested, so this precedes every round the
    # MR could go on to have, and never collides with one in the store.
    obl = Obligation(
        project_id=snapshot["project_id"],
        mr_iid=snapshot["mr_iid"],
        reviewer=author,
        round=0,
        requested_at=since,
    )
    tz = config.calendar.tz_for(author)
    if mr_waiver is not None:
        # A draft is not expected to have reviewers yet, so it says so in blue
        # rather than running a clock nobody agreed to.
        return _state(
            obl, config, CHIP_WAIVED, "assignment",
            f"no reviewers assigned — waived ({mr_waiver})", 0.0, 0.0, 0.0, 0.0,
            paused=False, tz=tz, resolved_at=None, resolution_type=None,
            within_sla=None, urgency=math.inf, kind=KIND_ASSIGNMENT,
        )

    elapsed = business_hours_between(since, now, config.calendar.calendar, tz)
    fraction = elapsed / budget if budget > 0 else math.inf
    remaining = budget - elapsed
    return _state(
        obl, config, _color_for_fraction(fraction), "assignment",
        "no reviewers assigned", budget, elapsed, remaining, fraction,
        paused=False, tz=tz, resolved_at=None, resolution_type=None,
        within_sla=None, urgency=remaining, kind=KIND_ASSIGNMENT,
    )


def derive_mr(
    events: list[Event],
    snapshot: dict,
    config: Config,
    now: datetime,
) -> list[ObligationState]:
    """Derive obligation states for a single MR."""
    events = sorted(events, key=lambda e: (e.occurred_at, e.event_type))
    mr_waiver = is_waived_by_mr(
        config.waive, snapshot.get("draft", False), snapshot.get("labels", [])
    )
    states = [
        _derive_one(o, config, snapshot, mr_waiver, now)
        for o in _build_obligations(events, snapshot.get("author"))
    ]
    # Derived from the snapshot, not the events, so a brand-new MR nobody has
    # touched (and which therefore has no events at all) still gets one.
    unassigned = _assignment_state(events, snapshot, config, mr_waiver, now)
    if unassigned is not None:
        states.append(unassigned)
    return states
