"""Config loading, validation, and credential handling."""

from __future__ import annotations

import pytest

from radar.config import ConfigError, gitlab_credentials, load_config

VALID = """
gitlab:
  projects: [group/hub-backend]
calendar:
  workdays: [mon, tue, wed, thu, fri]
  work_hours: {start: "09:00", end: "18:00"}
  default_timezone: America/New_York
slas:
  - match: {}
    first_response_business_hours: 16
    approval_business_hours: 24
waive: {draft: true}
"""


def _write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_valid_config(tmp_path):
    cfg = load_config(_write(tmp_path, VALID))
    assert cfg.gitlab.projects == ["group/hub-backend"]
    assert cfg.gitlab.poll_interval_minutes == 10  # default
    assert cfg.slas[-1].match.is_default


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_missing_projects(tmp_path):
    bad = VALID.replace("projects: [group/hub-backend]", "projects: []")
    with pytest.raises(ConfigError, match="projects"):
        load_config(_write(tmp_path, bad))


def test_requires_default_sla(tmp_path):
    bad = VALID.replace("match: {}", 'match: {target_branch: "main"}')
    with pytest.raises(ConfigError, match="default rule"):
        load_config(_write(tmp_path, bad))


def test_default_sla_must_be_last(tmp_path):
    text = """
gitlab: {projects: [g/p]}
calendar:
  workdays: [mon]
  work_hours: {start: "09:00", end: "18:00"}
  default_timezone: UTC
slas:
  - match: {}
    first_response_business_hours: 16
    approval_business_hours: 24
  - match: {labels: ["hotfix"]}
    first_response_business_hours: 4
    approval_business_hours: 8
waive: {}
"""
    with pytest.raises(ConfigError, match="must be last"):
        load_config(_write(tmp_path, text))


def test_bad_timezone(tmp_path):
    bad = VALID.replace("America/New_York", "Mars/Phobos")
    with pytest.raises(ConfigError, match="timezone"):
        load_config(_write(tmp_path, bad))


def test_bad_workday(tmp_path):
    bad = VALID.replace("[mon, tue, wed, thu, fri]", "[funday]")
    with pytest.raises(ConfigError, match="weekday"):
        load_config(_write(tmp_path, bad))


def test_reviewer_timezone_lookup(tmp_path):
    cfg = load_config(_write(tmp_path, VALID))
    assert str(cfg.calendar.tz_for("anyone")) == "America/New_York"


def test_credentials_missing(monkeypatch):
    monkeypatch.delenv("GITLAB_URL", raising=False)
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="GITLAB_URL"):
        gitlab_credentials()


def test_credentials_present(monkeypatch):
    monkeypatch.setenv("GITLAB_URL", "https://gitlab.example.com")
    monkeypatch.setenv("GITLAB_TOKEN", "secret-token")
    url, token = gitlab_credentials()
    assert url == "https://gitlab.example.com"
    assert token == "secret-token"
