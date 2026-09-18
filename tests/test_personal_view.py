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
    # aviva reviews nothing but opened both MRs, so she still gets a pill.
    assert {p["username"] for p in full["people"]} == {"dan", "maya", "aviva"}

    dan_view = build_dashboard(db, config, now=ny(2026, 3, 2, 10), view="dan")
    assert dan_view["view"]["kind"] == "reviewer"
    assert dan_view["view"]["token"] == "dan"
    assert dan_view["open_mrs"] == 1
    assert dan_view["rows"][0]["mr_iid"] == 1
    assert all(o["reviewer"] == "dan" for r in dan_view["rows"] for o in r["obligations"])
    db.close()


def test_person_view_lists_authored_and_review_requested_apart(config, tmp_path):
    db = Database(tmp_path / "p.db")
    _seed(db)
    # MR 3: dan opened it and maya is reviewing it.
    db.upsert_mr_snapshot(
        project_id=1, mr_iid=3, title="MR 3", author="dan",
        web_url="https://gl/mr/3", source_branch="g", target_branch="main",
        description="", labels=[], draft=False, state="opened", reviewers=["maya"],
        created_at="2026-03-02T09:00:00Z", updated_at="2026-03-02T09:00:00Z",
    )
    db.insert_events([ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="maya", mr_iid=3)])

    data = build_dashboard(db, config, now=ny(2026, 3, 2, 10), view="dan")
    authored, requested = data["sections"]
    assert authored["title"] == "Authored by dan"
    assert [r["mr_iid"] for r in authored["rows"]] == [3]
    # Every chip on his own MR stays: it's who he is waiting on.
    assert [o["reviewer"] for o in authored["rows"][0]["obligations"]] == ["maya"]
    assert requested["title"] == "Review requested from dan"
    assert [r["mr_iid"] for r in requested["rows"]] == [1]
    assert data["open_mrs"] == 2

    # maya's view is the mirror image: nothing authored, two reviews asked of her.
    maya = build_dashboard(db, config, now=ny(2026, 3, 2, 10), view="maya")
    assert maya["sections"][0]["rows"] == []
    assert sorted(r["mr_iid"] for r in maya["sections"][1]["rows"]) == [2, 3]
    db.close()


def test_own_mr_is_not_listed_twice(config, tmp_path):
    """Asked to review your own MR: it shows once, under authored, chip and all."""
    db = Database(tmp_path / "p.db")
    db.upsert_mr_snapshot(
        project_id=1, mr_iid=1, title="Self", author="dan",
        web_url="https://gl/mr/1", source_branch="f", target_branch="main",
        description="", labels=[], draft=False, state="opened", reviewers=["dan"],
        created_at="2026-03-02T09:00:00Z", updated_at="2026-03-02T09:00:00Z",
    )
    db.insert_events([ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan", mr_iid=1)])

    data = build_dashboard(db, config, now=ny(2026, 3, 2, 10), view="dan")
    authored, requested = data["sections"]
    assert [r["mr_iid"] for r in authored["rows"]] == [1]
    assert requested["rows"] == []
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
    assert "Authored by dan" in resp.text
    assert "Review requested from dan" in resp.text
    assert "No open MRs authored by dan." in resp.text
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
