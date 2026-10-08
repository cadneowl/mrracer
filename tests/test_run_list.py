"""The saved badge opens every run of that skill on that MR, not just the last.

The board keeps one answer per MR per skill and the next run replaces it; the
run record keeps all of them. Two models reviewing the same MR is the case this
exists for, so that is what these set up.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from radar.commands import RunStats, stats_to_json
from radar.config import load_config
from radar.db import Database
from radar.web.app import create_app
from tests.conftest import ev, ny
from tests.test_job_health import _BASE, PY
from tests.test_runlog import _SYNTHESIS

_OLDER = _SYNTHESIS.replace("### 2. Reads nothing back", "### 2. Something else entirely")


def _app(tmp_path) -> TestClient:
    skills = (
        f"  - name: full\n    label: Full review\n    enabled: true\n    stores_result: true\n"
        f"    command: '{PY} -c \"print(1)\"'\n"
        f"  - name: polish\n    enabled: true\n    command: '{PY} -c \"print(1)\"'\n"
        "deslopify:\n  skill: polish\n"
    )
    path = tmp_path / "config.yaml"
    path.write_text(_BASE.format(skills=skills), encoding="utf-8")
    with Database(tmp_path / "r.db") as db:
        db.upsert_mr_snapshot(
            project_id=1, mr_iid=7, title="Add widget", author="aviva",
            web_url="https://gitlab.example.com/g/p/-/merge_requests/7",
            source_branch="f", target_branch="main", description="", labels=[],
            draft=False, state="opened", reviewers=["dan"],
            created_at="2026-03-02T09:00:00Z", updated_at="2026-03-02T09:00:00Z",
        )
        db.insert_events([ev("review_requested", ny(2026, 3, 2, 9), reviewer="dan", mr_iid=7)])
        # Two models, one MR: glm first, deepseek later — the later one is what
        # the board kept.
        for run_id, model, ended, text, cost in (
            ("run-glm", "fireworks_ai/glm-5p3", "2026-09-24T12:00:00+00:00", _OLDER, 6.5),
            ("run-ds", "fireworks_ai/deepseek-v4p1-flash", "2026-09-24T15:00:00+00:00",
             _SYNTHESIS, 10.25),
        ):
            db.save_run({
                "id": run_id, "kind": "full", "project_id": 1, "mr_iid": 7,
                "subject": "!7", "status": "done", "ended_at": ended, "model": model,
                "output": text, "source": "live",
                "findings": json.dumps({"verdict": "blocked", "blockers": 2, "high": 1,
                                        "medium": 2, "shape": "synthesis"}),
                "metrics": json.dumps({"cost_usd": cost, "elapsed_s": 1800, "ttft_s": 8.0,
                                       "tokens_per_s": 79.4}),
                "stats": stats_to_json(RunStats(model=model, turns=3, cost_usd=cost)),
            })
        db.save_run({"id": "run-failed", "kind": "full", "project_id": 1, "mr_iid": 7,
                     "status": "error", "error": "timed out", "ended_at":
                     "2026-09-24T16:00:00+00:00", "model": "fireworks_ai/kimi-k3"})
        db.save_test_plan(1, 7, "full", "", _SYNTHESIS)
    return TestClient(create_app(load_config(path), str(tmp_path / "r.db")))


def test_the_saved_badge_opens_the_list_of_runs(tmp_path):
    client = _app(tmp_path)
    assert 'hx-get="/full/runs/1/7"' in client.get("/partials/board").text


def test_every_run_is_listed_newest_first_with_what_it_found(tmp_path):
    page = _app(tmp_path).get("/full/runs/1/7").text

    # Newest first, the failed one included: a run that died is part of the story.
    assert page.index("kimi-k3") < page.index("deepseek-v4p1-flash") < page.index("glm-5p3")
    assert "3 runs" in page and "failed" in page
    # The one the board shows is the newest that finished — not the failure.
    deepseek_row = page[page.index("deepseek-v4p1-flash") - 700:page.index("deepseek-v4p1-flash")]
    assert "on board" in deepseek_row
    assert "$10.25" in page and "$6.5" in page and "79" in page and "8.0s" in page
    assert 'hx-get="/full/run/run-glm"' in page
    # A failed run has no answer to open.
    assert 'hx-get="/full/run/run-failed"' not in page


def test_an_earlier_models_answer_opens_with_a_way_back(tmp_path):
    page = _app(tmp_path).get("/full/run/run-glm").text

    assert "Something else entirely" in page          # glm's answer, not the latest
    assert "glm-5p3" in page
    assert 'hx-get="/full/runs/1/7"' in page and "← all runs" in page


def test_a_run_of_another_skill_or_none_at_all_is_not_found(tmp_path):
    client = _app(tmp_path)
    assert client.get("/full/run/nope").status_code == 404
    assert client.get("/qa/run/run-glm").status_code == 404


def test_an_mr_with_nothing_recorded_falls_back_to_the_saved_answer(tmp_path):
    client = _app(tmp_path)
    with Database(tmp_path / "r.db") as db:
        db.save_test_plan(1, 8, "full", "", "## Blockers\n\n### 1. Old saved answer\n")
        db.conn.execute("DELETE FROM runs WHERE mr_iid = 8")
        db.conn.commit()
    page = client.get("/full/runs/1/8").text
    assert "Old saved answer" in page


def test_only_the_answer_the_board_shows_can_be_polished(tmp_path):
    """Polishing, like re-running a step, files its result as the MR's answer:
    from an older run it would replace the newer one."""
    client = _app(tmp_path)
    assert 'id="deslop"' in client.get("/full/run/run-ds").text
    older = client.get("/full/run/run-glm").text
    assert "Something else entirely" in older and 'id="deslop"' not in older


def test_a_row_whose_numbers_are_not_a_mapping_still_lists(tmp_path):
    client = _app(tmp_path)
    with Database(tmp_path / "r.db") as db:
        db.conn.execute("UPDATE runs SET findings = 'null', metrics = '[]' WHERE id = 'run-glm'")
        db.conn.commit()
    assert client.get("/full/runs/1/7").status_code == 200
