"""Backend context fetch: radar pulls the MR diff / Jira epic and pipes it to
the skill on stdin (so the skill needs no GitLab/Jira access)."""

from __future__ import annotations

import re
import sys
import time

import pytest
from fastapi.testclient import TestClient

from radar.commands import CommandRunner
from radar.config import ConfigError, ReviewConfig, jira_credentials, load_config
from radar.context import build_qa_input, build_review_input
from radar.db import Database
from radar.events import EventType as ET
from radar.gitlab_client import FixtureSource
from radar.jira_client import JiraClient
from radar.web.app import create_app
from tests.conftest import ev, ny

PY = f'"{sys.executable}"'
ECHO_STDIN = f'{PY} -c "import sys; sys.stdout.write(sys.stdin.read())"'


# --- context formatting ----------------------------------------------------


def test_build_review_input_from_source():
    source = FixtureSource(
        mrs_by_project={},
        discussions_by_mr={},
        mr_context_by_mr={
            (1, 7): {"title": "Add cache", "description": "why", "diff": "@@ -1 +1 @@"}
        },
    )
    out = build_review_input(source, 1, 7)
    assert "Add cache" in out and "why" in out
    assert "```diff" in out and "@@ -1 +1 @@" in out


def _fake_jira_getter(path):
    if "/issue/PROJ-1" in path:
        return {"fields": {"summary": "The epic", "issuetype": {"name": "Epic"},
                           "status": {"name": "In Progress"}, "description": "epic body",
                           "labels": ["backend"]}}
    if "/search" in path:
        return {"issues": [{"key": "PROJ-2", "fields": {"summary": "A story",
                "issuetype": {"name": "Story"}, "status": {"name": "To Do"},
                "description": "story body"}}]}
    return {"fields": {}}


def test_build_qa_input_with_epic_children():
    client = JiraClient("https://x.atlassian.net", "e@x", "tok", getter=_fake_jira_getter)
    out = build_qa_input(client, ["PROJ-1"])
    assert "PROJ-1 — The epic" in out and "epic body" in out
    assert "Type: Epic" in out and "backend" in out
    assert "Child PROJ-2 — A story" in out and "story body" in out  # epic children pulled


def test_epic_children_swallows_errors():
    def boom(path):
        if "/search" in path:
            from radar.jira_client import JiraError
            raise JiraError("nope")
        return {"fields": {"issuetype": {"name": "Epic"}, "summary": "E"}}

    client = JiraClient("https://x", "e", "t", getter=boom)
    out = build_qa_input(client, ["PROJ-1"])  # must not raise
    assert "PROJ-1" in out


# --- stdin transport -------------------------------------------------------


def _await(runner, job, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = runner.get(job.id)
        if current.status != "running":
            return current
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_runner_pipes_stdin_provider_to_command():
    cfg = ReviewConfig(enabled=True, command=ECHO_STDIN, timeout_seconds=30)
    runner = CommandRunner(cfg, "review")
    job = runner.start(
        {"project_id": 1, "mr_iid": 2}, stdin_provider=lambda: "# Diff\nHELLO-FROM-STDIN"
    )
    done = _await(runner, job)
    assert done.status == "done"
    assert "HELLO-FROM-STDIN" in done.output


def test_runner_stdin_provider_failure_is_job_error():
    def boom():
        raise RuntimeError("fetch failed")

    cfg = ReviewConfig(enabled=True, command=ECHO_STDIN, timeout_seconds=30)
    runner = CommandRunner(cfg, "review")
    done = _await(runner, runner.start({"project_id": 1, "mr_iid": 2}, stdin_provider=boom))
    assert done.status == "error"
    assert "fetch failed" in done.error


# --- config ----------------------------------------------------------------


def test_include_context_parsed(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
gitlab: {projects: [g/p]}
calendar: {workdays: [mon], work_hours: {start: "09:00", end: "18:00"}, default_timezone: UTC}
slas: [{match: {}, first_response_business_hours: 16, approval_business_hours: 24}]
waive: {}
review: {enabled: true, command: "claude -p /x", include_context: true}
""",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.review.include_context is True
    assert cfg.qa.include_context is False


def test_jira_credentials(monkeypatch):
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)
    with pytest.raises(ConfigError, match="JIRA_BASE_URL"):
        jira_credentials()
    monkeypatch.setenv("JIRA_BASE_URL", "https://x.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "e@x")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    assert jira_credentials() == ("https://x.atlassian.net", "e@x", "tok")


# --- web wiring ------------------------------------------------------------


def test_review_include_context_feeds_command(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""
gitlab: {{projects: [g/p]}}
calendar:
  workdays: [mon, tue, wed, thu, fri]
  work_hours: {{start: "09:00", end: "18:00"}}
  default_timezone: America/New_York
slas: [{{match: {{}}, first_response_business_hours: 16, approval_business_hours: 24}}]
waive: {{draft: true}}
review:
  enabled: true
  command: '{ECHO_STDIN}'
  include_context: true
  timeout_seconds: 30
""",
        encoding="utf-8",
    )
    config = load_config(path)
    db_path = tmp_path / "r.db"
    db = Database(db_path)
    db.upsert_mr_snapshot(
        project_id=1, mr_iid=7, title="MR", author="aviva", web_url="x",
        source_branch="f", target_branch="main", description="", labels=[], draft=False,
        state="opened", reviewers=["dan"], created_at="2026-03-02T09:00:00Z",
        updated_at="2026-03-02T09:00:00Z",
    )
    db.insert_events([ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan", mr_iid=7)])
    db.close()

    # Inject a provider so no real GitLab is contacted.
    import radar.web.app as appmod

    def fake_provider(kind, cfg, pid, iid, keys):
        return (lambda: "BACKEND-DIFF-MARKER") if kind == "review" else None

    monkeypatch.setattr(appmod, "stdin_provider_for", fake_provider)

    client = TestClient(create_app(config, str(db_path)))
    start = client.post("/review/1/7")
    job_id = re.search(r'data-job-id="([0-9a-f]+)"', start.text).group(1)
    html = ""
    for _ in range(300):
        html = client.get(f"/review/status/{job_id}").text
        if "review-output" in html or "review-error" in html:
            break
        time.sleep(0.05)
    assert "review-output" in html
    assert "BACKEND-DIFF-MARKER" in html  # radar-fetched context reached the skill via stdin
