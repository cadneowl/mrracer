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


@pytest.mark.parametrize("name", ["stop", "health"])
def test_the_panel_routes_are_reserved_names(tmp_path, name):
    path = tmp_path / "config.yaml"
    path.write_text(
        _BASE.format(skills=f"  - name: {name}\n    command: 'mytool'\n"), encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="reserved"):
        load_config(path)
