"""QA test-plan infrastructure: Jira chips, launch button, generation + storage."""

from __future__ import annotations

import re
import sys
import time

from fastapi.testclient import TestClient

from radar.config import load_config
from radar.db import Database
from radar.events import EventType as ET
from radar.web.app import create_app
from tests.conftest import ev, ny

PY = f'"{sys.executable}"'


def _config(tmp_path, qa_command):
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""
gitlab: {{projects: [g/p]}}
calendar:
  workdays: [mon, tue, wed, thu, fri]
  work_hours: {{start: "09:00", end: "18:00"}}
  default_timezone: America/New_York
slas:
  - match: {{}}
    first_response_business_hours: 16
    approval_business_hours: 24
waive: {{draft: true}}
jira:
  base_url: https://yourco.atlassian.net
  project_keys: [PROJ]
qa:
  enabled: true
  command: '{qa_command}'
  timeout_seconds: 30
""",
        encoding="utf-8",
    )
    return load_config(path)


def _seed(db, title="PROJ-42: add widget"):
    db.upsert_mr_snapshot(
        project_id=1, mr_iid=7, title=title, author="aviva",
        web_url="https://gitlab.example.com/g/p/-/merge_requests/7",
        source_branch="feature/PROJ-42-widget", target_branch="main",
        description="Implements PROJ-42.", labels=[], draft=False,
        state="opened", reviewers=["dan"], created_at="2026-03-02T09:00:00Z",
        updated_at="2026-03-02T09:00:00Z",
    )
    db.insert_events([ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan", mr_iid=7)])


def test_board_shows_jira_chip_and_qa_button(tmp_path):
    config = _config(tmp_path, f'{PY} -c "print(1)"')
    db_path = tmp_path / "r.db"
    db = Database(db_path)
    _seed(db)
    db.close()

    client = TestClient(create_app(config, str(db_path)))
    html = client.get("/").text
    assert "🧪 QA plan" in html
    assert "PROJ-42" in html
    assert "https://yourco.atlassian.net/browse/PROJ-42" in html


def test_qa_generates_stores_and_reopens_plan(tmp_path):
    # QA command echoes a plan that references the passed Jira key.
    config = _config(tmp_path, f'{PY} -c "import sys;print(chr(35),sys.argv[1])" {{jira_keys}}')
    db_path = tmp_path / "r.db"
    db = Database(db_path)
    _seed(db)
    db.close()

    client = TestClient(create_app(config, str(db_path)))

    start = client.post("/qa/1/7")
    assert start.status_code == 200
    job_id = re.search(r'data-job-id="([0-9a-f]+)"', start.text).group(1)

    html = ""
    for _ in range(300):
        html = client.get(f"/qa/status/{job_id}").text
        if "review-output" in html or "review-error" in html:
            break
        time.sleep(0.05)
    assert "review-output" in html
    assert "PROJ-42" in html  # the skill received the extracted key

    # Plan persisted -> board shows the badge and it re-opens from storage.
    board = client.get("/").text
    assert "✓ QA plan" in board
    stored = client.get("/qa/stored/1/7")
    assert stored.status_code == 200
    assert "PROJ-42" in stored.text
    assert "saved" in stored.text  # generated_at stamp

    # Directly confirm DB persistence.
    db = Database(db_path)
    plan = db.get_test_plan(1, 7)
    db.close()
    assert plan is not None
    assert plan["jira_keys"] == "PROJ-42"
    assert "PROJ-42" in plan["content"]


_QA_STREAM = (
    "import json\n"
    'print(json.dumps({"type": "assistant", "message": {"content":'
    ' [{"type": "tool_use", "name": "WebFetch"}]}}))\n'
    'print(json.dumps({"type": "result", "result": "# QA Plan for PROJ-42"}))\n'
)


def test_qa_stream_endpoint_emits_progress_and_end(tmp_path):
    script = tmp_path / "emit.py"
    script.write_text(_QA_STREAM, encoding="utf-8")
    config = _config(tmp_path, f'{PY} "{script}"')
    db_path = tmp_path / "r.db"
    db = Database(db_path)
    _seed(db)
    db.close()

    client = TestClient(create_app(config, str(db_path)))
    start = client.post("/qa/1/7")
    assert "EventSource" in start.text  # running panel wires up the live stream
    job_id = re.search(r'data-job-id="([0-9a-f]+)"', start.text).group(1)

    # Let the job finish, then read the (now terminal) SSE stream deterministically.
    for _ in range(300):
        if "review-output" in client.get(f"/qa/status/{job_id}").text:
            break
        time.sleep(0.05)

    body = client.get(f"/qa/stream/{job_id}").text
    assert "event: progress" in body
    assert "WebFetch" in body
    assert "event: end" in body
    assert '"status": "done"' in body


def test_qa_disabled_hides_button_and_blocks_endpoint(config, tmp_path):
    # `config` fixture has no qa section -> disabled.
    db_path = tmp_path / "r.db"
    db = Database(db_path)
    _seed(db)
    db.close()
    client = TestClient(create_app(config, str(db_path)))
    assert "🧪 QA plan" not in client.get("/").text
    assert client.post("/qa/1/7").status_code == 404


def test_stored_plan_404_when_none(tmp_path):
    config = _config(tmp_path, f'{PY} -c "print(1)"')
    db_path = tmp_path / "r.db"
    db = Database(db_path)
    _seed(db)
    db.close()
    client = TestClient(create_app(config, str(db_path)))
    assert client.get("/qa/stored/1/7").status_code == 404
