"""Team configuration and the team-authored / team-review board filters."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from radar.config import ConfigError, load_config
from radar.db import Database
from radar.events import EventType as ET
from radar.service import build_dashboard
from radar.web.app import COOKIE_NAME, create_app
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
waive: {draft: true}
teams:
  - name: backend
    members: [dan, maya]
  - name: platform
    members: [sam]
"""


def _config(tmp_path, yaml_text=_YAML):
    path = tmp_path / "config.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    return load_config(path)


def _seed(db):
    # (iid, author, reviewer)
    rows = [(1, "aviva", "dan"), (2, "priya", "maya"), (3, "dan", "sam")]
    for iid, author, reviewer in rows:
        db.upsert_mr_snapshot(
            project_id=1, mr_iid=iid, title=f"MR {iid}", author=author,
            web_url=f"https://gl/mr/{iid}", source_branch="f", target_branch="main",
            description="", labels=[], draft=False, state="opened", reviewers=[reviewer],
            created_at="2026-03-02T09:00:00Z", updated_at="2026-03-02T09:00:00Z",
        )
        db.insert_events(
            [ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer=reviewer, mr_iid=iid)]
        )


# --- config parsing --------------------------------------------------------


def test_team_config_parses(tmp_path):
    cfg = _config(tmp_path)
    assert [t.name for t in cfg.teams] == ["backend", "platform"]
    assert cfg.team_by_name("backend").member_set == {"dan", "maya"}
    assert cfg.team_by_name("nope") is None


def test_team_config_errors(tmp_path):
    dup = _YAML.replace("name: platform", "name: backend")
    with pytest.raises(ConfigError, match="duplicate team name"):
        _config(tmp_path, dup)
    empty = _YAML.replace("members: [sam]", "members: []")
    with pytest.raises(ConfigError, match="non-empty list"):
        _config(tmp_path, empty)


# --- filtering -------------------------------------------------------------


def test_team_authored_filter(tmp_path):
    db = Database(tmp_path / "t.db")
    _seed(db)
    # backend = {dan, maya}; only MR 3 is authored by a member (dan).
    view = "team:backend:authored"
    data = build_dashboard(db, _config(tmp_path), now=ny(2026, 3, 2, 10), view=view)
    assert data["view"]["kind"] == "team_authored"
    assert [r["mr_iid"] for r in data["rows"]] == [3]
    db.close()


def test_team_review_filter(tmp_path):
    db = Database(tmp_path / "t.db")
    _seed(db)
    # backend members are reviewers on MR 1 (dan) and MR 2 (maya).
    view = "team:backend:review"
    data = build_dashboard(db, _config(tmp_path), now=ny(2026, 3, 2, 10), view=view)
    assert data["view"]["kind"] == "team_review"
    assert sorted(r["mr_iid"] for r in data["rows"]) == [1, 2]
    # obligations are narrowed to backend members only
    assert all(
        o["reviewer"] in {"dan", "maya"} for r in data["rows"] for o in r["obligations"]
    )
    db.close()


def test_unknown_team_token_falls_back_to_all(tmp_path):
    db = Database(tmp_path / "t.db")
    _seed(db)
    view = "team:ghost:authored"
    data = build_dashboard(db, _config(tmp_path), now=ny(2026, 3, 2, 10), view=view)
    assert data["view"]["kind"] == "all"
    assert data["open_mrs"] == 3
    db.close()


# --- web / cookie ----------------------------------------------------------


def test_team_pills_and_cookie(tmp_path):
    db_path = tmp_path / "t.db"
    db = Database(db_path)
    _seed(db)
    db.close()

    client = TestClient(create_app(_config(tmp_path), str(db_path)))

    home = client.get("/").text
    assert "backend · authored" in home and "backend · to review" in home
    assert "platform · authored" in home

    resp = client.get("/?view=team:backend:review")
    assert "Review requested from team backend" in resp.text
    assert client.cookies.get(COOKIE_NAME) == "team:backend:review"
    assert "MR 1" in resp.text and "MR 2" in resp.text and "MR 3" not in resp.text

    # cookie persists on the polled partial
    partial = client.get("/partials/board")
    assert "MR 1" in partial.text and "MR 3" not in partial.text
