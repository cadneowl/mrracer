"""Config loading, validation, and credential handling."""

from __future__ import annotations

from pathlib import Path

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


def test_example_config_is_loadable_and_current():
    """The shipped example is the first thing an operator copies, so it is held
    to the same parser as their real file — a config.example.yaml that has
    drifted into a shape radar no longer accepts is worse than none."""
    cfg = load_config(Path(__file__).resolve().parent.parent / "config.example.yaml")
    # Every skill lives in `skills:`, including the two whose names carry
    # built-in capabilities. Nothing appears that the file did not declare.
    assert [s.name for s in cfg.skills] == ["review", "qa", "dba"]
    assert all(not s.enabled for s in cfg.skills)  # opt in deliberately
    assert cfg.skill_by_name("review").contexts == ("gitlab_diff",)
    qa = cfg.skill_by_name("qa")
    assert qa.contexts == ("jira",) and qa.stores_result is True
    assert cfg.skill_by_name("dba").contexts == ()  # a plain name inherits nothing


def test_working_dir_expands_tilde(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))       # posix expanduser
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # windows expanduser
    (tmp_path / "repo").mkdir()
    text = VALID + (
        "\nskills:\n"
        "  - {name: review, enabled: true, command: 'claude -p /x', working_dir: '~/repo'}\n"
    )
    cfg = load_config(_write(tmp_path, text))
    assert Path(cfg.skill_by_name("review").working_dir) == (tmp_path / "repo")  # ~ expanded


def test_working_dir_missing_reports_original(tmp_path):
    text = VALID + (
        "\nskills:\n  - {name: review, enabled: true, command: 'x', working_dir: '~/nope-xyz'}\n"
    )
    with pytest.raises(ConfigError, match="~/nope-xyz"):  # error shows what the user wrote
        load_config(_write(tmp_path, text))


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


def test_assignment_budget_is_optional(tmp_path):
    cfg = load_config(_write(tmp_path, VALID))
    assert cfg.slas[-1].assignment_business_hours is None  # unassigned check off

    on = VALID.replace(
        "approval_business_hours: 24",
        "approval_business_hours: 24\n    assignment_business_hours: 4",
    )
    assert load_config(_write(tmp_path, on)).slas[-1].assignment_business_hours == 4.0


def test_skill_env_parses(tmp_path):
    text = VALID + (
        "\nskills:\n"
        "  - name: review\n"
        "    enabled: true\n"
        "    command: 'claude -p /x'\n"
        "    env:\n"
        "      MY_FLAG: 1\n"
        "      MY_BOOL: true\n"
        "    env_unset: [CLAUDE_CODE_DISABLE_BACKGROUND_TASKS]\n"
    )
    skill = load_config(_write(tmp_path, text)).skill_by_name("review")
    # Values are written the way a shell reads them, not the way Python repr's them.
    assert dict(skill.env) == {"MY_FLAG": "1", "MY_BOOL": "true"}
    assert skill.env_unset == ("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS",)


def test_skill_env_refuses_a_valueless_key(tmp_path):
    """`FOO:` and `FOO: null` are one and the same to YAML, so neither can mean
    "remove this" without the half-finished edit quietly meaning it too."""
    text = VALID + (
        "\nskills:\n"
        "  - name: review\n"
        "    enabled: true\n"
        "    command: 'x'\n"
        "    env:\n"
        "      MY_TOKEN:\n"
    )
    with pytest.raises(ConfigError, match="env_unset"):
        load_config(_write(tmp_path, text))


def test_skill_env_refuses_a_non_scalar(tmp_path):
    text = VALID + (
        "\nskills:\n  - {name: review, enabled: true, command: 'x', "
        "env: {EXTRA_ARGS: ['--fast', '--quiet']}}\n"
    )
    with pytest.raises(ConfigError, match="expected text or a number"):
        load_config(_write(tmp_path, text))


def test_skill_env_refuses_a_credential_in_any_case(tmp_path):
    """Windows resolves variable names case-insensitively, so a lowercase
    spelling would reach the child as the real credential."""
    for spelling in ("GITLAB_TOKEN", "gitlab_token"):
        text = VALID + (
            f"\nskills:\n  - {{name: review, enabled: true, command: 'x', "
            f"env: {{{spelling}: abc}}}}\n"
        )
        with pytest.raises(ConfigError, match="refusing to name"):
            load_config(_write(tmp_path, text))

    unset = VALID + (
        "\nskills:\n  - {name: review, enabled: true, command: 'x', env_unset: [jira_email]}\n"
    )
    with pytest.raises(ConfigError, match="refusing to name"):
        load_config(_write(tmp_path, unset))

    bad = VALID + (
        "\nskills:\n  - {name: review, enabled: true, command: 'x', env: {'2BAD': v}}\n"
    )
    with pytest.raises(ConfigError, match="not a usable variable name"):
        load_config(_write(tmp_path, bad))


def test_business_hours_reject_a_boolean(tmp_path):
    """`false` is the obvious-looking way to switch a budget off, and float(False)
    would make it 0 — a zero-hour budget, i.e. the harshest setting there is."""
    bad = VALID.replace(
        "approval_business_hours: 24",
        "approval_business_hours: 24\n    assignment_business_hours: false",
    )
    with pytest.raises(ConfigError, match="leave its key out"):
        load_config(_write(tmp_path, bad))


def test_assignment_budget_must_be_on_every_rule_or_none(tmp_path):
    """Set on some rules only, an MR matching one of the others would silently
    go unchecked — so the parser names the rules that are missing it."""
    text = """
gitlab: {projects: [g/p]}
calendar:
  workdays: [mon]
  work_hours: {start: "09:00", end: "18:00"}
  default_timezone: UTC
slas:
  - match: {labels: ["hotfix"]}
    first_response_business_hours: 4
    approval_business_hours: 8
  - match: {}
    first_response_business_hours: 16
    approval_business_hours: 24
    assignment_business_hours: 4
waive: {}
"""
    with pytest.raises(ConfigError, match=r"slas\[0\]"):
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
