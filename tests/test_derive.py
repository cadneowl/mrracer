"""State derivation from synthetic event sequences."""

from __future__ import annotations

from radar.derive import (
    CHIP_AT_RISK,
    CHIP_BREACHED,
    CHIP_IN_SLA,
    CHIP_PENDING,
    CHIP_WAIVED,
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
