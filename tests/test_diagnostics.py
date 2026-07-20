"""radar check diagnostics."""

from __future__ import annotations

import sys

from radar.config import load_config
from radar.db import Database
from radar.diagnostics import (
    _check_commands,
    _check_database,
    _check_note_parsing,
    _first_token,
    run_checks,
)
from radar.events import EventType as ET
from tests.conftest import ev, ny

PY = f'"{sys.executable}"'


def _config(tmp_path, extra=""):
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""
gitlab: {{projects: [g/p]}}
database: {{path: {str(tmp_path / "diag.db").replace(chr(92), "/")}}}
calendar:
  workdays: [mon, tue, wed, thu, fri]
  work_hours: {{start: "09:00", end: "18:00"}}
  default_timezone: America/New_York
slas: [{{match: {{}}, first_response_business_hours: 16, approval_business_hours: 24}}]
waive: {{draft: true}}
{extra}
""",
        encoding="utf-8",
    )
    return load_config(path)


def test_first_token():
    assert _first_token(f'{PY} -c "print(1)"') == sys.executable
    assert _first_token('claude -p "/x"') == "claude"
    assert _first_token("") is None


def test_database_check_reports_counts(tmp_path):
    config = _config(tmp_path)
    db = Database(str(config.database_path))
    db.insert_events([ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan", mr_iid=1)])
    db.close()
    c = _check_database(config)
    assert c.status == "ok"
    assert "1 events" in c.detail


def test_commands_check(tmp_path):
    ok_cfg = _config(tmp_path, extra=f"review: {{enabled: true, command: '{PY} -c \"print(1)\"'}}")
    checks = {c.name: c for c in _check_commands(ok_cfg)}
    assert checks["review.command"].status == "ok"      # sys.executable is on PATH
    assert checks["qa.command"].status == "skip"        # disabled

    bad = _config(tmp_path, extra="qa: {enabled: true, command: 'no-such-binary-xyz {jira_keys}'}")
    assert {c.name: c for c in _check_commands(bad)}["qa.command"].status == "warn"


def test_note_parsing_warns_on_backfill(tmp_path):
    config = _config(tmp_path)
    db = Database(str(config.database_path))
    backfill = [
        ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan", mr_iid=i,
           source="reviewer_snapshot")
        for i in range(3)
    ]
    from_note = [ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="maya", mr_iid=99)]
    db.insert_events(backfill + from_note)
    db.close()
    c = _check_note_parsing(config)
    assert c.status == "warn"
    assert "backfill" in c.detail


def test_run_checks_flags_missing_gitlab(tmp_path, monkeypatch):
    for var in ("GITLAB_URL", "GITLAB_TOKEN", "JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    checks = {c.name: c for c in run_checks(_config(tmp_path))}
    assert checks["config"].status == "ok"
    assert checks["database"].status == "ok"
    assert checks["gitlab.env"].status == "fail"  # no creds -> clear failure (no network)
    assert checks["jira.env"].status == "skip"    # qa.include_context off
