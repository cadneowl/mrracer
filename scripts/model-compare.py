#!/usr/bin/env python
"""Compare the models radar's skills ran on, from the run record.

Reads the ``runs`` table (see ``radar/runlog.py``): every run of every skill,
and every step of every pipeline, with the model that answered, what it found
and what it cost. One table per skill, one row per model:

* **reliability** — runs, how many finished, timed out (live runs only), hit
  provider errors;
* **cost and speed** — money, wall time, time to first token, output tokens per
  second, requests and tool calls per run, cache hit rate, context reached;
* **what it found** — findings per run by severity (blockers / high / medium /
  low), how often the verdict was "Blocked", tests a QA plan proposed, how many
  files and ``file:line`` locations it cites, how often it hedges;
* **value** — cost per finding and per blocker, findings per 1k output tokens;
* **survival** — for the steps of a full review, how many of the step's
  findings the synthesis kept and credited to it ("Raised by"). The one
  measure here that is not the model's own word about itself.

Then a **head-to-head**: every merge request that the same skill ran on with
more than one model, side by side, so a difference is not just a difference in
which MRs each model happened to get.

Medians unless the column says otherwise — a single forty-minute run should not
decide which model is slower. Ratios are totals over totals.

    scripts/model-compare.py
    scripts/model-compare.py --kind full-review --kind review
    scripts/model-compare.py --since 2026-09-01 --csv runs.csv
    scripts/model-compare.py --by gateway          # model + gateway

It writes before it reads: like radar at startup, it seeds the record from
older saved results and recounts answers an older parser counted. Both are
idempotent.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar import runlog  # noqa: E402
from radar.config import load_config  # noqa: E402
from radar.db import Database  # noqa: E402


def _load(row: dict) -> dict:
    """One run, its JSON columns read and merged into one flat record."""
    out = dict(row)
    for column in ("findings", "metrics"):
        try:
            value = json.loads(row.get(column) or "{}")
        except ValueError:
            value = {}
        out[column] = value if isinstance(value, dict) else {}
    return out


def _num(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _median(values) -> float | None:
    values = [v for v in (_num(x) for x in values) if v is not None]
    return statistics.median(values) if values else None


def _mean(values) -> float | None:
    values = [v for v in (_num(x) for x in values) if v is not None]
    return statistics.fmean(values) if values else None


def _share(flags) -> float | None:
    flags = list(flags)
    return sum(1 for f in flags if f) / len(flags) if flags else None


def _survival(runs: list[dict]) -> dict[str, dict]:
    """Per step run id: what the synthesis of its pipeline credited to it."""
    by_parent: dict[str, list[dict]] = defaultdict(list)
    for run in runs:
        if run["parent_id"]:
            by_parent[run["parent_id"]].append(run)
    # Only steps a synthesis ever names as a source can be credited: the
    # synthesis itself is a step too, and "kept none of its own" says nothing.
    sources = {name for run in runs for name in run["findings"].get("raised_by") or {}}
    credit: dict[str, dict] = {}
    for run in runs:
        raised = run["findings"].get("raised_by")
        if not raised:
            continue
        # A retried step leaves one row per attempt under one label. The
        # synthesis read the last of them; the attempts it replaced found
        # nothing it kept, and crediting them too would count one step twice.
        latest: dict[str, dict] = {}
        for step in by_parent.get(run["id"], ()):
            if step["metrics"].get("superseded"):
                continue
            if step["ended_at"] >= latest.get(step["label"], {}).get("ended_at", ""):
                latest[step["label"]] = step
        for label, step in latest.items():
            if label in sources:
                credit[step["id"]] = raised.get(label) or {
                    "blockers": 0, "high": 0, "findings": 0}
    return credit


def _blocked(answered: list[dict]) -> str | None:
    """"13 of 13 (100%)": of the answers that gave a verdict, how many said
    Blocked. The count comes first because a percentage of four is not a rate."""
    verdicts = [x for x in answered if x.get("verdict")]
    if not verdicts:
        return None
    blocked = sum(1 for x in verdicts if x.get("blocked"))
    return f"{blocked} of {len(verdicts)} ({blocked * 100 // len(verdicts)}%)"


def summarise(group: list[dict], credit: dict[str, dict]) -> dict:
    everyone = [r["metrics"] for r in group]
    # Numbers only from runs that reported numbers (see `metrics.measured`).
    m = [x for x in everyone if x.get("measured", True) and (
        x.get("requests") or x.get("cost_usd") or x.get("output_tokens"))]
    done = [r for r in group if r["status"] == "done"]
    answered = [r["findings"] for r in done if r["findings"].get("chars")]
    # Value is money over what that money found: both from the same runs.
    paid = [r for r in done if r["findings"].get("chars") and r["metrics"] in m]
    cost = sum(_num(r["metrics"].get("cost_usd")) or 0 for r in paid)
    found = sum(r["findings"].get("findings", 0) for r in paid)
    blockers = sum(r["findings"].get("blockers", 0) for r in paid)
    output_tokens = sum(_num(r["metrics"].get("output_tokens")) or 0 for r in paid)
    credited = [credit[r["id"]] for r in group if r["id"] in credit]
    return {
        "runs": len(group),
        "mrs": len({(r["project_id"], r["mr_iid"]) for r in group if r["mr_iid"] is not None}),
        # Only live runs can say whether a run failed: a backfilled row is a
        # saved answer, so it always "finished" and never "timed out".
        "finished": _share(r["status"] == "done" for r in group if r["source"] == "live"),
        "timed_out": _share(r["metrics"].get("timed_out") for r in group
                            if r["source"] == "live"),
        "provider_errors": _share((x.get("api_errors") or 0) > 0 for x in m),
        "denied_calls": _mean(x.get("denied_calls") for x in m),
        # cost and speed
        "cost": _median(x.get("cost_usd") for x in m),
        "cost_total": sum(_num(x.get("cost_usd")) or 0 for x in m) if m else None,
        "minutes": (_median(x.get("elapsed_s") or (x.get("wall_ms") or 0) / 1000 or None
                            for x in m) or 0) / 60 or None,
        "ttft_s": _median(x.get("ttft_s") for x in m),
        "tok_per_s": _median(x.get("tokens_per_s") for x in m),
        "requests": _median(x.get("requests") for x in m),
        "tool_calls": _median(x.get("tool_calls") for x in m),
        "output_k": (_median(x.get("output_tokens") for x in m) or 0) / 1000 or None,
        "thinking_k": (_median(x.get("thinking_tokens") for x in m) or 0) / 1000 or None,
        "cache_hit": _median(x.get("cache_hit_rate") for x in m),
        "peak_context_k": (_median(x.get("peak_context_tokens") for x in m) or 0) / 1000 or None,
        "model_share": _median(x.get("model_share") for x in m),
        # what it found
        "answers": len(answered),
        "findings": _mean(x.get("findings") for x in answered),
        "blockers": _mean(x.get("blockers") for x in answered),
        "high": _mean(x.get("high") for x in answered),
        "medium": _mean(x.get("medium") for x in answered),
        "low": _mean(x.get("low") for x in answered),
        "blocked_verdict": _blocked(answered),
        "tests": _mean(x.get("tests_proposed") for x in answered) if any(
            x.get("shape") == "qa" for x in answered) else None,
        "files_cited": _mean(x.get("files_cited") for x in answered),
        "citations": _mean(x.get("citations") for x in answered),
        "hedges": _mean(x.get("verify_hedges") for x in answered),
        "words": _median(x.get("words") for x in answered),
        # value
        "cost_per_finding": cost / found if found else None,
        "cost_per_blocker": cost / blockers if blockers else None,
        "findings_per_1k_out": found * 1000 / output_tokens if output_tokens and found else None,
        # survival
        "kept_findings": _mean(c.get("findings", 0) for c in credited) if credited else None,
        "kept_blockers": _mean(c.get("blockers", 0) for c in credited) if credited else None,
    }


_TABLES = [
    ("Reliability", [
        ("runs", "runs", "d"), ("mrs", "MRs reviewed", "d"), ("finished", "finished", "%"),
        ("timed_out", "timed out", "%"),
        ("provider_errors", "provider errors", "%"), ("denied_calls", "denied calls (mean)", ".1f"),
    ]),
    ("Cost and speed", [
        ("cost", "cost $", ".2f"), ("cost_total", "total $", ".2f"),
        ("minutes", "minutes", ".1f"), ("ttft_s", "TTFT s", ".1f"),
        ("tok_per_s", "tok/s", ".0f"), ("requests", "requests", ".0f"),
        ("tool_calls", "tool calls", ".0f"), ("output_k", "output k", ".1f"),
        ("thinking_k", "thinking k", ".1f"), ("cache_hit", "cache hit", "%"),
        ("peak_context_k", "peak context k", ".0f"), ("model_share", "model time", "%"),
    ]),
    ("What it found (mean per answer)", [
        ("answers", "answers", "d"), ("findings", "findings", ".1f"),
        ("blockers", "blockers", ".1f"), ("high", "high", ".1f"), ("medium", "medium", ".1f"),
        ("low", "low", ".1f"), ("blocked_verdict", "verdict Blocked", "s"),
        ("tests", "tests proposed", ".1f"), ("files_cited", "files cited", ".1f"),
        ("citations", "file:line cites", ".1f"), ("hedges", "\"verify\" hedges", ".1f"),
        ("words", "words (median)", ".0f"),
    ]),
    ("Value and survival", [
        ("cost_per_finding", "$ / finding", ".2f"), ("cost_per_blocker", "$ / blocker", ".2f"),
        ("findings_per_1k_out", "findings / 1k out", ".2f"),
        ("kept_findings", "kept by synthesis", ".1f"), ("kept_blockers", "kept blockers", ".1f"),
    ]),
]


# What each table's less obvious columns mean, printed under it: the report is
# read by people who did not write it, and a bare "50%" answers nothing.
_LEGENDS = {
    "Reliability": [
        "runs: every run of this skill on this model, including re-runs of the same MR",
        "MRs reviewed: distinct merge requests those runs were about",
        "finished, timed out: of the live runs only (a backfilled row is a saved answer, "
        "so it always finished)",
        "provider errors: runs where the model provider refused at least one call",
    ],
    "Cost and speed": [
        "medians per run, except total $",
        "TTFT: time to first token, as the CLI reported it",
        "tok/s: output tokens per second of model time (TTFT included, tools not)",
        "model time: share of the run spent waiting on the model rather than on tools",
    ],
    "What it found (mean per answer)": [
        "answers: finished runs whose answer was kept and could be counted",
        "findings = blockers + high + medium + low, read from the answer's own sections",
        "verdict Blocked: of the answers that gave a verdict, how many said "
        "\"Blocked\" — i.e. told the author not to merge as it stands",
        "file:line cites: code locations the answer points at; \"verify\" hedges: "
        "places it says it could not check something",
    ],
    "Value and survival": [
        "$ / finding and $ / blocker: total cost over total findings of the same runs",
        "kept by synthesis: of a full-review step's findings, how many the final "
        "summary kept and credited to it (\"Raised by\") — the one measure that is "
        "not the model grading itself",
    ],
}


def _cell(value, fmt: str) -> str:
    if value is None:
        return "—"
    if fmt == "s":
        return str(value)
    if fmt == "%":
        return f"{value * 100:.0f}%"
    if fmt == "d":
        return str(int(value))
    return format(value, fmt)


def print_kind(kind: str, groups: dict[str, dict]) -> None:
    print(f"\n## {kind}\n")
    for title, columns in _TABLES:
        print(f"**{title}**\n")
        print("| model | " + " | ".join(label for _, label, _ in columns) + " |")
        print("|---|" + "|".join("---:" for _ in columns) + "|")
        for name, s in sorted(groups.items(), key=lambda kv: -kv[1]["runs"]):
            print(f"| {name} | " + " | ".join(_cell(s[k], fmt) for k, _, fmt in columns) + " |")
        print()
        for line in _LEGENDS.get(title, ()):
            print(f"- {line}")
        print()


def head_to_head(runs: list[dict], key) -> None:
    """Every (skill, MR) that more than one model answered."""
    seen: dict[tuple, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for run in runs:
        # A failed run keeps its error text, so "has output" is not "answered".
        if run["mr_iid"] is not None and run["status"] == "done" and run["findings"].get("chars"):
            seen[(run["kind"], run["project_id"], run["mr_iid"])][key(run)].append(run)
    pairs = {k: v for k, v in seen.items() if len(v) > 1}
    print("\n## Head to head — the same MR, more than one model\n")
    if not pairs:
        print("No merge request has been answered by more than one model yet. Run a skill "
              "on an MR with one model, switch the model, and run it again.\n")
        return
    print("| skill | MR | model | commit | findings | blockers | high | tests | $ | minutes |")
    print("|---|---|---|---|---:|---:|---:|---:|---:|---:|")
    for (kind, _project, mr), by_model in sorted(pairs.items()):
        for name, group in sorted(by_model.items()):
            run = group[-1]   # the latest answer from this model
            f, m = run["findings"], run["metrics"]
            minutes = (m.get("elapsed_s") or (m.get("wall_ms") or 0) / 1000) / 60
            print(f"| {kind} | !{mr} | {name} | {(run['head_sha'] or '')[:8] or '?'} | "
                  f"{f.get('findings', 0)} | {f.get('blockers', 0)} | {f.get('high', 0)} | "
                  f"{f.get('tests_proposed', 0) or '—'} | {m.get('cost_usd') or 0:.2f} | "
                  f"{minutes:.1f} |")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--db", help="database file (default: the one config.yaml names)")
    parser.add_argument("--kind", action="append", help="only these skills (repeatable)")
    parser.add_argument("--since", help="YYYY-MM-DD")
    parser.add_argument("--by", choices=("model", "gateway"), default="model",
                        help="group by model, or by model and gateway")
    parser.add_argument("--live-only", action="store_true",
                        help="leave out rows backfilled from saved results")
    parser.add_argument("--csv", help="also write every run, flattened, here")
    args = parser.parse_args()

    since = None
    if args.since:
        try:
            since = date.fromisoformat(args.since).isoformat()
        except ValueError:
            parser.error(f"--since {args.since!r}: expected YYYY-MM-DD")
    db_path = args.db or load_config(args.config).database_path
    with Database(db_path) as db:
        # The same two idempotent passes radar makes at startup, so a database
        # the server has not opened since an upgrade is read the same way.
        runlog.backfill(db)
        runlog.reanalyse(db)
        runs = [_load(r) for r in db.runs(since)]
    if args.live_only:
        runs = [r for r in runs if r["source"] == "live"]
    wanted = set(args.kind) if args.kind else None
    if wanted:
        runs = [r for r in runs if r["kind"] in wanted]

    def key(run: dict) -> str:
        name = run["model"] or "(model not reported)"
        return f"{name} @ {run['gateway'] or '?'}" if args.by == "gateway" else name

    credit = _survival(runs)
    by_kind: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for run in runs:
        if wanted is None or run["kind"] in wanted:
            by_kind[run["kind"]][key(run)].append(run)

    print("# Model comparison")
    print(f"\n{len(runs)} runs recorded; "
          f"{sum(1 for r in runs if r['source'] == 'live')} live, "
          f"{sum(1 for r in runs if r['source'] != 'live')} backfilled from saved results "
          "(the latest answer per MR, and a full review's steps without their answers).")
    covered: dict[str, set] = defaultdict(set)
    for run in runs:
        if run["mr_iid"] is not None and (wanted is None or run["kind"] in wanted):
            covered[key(run)].add((run["project_id"], run["mr_iid"]))
    if covered:
        print("\n| model | MRs reviewed (any skill) |\n|---|---:|")
        for name, mrs in sorted(covered.items(), key=lambda kv: -len(kv[1])):
            print(f"| {name} | {len(mrs)} |")
    for kind in sorted(by_kind):
        groups = {name: summarise(group, credit) for name, group in by_kind[kind].items()}
        print_kind(kind, groups)
    head_to_head([r for r in runs if wanted is None or r["kind"] in wanted], key)

    if args.csv:
        fields: list[str] = []
        flat = []
        for run in runs:
            row = {k: run[k] for k in ("id", "parent_id", "kind", "label", "source", "project_id",
                                       "mr_iid", "head_sha", "status", "started_at", "ended_at",
                                       "model", "gateway", "cli_version")}
            for prefix in ("findings", "metrics"):
                for k, v in run[prefix].items():
                    if not isinstance(v, (dict, list)):
                        row[f"{prefix}.{k}"] = v
            flat.append(row)
            fields += [k for k in row if k not in fields]
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            out = csv.DictWriter(fh, fieldnames=fields)
            out.writeheader()
            out.writerows(flat)
        print(f"wrote {len(flat)} runs to {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
