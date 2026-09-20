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


def test_a_failed_last_stage_keeps_what_the_earlier_ones_produced(tmp_path, step_script):
    """A synthesis that cannot even start is no reason to lose the reviews it
    was going to merge — half an hour of work, and a reader can merge them by
    eye. The pipeline's answer is everything that ran."""
    cfg = _config(tmp_path, (
        _skill(step_script, "arch")
        + _skill(step_script, "dba")
        + _skill(step_script, "synth", code=3)      # this one fails
        + "  - name: full\n"
        "    label: Full review\n"
        "    enabled: true\n"
        "    pipeline:\n"
        "      - parallel: [arch, dba]\n"
        "      - skill: synth\n"
    ))

    _, job = _run(cfg)

    assert job.status == "error"
    assert "arch says hi" in job.output and "dba says hi" in job.output
    assert "What did finish is below: arch, dba." in job.error


def _flaky_step(path, name, marker):
    """A step that fails the first time it runs and succeeds the second.

    The marker file is what remembers, because the step is a fresh process
    each time — exactly as a real skill is.
    """
    script = path / f"{name}.py"
    script.write_text(
        "import os, sys\n"
        f"flag = {str(marker)!r}\n"
        "handed = sys.stdin.read()\n"
        "if not os.path.exists(flag):\n"
        "    open(flag, 'w').close()\n"
        "    print('connection reset', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        f"print('{name} says hi on the second try')\n"
        "print('STDIN-LEN', len(handed))\n",
        encoding="utf-8",
    )
    return (f"  - name: {name}\n"
            f"    label: {name.upper()}\n"
            f"    enabled: false\n"
            f"    command: '{PY} \"{script}\"'\n"
            f"    timeout_seconds: 30\n")


def _pipeline_with_a_flaky_last_step(tmp_path, step_script):
    return _config(tmp_path, (
        _skill(step_script, "arch")
        + _skill(step_script, "dba")
        + _flaky_step(tmp_path, "synth", tmp_path / "synth.ran")
        + "  - name: full\n"
        "    label: Full review\n"
        "    enabled: true\n"
        "    pipeline:\n"
        "      - parallel: [arch, dba]\n"
        "      - skill: synth\n"
    ))


def test_a_failed_step_can_be_retried_without_paying_for_the_others_again(
    tmp_path, step_script
):
    """The case this exists for: two reviews took half an hour and the step
    that merges them died on a connection reset."""
    cfg = _pipeline_with_a_flaky_last_step(tmp_path, step_script)
    runners = build_runners(cfg.skills)
    runner = runners["full"]
    job = _wait(runner.start({"title": "Add widget", "subject": "!7"}))
    assert job.status == "error" and "arch says hi" in job.output

    # The two that worked are remembered by their job ids; the retry must not
    # start them again.
    before = {name: child.id for name, child in job.steps.items()}

    assert runner.retry_step(job.id, "synth") is True
    _wait(job)

    assert job.status == "done" and not job.error
    assert "synth says hi on the second try" in job.output
    assert job.steps["arch"].id == before["arch"]      # not re-run
    assert job.steps["dba"].id == before["dba"]
    assert job.steps["synth"].id != before["synth"]    # this one was


def test_the_retried_step_is_handed_what_the_earlier_ones_said(tmp_path, step_script):
    """A resumed step gets the same `## Earlier steps` section the first run
    would have given it — otherwise the synthesis has nothing to synthesise."""
    cfg = _pipeline_with_a_flaky_last_step(tmp_path, step_script)
    runner = build_runners(cfg.skills)["full"]
    job = _wait(runner.start({"title": "Add widget", "subject": "!7"}))
    runner.retry_step(job.id, "synth")
    _wait(job)

    handed = int(re.search(r"STDIN-LEN (\d+)", job.output).group(1))
    assert handed > 0, "the retried step was handed nothing"
    assert "arch says hi" in job.steps["synth"].output or handed > 40


def test_a_retry_is_refused_when_there_is_nothing_to_resume(tmp_path, step_script):
    """Not "nothing went wrong" — a step that worked can be run again on
    purpose. This is the list of things that genuinely cannot be resumed."""
    cfg = _config(tmp_path, (
        _skill(step_script, "arch") + _skill(step_script, "dba", code=1)
        + _skill(step_script, "synth")
        + "  - name: full\n    label: Full\n    enabled: true\n"
        "    pipeline:\n      - parallel: [arch, dba]\n      - skill: synth\n"
    ))
    runners = build_runners(cfg.skills)
    runner = runners["full"]
    job = _wait(runner.start({"title": "Add widget", "subject": "!7"}))

    assert job.status == "done"
    assert runner.retry_step(job.id, "nope") is False        # no such step
    assert runner.retry_step("no-such-job", "arch") is False  # no such job
    job.status = "running"                                   # a run still going
    assert runner.retry_step(job.id, "arch") is False
    job.status = "done"
    # A step this run never reached. Nothing to resume from: the answer it would
    # be handed does not exist, and the button that starts one is the board's.
    del job.steps["synth"]
    assert runner.retry_step(job.id, "synth") is False
    # A job from before radar kept what a retry needs.
    job.retry_with = {}
    assert runner.retry_step(job.id, "arch") is False


def test_a_step_that_succeeded_can_be_run_again_over_what_is_there_now(
    tmp_path, step_script
):
    """The case the failed-step-only rule got wrong.

    One review of three fails, the synthesis merges the two that worked and
    answers badly — a clean exit and a non-empty answer, which is a success by
    every measure radar has. Re-running the review is half the remedy; the other
    half is re-running the synthesis over what is there now, and *that* step did
    not fail either. Both are the same operation, and neither was allowed.
    """
    cfg = _config(tmp_path, (
        _skill(step_script, "arch")
        + _skill(step_script, "dba", code=1)
        + _skill(step_script, "qa")
        + _skill(step_script, "synth")
        + "  - name: full\n    label: Full\n    enabled: true\n"
        "    pipeline:\n      - parallel: [arch, dba, qa]\n      - skill: synth\n"
    ))
    runner = build_runners(cfg.skills)["full"]
    job = _wait(runner.start({"title": "Add widget", "subject": "!7"}))
    assert job.status == "done", job.error

    # The synthesis alone, over the outputs that are already there.
    assert runner.retry_step(job.id, "synth") is True
    _wait(job)
    assert job.status == "done", job.error
    handed = _handed(job.output)
    assert "### ARCH review\n\narch says hi" in handed
    assert "### QA review\n\nqa says hi" in handed
    # Including the one that failed: a synthesis told only about the reviews
    # that worked cannot say what went unreviewed.
    assert "### DBA review (failed)" in handed and "dba broke" in handed

    # And a review that worked, which also re-runs the synthesis after it,
    # because its answer is the synthesis's input.
    ran = [item["text"] for item in job.progress]
    assert runner.retry_step(job.id, "arch") is True
    _wait(job)
    assert job.status == "done", job.error
    after = [item["text"] for item in job.progress[len(ran):]]
    assert any("[arch]" in line for line in after)
    assert any("[synth]" in line for line in after)
    assert not any("[qa]" in line for line in after), "qa is kept, not paid for twice"


def test_the_panel_offers_a_retry_on_a_failed_step_and_it_works(tmp_path, step_script):
    """End to end from the browser's side: the failed row carries a retry, the
    post resumes the pipeline, and the panel comes back running."""
    cfg = _config(
        tmp_path,
        _skill(step_script, "arch")
        + _flaky_step(tmp_path, "synth", tmp_path / "web-synth.ran")
        + "  - name: full\n    label: Full review\n    enabled: true\n"
        "    stores_result: true\n    pipeline: [arch, synth]\n",
    )
    db_path = tmp_path / "retry.db"
    db = Database(db_path)
    _seed(db)
    db.close()
    client = TestClient(create_app(cfg, str(db_path)))

    job_id = re.search(r'data-job-id="([0-9a-f]+)"', client.post("/full/1/7").text).group(1)
    html = ""
    for _ in range(400):
        html = client.get(f"/full/status/{job_id}").text
        if "review-error" in html:
            break
        time.sleep(0.05)
    assert "review-error" in html, "the pipeline should have failed"
    # Both rows offer to run again, in the words their state calls for: the step
    # that failed is a retry, the one that worked is a deliberate re-run.
    assert f'hx-post="/full/retry/{job_id}?step=synth"' in html
    assert f'hx-post="/full/retry/{job_id}?step=arch"' in html
    assert "↻ retry" in html and "↻ run again" in html
    assert "Its answer is replaced by the new one." in html, "said before it is"
    # And what the first stage produced is still on the panel.
    assert "arch says hi" in html

    again = client.post(f"/full/retry/{job_id}?step=synth")
    assert again.status_code == 200
    assert "review-loading" in again.text        # the panel is running again

    for _ in range(400):
        html = client.get(f"/full/status/{job_id}").text
        if "review-output" in html and "review-loading" not in html:
            break
        time.sleep(0.05)
    assert "synth says hi on the second try" in html
    assert "review-error" not in html            # and no longer carrying the old fault

    # A step that is not a step of this pipeline is refused.
    assert client.post(f"/full/retry/{job_id}?step=nope").status_code == 404


def _priced_flaky_step(path, name, marker, first_cost, second_cost):
    """A step that reports what it cost in stream-json, fails, then succeeds.

    Both attempts are paid for: the first one spent its money and *then* the
    connection died, which is exactly the case the roll-up has to get right.
    """
    script = path / f"{name}-priced.py"
    script.write_text(
        "import json, os, sys\n"
        f"flag = {str(marker)!r}\n"
        "sys.stdin.read()\n"
        "first = not os.path.exists(flag)\n"
        "if first:\n"
        "    open(flag, 'w').close()\n"
        f"cost = {first_cost!r} if first else {second_cost!r}\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success',\n"
        f"    'result': '{name} says hi' if not first else 'half an answer',\n"
        "    'num_turns': 1, 'total_cost_usd': cost,\n"
        "    'usage': {'input_tokens': 10, 'output_tokens': 5}}))\n"
        "sys.stdout.flush()\n"
        "if first:\n"
        "    print('connection reset', file=sys.stderr)\n"
        "    sys.exit(1)\n",
        encoding="utf-8",
    )
    return (f"  - name: {name}\n"
            f"    label: {name.upper()}\n"
            f"    enabled: false\n"
            f"    command: '{PY} \"{script}\"'\n"
            f"    timeout_seconds: 30\n")


def test_a_retry_does_not_make_the_bill_smaller(tmp_path, step_script):
    """The first attempt's money was spent whether or not it produced an answer.

    The roll-up reads the step jobs that are still there, and a retry replaces
    one of them — so without keeping what the attempt it replaced had spent,
    running a step again would make the pipeline's total go *down*, which is
    the one number here nobody could then trust.
    """
    cfg = _config(tmp_path, (
        _skill(step_script, "arch")
        + _priced_flaky_step(tmp_path, "synth", tmp_path / "priced.ran", 1.0, 2.0)
        + "  - name: full\n    label: Full\n    enabled: true\n"
        "    pipeline: [arch, synth]\n"
    ))
    runner = build_runners(cfg.skills)["full"]
    job = _wait(runner.start({"title": "t", "subject": "!7"}))

    assert job.status == "error"
    assert job.stats.cost_usd == pytest.approx(1.0)

    assert runner.retry_step(job.id, "synth") is True
    _wait(job)

    assert job.status == "done"
    assert job.stats.cost_usd == pytest.approx(3.0), (
        "the failed attempt's dollar is still on the bill"
    )
    # The step's own row shows the attempt that is there, not the sum.
    assert job.steps["synth"].stats.cost_usd == pytest.approx(2.0)


def test_the_attempt_a_retry_replaced_keeps_a_row_of_its_own(tmp_path, step_script):
    """The total counts it, so the breakdown has to as well.

    Keeping the failed attempt's money on the bill is right — it was spent. But
    the rows under that total are what explain it, and a row missing from them
    is money with nowhere to be accounted for: two steps showing $2 under a
    total of $3, and nothing on the panel that says where the dollar went.
    """
    cfg = _config(tmp_path, (
        _skill(step_script, "arch")
        + _priced_flaky_step(tmp_path, "synth", tmp_path / "priced.ran", 1.0, 2.0)
        + "  - name: full\n    label: Full\n    enabled: true\n"
        "    pipeline: [arch, synth]\n"
    ))
    runner = build_runners(cfg.skills)["full"]
    job = _wait(runner.start({"title": "t", "subject": "!7"}))
    assert runner.retry_step(job.id, "synth") is True
    _wait(job)

    rows = [(step["label"], step["stats"].get("cost_usd", 0.0)) for step in job.stats.steps]
    assert rows == [
        ("ARCH review", 0.0),
        ("SYNTH (earlier attempt)", 1.0),
        ("SYNTH", 2.0),
    ], "the attempt that was replaced sits beside the one that replaced it"
    assert sum(cost for _, cost in rows) == pytest.approx(job.stats.cost_usd)
    # And it is a failed row, with how long it ran before it failed.
    earlier = job.stats.steps[1]
    assert earlier["status"] == "error" and "elapsed_s" in earlier


def test_the_panel_shows_the_replaced_attempt_and_prints_the_bill_once(tmp_path, step_script):
    """What the reader sees, on the panel and on the stored result.

    Two things in one render because they are one row's worth of layout: the
    superseded attempt has a line, and the pipeline's total appears exactly
    once. The panel used to print the same figures twice — unlabelled above the
    steps and again as the total below them — which reads as a mistake rather
    than as a summary.
    """
    cfg = _config(tmp_path, (
        _skill(step_script, "arch")
        + _priced_flaky_step(tmp_path, "synth", tmp_path / "priced.ran", 1.0, 2.0)
        + "  - name: full\n    label: Full\n    enabled: true\n"
        "    stores_result: true\n    pipeline: [arch, synth]\n"
    ))
    db_path = tmp_path / "retry-panel.db"
    db = Database(db_path)
    _seed(db)
    db.close()
    client = TestClient(create_app(cfg, str(db_path)))

    job_id = re.search(r'data-job-id="([0-9a-f]+)"', client.post("/full/1/7").text).group(1)
    for _ in range(400):
        html = client.get(f"/full/status/{job_id}").text
        if "review-loading" not in html and "↻ retry" in html:
            break
        time.sleep(0.05)
    assert client.post(f"/full/retry/{job_id}?step=synth").status_code == 200
    for _ in range(400):
        html = client.get(f"/full/status/{job_id}").text
        if "review-loading" not in html and "SYNTH (earlier attempt)" in html:
            break
        time.sleep(0.05)

    stored = client.get("/full/stored/1/7").text
    for name, page in (("the finished panel", html), ("the stored result", stored)):
        assert "SYNTH (earlier attempt)" in page, f"{name} should show what was replaced"
        # $3 is the total; $1 and $2 are the two attempts. The total is printed
        # under the label that says what it is, and nowhere else.
        assert page.count(">cost</span>$3") == 1, f"{name} prints the bill once"
        assert page.count('class="run-total"') == 1
        assert ">cost</span>$1<" in page and ">cost</span>$2<" in page


def test_the_retry_button_says_what_else_it_will_run(tmp_path, step_script):
    """A retry re-runs every stage after the step, because their input is about
    to change — so a step further down that already succeeded is paid for
    twice. One click, real money: the confirmation names it."""
    cfg = _config(tmp_path, (
        _flaky_step(tmp_path, "arch", tmp_path / "confirm-arch.ran")
        + _skill(step_script, "dba")
        + _skill(step_script, "synth")
        + "  - name: full\n    label: Full review\n    enabled: true\n"
        "    pipeline:\n      - parallel: [arch, dba]\n      - skill: synth\n"
    ))
    db_path = tmp_path / "confirm.db"
    db = Database(db_path)
    _seed(db)
    db.close()
    client = TestClient(create_app(cfg, str(db_path)))

    job_id = re.search(r'data-job-id="([0-9a-f]+)"', client.post("/full/1/7").text).group(1)
    html = ""
    for _ in range(400):
        html = client.get(f"/full/status/{job_id}").text
        if "review-loading" not in html and "↻ retry" in html:
            break
        time.sleep(0.05)

    # arch failed in the first stage; dba and the synthesis after it succeeded.

    assert 'hx-post="/full/retry/' in html
    confirm = re.search(r'hx-confirm="([^"]*)"', html).group(1)
    assert "SYNTH" in confirm and "paid for twice" in confirm
    assert "DBA" not in confirm, "a step beside it is not re-run and is not named"


def _priced_step(path, name, cost, seconds=0.0):
    """A step that reports a model, a bill and a turn count in stream-json."""
    script = path / f"{name}-priced.py"
    script.write_text(
        "import json, sys, time\n"
        "sys.stdin.read()\n"
        f"time.sleep({seconds!r})\n"
        "print(json.dumps({'type': 'system', 'subtype': 'init',\n"
        "    'model': 'deepseek-v4p1-flash', 'session_id': "
        f"'sess-{name}'" ", 'tools': []}))\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success',\n"
        f"    'result': '{name} says hi', 'num_turns': 3, 'duration_api_ms': 4000,\n"
        f"    'total_cost_usd': {cost!r},\n"
        "    'usage': {'input_tokens': 10, 'cache_read_input_tokens': 900,\n"
        "              'output_tokens': 50}}))\n",
        encoding="utf-8",
    )
    return (f"  - name: {name}\n"
            f"    label: {name.upper()}\n"
            f"    enabled: false\n"
            f"    command: '{PY} \"{script}\"'\n"
            f"    timeout_seconds: 30\n")


def test_re_opening_a_saved_answer_finds_the_run_that_wrote_it(tmp_path, step_script):
    """The ✓ badge on the board is how a review is read, and a closed panel is
    the normal state of one.

    Rebuilding the panel from the database alone made the step buttons last
    exactly as long as the panel stayed open: close it, re-open the answer, and
    the only way to run a step again was gone — while the job that could still
    do it sat in memory two functions away.
    """
    cfg = _config(tmp_path, (
        _skill(step_script, "arch")
        + _skill(step_script, "synth")
        + "  - name: full\n    label: Full review\n    enabled: true\n"
        "    stores_result: true\n    pipeline: [arch, synth]\n"
    ))
    db_path = tmp_path / "reopen.db"
    db = Database(db_path)
    _seed(db)
    db.close()
    client = TestClient(create_app(cfg, str(db_path)))

    job_id = re.search(r'data-job-id="([0-9a-f]+)"', client.post("/full/1/7").text).group(1)
    for _ in range(400):
        live = client.get(f"/full/status/{job_id}").text
        if "review-output" in live and "review-loading" not in live:
            break
        time.sleep(0.05)

    # Re-opened from the board, in the same radar that ran it.
    reopened = client.get("/full/stored/1/7").text
    assert "synth says hi" in reopened, "the saved answer is still the answer"
    assert "saved " in reopened, "and it still says when it was saved"
    assert f'hx-post="/full/retry/{job_id}?step=synth"' in reopened
    assert "↻ run again" in reopened
    # Which works from there, with no panel having stayed open in between.
    assert client.post(f"/full/retry/{job_id}?step=synth").status_code == 200

    # A radar restarted since has only the row, and says the same answer
    # without offering what it cannot do.
    later = TestClient(create_app(cfg, str(db_path)))
    assert "synth says hi" in later.get("/full/stored/1/7").text
    assert "↻" not in later.get("/full/stored/1/7").text


def test_a_stored_pipeline_result_keeps_the_breakdown_not_just_the_total(
    tmp_path, step_script
):
    """Added together, the steps stop saying which one took the half hour.

    The panel shows a line per step while the run is alive; re-opening the
    stored answer showed one total and four unexplained chart legends. This is
    that breakdown surviving the round trip through the database.
    """
    cfg = _config(tmp_path, (
        _priced_step(tmp_path, "arch", 1.5)
        + _priced_step(tmp_path, "synth", 0.5)
        + "  - name: full\n    label: Full review\n    enabled: true\n"
        "    stores_result: true\n    pipeline: [arch, synth]\n"
    ))
    db_path = tmp_path / "stored.db"
    db = Database(db_path)
    _seed(db)
    db.close()
    client = TestClient(create_app(cfg, str(db_path)))

    job_id = re.search(r'data-job-id="([0-9a-f]+)"', client.post("/full/1/7").text).group(1)
    for _ in range(400):
        live = client.get(f"/full/status/{job_id}").text
        if "review-output" in live and "review-loading" not in live:
            break
        time.sleep(0.05)
    assert "ARCH" in live and "SYNTH" in live, "the live panel should break the run down"

    # What the database actually kept.
    with Database(db_path) as db:
        saved = json.loads(db.get_test_plan(1, 7, "full")["stats"])
    assert [s["label"] for s in saved["steps"]] == ["ARCH", "SYNTH"]
    assert [s["status"] for s in saved["steps"]] == ["done", "done"]
    assert [s["stats"]["cost_usd"] for s in saved["steps"]] == [1.5, 0.5]
    assert all("elapsed_s" in s for s in saved["steps"])
    # The timeline is not stored twice — the charts read it from `series` — and
    # a field still at its default is left out rather than written as a zero.
    assert all("samples" not in s["stats"] for s in saved["steps"])
    assert all("web_searches" not in s["stats"] for s in saved["steps"])
    # Which the reader fills back in from the same defaults.
    from radar.commands import stats_from_mapping
    assert stats_from_mapping(saved["steps"][0]["stats"]).samples == []
    assert stats_from_mapping(saved["steps"][0]["stats"]).cost_usd == 1.5


    # And the re-opened panel shows it, the way the live one did. Read through
    # a second app over the same database — which is what "re-opened next week"
    # actually is: the job that ran is gone, and the row is the whole input.
    later = TestClient(create_app(cfg, str(db_path)))
    stored = later.get("/full/stored/1/7").text
    assert "ARCH" in stored and "SYNTH" in stored
    assert "✓ done in" in stored, "each step should say how long it took"
    assert "$1.5" in stored and "$0.5" in stored, "each step should say what it cost"
    assert "$2" in stored, "and the total should still be there"
    # Nothing live survives into a stored result: no stop, no run-again, no
    # polling. There is no job left to do any of it to.
    assert "■ stop" not in stored and "↻" not in stored
    assert "hx-get=\"/full/health/" not in stored
