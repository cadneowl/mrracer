"""The gateway: one endpoint every claude skill is held to, whatever its checkout says.

A merge request that ships a `.claude/settings.json` naming another
ANTHROPIC_BASE_URL used to send the skill — and the token from its
CLAUDE_CONFIG_DIR — to that gateway instead: a 401 that read like a dead token.
"""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from radar.commands import CommandRunner, pin_gateway
from radar.config import ConfigError, Gateway, SkillConfig, load_config
from radar.diagnostics import _check_gateways
from tests.test_config import VALID, _write
from tests.test_review import _await

GW = Gateway(base_url="https://llm.example.com", config_dir="/opt/claude-cfg")


# --- the flags -------------------------------------------------------------


def test_a_claude_command_is_pinned_to_the_gateway():
    argv = pin_gateway(["claude", "-p", "/review"], GW)
    assert argv[:3] == ["claude", "--setting-sources", "user"]
    assert argv[3] == "--settings"
    assert json.loads(argv[4]) == {"env": {"ANTHROPIC_BASE_URL": "https://llm.example.com"}}
    assert argv[5:] == ["-p", "/review"]


def test_a_claude_named_by_path_is_still_claude():
    assert pin_gateway(["/usr/local/bin/claude", "-p"], GW)[1] == "--setting-sources"


@pytest.mark.parametrize("argv", [["python", "-c", "pass"], ["claude-wrapper", "-p"]])
def test_anything_else_is_left_as_written(argv):
    """The flags would mean nothing to another tool, or something else."""
    assert pin_gateway(argv, GW) == argv


def test_no_gateway_changes_nothing():
    assert pin_gateway(["claude", "-p"], None) == ["claude", "-p"]


# --- the environment -------------------------------------------------------


def test_the_gateway_reaches_the_childs_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://core.example.com")  # radar's own
    runner = CommandRunner(SkillConfig(enabled=True, command="claude -p", gateway=GW), "review")
    env = runner._child_env()
    assert env["ANTHROPIC_BASE_URL"] == "https://llm.example.com"
    assert env["CLAUDE_CONFIG_DIR"] == "/opt/claude-cfg"


@pytest.mark.skipif(os.name == "nt", reason="a shell-script stand-in for claude")
def test_a_run_reaches_claude_pinned(tmp_path, monkeypatch):
    """End to end through the runner: what the CLI is actually started with."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "seen = json.dumps({'argv': sys.argv[1:],"
        " 'base': os.environ.get('ANTHROPIC_BASE_URL'),"
        " 'dir': os.environ.get('CLAUDE_CONFIG_DIR')})\n"
        "print(json.dumps({'type': 'result', 'result': seen}))\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    runner = CommandRunner(
        SkillConfig(enabled=True, command="claude -p /review", timeout_seconds=30, gateway=GW),
        "review",
    )
    done = _await(runner, runner.start({"project_id": 1, "mr_iid": 2}))
    seen = json.loads(done.output.strip().splitlines()[-1])
    assert seen["argv"][:2] == ["--setting-sources", "user"]
    assert json.loads(seen["argv"][3])["env"]["ANTHROPIC_BASE_URL"] == GW.base_url
    assert seen["argv"][-2:] == ["-p", "/review"]
    assert (seen["base"], seen["dir"]) == (GW.base_url, GW.config_dir)


# --- the config ------------------------------------------------------------


def _cfg(tmp_path, skill_extra="", gateway=True, command="claude -p /x"):
    cfg_dir = tmp_path / "claude-cfg"
    cfg_dir.mkdir(exist_ok=True)
    top = (
        f"\ngateway:\n  base_url: https://llm.example.com/\n  config_dir: {cfg_dir}\n"
        if gateway else ""
    )
    text = VALID + top + (
        "\nskills:\n"
        "  - name: review\n"
        "    enabled: true\n"
        f"    command: '{command}'\n"
        + skill_extra
    )
    return load_config(_write(tmp_path, text)), cfg_dir


def test_every_skill_inherits_the_top_level_gateway(tmp_path):
    cfg, cfg_dir = _cfg(tmp_path)
    gw = cfg.skill_by_name("review").gateway
    assert gw == cfg.gateway
    assert gw.base_url == "https://llm.example.com"  # trailing slash trimmed
    assert gw.config_dir == str(cfg_dir)


def test_a_skill_can_name_its_own_gateway(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    cfg, _ = _cfg(
        tmp_path,
        f"    gateway:\n      base_url: https://other.example.com\n      config_dir: {other}\n",
    )
    assert cfg.skill_by_name("review").gateway.host == "other.example.com"


def test_no_gateway_means_none(tmp_path):
    cfg, _ = _cfg(tmp_path, gateway=False)
    assert cfg.gateway is None and cfg.skill_by_name("review").gateway is None


def test_a_missing_config_dir_is_refused(tmp_path):
    text = VALID + "\ngateway:\n  base_url: https://llm.example.com\n  config_dir: /nope/x\n"
    with pytest.raises(ConfigError, match="config_dir does not exist"):
        load_config(_write(tmp_path, text))


def test_an_unknown_gateway_key_is_refused(tmp_path):
    text = VALID + (
        f"\ngateway:\n  base_url: https://llm.example.com\n  config_dir: {tmp_path}\n"
        "  token: x\n"
    )
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(_write(tmp_path, text))


@pytest.mark.parametrize("name", ["ANTHROPIC_BASE_URL", "CLAUDE_CONFIG_DIR"])
def test_a_skill_env_that_argues_with_the_gateway_is_refused(tmp_path, name):
    with pytest.raises(ConfigError, match=f"{name} is set by the gateway"):
        _cfg(tmp_path, f"    env:\n      {name}: x\n")


def test_without_a_gateway_a_skill_may_still_set_its_own(tmp_path):
    cfg, _ = _cfg(tmp_path, "    env:\n      ANTHROPIC_BASE_URL: https://x\n", gateway=False)
    assert dict(cfg.skill_by_name("review").env)["ANTHROPIC_BASE_URL"] == "https://x"


@pytest.mark.parametrize(
    "flag", ["--settings {}", "--setting-sources project", "--setting-sources=user"]
)
def test_a_command_bringing_its_own_settings_flags_is_refused(tmp_path, flag):
    with pytest.raises(ConfigError, match="drop --setting"):
        _cfg(tmp_path, command=f"claude {flag} -p /x")


# --- radar check -----------------------------------------------------------


def test_check_reports_the_pinned_skills(tmp_path):
    cfg, cfg_dir = _cfg(tmp_path)
    (cfg_dir / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "sk-secret"}}), encoding="utf-8"
    )
    [check] = _check_gateways(cfg)
    assert check.status == "ok"
    assert "pinned for review" in check.detail
    assert "sk-secret" not in check.detail


def test_check_fails_a_config_dir_with_no_token(tmp_path):
    cfg, cfg_dir = _cfg(tmp_path)
    (cfg_dir / "settings.json").write_text("{}", encoding="utf-8")
    [check] = _check_gateways(cfg)
    assert check.status == "fail" and "ANTHROPIC_AUTH_TOKEN" in check.detail


def test_check_warns_when_nothing_holds_a_claude_skill(tmp_path):
    cfg, _ = _cfg(tmp_path, gateway=False)
    [check] = _check_gateways(cfg)
    assert check.name == "review.gateway"
    assert check.status == "warn" and ".claude/settings.json" in check.detail


def test_check_flags_claude_behind_a_wrapper(tmp_path):
    """Only the environment reaches it, and a checkout's settings outrank that."""
    cfg, cfg_dir = _cfg(tmp_path, command="bash -c \"claude -p /x\"")
    [check] = _check_gateways(cfg)
    assert check.name == "review.gateway" and check.status == "warn"
    assert "wrapper" in check.detail and "llm.example.com" in check.detail


def test_check_rows_are_named_apart(tmp_path):
    """A skill opting out beside a pinned one: two rows, two names."""
    cfg_dir = tmp_path / "claude-cfg"
    cfg_dir.mkdir()
    (cfg_dir / "settings.json").write_text('{"apiKeyHelper": "x"}', encoding="utf-8")
    text = VALID + (
        f"\ngateway:\n  base_url: https://llm.example.com\n  config_dir: {cfg_dir}\n"
        "\nskills:\n"
        "  - name: review\n    enabled: true\n    command: claude -p /x\n"
        "  - name: qa\n    enabled: true\n    command: claude -p /y\n    gateway:\n"
    )
    checks = _check_gateways(load_config(_write(tmp_path, text)))
    names = [c.name for c in checks]
    assert sorted(names) == ["gateway", "qa.gateway"] and len(set(names)) == len(names)


def _fail_with_401(tmp_path, monkeypatch, command):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json\n"
        "print(json.dumps({'type': 'system', 'subtype': 'api_retry', 'attempt': 1,"
        " 'error_status': 401, 'error': 'authentication_failed'}))\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    runner = CommandRunner(
        SkillConfig(enabled=True, command=command, timeout_seconds=30, gateway=GW), "review"
    )
    return _await(runner, runner.start({"project_id": 1, "mr_iid": 2}))


@pytest.mark.skipif(os.name == "nt", reason="a shell-script stand-in for claude")
def test_a_401_on_a_pinned_run_blames_the_token(tmp_path, monkeypatch):
    done = _fail_with_401(tmp_path, monkeypatch, "claude -p /x")
    assert "radar pins this skill" in done.error


@pytest.mark.skipif(os.name == "nt", reason="a shell-script stand-in for claude")
def test_a_401_through_a_wrapper_does_not_claim_it_was_pinned(tmp_path, monkeypatch):
    done = _fail_with_401(tmp_path, monkeypatch, "sh -c \"claude -p /x\"")
    assert "radar pins this skill" not in done.error
    assert ".claude/settings.json" in done.error and "wrapper" in done.error
