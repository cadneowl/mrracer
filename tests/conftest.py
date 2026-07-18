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


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(_BASE_CONFIG, encoding="utf-8")
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
