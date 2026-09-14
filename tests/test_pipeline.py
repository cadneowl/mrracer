"""Pipelines: skills that run other skills, in stages, handing results forward."""

from __future__ import annotations

import json
import re
import sys
import time

import pytest
from fastapi.testclient import TestClient

from radar.config import ConfigError, load_config
from radar.db import Database
from radar.diagnostics import _check_commands
from radar.events import EventType as ET
from radar.pipeline import PipelineRunner, build_runners
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

# A stand-in skill: says who it is, can be slow or fail, and echoes whatever it
# was handed on stdin — as one JSON line, because radar drops blank lines from
# plain output and the blank lines are part of what a test wants to see.
_STEP = """
import json, sys, time
who, delay, code = sys.argv[1], float(sys.argv[2]), int(sys.argv[3])
handed = sys.stdin.read()
time.sleep(delay)
print(f"{who} says hi")
if handed:
    print("STDIN" + json.dumps(handed))
if code:
    print(f"{who} broke", file=sys.stderr)
sys.exit(code)
"""


@pytest.fixture
def step_script(tmp_path):
    path = tmp_path / "step.py"
    path.write_text(_STEP, encoding="utf-8")
    return path


def _skill(script, name, delay=0, code=0, timeout=30, enabled=False):
    return (
        f"  - name: {name}\n"
        f"    label: {name.upper()} review\n"
        f"    enabled: {str(enabled).lower()}\n"
        f"    command: '{PY} \"{script}\" {name} {delay} {code}'\n"
        f"    timeout_seconds: {timeout}\n"
    )


def _config(tmp_path, skills, extra=""):
    path = tmp_path / "config.yaml"
    path.write_text(_BASE.format(extra="skills:\n" + skills + extra), encoding="utf-8")
    return load_config(path)


def _wait(job, limit=30.0):
    deadline = time.monotonic() + limit
    while job.status == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert job.status != "running", "pipeline did not finish"
    return job


def _run(cfg, name="full"):
    runners = build_runners(cfg.skills)
    runner = runners[name]
    assert isinstance(runner, PipelineRunner)
    job = runner.start({"title": "Add widget", "subject": "!7"})
    return runners, _wait(job)


def _handed(output):
    """What the step that wrote ``output`` was given on stdin."""
    return json.loads(output.split("STDIN", 1)[1].splitlines()[0])


# --- config ----------------------------------------------------------------


def test_stages_parse_in_every_form_and_the_budget_is_derived(tmp_path, step_script):
    cfg = _config(
        tmp_path,
        _skill(step_script, "arch", timeout=100)
        + _skill(step_script, "dba", timeout=300)
        + _skill(step_script, "qa", timeout=200)
        + _skill(step_script, "synth", timeout=50)
        + "  - name: full\n"
        "    enabled: true\n"
        "    pipeline:\n"
        "      - arch\n"
        "      - parallel: [dba, qa]\n"
        "      - skill: synth\n",
    )
    full = cfg.skill_by_name("full")
    assert full.pipeline == (("arch",), ("dba", "qa"), ("synth",))
    # The slowest step of each stage, summed: 100 + 300 + 50.
    assert full.timeout_seconds == 450
    assert full.command == ""


def test_a_budget_the_steps_could_outlast_is_refused(tmp_path, step_script):
    skills = (
        _skill(step_script, "arch", timeout=100)
        + _skill(step_script, "dba", timeout=300)
        + "  - name: full\n    pipeline: [{parallel: [arch, dba]}]\n"
    )
    with pytest.raises(ConfigError, match="up to 300s"):
        _config(tmp_path, skills + "    timeout_seconds: 120\n")
    assert _config(tmp_path, skills + "    timeout_seconds: 900\n").skill_by_name(
        "full"
    ).timeout_seconds == 900


@pytest.mark.parametrize(
    "entry, message",
    [
        ("pipeline: [nope]", "no skill named 'nope'"),
        ("pipeline: []", "non-empty list of stages"),
        ("pipeline: [{parallel: []}]", "non-empty list of skill names"),
        ("pipeline: [{serial: [arch]}]", "expected a skill name"),
        ("pipeline: [arch, arch]", "listed twice"),
        ("pipeline: [full]", "names itself"),
        ("pipeline: [inner]", "'inner' is itself a pipeline"),
        ("pipeline: [blank]", "'blank' has no command"),
        ("pipeline: [arch]\n    command: 'mytool'", "command would do nothing here"),
        ("pipeline: [arch]\n    context: gitlab_diff", "context would do nothing here"),
    ],
)
def test_a_pipeline_that_could_not_run_is_refused(tmp_path, step_script, entry, message):
    skills = (
        _skill(step_script, "arch")
        + "  - name: blank\n"
        + "  - name: inner\n    pipeline: [arch]\n"
        + f"  - name: full\n    {entry}\n"
    )
    with pytest.raises(ConfigError, match=message):
        _config(tmp_path, skills)


_JENKINS = """
jenkins:
  base_url: https://jenkins.example.com
  analysis: {{skill: {skill}}}
  jobs:
    - name: ci
      path: hub/ci
"""


def test_the_build_analyser_cannot_be_a_pipeline_or_a_step(tmp_path, step_script):
    with pytest.raises(ConfigError, match="is a pipeline"):
        _config(
            tmp_path,
            _skill(step_script, "arch")
            + "  - name: full\n    enabled: true\n    pipeline: [arch]\n",
            _JENKINS.format(skill="full"),
        )
    with pytest.raises(ConfigError, match="analyses a build"):
        _config(
            tmp_path,
            _skill(step_script, "doctor", enabled=True)
            + "  - name: full\n    enabled: true\n    pipeline: [doctor]\n",
            _JENKINS.format(skill="doctor"),
        )


# --- running ---------------------------------------------------------------


def test_a_later_step_is_handed_what_every_earlier_step_said(tmp_path, step_script):
    cfg = _config(
        tmp_path,
        _skill(step_script, "arch")
        + _skill(step_script, "dba")
        + _skill(step_script, "synth")
        + "  - name: full\n    pipeline: [{parallel: [arch, dba]}, synth]\n",
    )
    _, job = _run(cfg)
    assert job.status == "done", job.error
    # The last stage's answer is the pipeline's answer…
    assert job.output.startswith("synth says hi")
    # …and it was written having read both earlier reviews, under their labels.
    handed = _handed(job.output)
    assert handed.startswith("## Earlier steps\n\n")
    assert "### ARCH review\n\narch says hi" in handed
    assert "### DBA review\n\ndba says hi" in handed


def test_the_steps_of_one_stage_run_at_the_same_time(tmp_path, step_script):
    cfg = _config(
        tmp_path,
        _skill(step_script, "arch", delay=1.5)
        + _skill(step_script, "dba", delay=1.5)
        + "  - name: full\n    pipeline: [{parallel: [arch, dba]}]\n",
    )
    started = time.monotonic()
    _, job = _run(cfg)
    assert job.status == "done", job.error
    assert time.monotonic() - started < 2.8  # one after the other would be 3s+


def test_a_failed_step_is_reported_forward_and_the_run_goes_on(tmp_path, step_script):
    cfg = _config(
        tmp_path,
        _skill(step_script, "arch")
        + _skill(step_script, "dba", code=1)
        + _skill(step_script, "synth")
        + "  - name: full\n    pipeline: [{parallel: [arch, dba]}, synth]\n",
    )
    _, job = _run(cfg)
    assert job.status == "done", job.error
    handed = _handed(job.output)
    assert "### DBA review (failed)\n\nThis step failed:" in handed
    assert "dba broke" in handed
    assert "### ARCH review\n\narch says hi" in handed


def test_a_step_that_ran_out_of_time_hands_on_what_it_wrote(tmp_path, step_script):
    # Says something straight away, then sleeps past its one-second budget.
    slow = tmp_path / "slow.py"
    slow.write_text(
        "import sys, time\nprint('dba started', flush=True)\ntime.sleep(5)\n",
        encoding="utf-8",
    )
    cfg = _config(
        tmp_path,
        _skill(step_script, "arch")
        + f"  - name: dba\n    label: DBA review\n    command: '{PY} \"{slow}\"'\n"
        "    timeout_seconds: 1\n"
        + _skill(step_script, "synth")
        + "  - name: full\n    pipeline: [{parallel: [arch, dba]}, synth]\n",
    )
    _, job = _run(cfg)
    assert job.status == "done", job.error
    handed = _handed(job.output)
    assert "### DBA review (failed)" in handed and "timed out" in handed
    assert "What it wrote before failing:\n\ndba started" in handed


def test_a_stage_where_every_step_failed_ends_the_run(tmp_path, step_script):
    cfg = _config(
        tmp_path,
        _skill(step_script, "dba", code=1)
        + _skill(step_script, "synth")
        + "  - name: full\n    pipeline: [dba, synth]\n",
    )
    runners, job = _run(cfg)
    assert job.status == "error"
    assert "stage 1 of 2 failed" in job.error and "nothing ran after it" in job.error
    assert "dba broke" in job.output
    assert runners["synth"]._jobs == {}  # never started


def test_a_last_stage_of_several_steps_answers_with_all_of_them(tmp_path, step_script):
    cfg = _config(
        tmp_path,
        _skill(step_script, "arch")
        + _skill(step_script, "dba")
        + "  - name: full\n    pipeline: [{parallel: [arch, dba]}]\n",
    )
    _, job = _run(cfg)
    assert job.status == "done", job.error
    assert job.output.startswith("## ARCH review\n\narch says hi")
    assert "## DBA review\n\ndba says hi" in job.output


def test_progress_names_the_stage_and_the_step_it_came_from(tmp_path, step_script):
    cfg = _config(
        tmp_path,
        _skill(step_script, "arch")
        + _skill(step_script, "synth")
        + "  - name: full\n    pipeline: [arch, synth]\n",
    )
    runners, job = _run(cfg)
    lines = [item["text"] for item in runners["full"].progress_since(job.id, 0)[0]]
    assert lines[0] == "stage 1/2: arch"
    assert "[arch] arch says hi" in lines
    assert "[arch] finished" in lines
    assert "stage 2/2: synth" in lines
    assert lines.index("[arch] finished") < lines.index("stage 2/2: synth")


# --- the board -------------------------------------------------------------


def _seed(db):
    db.upsert_mr_snapshot(
        project_id=1, mr_iid=7, title="Add widget", author="aviva",
        web_url="https://gitlab.example.com/g/p/-/merge_requests/7",
        source_branch="f", target_branch="main", description="", labels=[], draft=False,
        state="opened", reviewers=["dan"], created_at="2026-03-02T09:00:00Z",
        updated_at="2026-03-02T09:00:00Z",
    )
    db.insert_events([ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan", mr_iid=7)])


def test_a_pipeline_is_a_board_button_whose_steps_need_none(tmp_path, step_script):
    cfg = _config(
        tmp_path,
        _skill(step_script, "arch")
        + _skill(step_script, "synth")
        + "  - name: full\n"
        "    label: Full review\n"
        "    icon: \"🧭\"\n"
        "    enabled: true\n"
        "    stores_result: true\n"
        "    pipeline: [arch, synth]\n",
    )
    db_path = tmp_path / "r.db"
    db = Database(db_path)
    _seed(db)
    db.close()
    client = TestClient(create_app(cfg, str(db_path)))

    board = client.get("/").text
    assert "🧭 Full review" in board
    assert "ARCH review" not in board  # a disabled step runs, but has no button
    assert client.post("/arch/1/7").status_code == 404

    start = client.post("/full/1/7")
    assert start.status_code == 200
    job_id = re.search(r'data-job-id="([0-9a-f]+)"', start.text).group(1)
    html = ""
    for _ in range(400):
        html = client.get(f"/full/status/{job_id}").text
        if "review-output" in html or "review-error" in html:
            break
        time.sleep(0.05)
    assert "review-output" in html
    assert "synth says hi" in html

    stored = client.get("/full/stored/1/7")
    assert stored.status_code == 200 and "synth says hi" in stored.text


def test_a_step_that_stores_its_result_still_does_inside_a_pipeline(tmp_path, step_script):
    cfg = _config(
        tmp_path,
        _skill(step_script, "qa")
        + "    stores_result: true\n"
        + "  - name: full\n    enabled: true\n    pipeline: [qa]\n",
    )
    db_path = tmp_path / "r.db"
    db = Database(db_path)
    _seed(db)
    db.close()
    client = TestClient(create_app(cfg, str(db_path)))
    start = client.post("/full/1/7")
    job_id = re.search(r'data-job-id="([0-9a-f]+)"', start.text).group(1)
    for _ in range(400):
        if "review-output" in client.get(f"/full/status/{job_id}").text:
            break
        time.sleep(0.05)
    with Database(db_path) as db:
        saved = db.get_test_plan(1, 7, "qa")
    assert saved is not None and "qa says hi" in saved["content"]


def test_radar_check_shows_the_flow_and_checks_disabled_steps(tmp_path, step_script):
    cfg = _config(
        tmp_path,
        _skill(step_script, "arch", timeout=40)
        + _skill(step_script, "dba", timeout=60)
        + _skill(step_script, "synth", timeout=20)
        + "  - name: full\n    enabled: true\n    pipeline: [{parallel: [arch, dba]}, synth]\n",
    )
    checks = {c.name: c for c in _check_commands(cfg)}
    assert checks["full.pipeline"].status == "ok"
    assert "arch + dba → synth" in checks["full.pipeline"].detail
    assert "up to 80s" in checks["full.pipeline"].detail
    # Disabled, but an enabled pipeline runs it — so its command is checked.
    assert checks["arch.command"].status == "ok"
