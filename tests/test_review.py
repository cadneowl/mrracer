"""AI-review launch: argv safety, job lifecycle, and the web endpoints."""

from __future__ import annotations

import json
import os
import re
import sys
import time

import pytest
from fastapi.testclient import TestClient

from radar.commands import (
    _DEFAULT_CHILD_ENV,
    CommandError,
    CommandJob,
    CommandRunner,
    build_argv,
)
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


def _stream_job(tmp_path, events: list[dict], name: str = "stream.py"):
    """Run a command that emits these stream-json events and return the job."""
    script = tmp_path / name
    script.write_text(
        "import json\n"
        + "".join(f"print(json.dumps({event!r}))\n" for event in events),
        encoding="utf-8",
    )
    cfg = ReviewConfig(enabled=True, command=f'{PY} "{script}"', timeout_seconds=30)
    runner = CommandRunner(cfg, "review")
    return _await(runner, runner.start({"project_id": 1, "mr_iid": 2}))


def _tool_use(name: str, tool_input: dict) -> dict:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": name, "input": tool_input},
    ]}}


@pytest.mark.parametrize("name,tool_input,expected", [
    ("Bash", {"command": "git diff --stat"}, "Bash: git diff --stat"),
    # A human-written description says more than the machinery it wraps.
    ("Bash", {"command": "rg -n foo", "description": "Find the callers"},
     "Bash: Find the callers"),
    ("Read", {"file_path": "/src/api/users.py"}, "Read: /src/api/users.py"),
    ("Grep", {"pattern": "TODO", "path": "radar/"}, "Grep: TODO in radar/"),
    ("WebFetch", {"url": "https://example.test/x"}, "WebFetch: https://example.test/x"),
    ("Task", {"subagent_type": "code-reviewer"}, "Task: code-reviewer"),
    ("TodoWrite", {"todos": [1, 2]}, "TodoWrite"),   # nothing worth naming
    ("Bash", "not-a-mapping", "Bash"),
])
def test_progress_names_what_a_tool_is_doing(tmp_path, name, tool_input, expected):
    """"using Bash" forty times tells an operator nothing about where a long
    review has got to; the command it ran does."""
    done = _stream_job(tmp_path, [_tool_use(name, tool_input)])
    assert any(p["text"] == expected for p in done.progress), done.progress


def test_a_repeated_line_is_collapsed_into_a_count(tmp_path):
    """A true repeat is one line worth seeing as a loop, not twenty identical
    ones scrolling the rest of the log out of view."""
    done = _stream_job(tmp_path, [_tool_use("Bash", {"command": "pytest -q"})] * 3)
    tools = [p for p in done.progress if p["kind"] == "tool"]
    assert len(tools) == 1
    assert tools[0]["text"] == "Bash: pytest -q (×3)"


def test_only_a_real_session_start_is_logged_as_one(tmp_path):
    """Every system event logged as "session started" reads like the run keeps
    restarting — which is what the panel was showing."""
    done = _stream_job(tmp_path, [
        {"type": "system", "subtype": "init"},
        {"type": "system", "subtype": "compact_boundary"},
        {"type": "result", "result": "done"},
    ])
    texts = [p["text"] for p in done.progress]
    assert texts.count("session started") == 1
    assert "compact boundary" in texts


def test_a_system_event_without_a_subtype_still_shows_a_session_line(tmp_path):
    """A CLI that stops labelling its init event must not make the session line
    disappear — silently showing less is the failure this whole area is about."""
    done = _stream_job(tmp_path, [
        {"type": "system"},
        {"type": "result", "result": "done"},
    ])
    assert "session started" in [p["text"] for p in done.progress]


def test_a_child_cannot_flood_the_log_with_one_giant_line(tmp_path):
    """Every other line here is bounded; a subtype comes from the child too."""
    done = _stream_job(tmp_path, [
        {"type": "system", "subtype": "x" * 5000},
        {"type": "result", "result": "done"},
    ])
    assert all(len(p["text"]) <= 100 for p in done.progress), done.progress


def test_progress_items_keep_stable_ids_for_live_updates(tmp_path):
    """The stream keys lines by id, not by position: the log is trimmed from the
    front, and a collapsed repeat has to update the line the browser drew."""
    done = _stream_job(tmp_path, [
        _tool_use("Read", {"file_path": "/a.py"}),
        _tool_use("Read", {"file_path": "/b.py"}),
    ])
    ids = [p["id"] for p in done.progress]
    assert ids == sorted(set(ids))  # unique and ascending


def test_a_collapsed_count_still_reaches_a_reader_that_is_behind():
    """The bug this revision counter exists for: a reader that has already seen
    a line must still be told its count went up, even when newer lines arrived
    in the same breath — otherwise the browser shows the stale line forever."""
    cfg = ReviewConfig(enabled=True, command="unused", timeout_seconds=30)
    runner = CommandRunner(cfg, "review")
    job = CommandJob(id="j1", kind="review", project_id=1, mr_iid=2)
    with runner._lock:
        runner._jobs[job.id] = job

    runner._add(job, "tool", "Bash: pytest")
    items, _ = runner.progress_since(job.id, 0)
    seen = max(item["rev"] for item in items)
    assert [item["text"] for item in items] == ["Bash: pytest"]

    # The repeat collapses onto the line already sent, and a different line is
    # appended after it — all before the reader comes back.
    runner._add(job, "tool", "Bash: pytest")
    runner._add(job, "tool", "Read: /a.py")

    items, _ = runner.progress_since(job.id, seen)
    texts = {item["text"] for item in items}
    assert "Bash: pytest (×2)" in texts, "the collapsed count never reached the reader"
    assert "Read: /a.py" in texts


def test_progress_items_carry_only_what_a_reader_draws():
    """The collapse bookkeeping is server-side; it should not ride along in
    every frame of a stream that re-sends on each change."""
    cfg = ReviewConfig(enabled=True, command="unused", timeout_seconds=30)
    runner = CommandRunner(cfg, "review")
    job = CommandJob(id="j2", kind="review", project_id=1, mr_iid=2)
    with runner._lock:
        runner._jobs[job.id] = job
    runner._add(job, "tool", "Bash: pytest")
    items, _ = runner.progress_since(job.id, 0)
    assert set(items[0]) == {"id", "rev", "kind", "text"}


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


@pytest.mark.parametrize("name,value", sorted(_DEFAULT_CHILD_ENV.items()))
def test_child_env_defaults_reach_every_skill(name, value, monkeypatch):
    """Every default radar promises a skill (inline subagents, unbounded
    background wait) must actually arrive in the child. Parametrized over the
    dict itself so a future default cannot ship untested; the ambient variable
    is scrubbed because setdefault deliberately yields to an operator export."""
    monkeypatch.delenv(name, raising=False)
    cfg = ReviewConfig(enabled=True, command=_echo_env(name), timeout_seconds=30)
    runner = CommandRunner(cfg, "review")
    done = _await(runner, runner.start({"project_id": 1, "mr_iid": 2}))
    assert done.status == "done"
    assert done.output.strip() == value


# --- background-agent runs must never masquerade as results -----------------


def _survivor_cmd(body: str, sleep: int = 30) -> str:
    """A child that leaves a process holding its inherited pipes, then exits."""
    script = (
        "import subprocess, sys; "
        f"subprocess.Popen([sys.executable, '-c', 'import time; time.sleep({sleep})']); "
        + body
    )
    return f'{PY} -c "{script}"'


def test_swept_background_agents_fail_the_job_and_are_not_persisted():
    """If the CLI reports on stderr that it stopped waiting for background
    tasks, the run's output is a deferral note, not the findings — keep it
    visible under an error, never store it as the review."""
    cmd = (
        f"{PY} -c \"import sys; print('The review is running; findings later.'); "
        "print('Background tasks still running after 600s; terminating.', file=sys.stderr)\""
    )
    cfg = ReviewConfig(enabled=True, command=cmd, timeout_seconds=30)
    runner = CommandRunner(cfg, "review")
    saved = []
    done = _await(runner, runner.start({"project_id": 1, "mr_iid": 2}, on_success=saved.append))
    assert done.status == "error"
    assert "stopped waiting" in done.error
    assert "findings later" in done.output  # the note stays visible under the error
    assert saved == []                      # but is never stored as the review


def test_sweep_is_detected_even_when_a_survivor_holds_stderr():
    """The case the sweep guard exists for: the agent that outlived the CLI is
    also holding the pipe the evidence arrives on. Reading stderr must not give
    up before that survivor is stopped, or the note is stored as the review."""
    cmd = _survivor_cmd(
        "import sys; print('The review is running; findings later.'); "
        "print('Background tasks still running after 600s; terminating.', file=sys.stderr)"
    )
    cfg = ReviewConfig(enabled=True, command=cmd, timeout_seconds=300)
    runner = CommandRunner(cfg, "review")
    saved = []
    done = _await(
        runner,
        runner.start({"project_id": 1, "mr_iid": 2}, on_success=saved.append),
        timeout=40.0,
    )
    assert done.status == "error"
    assert "stopped waiting" in done.error
    assert saved == []


def test_job_completes_even_if_a_survivor_holds_the_stdout_pipe():
    """EOF on the stdout pipe needs every write end closed; a survivor that
    inherited it must not keep the job 'running' after the child exited."""
    cmd = _survivor_cmd("print('parent done')", sleep=60)
    cfg = ReviewConfig(enabled=True, command=cmd, timeout_seconds=300)
    runner = CommandRunner(cfg, "review")
    start = time.time()
    done = _await(runner, runner.start({"project_id": 1, "mr_iid": 2}), timeout=40.0)
    # Finished on the child's exit and its own budget, not on the survivor's
    # 60s life or the 300s timeout: the grace windows bound the wait.
    assert done.status == "done"
    assert "parent done" in done.output
    assert time.time() - start < 30


@pytest.mark.parametrize("bad_event", [
    {"type": "assistant", "message": {"content": ["a bare string, not a block"]}},
    {"type": "assistant", "message": "not a mapping at all"},
    {"type": "assistant", "message": {"content": "not a list"}},
    {"type": "user", "message": {"content": [None]}},
])
def test_odd_stream_shapes_do_not_cost_the_run_its_result(bad_event, tmp_path):
    """The reader runs on its own thread: a shape radar didn't expect must not
    kill it mid-stream and leave the fragment read so far to pass for the whole
    run. The child's output is untrusted — surviving it is the reader's job."""
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        json.dumps(bad_event) + "\n"
        + json.dumps({"type": "result", "result": "REAL FINDINGS"}) + "\n",
        encoding="utf-8",
    )
    cmd = f'{PY} -c "import sys; sys.stdout.write(open(sys.argv[1]).read())" "{stream}"'
    cfg = ReviewConfig(enabled=True, command=cmd, timeout_seconds=30)
    runner = CommandRunner(cfg, "review")
    saved = []
    done = _await(runner, runner.start({"project_id": 1, "mr_iid": 2}, on_success=saved.append))
    assert done.status == "done"
    assert "REAL FINDINGS" in done.output  # the event after the odd one still lands
    assert len(saved) == 1


def test_a_reader_that_fails_is_reported_not_passed_off_as_the_result(monkeypatch):
    """If the reader does hit something it can't handle, the job must say so
    rather than publish the fragment it managed to collect."""
    import radar.commands as commands

    def boom(self, job, line, result_parts, raw_parts, stats):
        raise RuntimeError("reader exploded")

    monkeypatch.setattr(commands.CommandRunner, "_ingest", boom)
    cmd = f"{PY} -c \"print('anything')\""
    cfg = ReviewConfig(enabled=True, command=cmd, timeout_seconds=30)
    runner = CommandRunner(cfg, "review")
    saved = []
    done = _await(runner, runner.start({"project_id": 1, "mr_iid": 2}, on_success=saved.append))
    assert done.status == "error"
    assert "could not read" in done.error and "reader exploded" in done.error
    assert saved == []


def test_timeout_kills_the_whole_process_tree(tmp_path):
    """With the wait ceiling defaulted to 0 the CLI never reaps its own
    background agents, so radar's timeout must take down grandchildren too —
    otherwise they keep running (and spending) after the job is failed."""
    marker = tmp_path / "grandchild-was-alive.txt"
    grandchild = (
        "import time, os; time.sleep(3); "
        "open(os.environ['RADAR_TEST_MARKER'], 'w').write('x')"
    )
    script = (
        "import subprocess, sys, os, time; "
        "subprocess.Popen([sys.executable, '-c', os.environ['RADAR_GRANDCHILD']]); "
        "time.sleep(60)"
    )
    cmd = f'{PY} -c "{script}"'
    cfg = ReviewConfig(
        enabled=True, command=cmd, timeout_seconds=1,
        env=(("RADAR_GRANDCHILD", grandchild), ("RADAR_TEST_MARKER", str(marker))),
    )
    runner = CommandRunner(cfg, "review")
    done = _await(runner, runner.start({"project_id": 1, "mr_iid": 2}))
    assert done.status == "error" and "timed out" in done.error
    # Poll past the grandchild's write time instead of sleeping through it: a
    # live grandchild shows up as soon as it writes, and a killed one never does.
    deadline = time.time() + 6
    while time.time() < deadline:
        assert not marker.exists(), "grandchild survived the timeout kill"
        time.sleep(0.1)


def test_timeout_error_names_a_still_running_background_agent():
    """When the stream showed a subagent dispatched to the background, a
    timeout should say that's what ran out of clock, so the operator raises
    timeout_seconds instead of hunting for a slow review."""
    script = (
        "import json, sys, time; "
        "print(json.dumps({'type': 'user', 'status': 'async_launched'})); "
        "sys.stdout.flush(); time.sleep(60)"
    )
    cmd = f'{PY} -c "{script}"'
    cfg = ReviewConfig(enabled=True, command=cmd, timeout_seconds=2)
    runner = CommandRunner(cfg, "review")
    done = _await(runner, runner.start({"project_id": 1, "mr_iid": 2}))
    assert done.status == "error"
    assert "background agent" in done.error


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


def test_running_panel_shows_a_countdown_and_the_stream_re_anchors_it(tmp_path):
    """The operator watching a long review needs to know how much of the budget
    is left, and the browser's own tick must be corrected by the worker's clock
    rather than trusted on its own."""
    slow = f'{PY} -c "import time; time.sleep(2); print(chr(35),42)"'
    config = _review_config(tmp_path, slow)
    db_path = tmp_path / "r.db"
    db = Database(db_path)
    _seed(db)
    db.close()

    client = TestClient(create_app(config, str(db_path)))
    start = client.post("/review/1/7")
    job_id = re.search(r'data-job-id="([0-9a-f]+)"', start.text).group(1)

    # Rendered with seconds left, from the same budget the worker enforces —
    # and with its text already in place, so the pill is never a blank box.
    countdown = re.search(
        r'class="countdown[^"]*"[^>]*data-remaining="(\d+)"[^>]*>([^<]*)<', start.text
    )
    assert countdown, start.text
    assert 0 < int(countdown.group(1)) <= 30
    assert re.fullmatch(r"\d+:\d\d left", countdown.group(2).strip()), countdown.group(2)

    # The stream re-anchors that number as the run proceeds. A real number has
    # to arrive: an all-None stream would mean the countdown silently vanished.
    with client.stream("GET", f"/review/stream/{job_id}") as stream:
        clocks = []
        for line in stream.iter_lines():
            if line.startswith("data:") and "remaining_s" in line:
                clocks.append(json.loads(line[5:])["remaining_s"])
            if line == "event: end" or len(clocks) >= 2:
                break
    numbers = [c for c in clocks if isinstance(c, int)]
    assert numbers, f"no real countdown value on the stream: {clocks}"
    assert all(0 <= c <= 30 for c in numbers)
    assert numbers == sorted(numbers, reverse=True)  # counts down, never up


def test_review_disabled_hides_button_and_blocks_endpoint(config, tmp_path):
    # `config` fixture has no review section -> disabled.
    db_path = tmp_path / "r.db"
    db = Database(db_path)
    _seed(db)
    db.close()
    client = TestClient(create_app(config, str(db_path)))
    assert "🔍 review" not in client.get("/").text
    assert client.post("/review/1/7").status_code == 404
