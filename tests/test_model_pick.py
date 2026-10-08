"""Picking the model for the next run from the board.

The model a run uses was whatever the command's settings.json named; the board
now offers a list (`models:`) and hands the pick to the child as
ANTHROPIC_MODEL, which Claude Code ranks above that setting. Checked where it
matters — in the environment the child actually sees, for every step of a
pipeline — rather than only in radar's own bookkeeping.
"""

from __future__ import annotations

import json
import re

import pytest

from radar.commands import CommandRunner
from radar.config import ConfigError, SkillConfig, _parse_models, load_config
from radar.db import Database
from radar.web.app import MODEL_COOKIE, create_app
from tests.conftest import ev, ny
from tests.test_job_health import _BASE, PY, _until

_ECHO = "import os; print('model=' + os.environ.get('ANTHROPIC_MODEL', 'unset'))"


def _app(tmp_path, models="[fireworks_ai/glm-5p3, fireworks_ai/kimi-k3]"):
    from fastapi.testclient import TestClient

    script = tmp_path / "echo.py"
    script.write_text(_ECHO, encoding="utf-8")
    skills = "".join(
        f"  - name: {name}\n    label: {name.upper()} review\n"
        f"    command: '{PY} \"{script}\"'\n    timeout_seconds: 60\n"
        f"    env: {{ANTHROPIC_MODEL: from-skill-env}}\n"
        for name in ("arch", "synth")
    ) + "  - name: full\n    enabled: true\n    pipeline: [arch, synth]\n"
    path = tmp_path / "config.yaml"
    path.write_text(_BASE.format(skills=skills) + f"models: {models}\n", encoding="utf-8")
    db = Database(tmp_path / "r.db")
    db.upsert_mr_snapshot(
        project_id=1, mr_iid=7, title="Add widget", author="aviva",
        web_url="https://gitlab.example.com/g/p/-/merge_requests/7",
        source_branch="f", target_branch="main", description="", labels=[], draft=False,
        state="opened", reviewers=["dan"], created_at="2026-03-02T09:00:00Z",
        updated_at="2026-03-02T09:00:00Z",
    )
    db.insert_events([ev("review_requested", ny(2026, 3, 2, 9), reviewer="dan", mr_iid=7)])
    db.close()
    return TestClient(create_app(load_config(path), str(tmp_path / "r.db")))


def _run_full(client, tmp_path) -> dict[str, dict]:
    job_id = re.search(r'data-job-id="([0-9a-f]+)"', client.post("/full/1/7").text).group(1)

    def runs():
        with Database(tmp_path / "r.db") as db:
            return {r["kind"]: r for r in db.runs()}

    _until(lambda: "full" in runs(), 30)
    got = runs()
    assert got["full"]["id"] == job_id
    return got


def test_the_picked_model_reaches_every_step_of_a_pipeline(tmp_path):
    client = _app(tmp_path)
    assert client.post("/model?model=fireworks_ai/kimi-k3").status_code == 204

    runs = _run_full(client, tmp_path)

    for step in ("arch", "synth"):
        # In the child's own environment — over the skill's env, which named
        # another model: the pick is the more specific choice.
        assert "model=fireworks_ai/kimi-k3" in runs[step]["output"]
        assert json.loads(runs[step]["metrics"])["requested_model"] == "fireworks_ai/kimi-k3"


def test_with_nothing_picked_the_skill_keeps_its_own_choice(tmp_path):
    client = _app(tmp_path)
    client.post("/model?model=")          # back to "settings default"

    runs = _run_full(client, tmp_path)

    assert "model=from-skill-env" in runs["arch"]["output"]


def test_a_model_the_config_does_not_offer_is_refused_and_never_used(tmp_path):
    client = _app(tmp_path)
    assert client.post("/model?model=evil; rm -rf").status_code == 400

    # Even a cookie set by hand: only an offered model reaches a child.
    client.cookies.set(MODEL_COOKIE, "not-offered")
    runs = _run_full(client, tmp_path)
    assert "model=from-skill-env" in runs["arch"]["output"]


def test_the_picker_is_drawn_only_when_models_are_configured(tmp_path):
    client = _app(tmp_path)
    client.post("/model?model=fireworks_ai/glm-5p3")
    page = client.get("/").text
    assert 'id="model-pick"' in page
    assert '<option value="fireworks_ai/glm-5p3" selected>glm-5p3</option>' in page

    (tmp_path / "bare").mkdir()
    bare = _app(tmp_path / "bare", models="[]")
    assert 'id="model-pick"' not in bare.get("/").text


def test_the_child_env_gives_the_picked_model_the_last_word():
    runner = CommandRunner(
        SkillConfig(name="r", command="x", env={"ANTHROPIC_MODEL": "skill"}), "r"
    )
    assert runner._child_env()["ANTHROPIC_MODEL"] == "skill"
    assert runner._child_env("picked")["ANTHROPIC_MODEL"] == "picked"


def test_models_config_is_checked():
    assert [m.label for m in _parse_models(["a/b", {"id": "c/d", "label": "D"}])] == ["b", "D"]
    assert _parse_models([{"id": "a/b", "label": None}])[0].label == "b"
    for bad in (["a", "a"], ["has space"], [{"label": "no id"}], [{"id": None}], [None], "a/b"):
        with pytest.raises(ConfigError):
            _parse_models(bad)


def test_a_command_that_names_its_own_model_runs_the_picked_one():
    """``--model`` outranks ANTHROPIC_MODEL, so leaving it alone would run the
    command's model while the panel and the run record claimed the pick."""
    from radar.commands import _pick_model

    assert _pick_model(["claude", "-p", "--model", "a", "/x"], "b") == [
        "claude", "-p", "--model", "b", "/x"]
    assert _pick_model(["claude", "--model=a"], "b") == ["claude", "--model=b"]
    # A command naming none is left as written: the env var covers it.
    assert _pick_model(["wrapper.sh", "/x"], "b") == ["wrapper.sh", "/x"]


def test_a_retried_steps_failed_attempt_is_not_credited_with_the_retrys_findings():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "model_compare", Path(__file__).parent.parent / "scripts" / "model-compare.py")
    mc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mc)

    def run(run_id, ended, parent="", label="AI review", raised=None):
        return {"id": run_id, "parent_id": parent, "label": label, "ended_at": ended,
                "metrics": {}, "findings": {"raised_by": raised} if raised else {}}

    raised = {"AI review": {"blockers": 2, "high": 1, "findings": 3}}
    credit = mc._survival([
        run("pipe", "2026-09-24T12:00", raised=raised),
        run("first", "2026-09-24T11:00", parent="pipe"),      # failed, then retried
        run("retry", "2026-09-24T11:30", parent="pipe"),
    ])
    assert credit == {"retry": raised["AI review"]}
