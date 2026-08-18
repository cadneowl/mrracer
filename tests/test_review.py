"""AI-review launch: argv safety, job lifecycle, and the web endpoints."""

from __future__ import annotations

import os
import re
import sys
import time

import pytest
from fastapi.testclient import TestClient

from radar.commands import CommandError, CommandRunner, build_argv
from radar.config import ReviewConfig, load_config
from radar.db import Database
from radar.events import EventType as ET
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
    with pytest.raises(CommandError):
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
    runner = CommandRunner(cfg, "review")
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
    runner = CommandRunner(cfg, "review")
    job = runner.start({"project_id": 1, "mr_iid": 2, "title": "T"})
    done = _await(runner, job)
    assert done.status == "done"
    assert "42" in done.output


def test_runner_captures_utf8_output():
    # Markdown plans contain arrows/em-dashes/emoji; radar must decode UTF-8
    # regardless of the OS locale (Windows defaults to cp1252).
    cmd = f"{PY} -c \"print('café → test')\""
    cfg = ReviewConfig(enabled=True, command=cmd, timeout_seconds=30)
    runner = CommandRunner(cfg, "review")
    job = runner.start({"project_id": 1, "mr_iid": 2})
    done = _await(runner, job)
    assert done.status == "done"
    assert "café → test" in done.output


_STREAM_JSON = (
    "import json\n"
    'print(json.dumps({"type": "system"}))\n'
    'print(json.dumps({"type": "assistant", "message": {"content":'
    ' [{"type": "tool_use", "name": "WebFetch"}]}}))\n'
    'print(json.dumps({"type": "assistant", "message": {"content":'
    ' [{"type": "text", "text": "Reading the diff"}]}}))\n'
    'print(json.dumps({"type": "result", "result": "# Review\\n\\nLGTM"}))\n'
)


def test_runner_parses_stream_json(tmp_path):
    # A command that speaks Claude's --output-format stream-json: tool_use ->
    # progress, and the final answer comes from the 'result' event.
    script = tmp_path / "emit.py"
    script.write_text(_STREAM_JSON, encoding="utf-8")
    cfg = ReviewConfig(enabled=True, command=f'{PY} "{script}"', timeout_seconds=30)
    runner = CommandRunner(cfg, "review")
    done = _await(runner, runner.start({"project_id": 1, "mr_iid": 2}))
    assert done.status == "done"
    assert done.output.startswith("# Review")  # from the result event, not raw JSON
    kinds = {p["kind"] for p in done.progress}
    assert "tool" in kinds and "text" in kinds
    assert any("WebFetch" in p["text"] for p in done.progress)


def test_two_results_are_separated(tmp_path):
    """A run can report more than one result; joined bare, the second one's
    heading would be swallowed into the first one's paragraph."""
    script = tmp_path / "two.py"
    script.write_text(
        "import json\n"
        'print(json.dumps({"type": "result", "result": "working on it"}))\n'
        'print(json.dumps({"type": "result", "result": "# Review\\n\\nLGTM"}))\n',
        encoding="utf-8",
    )
    cfg = ReviewConfig(enabled=True, command=f'{PY} "{script}"', timeout_seconds=30)
    runner = CommandRunner(cfg, "review")
    done = _await(runner, runner.start({"project_id": 1, "mr_iid": 2}))
    assert done.output == "working on it\n\n# Review\n\nLGTM"


def test_a_timed_out_run_keeps_what_it_wrote(tmp_path):
    """Half a review beats the word "timeout": the partial answer is kept on the
    job so the panel can still show it."""
    script = tmp_path / "slow.py"
    script.write_text(
        "import json, sys, time\n"
        'print(json.dumps({"type": "result", "result": "# Partial\\n\\nfound one bug"}))\n'
        "sys.stdout.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    cfg = ReviewConfig(enabled=True, command=f'{PY} "{script}"', timeout_seconds=1)
    runner = CommandRunner(cfg, "review")
    done = _await(runner, runner.start({"project_id": 1, "mr_iid": 2}))
    assert done.status == "error"
    assert "timed out" in done.error
    assert done.output.startswith("# Partial")


def test_runner_reports_error_on_nonzero():
    cmd = f'{PY} -c "import sys; sys.exit(3)"'
    cfg = ReviewConfig(enabled=True, command=cmd, timeout_seconds=30)
    runner = CommandRunner(cfg, "review")
    job = runner.start({"project_id": 1, "mr_iid": 2})
    done = _await(runner, job)
    assert done.status == "error"
    assert done.returncode == 3


def test_child_env_excludes_gitlab_token(monkeypatch):
    # The skill subprocess must NOT inherit the GitLab PAT.
    monkeypatch.setenv("GITLAB_TOKEN", "super-secret-pat")
    cmd = f"{PY} -c \"import os; print(os.environ.get('GITLAB_TOKEN', 'ABSENT'))\""
    cfg = ReviewConfig(enabled=True, command=cmd, timeout_seconds=30)
    runner = CommandRunner(cfg, "review")
    done = _await(runner, runner.start({"project_id": 1, "mr_iid": 2}))
    assert done.status == "done"
    assert "ABSENT" in done.output
    assert "super-secret-pat" not in done.output


def _echo_env(name: str) -> str:
    return f"{PY} -c \"import os; print(os.environ.get('{name}', 'ABSENT'))\""


def test_background_tasks_are_off_for_every_skill():
    """A headless agent that defers to a background subagent answers with a
    placeholder and its real findings only later, so radar turns that off."""
    cfg = ReviewConfig(
        enabled=True, command=_echo_env("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"),
        timeout_seconds=30,
    )
    runner = CommandRunner(cfg, "review")
    done = _await(runner, runner.start({"project_id": 1, "mr_iid": 2}))
    assert done.status == "done"
    assert "1" in done.output


def test_skill_env_overrides_and_unsets():
    over = ReviewConfig(
        enabled=True, command=_echo_env("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"),
        timeout_seconds=30, env=(("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "0"),),
    )
    runner = CommandRunner(over, "review")
    assert "0" in _await(runner, runner.start({"project_id": 1, "mr_iid": 2})).output

    off = ReviewConfig(
        enabled=True, command=_echo_env("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"),
        timeout_seconds=30, env_unset=("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS",),
    )
    runner = CommandRunner(off, "review")
    assert "ABSENT" in _await(runner, runner.start({"project_id": 1, "mr_iid": 2})).output


def test_an_operators_own_export_is_not_overruled(monkeypatch):
    """radar fills a gap in the child's environment; it does not overrule
    someone who set the variable themselves before starting radar."""
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "0")
    cfg = ReviewConfig(
        enabled=True, command=_echo_env("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"),
        timeout_seconds=30,
    )
    runner = CommandRunner(cfg, "review")
    assert "0" in _await(runner, runner.start({"project_id": 1, "mr_iid": 2})).output


def test_skill_env_cannot_resurrect_a_stripped_credential(monkeypatch):
    """The denylist is not a default a skill can talk its way past. Config
    refuses these names, but the promise belongs to _child_env, so it is tested
    against a SkillConfig built by hand — which is how one reaches the runner in
    every test and every future caller that isn't the YAML parser."""
    monkeypatch.setenv("GITLAB_TOKEN", "super-secret-pat")
    cfg = ReviewConfig(
        enabled=True, command=_echo_env("GITLAB_TOKEN"), timeout_seconds=30,
        env=(("GITLAB_TOKEN", "attacker-chosen"),),
    )
    runner = CommandRunner(cfg, "review")
    done = _await(runner, runner.start({"project_id": 1, "mr_iid": 2}))
    assert "ABSENT" in done.output
    assert "super-secret-pat" not in done.output and "attacker-chosen" not in done.output


@pytest.mark.skipif(os.name != "nt", reason="only Windows resolves env names case-insensitively")
def test_a_lowercase_credential_is_still_a_credential(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "super-secret-pat")
    cfg = ReviewConfig(
        enabled=True, command=_echo_env("GITLAB_TOKEN"), timeout_seconds=30,
        env=(("gitlab_token", "attacker-chosen"),),
    )
    runner = CommandRunner(cfg, "review")
    done = _await(runner, runner.start({"project_id": 1, "mr_iid": 2}))
    assert "ABSENT" in done.output and "attacker-chosen" not in done.output


def test_runner_catchall_sets_terminal_state(monkeypatch):
    # An unexpected error in the worker must not strand the job in "running".
    from radar import commands

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(commands.subprocess, "Popen", boom)
    cfg = ReviewConfig(enabled=True, command=f'{PY} -c "print(1)"', timeout_seconds=30)
    runner = CommandRunner(cfg, "review")
    done = _await(runner, runner.start({"project_id": 1, "mr_iid": 2}))
    assert done.status == "error"
    assert "unexpected error" in done.error


def test_runner_missing_command():
    cfg = ReviewConfig(enabled=True, command="definitely-not-a-real-binary-xyz", timeout_seconds=30)
    runner = CommandRunner(cfg, "review")
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
skills:
  - name: review
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
        source_branch="f", target_branch="main", description="", labels=[], draft=False,
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
    m = re.search(r'data-job-id="([0-9a-f]+)"', start.text)
    assert m, "expected a job id on the running panel"
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
