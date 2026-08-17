"""MRs nobody was asked to review: the NO REVIEWERS chip and its SLA."""

from __future__ import annotations

from fastapi.testclient import TestClient

from radar.coach import build_coach
from radar.config import load_config
from radar.db import Database
from radar.events import EventType as ET
from radar.service import build_dashboard, recompute
from radar.threads import Thread
from radar.web.app import create_app
from tests.conftest import ev, ny

_YAML = """
gitlab: {projects: [g/p]}
calendar:
  workdays: [mon, tue, wed, thu, fri]
  work_hours: {start: "09:00", end: "18:00"}
  default_timezone: America/New_York
slas:
  - match: {}
    first_response_business_hours: 16
    approval_business_hours: 24
    assignment_business_hours: 4
waive: {draft: true}
teams:
  - name: backend
    members: [dan, maya]
"""


def _config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(_YAML, encoding="utf-8")
    return load_config(path)


def _seed(db):
    """MR 1: aviva's, dan reviewing. MR 2: dan's, nobody reviewing and — the
    case that used to be invisible — no events on it at all."""
    db.upsert_mr_snapshot(
        project_id=1, mr_iid=1, title="Reviewed MR", author="aviva",
        web_url="https://gl/mr/1", source_branch="f1", target_branch="main",
        description="", labels=[], draft=False, state="opened", reviewers=["dan"],
        created_at="2026-03-02T09:00:00Z", updated_at="2026-03-02T09:00:00Z",
    )
    db.insert_events([ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan", mr_iid=1)])
    db.upsert_mr_snapshot(
        project_id=1, mr_iid=2, title="Orphan MR", author="dan",
        web_url="https://gl/mr/2", source_branch="f2", target_branch="main",
        description="", labels=[], draft=False, state="opened", reviewers=[],
        created_at="2026-03-02T09:00:00Z", updated_at="2026-03-02T09:00:00Z",
    )


def test_unassigned_mr_reaches_the_board(tmp_path):
    db = Database(tmp_path / "u.db")
    _seed(db)
    data = build_dashboard(db, _config(tmp_path), now=ny(2026, 3, 2, 12))

    row = next(r for r in data["rows"] if r["mr_iid"] == 2)
    [obligation] = row["obligations"]
    assert obligation["kind"] == "assignment"
    assert obligation["status_text"] == "no reviewers assigned"
    assert obligation["chip_state"] == "AT_RISK"  # 3h of a 4h budget
    assert obligation["remaining_label"] == "1.0h left"
    # Sorted most-urgent first, so an orphan outranks a review with 13h left.
    assert [r["mr_iid"] for r in data["rows"]] == [2, 1]
    assert data["summary"]["AT_RISK"] == 1
    db.close()


def test_the_chip_carries_no_name(tmp_path):
    db_path = tmp_path / "u.db"
    db = Database(db_path)
    _seed(db)
    db.close()

    page = TestClient(create_app(_config(tmp_path), str(db_path))).get("/").text
    assert "NO REVIEWERS" in page
    assert "chip-unassigned" in page
    assert "Orphan MR" in page


def test_the_author_owes_it(tmp_path):
    db = Database(tmp_path / "u.db")
    _seed(db)
    config = _config(tmp_path)

    # dan is reviewing MR 1 and owes an assignment on the MR he opened, so both
    # are waiting on him and both show in his personal view.
    data = build_dashboard(db, config, now=ny(2026, 3, 2, 12), view="dan")
    assert sorted(r["mr_iid"] for r in data["rows"]) == [1, 2]
    dan = next(p for p in data["people"] if p["username"] == "dan")
    assert dan["waiting"] == 2

    # But nobody was *asked* to review MR 2, so it is not a review request and
    # stays out of "review requested from team backend".
    team = build_dashboard(db, config, now=ny(2026, 3, 2, 12), view="team:backend:review")
    assert [r["mr_iid"] for r in team["rows"]] == [1]
    db.close()


def test_the_chip_shows_no_thread_badge_for_the_authors_own_threads(tmp_path):
    """The badge means "threads this reviewer opened and nobody resolved". On a
    chip whose whole point is that there is no reviewer, the author's own
    unresolved thread is not what anyone is waiting on."""
    db = Database(tmp_path / "u.db")
    _seed(db)
    db.replace_threads(1, 2, [Thread(
        project_id=1, mr_iid=2, discussion_id="d1", author="dan",
        created_at="2026-03-02T10:00:00Z", updated_at="2026-03-02T10:00:00Z",
        resolvable=True, resolved=False, resolved_by=None, resolved_at=None,
        file_path="api/vex.py", line=42, notes=[],
    )])

    data = build_dashboard(db, _config(tmp_path), now=ny(2026, 3, 2, 12))
    row = next(r for r in data["rows"] if r["mr_iid"] == 2)
    assert row["obligations"][0]["open_threads"] == 0
    assert row["threads"]["open"] == 1  # still counted on the MR itself
    db.close()


def test_recompute_records_what_kind_of_obligation_it_was(tmp_path):
    db = Database(tmp_path / "u.db")
    _seed(db)
    recompute(db, _config(tmp_path), now=ny(2026, 3, 2, 12))
    stored = dict(db.conn.execute("SELECT mr_iid, kind FROM obligations").fetchall())
    # Both rows put a username in `reviewer`; only one of them is a review.
    assert stored == {1: "review", 2: "assignment"}
    db.close()


def test_coach_does_not_book_it_against_a_reviewer(tmp_path):
    db = Database(tmp_path / "u.db")
    _seed(db)
    # 14:00: MR 2 is an hour past its assignment budget, MR 1's review is not.
    data = build_coach(db, _config(tmp_path), now=ny(2026, 3, 2, 14))
    [dan] = data["reviewers"]
    assert dan["username"] == "dan"
    assert dan["open_load"] == 1  # the review on MR 1, and nothing else
    assert dan["open_breach_count"] == 0
    assert data["team"]["open_breaches"] == 0
    db.close()
