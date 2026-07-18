"""Web layer smoke tests: templates render and the board reflects DB state."""

from __future__ import annotations

from fastapi.testclient import TestClient

from radar.db import Database
from radar.gitlab_client import FixtureSource
from radar.poller import poll_once
from radar.web.app import create_app
from tests.test_poller import PID, PROJECT, _discussions, _mr


def _seed(db):
    src = FixtureSource(
        mrs_by_project={PROJECT: [_mr(["dan"])]},
        discussions_by_mr={(PID, 1): _discussions()},
    )
    return src


def test_dashboard_renders(config, tmp_path):
    db_path = tmp_path / "web.db"
    db = Database(db_path)
    poll_once(db, config, _seed(db))
    db.close()

    app = create_app(config, str(db_path))
    client = TestClient(app)

    resp = client.get("/")
    assert resp.status_code == 200
    assert "review SLA board" in resp.text
    assert "Add widget" in resp.text
    assert "dan" in resp.text

    partial = client.get("/partials/board")
    assert partial.status_code == 200
    assert "chip" in partial.text

    assert client.get("/healthz").json() == {"status": "ok"}


def test_empty_dashboard(config, tmp_path):
    db_path = tmp_path / "empty.db"
    Database(db_path).close()
    app = create_app(config, str(db_path))
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "No open merge requests" in resp.text
