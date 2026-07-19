"""Coach view: per-reviewer breach detail, compliance, chronic flag."""

from __future__ import annotations

from fastapi.testclient import TestClient

from radar.coach import build_coach
from radar.db import Database
from radar.events import EventType as ET
from radar.web.app import create_app
from tests.conftest import ev, ny, snapshot


def _snap(db, iid, reviewer, state="opened", title=None):
    s = snapshot(mr_iid=iid, reviewers=[reviewer], title=title or f"MR {iid}", state=state)
    db.upsert_mr_snapshot(
        project_id=1, mr_iid=iid, title=s["title"], author=s["author"],
        web_url=s["web_url"], source_branch="f", target_branch="main",
        description="", labels=[], draft=False, state=state, reviewers=[reviewer],
        created_at="2026-03-02T09:00:00Z", updated_at="2026-03-02T09:00:00Z",
    )


def test_open_breach_and_compliance(config, tmp_path):
    db = Database(tmp_path / "c.db")

    # dan: one MR currently breached (requested Mon 9:00, default 16h budget).
    _snap(db, 1, "dan", title="Breached MR")
    db.insert_events([ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan", mr_iid=1)])

    # dan: one resolved-within-SLA obligation (fast approval).
    _snap(db, 2, "dan", title="Clean MR")
    db.insert_events([
        ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan", mr_iid=2),
        ev(ET.APPROVAL_ADDED, ny(2026, 3, 2, 10), reviewer="dan", mr_iid=2),
    ])

    # now = Wed 09:00 -> MR 1 has consumed 18h (> 16h) -> BREACHED.
    data = build_coach(db, config, now=ny(2026, 3, 4, 9))
    by = {r["username"]: r for r in data["reviewers"]}
    dan = by["dan"]
    assert dan["open_breach_count"] == 1
    assert dan["open_breaches"][0]["mr_iid"] == 1
    assert dan["open_breaches"][0]["overdue_hours"] > 0
    assert dan["resolved"] == 1 and dan["resolved_within"] == 1
    assert dan["compliance_pct"] == 100
    assert dan["median_first_response"] is not None
    assert data["team"]["open_breaches"] == 1
    db.close()


def test_chronic_flag(config, tmp_path):
    db = Database(tmp_path / "c.db")
    # maya: 3 resolved obligations that breached the approval SLA -> chronic.
    # Fast first response, but approval lands far past the 24h approval budget.
    for iid in (1, 2, 3):
        _snap(db, iid, "maya")
        db.insert_events([
            ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="maya", mr_iid=iid),
            ev(ET.CHANGES_REQUESTED, ny(2026, 3, 2, 10), reviewer="maya", mr_iid=iid),
            ev(ET.COMMITS_PUSHED, ny(2026, 3, 2, 11), actor="aviva", mr_iid=iid),
            ev(ET.APPROVAL_ADDED, ny(2026, 3, 5, 12), reviewer="maya", mr_iid=iid),  # ~28h owed
        ])
    data = build_coach(db, config, now=ny(2026, 3, 6, 9))
    maya = {r["username"]: r for r in data["reviewers"]}["maya"]
    assert maya["breach_total"] == 3
    assert maya["compliance_pct"] == 0
    assert maya["chronic"] is True
    assert data["team"]["chronic"] == 1
    db.close()


def test_waived_excluded(config, tmp_path):
    db = Database(tmp_path / "c.db")
    _snap(db, 1, "sam")
    db.upsert_mr_snapshot(
        project_id=1, mr_iid=1, title="Draft MR", author="aviva", web_url="x",
        source_branch="f", target_branch="main", description="", labels=[], draft=True,
        state="opened", reviewers=["sam"], created_at="2026-03-02T09:00:00Z",
        updated_at="2026-03-02T09:00:00Z",
    )
    db.insert_events([ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="sam", mr_iid=1)])
    data = build_coach(db, config, now=ny(2026, 3, 4, 9))
    assert data["reviewers"] == []  # draft -> waived -> excluded
    db.close()


def test_coach_page_renders(config, tmp_path):
    db_path = tmp_path / "c.db"
    db = Database(db_path)
    _snap(db, 1, "dan", title="Breached MR")
    db.insert_events([ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan", mr_iid=1)])
    db.close()

    client = TestClient(create_app(config, str(db_path)))
    page = client.get("/coach")
    assert page.status_code == 200
    assert "coach view" in page.text and "manager only" in page.text
    assert "dan" in page.text
    assert client.get("/coach/partial").status_code == 200

    # The board footer links to the coach view.
    assert '/coach' in client.get("/").text
