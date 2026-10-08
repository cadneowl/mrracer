"""The run record: every run kept, with its model, findings and numbers.

The point of it is comparing models, which the saved results cannot do — a
second model's answer on the same MR replaces the first. So these check that
nothing is overwritten, that a pipeline's steps are rows of their own tied to
it, and that the findings are counted the same way whatever shape the answer
came in.
"""

from __future__ import annotations

import json
import re

from radar.commands import RunStats, stats_to_json
from radar.db import Database
from radar.findings import analyse
from radar.runlog import backfill, metrics
from tests.test_job_health import _app, _until

# --- counting findings ------------------------------------------------------

_SYNTHESIS = """## Verdict

**Blocked.** Two problems.

## Blockers

### 1. The test cannot pass
**Where:** `Repo.java:219` · **Raised by:** AI review, DBA review, QA test plan
**Fix:** make them agree.

### 2. Reads nothing back
**Where:** `Repo.java:336-339` · **Raised by:** DBA review
**Fix:** query it.

## Should fix

### 3. No unique index
**Where:** `Repo.java:274` · **Raised by:** QA test plan (unanimous)

## Consider

**4. Deletes both specifications** (`Service.java:552`, DBA review) — scoped wrongly.

**5. DISTINCT over id** (`Repo.java:60`) — dedups nothing.

## Note

**6.** Title should carry the JIRA id.
"""

_QA = """# QA Test Plan — HUB-1

### 🚫 Blocking Gaps

**1 — No HTTP-level test**

- **What**: nothing calls it.
- **Dimension**: Functional

**2 — No negative test**

**Dimension**: Negative · **Evidence**: none

### ⚠️ Strong Recommendations

**3 — Verify no write access**

### 🔍 Probing / Exploratory

- **Audit other roles?** maybe.
- **LDAP groups?** maybe.

### Summary

**Blocking** (must fix):
- one
- two
"""

_FREEFORM = """# Architectural Review

**Verdict: Approve with required changes.**

## 1. BLOCKING — first path wins
## 2. The mapper reads column 12
## 3. Nit: naming

## Summary

| # | Finding | Severity |
|---|---------|----------|
| 1 | first path wins | **Blocking** |
| 2 | mapper | Strong |
| 3 | naming | Nit |
| 4 | migration | Confirm before merge |
"""


def test_a_synthesis_is_counted_by_its_sections_and_says_who_raised_what():
    found = analyse(_SYNTHESIS)

    assert found["shape"] == "synthesis"
    assert (found["blockers"], found["high"], found["medium"], found["low"]) == (2, 1, 2, 1)
    assert found["findings"] == 6
    assert found["verdict"] == "blocked" and found["blocked"] == 1
    # Credit per reviewer: the one measure that is not a model's own word.
    assert found["raised_by"]["DBA review"] == {
        "blockers": 2, "high": 0, "medium": 0, "low": 0, "findings": 2}
    assert found["raised_by"]["QA test plan"]["high"] == 1   # "(unanimous)" dropped
    assert found["citations"] == 5 and found["files_cited"] == 2


def test_a_qa_plan_counts_its_items_not_the_labels_inside_them():
    """Each item carries **Dimension**: and **What**: lines, and the plan ends
    with a summary repeating the items — none of them are tests of their own."""
    found = analyse(_QA)

    assert found["shape"] == "qa"
    assert (found["blockers"], found["high"], found["medium"]) == (2, 1, 2)
    assert found["tests_proposed"] == 5


def test_a_single_reviewers_own_severity_table_wins_over_its_headings():
    found = analyse(_FREEFORM)

    assert found["shape"] == "freeform"
    assert (found["blockers"], found["high"], found["low"], found["medium"]) == (1, 1, 1, 1)
    assert found["verdict"] == "approve with required changes" and not found["blocked"]


def test_without_a_table_severity_comes_from_the_finding_titles():
    found = analyse(_FREEFORM.split("## Summary")[0])

    assert (found["blockers"], found["medium"], found["low"]) == (1, 1, 1)


def test_the_value_ratios_are_what_the_money_bought():
    stats = RunStats(turns=3, cost_usd=6.0, output_tokens=2000, tool_calls=12)
    out = metrics(stats, {"findings": 4, "blockers": 2})

    assert out["cost_per_finding"] == 1.5 and out["cost_per_blocker"] == 3.0
    assert out["findings_per_1k_output"] == 2.0 and out["tool_calls_per_finding"] == 3.0
    assert metrics(RunStats(), {"findings": 0})["measured"] is False


# --- recording --------------------------------------------------------------


def _runs(tmp_path) -> list[dict]:
    with Database(tmp_path / "r.db") as db:
        return db.runs()


def test_a_pipeline_and_every_step_are_recorded_and_tied_together(tmp_path):
    client = _app(tmp_path)
    start = client.post("/full/1/7")
    job_id = re.search(r'data-job-id="([0-9a-f]+)"', start.text).group(1)
    _until(lambda: any(r["id"] == job_id for r in _runs(tmp_path)), 60)

    runs = {r["kind"]: r for r in _runs(tmp_path)}
    assert set(runs) == {"full", "arch", "dba", "synth"}
    for step in ("arch", "dba", "synth"):
        assert runs[step]["parent_id"] == job_id
        assert runs[step]["mr_iid"] == 7 and runs[step]["status"] == "done"
        assert "says hi" in runs[step]["output"]     # each step's own answer, kept
    pipeline = runs["full"]
    assert pipeline["parent_id"] == "" and pipeline["command"] == "arch + dba → synth"
    assert pipeline["started_at"] and pipeline["ended_at"]
    assert json.loads(pipeline["metrics"])["steps"] == ["arch", "dba", "synth"]


def test_running_again_adds_a_row_rather_than_replacing_one(tmp_path):
    client = _app(tmp_path)
    ids = {
        re.search(r'data-job-id="([0-9a-f]+)"', client.post("/full/1/7").text).group(1)
        for _ in range(2)
    }
    _until(lambda: ids <= {r["id"] for r in _runs(tmp_path)}, 90)

    kinds = [r["kind"] for r in _runs(tmp_path)]
    assert kinds.count("full") == 2 and kinds.count("dba") == 2


def test_backfill_seeds_saved_results_once_and_never_twice_for_a_live_run(tmp_path):
    step_stats = json.loads(stats_to_json(RunStats(model="m-1", turns=2, cost_usd=1.0)))
    total = RunStats(model="m-1", turns=4, cost_usd=2.0, steps=[
        {"name": "review", "label": "AI review", "status": "done", "elapsed_s": 60,
         "session_id": "s1", "stats": step_stats},
    ])
    with Database(tmp_path / "r.db") as db:
        db.save_test_plan(1, 7, "full-review", "", _SYNTHESIS, stats_to_json(total))
        # A live run recorded under its own job id, whose answer was then saved:
        # seeding the saved copy as well would count that one run twice.
        db.save_run({"id": "a1b2c3d4e5f6", "kind": "qa", "project_id": 1, "mr_iid": 8,
                     "status": "done", "ended_at": "2026-09-01", "source": "live"})
        db.save_test_plan(1, 8, "qa", "", _QA)

        assert backfill(db) == 2          # the pipeline and its one step, not the qa
        assert backfill(db) == 0          # and never again
        rows = {r["id"]: r for r in db.runs()}

    assert "saved:qa:1:8" not in rows
    pipeline = rows["saved:full-review:1:7"]
    assert pipeline["source"] == "saved result" and pipeline["model"] == "m-1"
    assert json.loads(pipeline["findings"])["blockers"] == 2
    step = rows["saved:full-review:1:7:review"]
    assert step["parent_id"] == "saved:full-review:1:7" and step["label"] == "AI review"
    assert json.loads(step["metrics"])["cost_usd"] == 1.0


def test_backfill_keeps_the_attempt_a_retried_step_ended_with(tmp_path):
    """A retried step is listed once per attempt under one name, the earlier
    first. The last attempt is the one the synthesis read."""
    def attempt(label, status, cost):
        stats = json.loads(stats_to_json(RunStats(model="m", turns=1, cost_usd=cost)))
        return {"name": "review", "label": label, "status": status, "stats": stats}

    total = RunStats(model="m", turns=2, steps=[
        attempt("AI review (earlier attempt)", "error", 1.0),
        attempt("AI review", "done", 2.0),
    ])
    with Database(tmp_path / "r.db") as db:
        db.save_test_plan(1, 7, "full-review", "", _SYNTHESIS, stats_to_json(total))
        backfill(db)
        rows = {r["id"]: r for r in db.runs()}

    final = rows["saved:full-review:1:7:review"]
    assert final["status"] == "done" and final["label"] == "AI review"
    assert not json.loads(final["metrics"])["superseded"]
    earlier = rows["saved:full-review:1:7:review:earlier-1"]
    assert earlier["status"] == "error" and json.loads(earlier["metrics"])["superseded"]


def test_findings_titled_finding_n_or_by_severity_are_counted():
    """Two shapes the reviewers actually write, which read as "no findings"
    before: "## Finding 1 — Blocking: …" and unnumbered "### Blocking: …"."""
    numbered = analyse(
        "# Architecture Review\n\n## Finding 1 — Blocking: throws on the new field\n\n"
        "## Finding 2 — Recommendation: tests prove too little\n\n### Summary\n"
    )
    assert (numbered["blockers"], numbered["medium"], numbered["findings"]) == (1, 1, 2)

    graded = analyse(
        "## Review\n\n### Blocking: `@Primary` is the wrong mechanism\n\n"
        "### Should fix — the precedent is misquoted\n\n### Not findings\n"
    )
    assert (graded["blockers"], graded["high"], graded["findings"]) == (1, 1, 2)


def test_rows_counted_by_an_older_parser_are_counted_again(tmp_path):
    from radar.runlog import reanalyse

    with Database(tmp_path / "r.db") as db:
        db.save_run({"id": "r1", "kind": "review", "status": "done", "ended_at": "x",
                     "output": "## Finding 1 — Blocking: broken\n",
                     "findings": json.dumps({"findings": 0}), "metrics": "{}"})
        assert reanalyse(db) == 1 and reanalyse(db) == 0
        found = json.loads(db.get_run("r1")["findings"])
    assert found["blockers"] == 1


def test_an_unnumbered_finding_and_a_plain_verdict_are_read():
    """Two more shapes seen in real answers: a single "### Finding: …" with no
    number, and a verdict written as the first words under "## Verdict"."""
    single = analyse(
        "## DBA Review\n\n### Finding: script numbered under a removed release\n\n"
        "### Other checks performed, no issues found\n"
    )
    assert (single["medium"], single["findings"]) == (1, 1)

    plain = analyse(_SYNTHESIS.replace("**Blocked.** Two problems.",
                                       "Request changes — two problems."))
    assert plain["verdict"] == "request changes"


def test_a_heading_inside_a_code_sample_does_not_end_the_section():
    found = analyse(
        "## Blockers\n\n### 1. a\n**Raised by:** AI review\n```python\n# reproduce\nx = 1\n```\n\n"
        "### 2. b\n**Raised by:** DBA review\n\n## Should fix\n"
    )
    assert found["blockers"] == 2 and set(found["raised_by"]) == {"AI review", "DBA review"}


def test_a_long_line_is_counted_in_linear_time():
    import time

    started = time.monotonic()
    for text in ("# " + " " * 200_000, "x" * 200_000, "a-" * 100_000):
        analyse(text)
    assert time.monotonic() - started < 2


def test_a_step_that_saved_its_own_result_is_one_run_with_its_answer(tmp_path):
    """A full review's QA plan is saved as the plan and inside the pipeline's
    numbers. Backfilled, that is one QA run — the step — and it has the plan."""
    qa = json.loads(stats_to_json(RunStats(model="m", turns=3, output_tokens=900, api_ms=4000)))
    total = RunStats(model="m", turns=3, steps=[
        {"name": "qa", "label": "QA plan", "status": "done", "stats": qa},
    ])
    with Database(tmp_path / "r.db") as db:
        db.save_test_plan(1, 7, "qa", "", _QA, json.dumps(qa))
        db.save_test_plan(1, 7, "full-review", "", _SYNTHESIS, stats_to_json(total))
        backfill(db)
        qa_rows = [r for r in db.runs() if r["kind"] == "qa"]

    assert [r["id"] for r in qa_rows] == ["saved:full-review:1:7:qa"]
    assert json.loads(qa_rows[0]["findings"])["tests_proposed"] == 5


def test_an_older_backfill_that_wrote_the_plan_twice_is_repaired(tmp_path):
    """Rows an earlier backfill wrote for one run, after the saved results have
    moved on to a later run: merged by their own numbers, and the later run's
    answer is not pinned on the older run's step."""
    old = json.dumps(json.loads(stats_to_json(
        RunStats(model="m", turns=3, output_tokens=900, api_ms=4000))))
    with Database(tmp_path / "r.db") as db:
        common = {"kind": "qa", "project_id": 1, "mr_iid": 7, "status": "done",
                  "ended_at": "x", "source": "saved result", "stats": old}
        db.save_run({**common, "id": "saved:qa:1:7", "output": _QA})
        db.save_run({**common, "id": "saved:full-review:1:7:qa",
                     "parent_id": "saved:full-review:1:7"})
        # Since then a different run's plan was saved.
        newer = json.loads(stats_to_json(RunStats(model="n", turns=2, output_tokens=50)))
        db.save_test_plan(1, 7, "qa", "", "# a later plan", json.dumps(newer))
        backfill(db)
        rows = {r["id"]: r for r in db.runs()}

    assert "saved:qa:1:7" not in rows
    assert rows["saved:full-review:1:7:qa"]["output"] == _QA
