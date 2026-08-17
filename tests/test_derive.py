"""State derivation from synthetic event sequences."""

from __future__ import annotations

from radar.derive import (
    CHIP_AT_RISK,
    CHIP_BREACHED,
    CHIP_IN_SLA,
    CHIP_PENDING,
    CHIP_WAIVED,
    KIND_ASSIGNMENT,
    KIND_REVIEW,
    derive_mr,
)
from radar.events import EventType as ET
from tests.conftest import ev, ny, snapshot


def _one(events, config, now, snap=None):
    states = derive_mr(events, snap or snapshot(), config, now)
    assert len(states) == 1
    return states[0]


# --- phase A: awaiting first response --------------------------------------


def test_pending_green_when_fresh(config):
    events = [ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan", actor="aviva")]
    st = _one(events, config, now=ny(2026, 3, 2, 10))  # 1h into 16h budget
    assert st.chip_state == CHIP_IN_SLA
    assert st.phase == "first_response"
    assert st.elapsed_hours == 1.0
    assert st.remaining_hours == 15.0


def test_at_risk_amber_at_75_percent(config):
    events = [ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan")]
    # 12h = 75% of 16h: Mon 9-18 (9h) + Tue 9-12 (3h)
    st = _one(events, config, now=ny(2026, 3, 3, 12))
    assert st.chip_state == CHIP_AT_RISK
    assert st.elapsed_hours == 12.0


def test_breached_red_over_budget(config):
    events = [ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan")]
    # Mon 9-18 (9h) + Tue 9-18 (9h) = 18h > 16h
    st = _one(events, config, now=ny(2026, 3, 4, 9))
    assert st.chip_state == CHIP_BREACHED
    assert st.remaining_hours < 0


# --- first-response resolution rules ---------------------------------------


def test_bare_note_does_not_resolve(config):
    events = [
        ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan"),
        # a non-thread, non-diff comment by the reviewer
        ev(ET.NOTE_ADDED, ny(2026, 3, 2, 10), reviewer="dan", opens_thread=False, on_diff=False),
    ]
    st = _one(events, config, now=ny(2026, 3, 2, 11))
    assert st.phase == "first_response"  # still awaiting
    assert st.first_response_at is None


def test_diff_thread_resolves_first_response(config):
    events = [
        ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan"),
        ev(ET.NOTE_ADDED, ny(2026, 3, 2, 11), reviewer="dan", opens_thread=True, on_diff=True),
    ]
    st = _one(events, config, now=ny(2026, 3, 2, 12))
    assert st.first_response_at == ny(2026, 3, 2, 11)
    assert st.phase == "approval"
    assert st.thread_count == 1
    # ball is with the author right after the thread -> paused -> grey
    assert st.chip_state == CHIP_PENDING
    assert st.paused is True


def test_approval_as_first_action_resolves(config):
    events = [
        ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan"),
        ev(ET.APPROVAL_ADDED, ny(2026, 3, 2, 11), reviewer="dan"),
    ]
    st = _one(events, config, now=ny(2026, 3, 2, 12))
    assert st.phase == "resolved"
    assert st.chip_state == CHIP_PENDING  # grey, resolved-awaiting
    assert st.resolution_type == "approval"
    assert st.within_sla is True  # 2h <= 16h


# --- phase B: approval clock with fairness pause ---------------------------


def test_changes_requested_pauses_until_author_pushes(config):
    events = [
        ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan"),
        ev(ET.CHANGES_REQUESTED, ny(2026, 3, 2, 11), reviewer="dan"),
    ]
    # 3 business days later, but the ball never returned to the reviewer:
    st = _one(events, config, now=ny(2026, 3, 5, 11))
    assert st.phase == "approval"
    assert st.paused is True
    assert st.chip_state == CHIP_PENDING
    assert st.elapsed_hours == 0.0  # clock paused the whole time


def test_clock_resumes_after_author_pushes(config):
    events = [
        ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan"),
        ev(ET.CHANGES_REQUESTED, ny(2026, 3, 2, 11), reviewer="dan"),
        # author pushes fixes Tuesday 09:00 -> ball back to reviewer
        ev(ET.COMMITS_PUSHED, ny(2026, 3, 3, 9), actor="aviva"),
    ]
    # Now Tuesday 12:00 -> 3 business hours of reviewer-owed time on 8h approval budget
    st = _one(events, config, now=ny(2026, 3, 3, 12))
    assert st.paused is False
    assert st.elapsed_hours == 3.0
    assert st.chip_state == CHIP_IN_SLA


def test_author_note_also_resumes_clock(config):
    events = [
        ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan"),
        ev(ET.NOTE_ADDED, ny(2026, 3, 2, 11), reviewer="dan", opens_thread=True, on_diff=True),
        ev(ET.NOTE_ADDED, ny(2026, 3, 3, 9), actor="aviva", opens_thread=False, on_diff=False),
    ]
    st = _one(events, config, now=ny(2026, 3, 3, 12))
    assert st.paused is False
    assert st.elapsed_hours == 3.0


def test_late_approval_breaches_approval_sla(config):
    # Fast first response, but the reviewer sits on the approval far past the
    # approval budget (24h default) -> within_sla must be False, and the
    # recorded elapsed reflects the approval-phase reviewer-owed hours.
    events = [
        ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan"),
        ev(ET.CHANGES_REQUESTED, ny(2026, 3, 2, 10), reviewer="dan"),  # 1h first response
        ev(ET.COMMITS_PUSHED, ny(2026, 3, 2, 11), actor="aviva"),      # ball back to dan
        ev(ET.APPROVAL_ADDED, ny(2026, 3, 5, 12), reviewer="dan"),     # Thu — very late
    ]
    st = _one(events, config, now=ny(2026, 3, 6, 9))
    assert st.phase == "resolved"
    # Mon 11-18 (7) + Tue 9-18 (9) + Wed 9-18 (9) + Thu 9-12 (3) = 28h
    assert st.elapsed_hours == 28.0
    assert st.within_sla is False


def test_full_cycle_ends_on_approval(config):
    events = [
        ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan"),
        ev(ET.CHANGES_REQUESTED, ny(2026, 3, 2, 11), reviewer="dan"),
        ev(ET.COMMITS_PUSHED, ny(2026, 3, 3, 9), actor="aviva"),
        ev(ET.APPROVAL_ADDED, ny(2026, 3, 3, 12), reviewer="dan"),
    ]
    st = _one(events, config, now=ny(2026, 3, 4, 9))
    assert st.phase == "resolved"
    assert st.resolution_type == "approval"


# --- waivers ---------------------------------------------------------------


def test_draft_waives(config):
    events = [ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan")]
    st = _one(events, config, now=ny(2026, 3, 4, 9), snap=snapshot(draft=True))
    assert st.chip_state == CHIP_WAIVED


def test_waive_label(config):
    events = [ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan")]
    st = _one(events, config, now=ny(2026, 3, 4, 9), snap=snapshot(labels=["blocked"]))
    assert st.chip_state == CHIP_WAIVED


def test_reviewer_removed_waives(config):
    events = [
        ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan"),
        ev(ET.REVIEWER_REMOVED, ny(2026, 3, 2, 12), reviewer="dan"),
    ]
    st = _one(events, config, now=ny(2026, 3, 4, 9))
    assert st.chip_state == CHIP_WAIVED
    assert st.resolution_type == "reviewer_removed"


# --- multiple reviewers & rounds -------------------------------------------


def test_independent_clocks_per_reviewer(config):
    events = [
        ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan"),
        ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="ophira"),
        ev(ET.APPROVAL_ADDED, ny(2026, 3, 2, 10), reviewer="dan"),
    ]
    snap = snapshot(reviewers=["dan", "ophira"])
    states = derive_mr(events, snap, config, now=ny(2026, 3, 2, 12))
    by_rev = {s.reviewer: s for s in states}
    assert by_rev["dan"].phase == "resolved"
    assert by_rev["ophira"].phase == "first_response"
    # ophira is in Jerusalem tz; her clock still runs
    assert by_rev["ophira"].reviewer_tz == "Asia/Jerusalem"


def test_reopen_creates_new_round(config):
    events = [
        ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan"),
        ev(ET.APPROVAL_ADDED, ny(2026, 3, 2, 10), reviewer="dan"),
        # author pushes and re-requests review
        ev(ET.COMMITS_PUSHED, ny(2026, 3, 3, 9), actor="aviva"),
        ev(ET.REVIEW_REQUESTED, ny(2026, 3, 3, 10), reviewer="dan"),
    ]
    states = derive_mr(events, snapshot(), config, now=ny(2026, 3, 3, 12))
    rounds = sorted(s.round for s in states)
    assert rounds == [1, 2]
    r2 = next(s for s in states if s.round == 2)
    assert r2.phase == "first_response"  # fresh clock
    assert r2.requested_at == ny(2026, 3, 3, 10)


# --- SLA rule selection ----------------------------------------------------


def test_release_branch_uses_tighter_sla(config):
    events = [ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan")]
    snap = snapshot(target_branch="release/2026.3")
    st = _one(events, config, now=ny(2026, 3, 2, 12), snap=snap)  # 3h elapsed
    assert st.budget_hours == 4.0  # release/* rule
    assert st.chip_state == CHIP_AT_RISK  # 3/4 = 75%


def test_hotfix_label_uses_tighter_sla(config):
    events = [ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan")]
    snap = snapshot(labels=["hotfix"])
    st = _one(events, config, now=ny(2026, 3, 2, 10), snap=snap)
    assert st.budget_hours == 4.0


# --- assignment: an open MR with nobody reviewing it ------------------------
# The default snapshot has no reviewers and was created 09:00Z = 04:00 New York,
# so its assignment clock starts at 09:00 when the workday opens.


def test_no_assignment_obligation_unless_config_asks_for_one(config):
    # The base config sets no assignment_business_hours, so an MR nobody is
    # reviewing stays invisible — exactly the behaviour before the check existed.
    assert derive_mr([], snapshot(), config, now=ny(2026, 3, 2, 11)) == []


def test_unassigned_mr_is_tracked_with_no_events_at_all(assign_config):
    states = derive_mr([], snapshot(), assign_config, now=ny(2026, 3, 2, 11))
    assert len(states) == 1
    st = states[0]
    assert st.kind == KIND_ASSIGNMENT
    assert st.phase == "assignment"
    assert st.status_text == "no reviewers assigned"
    assert st.reviewer == "aviva"  # the author owes the assignment
    assert st.round == 0
    assert st.budget_hours == 4.0
    assert st.elapsed_hours == 2.0
    assert st.chip_state == CHIP_IN_SLA


def test_unassigned_mr_goes_amber_then_red(assign_config):
    at_risk = derive_mr([], snapshot(), assign_config, now=ny(2026, 3, 2, 12))[0]
    assert at_risk.chip_state == CHIP_AT_RISK  # 3h of 4h
    breached = derive_mr([], snapshot(), assign_config, now=ny(2026, 3, 2, 14))[0]
    assert breached.chip_state == CHIP_BREACHED
    assert breached.remaining_hours == -1.0
    assert breached.urgency == -1.0  # sorts among the overdue, not after them


def test_assigned_mr_has_no_assignment_obligation(assign_config):
    events = [ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan")]
    states = derive_mr(events, snapshot(reviewers=["dan"]), assign_config, now=ny(2026, 3, 2, 11))
    assert [s.kind for s in states] == [KIND_REVIEW]


def test_hotfix_label_tightens_the_assignment_budget_too(assign_config):
    st = derive_mr([], snapshot(labels=["hotfix"]), assign_config, now=ny(2026, 3, 2, 11))[0]
    assert st.budget_hours == 2.0  # the hotfix rule's own budget, not the default's 4


def test_draft_with_no_reviewers_is_waived_not_clocked(assign_config):
    # A draft is not expected to have reviewers yet, so it says so in blue.
    st = derive_mr([], snapshot(draft=True), assign_config, now=ny(2026, 3, 4, 12))[0]
    assert st.chip_state == CHIP_WAIVED
    assert "draft" in st.status_text
    assert st.elapsed_hours == 0.0


def test_the_draft_window_is_not_billed_once_the_mr_is_marked_ready(assign_config):
    """A draft owes nobody a reviewer, so the clock starts when it goes ready —
    otherwise every draft-first MR would arrive on the board already breached."""
    events = [ev(ET.DRAFT_TOGGLED, ny(2026, 3, 9, 10), actor="aviva", draft=False)]
    # Opened a week before it was marked ready, budget 4h.
    st = derive_mr(events, snapshot(), assign_config, now=ny(2026, 3, 9, 12))[0]
    assert st.chip_state == CHIP_IN_SLA
    assert st.elapsed_hours == 2.0
    assert st.requested_at == ny(2026, 3, 9, 10)


def test_approved_mr_is_not_asked_for_another_reviewer(assign_config):
    """It was reviewed and approved; losing the reviewer afterwards leaves it
    waiting to merge, not waiting for somebody to look at it."""
    events = [
        ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan"),
        ev(ET.APPROVAL_ADDED, ny(2026, 3, 2, 10), reviewer="dan"),
        ev(ET.REVIEWER_REMOVED, ny(2026, 3, 2, 11), reviewer="dan"),
    ]
    states = derive_mr(events, snapshot(), assign_config, now=ny(2026, 3, 4, 17))
    assert [s.kind for s in states] == [KIND_REVIEW]


def test_merged_mr_never_gets_an_assignment_obligation(assign_config):
    # Nobody reviewed it and it is already merged: that is history, not a task.
    assert derive_mr([], snapshot(state="merged"), assign_config, now=ny(2026, 3, 4, 12)) == []


def test_clock_starts_when_the_last_reviewer_was_removed(assign_config):
    events = [
        ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan"),
        ev(ET.REVIEWER_REMOVED, ny(2026, 3, 3, 12), reviewer="dan"),
    ]
    states = derive_mr(events, snapshot(), assign_config, now=ny(2026, 3, 3, 14))
    unassigned = [s for s in states if s.kind == KIND_ASSIGNMENT]
    assert len(unassigned) == 1
    # 2h since dan was dropped, not the two days since the MR was opened.
    assert unassigned[0].elapsed_hours == 2.0
    assert unassigned[0].requested_at == ny(2026, 3, 3, 12)
