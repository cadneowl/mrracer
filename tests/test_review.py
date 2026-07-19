"""AI-review launch: argv safety, job lifecycle, and the web endpoints."""

from __future__ import annotations

import re
import sys
import time

import pytest
from fastapi.testclient import TestClient

from radar.config import ReviewConfig, load_config
from radar.db import Database
from radar.events import EventType as ET
from radar.review import ReviewCommandError, ReviewRunner, build_argv
from radar.web.app import _render_markdown, create_app
from tests.conftest import ev, ny

# Quoted interpreter path so a Windows path with spaces/backslashes stays one
# argv token; the command is a Python one-liner so tests need no external tool.
PY = f'"{sys.executable}"'


# --- argv safety -----------------------------------------------------------


def test_build_argv_substitutes_into_single_token():
    argv = build_argv('claude -p "/review {web_url}"', {"web_url": "https://x/y"})
    assert argv == ["claude", "-p", "/review https://x/y"]


def test_build_argv_no_shell_injection():
    # A hostile branch name must stay inside one argv token, never split out.
    argv = build_argv(
        "mytool --branch {source_branch}",
        {"source_branch": "foo; rm -rf / #"},
    )
    assert argv == ["mytool", "--branch", "foo; rm -rf / #"]


def test_build_argv_preserves_interpreter_path():
    argv = build_argv(f'{PY} -c "print(1)"', {})
    assert argv[0] == sys.executable
    assert argv[1:] == ["-c", "print(1)"]


def test_build_argv_rejects_flag_smuggling():
    # A standalone placeholder token whose value starts with '-' is a flag-
    # smuggling attempt via attacker-influenced MR metadata (e.g. the title).
    with pytest.raises(ReviewCommandError):
        build_argv("mytool {title}", {"title": "--upload-file=/etc/passwd"})


def test_build_argv_allows_embedded_placeholder_with_dashy_value():
    # Embedded after a fixed prefix -> value stays inside one token, safe even
    # if it contains dashes.
    argv = build_argv("mytool --title={title}", {"title": "--not-a-flag"})
    assert argv == ["mytool", "--title=--not-a-flag"]


def test_build_argv_allows_literal_flags():
    argv = build_argv("claude -p /review", {})
    assert argv == ["claude", "-p", "/review"]


def test_runner_reports_flag_smuggling_as_job_error():
    cfg = ReviewConfig(enabled=True, command="mytool {title}", timeout_seconds=30)
    runner = ReviewRunner(cfg)
    job = runner.start({"project_id": 1, "mr_iid": 2, "title": "-rf"})
    assert job.status == "error"
    assert "flag" in job.error


# --- markdown sanitization (review output is untrusted) --------------------


def test_render_strips_script_but_keeps_markdown():
    html = str(_render_markdown("# Title\n\n<script>alert(document.cookie)</script>\n\n**bold**"))
    assert "<script>" not in html and "alert(document.cookie)" not in html
    assert "<h1>" in html and "<strong>bold</strong>" in html


def test_render_strips_event_handlers_and_js_urls():
    html = str(
        _render_markdown('<img src=x onerror="alert(1)">\n\n[click](javascript:alert(1))')
    )
    assert "onerror" not in html
    assert "javascript:" not in html


def test_render_keeps_code_blocks_verbatim():
    # A code review is full of angle brackets; they must render, not corrupt.
    html = str(_render_markdown("```python\nif a < b and c > d:\n    pass\n```"))
    assert "<pre>" in html or "<code>" in html
    assert "a &lt; b" in html and "c &gt; d" in html


# --- runner lifecycle ------------------------------------------------------


def _await(runner, job, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = runner.get(job.id)
        if current.status != "running":
            return current
        time.sleep(0.05)
    raise AssertionError("review job did not finish in time")


def test_runner_captures_stdout():
    cfg = ReviewConfig(enabled=True, command=f'{PY} -c "print(chr(35),42)"', timeout_seconds=30)
    runner = ReviewRunner(cfg)
    job = runner.start({"project_id": 1, "mr_iid": 2, "title": "T"})
    done = _await(runner, job)
    assert done.status == "done"
    assert "42" in done.output


def test_runner_reports_error_on_nonzero():
    cmd = f'{PY} -c "import sys; sys.exit(3)"'
    cfg = ReviewConfig(enabled=True, command=cmd, timeout_seconds=30)
    runner = ReviewRunner(cfg)
    job = runner.start({"project_id": 1, "mr_iid": 2})
    done = _await(runner, job)
    assert done.status == "error"
    assert done.returncode == 3


def test_runner_missing_command():
    cfg = ReviewConfig(enabled=True, command="definitely-not-a-real-binary-xyz", timeout_seconds=30)
    runner = ReviewRunner(cfg)
    job = runner.start({"project_id": 1, "mr_iid": 2})
    done = _await(runner, job)
    assert done.status == "error"
    assert "not found" in done.error


# --- web endpoints ---------------------------------------------------------


def _review_config(tmp_path, command):
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
review:
  enabled: true
  command: '{command}'
  timeout_seconds: 30
""",
        encoding="utf-8",
    )
    return load_config(path)


def _seed(db):
    db.upsert_mr_snapshot(
        project_id=1, mr_iid=7, title="Add widget", author="aviva",
        web_url="https://gitlab.example.com/g/p/-/merge_requests/7",
        source_branch="f", target_branch="main", labels=[], draft=False,
        state="opened", reviewers=["dan"], created_at="2026-03-02T09:00:00Z",
        updated_at="2026-03-02T09:00:00Z",
    )
    db.insert_events([ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan", mr_iid=7)])


def test_review_button_and_flow(tmp_path):
    config = _review_config(tmp_path, f'{PY} -c "print(chr(35),42)"')
    db_path = tmp_path / "r.db"
    db = Database(db_path)
    _seed(db)
    db.close()

    client = TestClient(create_app(config, str(db_path)))

    # The board shows a review button when enabled.
    assert "🔍 review" in client.get("/").text

    # Starting a review returns the modal; poll status until it finishes.
    start = client.post("/review/1/7")
    assert start.status_code == 200
    assert "AI review" in start.text
    m = re.search(r"/review/status/([0-9a-f]+)", start.text)
    assert m, "expected a status poll URL while running"
    job_id = m.group(1)

    html = ""
    for _ in range(300):
        html = client.get(f"/review/status/{job_id}").text
        if "review-output" in html or "review-error" in html:
            break
        time.sleep(0.05)
    assert "review-output" in html  # rendered markdown result

    # Close returns empty content to dismiss the modal.
    assert client.get("/review/close").text == ""


def test_review_disabled_hides_button_and_blocks_endpoint(config, tmp_path):
    # `config` fixture has no review section -> disabled.
    db_path = tmp_path / "r.db"
    db = Database(db_path)
    _seed(db)
    db.close()
    client = TestClient(create_app(config, str(db_path)))
    assert "🔍 review" not in client.get("/").text
    assert client.post("/review/1/7").status_code == 404
