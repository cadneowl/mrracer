"""Shared test fixtures and builders."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from radar.config import load_config
from radar.events import Event

NY = ZoneInfo("America/New_York")

_BASE_CONFIG = """
gitlab:
  projects: [group/hub-backend]
  poll_interval_minutes: 10
database:
  path: test.db
calendar:
  workdays: [mon, tue, wed, thu, fri]
  work_hours: {start: "09:00", end: "18:00"}
  default_timezone: America/New_York
  reviewer_timezones:
    ophira: Asia/Jerusalem
slas:
  - match: {target_branch: "release/*"}
    first_response_business_hours: 4
    approval_business_hours: 8
  - match: {labels: ["hotfix"]}
    first_response_business_hours: 4
    approval_business_hours: 8
  - match: {}
    first_response_business_hours: 16
    approval_business_hours: 24
waive:
  draft: true
  labels: ["blocked", "do-not-review"]
gamification:
  points: {review_within_sla: 10}
  streak_bonus_per_day: 1
"""


@pytest.fixture(autouse=True)
def _own_working_directory(tmp_path, monkeypatch):
    """Run every test from a directory of its own.

    radar reads a ``.env`` from beside the config it was given *and* from the
    process working directory, so without this a ``.env`` in whatever directory
    pytest was started from reaches tests that never mentioned one. The tests
    that assert nothing was found break, and so does the one that clears
    GITLAB_URL — ``run_checks`` re-reads the file and puts it back.

    A fresh checkout has no ``.env``, and a machine actually running radar
    does. So the suite passed for whoever wrote it and failed with five or six
    unrelated-looking errors for everyone running it where it matters. Tests
    that care about the working directory chdir again themselves.
    """
    home = tmp_path / "cwd"
    home.mkdir()
    monkeypatch.chdir(home)


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(_BASE_CONFIG, encoding="utf-8")
    return load_config(path)


@pytest.fixture
def assign_config(tmp_path):
    """The base config plus an assignment budget on every SLA rule, which is what
    switches on tracking of MRs that have no reviewers at all.

    A *different* budget per rule, so a test asserting the tighter one proves
    the rule was actually matched rather than passing on the default's value.
    """
    per_rule = iter(["assignment_business_hours: 1", "assignment_business_hours: 2",
                     "assignment_business_hours: 4"])
    text = "".join(
        f"{line}\n    {next(per_rule)}\n" if line.startswith("    approval_business_hours:")
        else f"{line}\n"
        for line in _BASE_CONFIG.splitlines()
    )
    path = tmp_path / "config-assign.yaml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


_JENKINS_JOBS = """
jenkins:
  poll_interval_seconds: 30
  jobs:
    - name: backend-ci
      url: https://jenkins.example.com/job/hub/job/backend/job/main
    - name: nightly-e2e
      url: https://jenkins.example.com/job/e2e
"""


@pytest.fixture
def jenkins_config(tmp_path):
    """The base config plus two watched Jenkins jobs — what turns the CI strip on."""
    path = tmp_path / "config-jenkins.yaml"
    path.write_text(_BASE_CONFIG + _JENKINS_JOBS, encoding="utf-8")
    return load_config(path)


def ny(y, m, d, hh, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=NY)


_counter = {"n": 0}


def ev(etype, when, *, reviewer=None, actor=None, project_id=1, mr_iid=1, **payload) -> Event:
    _counter["n"] += 1
    return Event(
        project_id=project_id,
        mr_iid=mr_iid,
        event_type=etype,
        occurred_at=when,
        dedup_key=f"k{_counter['n']}",
        actor=actor,
        reviewer=reviewer,
        payload=payload,
    )


def snapshot(**overrides) -> dict:
    base = {
        "project_id": 1,
        "mr_iid": 1,
        "title": "Add widget",
        "author": "aviva",
        "web_url": "https://gitlab.example.com/mr/1",
        "source_branch": "feature/widget",
        "target_branch": "main",
        "labels": [],
        "draft": False,
        "state": "opened",
        "reviewers": [],
        "created_at": "2026-03-02T09:00:00Z",
        "updated_at": "2026-03-02T09:00:00Z",
    }
    base.update(overrides)
    return base
