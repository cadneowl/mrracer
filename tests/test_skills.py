"""Config-driven skills list: custom skills beyond the built-in review + qa."""

from __future__ import annotations

import re
import sqlite3
import sys
import time

import pytest
from fastapi.testclient import TestClient

from radar.config import ConfigError, load_config
from radar.db import Database
from radar.diagnostics import _check_commands, _check_jira
from radar.events import EventType as ET
from radar.web.app import create_app
from tests.conftest import ev, ny

PY = f'"{sys.executable}"'

_BASE = """
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
{extra}
"""


def _config(tmp_path, extra):
    path = tmp_path / "config.yaml"
    path.write_text(_BASE.format(extra=extra), encoding="utf-8")
    return load_config(path)


def _seed(db):
    db.upsert_mr_snapshot(
        project_id=1, mr_iid=7, title="Add widget", author="aviva",
        web_url="https://gitlab.example.com/g/p/-/merge_requests/7",
        source_branch="f", target_branch="main", description="", labels=[], draft=False,
        state="opened", reviewers=["dan"], created_at="2026-03-02T09:00:00Z",
        updated_at="2026-03-02T09:00:00Z",
    )
    db.insert_events([ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan", mr_iid=7)])


def test_custom_skill_parsed_with_defaults(tmp_path):
    cfg = _config(
        tmp_path,
        "skills:\n"
        "  - name: dba\n"
        "    label: DBA review\n"
        "    icon: \"🗄\"\n"
        "    enabled: true\n"
        "    command: 'mytool {web_url}'\n",
    )
    # Built-in review + qa still present (disabled), custom skill appended.
    names = [s.name for s in cfg.skills]
    assert names == ["review", "qa", "dba"]
    dba = cfg.skill_by_name("dba")
    assert dba.enabled and dba.label == "DBA review" and dba.icon == "🗄"
    assert dba.button == "DBA review"  # button defaults to label
    assert dba.context is None and dba.stores_result is False  # generic: no capabilities


def test_custom_skill_button_and_flow(tmp_path):
    cfg = _config(
        tmp_path,
        "skills:\n"
        "  - name: dba\n"
        "    label: DBA review\n"
        "    icon: \"🗄\"\n"
        "    enabled: true\n"
        f"    command: '{PY} -c \"print(chr(35),42)\"'\n"
        "    timeout_seconds: 30\n",
    )
    db_path = tmp_path / "r.db"
    db = Database(db_path)
    _seed(db)
    db.close()
    client = TestClient(create_app(cfg, str(db_path)))

    assert "🗄 DBA review" in client.get("/").text  # board button rendered from config

    start = client.post("/dba/1/7")
    assert start.status_code == 200
    assert "DBA review" in start.text  # panel heading is the label
    job_id = re.search(r'data-job-id="([0-9a-f]+)"', start.text).group(1)

    html = ""
    for _ in range(300):
        html = client.get(f"/dba/status/{job_id}").text
        if "review-output" in html or "review-error" in html:
            break
        time.sleep(0.05)
    assert "review-output" in html

    # A generic skill never persists: no badge, and /stored is 404.
    assert "✓ plan" not in client.get("/").text
    assert client.get("/dba/stored/1/7").status_code == 404


def test_disabled_custom_skill_hides_button_and_blocks_endpoint(tmp_path):
    cfg = _config(
        tmp_path,
        "skills:\n  - name: dba\n    label: DBA review\n    command: 'mytool'\n",
    )
    db_path = tmp_path / "r.db"
    db = Database(db_path)
    _seed(db)
    db.close()
    client = TestClient(create_app(cfg, str(db_path)))
    assert "DBA review" not in client.get("/").text
    assert client.post("/dba/1/7").status_code == 404


def test_skills_list_entry_can_configure_builtin_review(tmp_path):
    # review expressed via the skills list (instead of the legacy top-level block)
    # keeps its built-in capabilities (gitlab_diff context, 🔍 icon, button text).
    cfg = _config(
        tmp_path,
        "skills:\n"
        "  - name: review\n"
        "    enabled: true\n"
        "    command: 'mytool {web_url}'\n"
        "    include_context: true\n",
    )
    review = cfg.skill_by_name("review")
    assert review.enabled and review.icon == "🔍" and review.button == "review"
    assert review.context == "gitlab_diff" and review.include_context is True


def test_include_context_without_source_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="context"):
        _config(
            tmp_path,
            "skills:\n  - name: dba\n    enabled: true\n"
            "    command: 'x'\n    include_context: true\n",
        )


def test_unknown_context_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown value"):
        _config(
            tmp_path,
            "skills:\n  - name: dba\n    command: 'x'\n    context: postgres\n",
        )


def test_duplicate_skill_name_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="duplicate skill name"):
        _config(
            tmp_path,
            "skills:\n  - name: dba\n    command: 'x'\n  - name: dba\n    command: 'y'\n",
        )


@pytest.mark.parametrize("bad", ["DBA", "d ba", "a/b", "-lead", "db!"])
def test_non_slug_skill_name_is_rejected(tmp_path, bad):
    with pytest.raises(ConfigError, match="slug"):
        _config(tmp_path, f"skills:\n  - name: {bad!r}\n    command: 'x'\n")


def test_reserved_skill_name_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="reserved"):
        _config(tmp_path, "skills:\n  - name: stored\n    command: 'x'\n")


def test_skills_list_entry_wins_over_legacy_block(tmp_path):
    # Both a legacy top-level qa: block and a skills-list entry named qa exist;
    # the skills-list entry wins, while keeping qa's built-in capabilities.
    cfg = _config(
        tmp_path,
        "qa:\n  enabled: true\n  command: 'legacy-command'\n"
        "skills:\n"
        "  - name: qa\n"
        "    enabled: true\n"
        "    command: 'list-command {jira_keys}'\n",
    )
    qa = cfg.skill_by_name("qa")
    assert qa.command == "list-command {jira_keys}"  # skills-list overrides legacy
    assert qa.context == "jira" and qa.stores_result is True  # built-in caps retained
    assert [s.name for s in cfg.skills] == ["review", "qa"]  # no duplicate entry


def test_custom_stores_result_is_isolated_from_qa(tmp_path):
    # A custom skill that persists output owns its OWN badge + stored route and
    # its OWN row (keyed by kind) — it is never misattributed to qa.
    cfg = _config(
        tmp_path,
        "skills:\n"
        "  - name: dba\n"
        "    label: DBA review\n"
        "    button: DBA\n"
        "    icon: \"🗄\"\n"
        "    enabled: true\n"
        "    stores_result: true\n"
        f"    command: '{PY} -c \"print(chr(35),42)\"'\n"
        "    timeout_seconds: 30\n",
    )
    db_path = tmp_path / "r.db"
    db = Database(db_path)
    _seed(db)
    db.close()
    client = TestClient(create_app(cfg, str(db_path)))

    start = client.post("/dba/1/7")
    job_id = re.search(r'data-job-id="([0-9a-f]+)"', start.text).group(1)
    for _ in range(300):
        if "review-output" in client.get(f"/dba/status/{job_id}").text:
            break
        time.sleep(0.05)

    board = client.get("/").text
    assert "✓ DBA" in board  # dba's own badge (its button text), not "✓ QA plan"
    assert "/dba/stored/1/7" in board

    stored = client.get("/dba/stored/1/7")
    assert stored.status_code == 200
    assert "🗄 DBA review" in stored.text  # rendered under dba's heading/icon
    assert client.get("/qa/stored/1/7").status_code == 404  # qa has no row for this MR

    db = Database(db_path)
    assert db.stored_kinds(1, 7) == ["dba"]  # persisted keyed by skill
    assert db.get_test_plan(1, 7, "qa") is None
    db.close()


def test_diagnostics_lists_custom_skill(tmp_path):
    cfg = _config(
        tmp_path,
        "skills:\n  - name: dba\n    enabled: true\n    command: 'no-such-binary-xyz'\n",
    )
    checks = {c.name: c for c in _check_commands(cfg)}
    assert checks["dba.command"].status == "warn"  # enabled but not on PATH
    assert checks["review.command"].status == "skip"  # built-in, disabled


def test_migration_preserves_existing_test_plans(tmp_path):
    # A DB created before the `kind` column existed keeps its rows, now as qa.
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE test_plans (
            project_id   INTEGER NOT NULL,
            mr_iid       INTEGER NOT NULL,
            jira_keys    TEXT NOT NULL DEFAULT '',
            content      TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            PRIMARY KEY (project_id, mr_iid)
        );
        INSERT INTO test_plans VALUES (1, 7, 'PROJ-42', '# old plan', '2026-03-02T09:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    db = Database(db_path)  # opening runs init_schema() -> _migrate()
    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(test_plans)")}
    assert "kind" in cols
    assert db.stored_kinds(1, 7) == ["qa"]
    plan = db.get_test_plan(1, 7, "qa")
    assert plan is not None and plan["content"] == "# old plan"
    assert plan["jira_keys"] == "PROJ-42"
    db.close()


def test_jira_check_detects_custom_jira_context_skill(tmp_path, monkeypatch):
    for var in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    cfg = _config(
        tmp_path,
        "skills:\n"
        "  - name: audit\n"
        "    enabled: true\n"
        "    command: 'x'\n"
        "    context: jira\n"
        "    include_context: true\n",
    )
    # A non-qa skill needing Jira context still makes the credential check fail.
    assert _check_jira(cfg).status == "fail"
