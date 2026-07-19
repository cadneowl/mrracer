"""Personal-view filtering (click-a-person) and cookie persistence."""

from __future__ import annotations

from fastapi.testclient import TestClient

from radar.db import Database
from radar.events import EventType as ET
from radar.service import build_dashboard
from radar.web.app import COOKIE_NAME, create_app
from tests.conftest import ev, ny, snapshot


def _seed(db):
    # MR 1: dan is the reviewer; MR 2: maya is the reviewer.
    for iid, reviewer in ((1, "dan"), (2, "maya")):
        snap = snapshot(mr_iid=iid, reviewers=[reviewer], title=f"MR {iid}")
        db.upsert_mr_snapshot(
            project_id=1, mr_iid=iid, title=snap["title"], author="aviva",
            web_url=snap["web_url"], source_branch="f", target_branch="main",
            description="", labels=[], draft=False, state="opened", reviewers=[reviewer],
            created_at="2026-03-02T09:00:00Z", updated_at="2026-03-02T09:00:00Z",
        )
        db.insert_events(
            [ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer=reviewer, mr_iid=iid)]
        )


def test_filter_to_one_reviewer(config, tmp_path):
    db = Database(tmp_path / "p.db")
    _seed(db)

    full = build_dashboard(db, config, now=ny(2026, 3, 2, 10))
    assert full["open_mrs"] == 2
    assert {p["username"] for p in full["people"]} == {"dan", "maya"}

    dan_view = build_dashboard(db, config, now=ny(2026, 3, 2, 10), view="dan")
    assert dan_view["view"]["kind"] == "reviewer"
    assert dan_view["view"]["token"] == "dan"
    assert dan_view["open_mrs"] == 1
    assert dan_view["rows"][0]["mr_iid"] == 1
    assert all(o["reviewer"] == "dan" for r in dan_view["rows"] for o in r["obligations"])
    db.close()


def test_people_waiting_counts(config, tmp_path):
    db = Database(tmp_path / "p.db")
    _seed(db)
    data = build_dashboard(db, config, now=ny(2026, 3, 2, 10))
    by_name = {p["username"]: p for p in data["people"]}
    assert by_name["dan"]["waiting"] == 1  # fresh obligation, clock running
    assert by_name["dan"]["total"] == 1
    db.close()


def test_cookie_roundtrip(config, tmp_path):
    db_path = tmp_path / "p.db"
    db = Database(db_path)
    _seed(db)
    db.close()

    app = create_app(config, str(db_path))
    client = TestClient(app)

    # Picking a person sets the cookie and renders the personal view.
    resp = client.get("/?view=dan")
    assert resp.status_code == 200
    assert "MRs waiting on" in resp.text
    assert client.cookies.get(COOKIE_NAME) == "dan"

    # The polled partial honours the remembered cookie (personal view persists).
    partial = client.get("/partials/board")
    assert "MR 1" in partial.text
    assert "MR 2" not in partial.text

    # Clearing returns to the team board and deletes the cookie.
    resp = client.get("/?view=")
    assert "MR 1" in resp.text and "MR 2" in resp.text
    assert not client.cookies.get(COOKIE_NAME)
    db_check = Database(db_path)
    db_check.close()
