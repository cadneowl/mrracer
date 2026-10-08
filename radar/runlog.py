"""The run record: every run of every skill, kept so models can be compared.

``test_plans`` keeps the latest answer per merge request, which is what the
board needs and exactly what a comparison cannot use — running a second model
over the same MR erases the first model's answer. This writes one ``runs`` row
per run instead, and one per step of a pipeline, when the run ends (see
``CommandRunner.on_finish``).

A row carries three kinds of thing:

* **what ran** — the skill, the MR and the commit it was about, the model the
  stream reported, the gateway the command was pointed at, the CLI version and
  the command template, so two runs "on the same MR" can be checked to have
  seen the same code through the same prompt;
* **what it found** — the answer itself, and the findings counted from it
  (``findings.analyse``);
* **what it cost** — every number ``RunStats`` measured, flattened into
  ``metrics`` along with the ratios a comparison actually asks for: cost per
  finding, output tokens per second, findings per thousand output tokens.

``backfill`` seeds the table from the results saved before it existed, marked
``source = 'saved result'`` — the latest answer per MR, and its steps' numbers
without their answers, which is all that was kept.
"""

from __future__ import annotations

import json
import os
import shlex
from datetime import UTC, datetime
from urllib.parse import urlparse

from .commands import CommandJob, RunStats, stats_from_json, stats_to_json
from .db import Database
from .findings import PARSER_VERSION, analyse

_MAX_OUTPUT = 400_000   # an answer, not a transcript: bound one row's size


def _ratio(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def metrics(stats: RunStats, found: dict, elapsed_s: float | None = None) -> dict:
    """A run's numbers, flat — everything a comparison groups and averages."""
    read = stats.input_tokens + stats.cache_read_tokens + stats.cache_write_tokens
    findings = found.get("findings", 0)
    out = {
        # False for a run that reported no numbers at all (a result saved before
        # radar measured runs, or a command that speaks no stream-json): its
        # zeros are absence, and a comparison must leave it out, not average it.
        "measured": stats.measured,
        # money
        "cost_usd": round(stats.cost_usd, 6),
        # tokens
        "input_tokens": stats.input_tokens,
        "cache_read_tokens": stats.cache_read_tokens,
        "cache_write_tokens": stats.cache_write_tokens,
        "prompt_tokens": read,
        "output_tokens": stats.output_tokens,
        "thinking_tokens": stats.thinking_tokens,
        "total_tokens": stats.total_tokens,
        "cache_hit_rate": stats.cache_hit_rate,
        "peak_context_tokens": stats.peak_context_tokens,
        "context_window": stats.context_window,
        "context_share": stats.context_share,
        "billed": stats.billed,
        # time
        "elapsed_s": round(elapsed_s, 1) if elapsed_s is not None else None,
        "wall_ms": stats.wall_ms,
        "api_ms": stats.api_ms,
        "model_share": stats.model_share,
        "ttft_s": stats.ttft_s,
        "slowest_ttft_s": stats.first_token_ms / 1000 if stats.first_token_ms else None,
        "tokens_per_s": stats.output_tokens_per_s,
        "avg_response_s": stats.avg_response_s,
        # work
        "requests": stats.turn_count,
        "requests_with_usage": stats.requests_with_usage,
        "tool_calls": stats.tool_calls,
        "tools_distinct": len(stats.tools),
        "tools": dict(stats.tools),
        "web_searches": stats.web_searches,
        "web_fetches": stats.web_fetches,
        "subagents_spawned": stats.subagents_spawned,
        "subagents_failed": stats.subagents_failed,
        "subagent_depth": stats.subagent_depth,
        # friction
        "denied_calls": stats.denied_calls,
        "denials": dict(stats.denials),
        "api_errors": sum(stats.api_errors.values()),
        "last_api_error": stats.last_api_error,
        "result_errors": stats.errors,
        "outcome": stats.outcome,
        "stop_reason": stats.stop_reason,
        "rate_limit_peak": max(stats.rate_limits.values(), default=None),
        # setup
        "service_tier": stats.service_tier,
        "speed": stats.speed,
        "permission_mode": stats.permission_mode,
        "tools_offered": stats.tools_offered,
        "mcp_servers": stats.mcp_servers,
        "models": sorted(stats.models),
        # what the money bought
        "cost_per_finding": _ratio(stats.cost_usd, findings),
        "cost_per_blocker": _ratio(stats.cost_usd, found.get("blockers", 0)),
        "findings_per_1k_output": _ratio(findings * 1000, stats.output_tokens),
        "tool_calls_per_finding": _ratio(stats.tool_calls, findings),
    }
    return out


def _gateway(env: dict) -> str:
    """Where the command's model calls went: the gateway host, or "default"."""
    url = env.get("ANTHROPIC_BASE_URL") or ""
    if not url:
        return "default"
    return urlparse(url).netloc or url


def _requested_model(command: str, env: dict) -> str:
    """The model the command asked for, if it named one (``--model`` or env)."""
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = []
    for i, token in enumerate(argv):
        if token == "--model" and i + 1 < len(argv):
            return argv[i + 1]
        if token.startswith("--model="):
            return token.split("=", 1)[1]
    return env.get("ANTHROPIC_MODEL", "")


def _elapsed(job: CommandJob) -> float | None:
    if job.ended_mono and job.started_mono:
        return max(0.0, job.ended_mono - job.started_mono)
    return None


def run_row(job: CommandJob, runner) -> dict:
    """The ``runs`` row for a job that has ended."""
    config = runner.config
    command = getattr(config, "command", "") or ""
    try:
        env = runner._child_env() if command else dict(os.environ)
    except Exception:  # noqa: BLE001 - a record without a gateway beats none
        env = {}
    output = (job.output or "")[:_MAX_OUTPUT]
    found = analyse(output) if output.strip() else {"findings": 0}
    stats = job.stats
    measured = metrics(stats, found, _elapsed(job))
    measured["timed_out"] = "timed out" in (job.error or "") or bool(job.out_of_time_mono)
    measured["extra_s_granted"] = job.extra_s
    measured["retried_steps"] = len(job.retried_spend)
    measured["requested_model"] = job.requested_model or _requested_model(command, env)
    measured["config_dir"] = os.path.basename(env.get("CLAUDE_CONFIG_DIR", "") or "")
    measured["steps"] = sorted(job.steps)
    return {
        "id": job.id,
        "parent_id": job.parent_id,
        "kind": job.kind,
        "label": getattr(config, "label", "") or job.kind,
        "source": "live",
        "project_id": job.project_id,
        "mr_iid": job.mr_iid,
        "build_number": job.build_number,
        "subject": job.subject,
        "head_sha": job.head_sha,
        "status": job.status,
        "error": (job.error or "")[:4000],
        "started_at": job.started_at,
        "ended_at": datetime.now(UTC).isoformat(),
        "model": stats.model,
        "gateway": _gateway(env) if command else ", ".join(
            sorted({_gateway(step._child_env()) for step in getattr(runner, "steps", {}).values()
                    if getattr(step.config, "command", "")})
        ),
        "cli_version": stats.cli_version,
        "command": command or " → ".join(
            " + ".join(stage) for stage in getattr(config, "pipeline", ()) or ()
        ),
        "session_id": job.session_id,
        "output": output,
        "findings": json.dumps(found),
        "metrics": json.dumps(measured),
        "stats": stats_to_json(stats),
    }


def recorder(db_path: str, runner):
    """The ``on_finish`` hook for one runner: write the run's record."""

    def record(job: CommandJob) -> None:
        row = run_row(job, runner)
        with Database(db_path) as db:
            db.save_run(row)

    return record


def _fingerprint(stats: dict) -> tuple | None:
    """What makes two records of numbers the same run: no two runs report the
    same output tokens, time on the model and wall time. None for a run that
    reported no numbers, which matches nothing."""
    key = tuple(stats.get(k) or 0 for k in ("output_tokens", "api_ms", "wall_ms"))
    return key if any(key) else None


def backfill(db: Database) -> int:
    """Seed ``runs`` from results saved before it existed; returns rows added.

    Idempotent: keyed by skill and MR, and never overwriting. A saved result
    for a skill and MR that a live run has finished since is skipped outright:
    the saved answer is that run's (or a later live run's), and it is already
    in the record under its own id — seeding it again would count the run
    twice. What was backfilled before that live run stays: it was an earlier
    run, and a real one.
    """
    before = db.conn.execute("SELECT count(*) FROM runs").fetchone()[0]
    recorded = {
        (row[0], row[1], row[2]) for row in db.conn.execute(
            "SELECT kind, project_id, mr_iid FROM runs "
            "WHERE source = 'live' AND status = 'done' AND mr_iid IS NOT NULL"
        )
    }
    saved_results = db.saved_results()
    # A pipeline step that stores its own result (a full review's QA plan) is
    # saved twice: as the plan, with its answer, and inside the pipeline's
    # numbers, without one. One run: the step's row takes the answer, and the
    # plan gets no row of its own.
    standalone: dict[tuple, dict] = {}
    for saved in saved_results:
        try:
            fp = _fingerprint(json.loads(saved.get("stats") or "{}"))
        except ValueError:
            fp = None
        if fp is not None:
            standalone[(saved["kind"], saved["project_id"], saved["mr_iid"], fp)] = saved
    as_step: dict[tuple, dict] = {}   # (kind, project, mr) of the plan → the plan
    for saved in saved_results:
        if (saved["kind"], saved["project_id"], saved["mr_iid"]) in recorded:
            continue
        stats = stats_from_json(saved.get("stats"))
        for step in stats.steps if isinstance(stats.steps, list) else ():
            if not isinstance(step, dict) or not step.get("name"):
                continue
            fp = _fingerprint(step.get("stats") or {})
            plan = standalone.get((step["name"], saved["project_id"], saved["mr_iid"], fp))
            if fp is None or plan is None or plan is saved:
                continue
            as_step[(step["name"], saved["project_id"], saved["mr_iid"])] = plan
    for saved in saved_results:
        if (saved["kind"], saved["project_id"], saved["mr_iid"]) in recorded:
            continue
        if (saved["kind"], saved["project_id"], saved["mr_iid"]) in as_step:
            continue
        stats = stats_from_json(saved.get("stats"))
        found = analyse(saved["content"])
        run_id = f"saved:{saved['kind']}:{saved['project_id']}:{saved['mr_iid']}"
        common = {
            "source": "saved result", "project_id": saved["project_id"],
            "mr_iid": saved["mr_iid"], "subject": f"!{saved['mr_iid']}",
            "status": "done", "ended_at": saved["generated_at"],
        }
        db.save_run({
            **common, "id": run_id, "kind": saved["kind"], "label": saved["kind"],
            "model": stats.model, "cli_version": stats.cli_version,
            "output": saved["content"], "findings": json.dumps(found),
            "metrics": json.dumps(metrics(stats, found)),
            "stats": saved.get("stats") or "",
        }, replace=False)
        # A pipeline kept its steps' numbers but not their answers: rows with
        # the numbers. What each step contributed is read from the synthesis's
        # "Raised by" at comparison time, as it is for live runs.
        steps = [step for step in (stats.steps if isinstance(stats.steps, list) else ())
                 if isinstance(step, dict) and step.get("name")]
        # A retried step appears once per attempt under one name, the earlier
        # ones first (see `PipelineRunner._roll_up`). The last is the attempt
        # whose answer the pipeline used, and it takes the plain id; the ones
        # it replaced are kept — they were paid for — under ids of their own.
        remaining = {}
        for step in steps:
            remaining[step["name"]] = remaining.get(step["name"], 0) + 1
        for step in steps:
            remaining[step["name"]] -= 1
            earlier = remaining[step["name"]]
            step_id = f"{run_id}:{step['name']}" + (f":earlier-{earlier}" if earlier else "")
            step_stats = stats_from_json(json.dumps(step.get("stats") or {}))
            plan = as_step.get((step["name"], saved["project_id"], saved["mr_iid"]))
            answer = plan["content"] if plan is not None and not earlier else ""
            step_found = analyse(answer) if answer else {"findings": 0, "answer_kept": False}
            step_metrics = metrics(step_stats, step_found, step.get("elapsed_s"))
            step_metrics["superseded"] = bool(earlier)
            db.save_run({
                **common, "id": step_id, "parent_id": run_id,
                "kind": step["name"], "label": step.get("label") or step["name"],
                "status": step.get("status") or "done",
                "model": step_stats.model, "cli_version": step_stats.cli_version,
                "session_id": step.get("session_id") or "",
                "output": answer,
                "findings": json.dumps(step_found),
                "metrics": json.dumps(step_metrics),
                "stats": json.dumps(step.get("stats") or {}),
            }, replace=False)
    _merge_saved_twice(db)
    return db.conn.execute("SELECT count(*) FROM runs").fetchone()[0] - before


def _merge_saved_twice(db: Database) -> None:
    """Repair what earlier backfills wrote before they knew about a step that
    saves its own result: the plan's row and the step's row are one run (same
    numbers), so the step keeps the answer and the plan's row goes."""
    rows = [dict(r) for r in db.conn.execute(
        "SELECT id, parent_id, kind, project_id, mr_iid, output, stats FROM runs "
        "WHERE source = 'saved result'")]
    plans = {(r["kind"], r["project_id"], r["mr_iid"]): r for r in rows
             if r["id"] == f"saved:{r['kind']}:{r['project_id']}:{r['mr_iid']}"}
    for step in rows:
        plan = plans.get((step["kind"], step["project_id"], step["mr_iid"]))
        if not step["parent_id"] or plan is None or plan is step:
            continue
        try:
            same = _fingerprint(json.loads(step["stats"] or "{}")) is not None and (
                _fingerprint(json.loads(step["stats"] or "{}"))
                == _fingerprint(json.loads(plan["stats"] or "{}")))
        except ValueError:
            continue
        if not same:
            continue
        if not (step["output"] or "").strip() and (plan["output"] or "").strip():
            db.conn.execute("UPDATE runs SET output = ?, findings = ? WHERE id = ?",
                            (plan["output"], json.dumps(analyse(plan["output"])), step["id"]))
        db.conn.execute("DELETE FROM runs WHERE id = ?", (plan["id"],))
    db.conn.commit()


def reanalyse(db: Database) -> int:
    """Count again every recorded answer an older parser counted; returns how
    many. The answers are kept whole, so a better parser improves the history
    too — and a comparison never mixes two ways of counting."""
    changed = 0
    # Only the rows that need it, not the table: every row carries its
    # whole answer, and this runs on every startup.
    stale = db.conn.execute(
        "SELECT id, output, findings, metrics, stats FROM runs "
        "WHERE coalesce(output, '') != '' AND (NOT json_valid(findings) "
        "OR coalesce(json_extract(findings, '$.parser'), 0) != ?)",
        (PARSER_VERSION,),
    ).fetchall()
    for row in map(dict, stale):
        output = row.get("output") or ""
        try:
            found = json.loads(row.get("findings") or "{}")
            measured = json.loads(row.get("metrics") or "{}")
        except ValueError:
            continue
        if not output.strip() or found.get("parser") == PARSER_VERSION:
            continue
        found = analyse(output)
        stats = stats_from_json(row.get("stats"))
        fresh = metrics(stats, found)
        # Only the ratios depend on the findings; everything else was measured
        # when the run ended and some of it (elapsed time) cannot be redone.
        for key in ("cost_per_finding", "cost_per_blocker", "findings_per_1k_output",
                    "tool_calls_per_finding"):
            measured[key] = fresh[key]
        db.conn.execute("UPDATE runs SET findings = ?, metrics = ? WHERE id = ?",
                        (json.dumps(found), json.dumps(measured), row["id"]))
        changed += 1
    db.conn.commit()
    return changed
