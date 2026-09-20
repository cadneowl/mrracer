"""Seeing inside a running job: waiting told apart from working, subagents
named, and a job — or one step of a pipeline — stopped from the panel."""

from __future__ import annotations

import json
import re
import sys
import time

import pytest
from fastapi.testclient import TestClient

from radar.commands import STALL_AFTER_S, CommandJob, CommandRunner, job_health
from radar.config import ConfigError, SkillConfig, load_config
from radar.db import Database
from radar.events import EventType as ET
from radar.pipeline import build_runners
from radar.web.app import _health_rows, create_app
from tests.conftest import ev, ny

PY = f'"{sys.executable}"'


def _tool(name, parent=None, **tool_input):
    return json.dumps({
        "type": "assistant",
        "parent_tool_use_id": parent,
        "message": {"content": [{"type": "tool_use", "name": name, "input": tool_input}]},
    })


def _feed(runner, job, *lines):
    stats = {}  # one run's worth: a subagent's end is matched to its start
    for line in lines:
        runner._ingest(job, line, [], [], stats)


def _runner():
    return CommandRunner(SkillConfig(name="review", label="AI review", command="x"), "review")


def _running_job():
    return CommandJob(id="j", kind="review", started_mono=time.monotonic())


def _texts(job):
    return [item["text"] for item in job.progress]


def _until(predicate, limit=20.0):
    deadline = time.monotonic() + limit
    while not predicate():
        assert time.monotonic() < deadline, "condition never became true"
        time.sleep(0.05)


def _await(runner, job, limit=20.0):
    """The job, once it has reached a terminal state."""
    _until(lambda: runner.get(job.id).status != "running", limit)
    return runner.get(job.id)


# --- working or waiting ----------------------------------------------------


def test_waiting_is_told_apart_from_working():
    runner, job = _runner(), _running_job()
    _feed(runner, job, _tool("Read", file_path="/src/A.java"))
    assert job.waits_since_work == 0

    # Exactly the loop a stuck review step ran: list sessions, message one, sleep.
    _feed(
        runner, job,
        _tool("ListAgents"),
        _tool("SendMessage", to="hub-backend-98 [03d080]", message="status?"),
        _tool("Bash", command="sleep 90", description="Wait 90 more seconds"),
    )
    assert job.waits_since_work == 3
    assert job.waiting_on == "Bash: Wait 90 more seconds"

    # A shell command that is not a sleep is work, and resets the count.
    _feed(runner, job, _tool("Bash", command="git log --oneline -5"))
    assert job.waits_since_work == 0 and job.waiting_on == ""


def test_only_a_run_that_has_done_nothing_but_wait_for_a_while_is_stalled():
    job = _running_job()
    job.last_work_mono = job.started_mono
    job.waits_since_work, job.waiting_on = 3, "ListAgents"
    later = job.started_mono + STALL_AFTER_S + 100

    health = job_health(job, now=later)
    assert health["stalled"] and health["idle_s"] == STALL_AFTER_S + 100
    assert health["waiting_on"] == "ListAgents"

    job.waits_since_work = 1  # one long sleep before a check is patience
    assert not job_health(job, now=later)["stalled"]
    job.waits_since_work = 3
    assert not job_health(job, now=job.started_mono + 60)["stalled"]  # not long enough yet
    job.status = "done"
    assert not job_health(job, now=later)["stalled"]


def test_the_session_and_its_subagents_are_named_in_the_log():
    runner, job = _runner(), _running_job()
    _feed(
        runner, job,
        json.dumps({"type": "system", "subtype": "init", "session_id": "944faa5b-3a9f"}),
        json.dumps({
            "type": "system", "subtype": "task_started", "task_id": "t1",
            "description": "Arch review skill result relay", "is_backgrounded": False,
        }),
        _tool("ListAgents", parent="toolu_1"),
        json.dumps({"type": "system", "subtype": "task_progress", "task_id": "t1"}),
        json.dumps({"type": "system", "subtype": "task_updated", "task_id": "t1"}),
        json.dumps({
            "type": "system", "subtype": "task_notification", "task_id": "t1",
            "status": "completed",
        }),
    )
    assert job.session_id == "944faa5b-3a9f"
    assert _texts(job) == [
        "session started",
        "subagent started: Arch review skill result relay",
        "↳ ListAgents",
        "subagent completed: Arch review skill result relay",
    ]


def test_a_stalled_row_says_how_long_and_on_what():
    runner, job = _runner(), _running_job()
    job.started_mono -= 700
    job.last_work_mono = job.started_mono + 100
    job.waits_since_work, job.waiting_on = 12, "Bash: Wait 90 more seconds"
    job.session_id = "944faa5b-3a9f-41af"
    job.cwd = "/src/hub-backend"
    [row] = _health_rows(runner, job)
    assert row["stalled"] and row["can_stop"]
    assert "only waiting for 10m" in row["note"]
    assert "Wait 90 more seconds" in row["note"]
    assert row["resume"] == "cd /src/hub-backend && claude --resume 944faa5b-3a9f-41af"


# --- out of time, and being given more -------------------------------------


def test_a_run_out_of_time_is_held_rather_than_killed():
    """The whole point: a deadline is no longer the end of the work.

    A review killed on the stroke of its budget takes forty minutes of work with
    it and the only way back is to pay for all of it again. Held instead, it
    finishes — and the thing that was about to be thrown away is the answer.
    """
    code = "import time; print('working', flush=True); time.sleep(3); print('done')"
    runner = CommandRunner(
        SkillConfig(name="review", command=f'{PY} -c "{code}"',
                    timeout_seconds=1, timeout_grace_seconds=30),
        "review",
    )
    job = runner.start({"project_id": 1, "mr_iid": 2})
    done = _await(runner, job, limit=30)

    assert done.status == "done", done.error
    assert "done" in done.output
    assert any("out of time" in item["text"] for item in done.progress), (
        "and it said so while it was being held"
    )


def test_a_held_run_nobody_answers_ends_as_it_always_did():
    """The hold is bounded. Nobody there, and it fails — a few minutes later,
    with what it wrote, and saying it could have been saved."""
    code = "import time; print('half an answer', flush=True); time.sleep(60)"
    runner = CommandRunner(
        SkillConfig(name="review", command=f'{PY} -c "{code}"',
                    timeout_seconds=1, timeout_grace_seconds=2),
        "review",
    )
    done = _await(runner, runner.start({"project_id": 1, "mr_iid": 2}), limit=30)

    assert done.status == "error"
    assert "timed out" in done.error
    assert "held for 2s" in done.error and "nobody gave it more time" in done.error
    assert "half an answer" in done.output, "and what it wrote is still kept"


def test_a_run_still_inside_its_budget_can_be_given_more_time():
    """Not only at the deadline: a countdown getting short while a review is
    plainly mid-thought is exactly when to say carry on."""
    code = "import time; print('working', flush=True); time.sleep(3); print('done')"
    runner = CommandRunner(
        SkillConfig(name="review", command=f'{PY} -c "{code}"',
                    timeout_seconds=2, timeout_grace_seconds=0),   # no hold to fall back on
        "review",
    )
    job = runner.start({"project_id": 1, "mr_iid": 2})
    _until(lambda: "working" in job.output or job.progress, limit=10)
    assert runner.extend(job.id, 30) is True

    done = _await(runner, job, limit=30)
    assert done.status == "done", done.error
    assert "done" in done.output
    assert done.extra_s == 30

    # And nothing to give it once it has ended.
    assert runner.extend(job.id, 30) is False
    assert runner.extend("no-such-job", 30) is False
    assert runner.extend(job.id, 0) is False


def test_a_held_row_says_what_the_choice_is_and_how_long_there_is_to_make_it():
    runner = CommandRunner(
        SkillConfig(name="review", label="AI review", command="x",
                    timeout_seconds=600, timeout_grace_seconds=300),
        "review",
    )
    job = _running_job()
    job.started_mono -= 600
    job.out_of_time_mono = time.monotonic() - 60      # held a minute ago
    [row] = _health_rows(runner, job)

    assert row["out_of_time"] and row["can_extend"] and row["can_stop"]
    assert "out of time after 10m" in row["state_text"]
    assert "to give it more time" in row["note"] and "its work is lost" in row["note"]
    # The seconds left to decide in, counted down rather than called "soon".
    left = job_health(job, grace=300)["decide_s"]
    assert 230 <= left <= 240, left

    # Given more, it is no longer out of time and says what it was given.
    runner._grant(job, 600)
    [row] = _health_rows(runner, job)
    assert not row["out_of_time"] and "+10m given" in row["state_text"]


# --- stopping --------------------------------------------------------------


def test_a_job_stopped_from_the_panel_ends_now_and_keeps_what_it_wrote():
    code = "import time; print('working', flush=True); time.sleep(30)"
    runner = CommandRunner(
        SkillConfig(name="review", command=f'{PY} -c "{code}"', timeout_seconds=60), "review"
    )
    started = time.monotonic()
    job = runner.start({"subject": "!7"})
    _until(lambda: "working" in _texts(job))
    assert runner.stop(job.id)
    _until(lambda: job.status != "running")
    assert job.status == "error"
    assert "stopped from the panel" in job.error
    assert "working" in job.output
    assert time.monotonic() - started < 15
    assert job.ended_mono > 0
    assert runner.stop(job.id) is False  # nothing left to stop


_STEP = """
import json, sys, time
who, delay = sys.argv[1], float(sys.argv[2])
handed = sys.stdin.read()
print(f"{who} says hi", flush=True)
time.sleep(delay)
if handed:
    print("STDIN" + json.dumps(handed))
"""


def _pipeline(tmp_path, arch_delay):
    script = tmp_path / "step.py"
    script.write_text(_STEP, encoding="utf-8")

    def step(name, delay=0):
        return SkillConfig(
            name=name, label=f"{name.upper()} review",
            command=f'{PY} "{script}" {name} {delay}', timeout_seconds=60,
        )

    full = SkillConfig(
        name="full", label="Full review", enabled=True,
        pipeline=(("arch", "dba"), ("synth",)), timeout_seconds=120,
    )
    return build_runners((step("arch", arch_delay), step("dba"), step("synth"), full))


def test_stopping_one_step_lets_the_pipeline_carry_on_without_it(tmp_path):
    runners = _pipeline(tmp_path, arch_delay=30)
    job = runners["full"].start({"subject": "!7"})
    _until(lambda: "[arch] arch says hi" in _texts(job))
    assert runners["full"].stop(job.id, step="arch")
    _until(lambda: job.status != "running", limit=30)
    assert job.status == "done", job.error
    handed = json.loads(job.output.split("STDIN", 1)[1].splitlines()[0])
    assert "### ARCH review (failed)" in handed and "stopped from the panel" in handed
    assert "### DBA review\n\ndba says hi" in handed


def test_stopping_the_pipeline_skips_the_stages_still_to_come(tmp_path):
    runners = _pipeline(tmp_path, arch_delay=30)
    job = runners["full"].start({"subject": "!7"})
    _until(lambda: "[arch] arch says hi" in _texts(job))
    assert runners["full"].stop(job.id)
    _until(lambda: job.status != "running", limit=30)
    assert job.status == "error" and "stopped from the panel" in job.error
    assert runners["synth"]._jobs == {}  # never started
    assert runners["full"].stop(job.id) is False


# --- the panel -------------------------------------------------------------

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
skills:
{skills}
"""


def _clock_seconds(panel: str) -> int:
    """The countdown the panel is showing, in seconds."""
    found = re.search(r"(\d+):(\d\d) left", panel)
    assert found, "the panel should be showing a countdown"
    return int(found.group(1)) * 60 + int(found.group(2))


def _app(tmp_path):
    script = tmp_path / "step.py"
    script.write_text(_STEP, encoding="utf-8")
    skills = "".join(
        f"  - name: {name}\n    label: {name.upper()} review\n"
        f"    command: '{PY} \"{script}\" {name} {delay}'\n    timeout_seconds: 60\n"
        for name, delay in (("arch", 30), ("dba", 0), ("synth", 0))
    ) + "  - name: full\n    enabled: true\n    pipeline: [{parallel: [arch, dba]}, synth]\n"
    path = tmp_path / "config.yaml"
    path.write_text(_BASE.format(skills=skills), encoding="utf-8")
    cfg = load_config(path)

    db_path = tmp_path / "r.db"
    db = Database(db_path)
    db.upsert_mr_snapshot(
        project_id=1, mr_iid=7, title="Add widget", author="aviva",
        web_url="https://gitlab.example.com/g/p/-/merge_requests/7",
        source_branch="f", target_branch="main", description="", labels=[], draft=False,
        state="opened", reviewers=["dan"], created_at="2026-03-02T09:00:00Z",
        updated_at="2026-03-02T09:00:00Z",
    )
    db.insert_events([ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan", mr_iid=7)])
    db.close()
    return TestClient(create_app(cfg, str(db_path)))


def test_the_panel_shows_every_step_and_stops_one(tmp_path):
    client = _app(tmp_path)
    start = client.post("/full/1/7")
    job_id = re.search(r'data-job-id="([0-9a-f]+)"', start.text).group(1)
    assert f'hx-get="/full/health/{job_id}"' in start.text

    _until(lambda: "arch says hi" in client.get(f"/full/health/{job_id}").text)
    health = client.get(f"/full/health/{job_id}").text
    for label in ("ARCH review", "DBA review", "SYNTH review"):
        assert label in health
    assert "pending" in health  # synth waits for its stage
    assert "■ stop" in health

    stopped = client.post(f"/full/stop/{job_id}?step=arch")
    assert stopped.status_code == 200 and "ARCH review" in stopped.text

    _until(lambda: "hx-trigger" not in client.get(f"/full/health/{job_id}").text, limit=30)
    final = client.get(f"/full/health/{job_id}").text
    assert "stopped from the panel" in final
    assert "■ stop" not in final

    assert client.post("/full/stop/nope").status_code == 404
    assert client.get("/full/health/nope").status_code == 404


def test_the_panel_gives_more_time_to_one_step_and_to_all_of_them(tmp_path):
    """From the browser's side, on a pipeline: the offer is on every running row
    and once more for the stage, and it is what moves the clock.

    Asserted through the panel and the rows, because that is all an operator
    has: the grant is only real if it shows up in what the next refresh draws.
    """
    client = _app(tmp_path)
    panel = client.post("/full/1/7").text
    job_id = re.search(r'data-job-id="([0-9a-f]+)"', panel).group(1)
    _until(lambda: "ARCH review" in client.get(f"/full/health/{job_id}").text)

    health = client.get(f"/full/health/{job_id}").text
    assert f'hx-post="/full/extend/{job_id}?step=arch"' in health
    assert f'hx-post="/full/extend/{job_id}"' in health, "and one for the whole stage"
    assert "＋ 10 min" in health
    before = _clock_seconds(client.get(f"/full/status/{job_id}").text)

    assert client.post(f"/full/extend/{job_id}?step=arch").status_code == 200
    assert "(+10m given)" in client.get(f"/full/health/{job_id}").text

    # The pipeline's own countdown follows its steps', so the panel's clock does
    # not sit at 0:00 through work somebody deliberately paid to continue.
    _until(lambda: _clock_seconds(client.get(f"/full/status/{job_id}").text) > before + 500)

    # Every step running now, in one click — dba has finished and synth has not
    # started, so that is arch, again.
    client.post(f"/full/extend/{job_id}")
    assert "(+20m given)" in client.get(f"/full/health/{job_id}").text

    # A step that is not this pipeline's, and figures nobody meant.
    assert client.post(f"/full/extend/{job_id}?step=nope").status_code == 404
    assert client.post(f"/full/extend/{job_id}?seconds=0").status_code == 400
    assert client.post(f"/full/extend/{job_id}?seconds=999999").status_code == 400
    assert client.post("/full/extend/nope").status_code == 404

    client.post(f"/full/stop/{job_id}")


@pytest.mark.parametrize(
    "name", ["stop", "health", "status", "stream", "close", "stored", "retry", "stats", "extend"]
)
def test_the_panel_routes_are_reserved_names(tmp_path, name):
    path = tmp_path / "config.yaml"
    path.write_text(
        _BASE.format(skills=f"  - name: {name}\n    command: 'mytool'\n"), encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="reserved"):
        load_config(path)
