#!/usr/bin/env python
"""A team's review numbers over time, mined from what radar already stores.

Three questions, one row per month (or week):

* **How long do reviews take?** Two clocks, because they measure different
  things. *Cycle* is an MR's first review request to its merge — what the
  author waits. *First response* is each reviewer's request to their first
  comment or approval — what the reviewer owes. Both in business hours on the
  calendar radar already uses for its SLAs, and computed by radar's own
  obligation logic (``derive_mr``), so they agree with the board and the coach
  page rather than approximating them.
* **What did the AI reviews find?** Blockers and should-fix items (the "high"
  tier) in every saved full review, and how many were verdicts of "Blocked".
* **How many tests did the QA plans propose?** Items under Blocking Gaps,
  Strong Recommendations and Probing in every saved QA test plan.

One limit to know before reading the AI columns: radar keeps the *latest*
result per MR per skill. Running a skill again on the same MR replaces the
earlier answer, so these count MRs analysed, not runs.

    scripts/team-stats.py                       # every team in config.yaml, by month
    scripts/team-stats.py --team my-team --by week
    scripts/team-stats.py --since 2026-07-01 --csv team.csv
    scripts/team-stats.py --everyone            # no team filter
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar.business_time import business_hours_between  # noqa: E402
from radar.config import load_config  # noqa: E402
from radar.db import Database  # noqa: E402
from radar.derive import KIND_REVIEW, derive_mr  # noqa: E402
from radar.findings import analyse  # noqa: E402

# --- reading the saved answers ----------------------------------------------


def parse_review(text: str) -> dict:
    found = analyse(text)
    return {"blockers": found["blockers"], "should_fix": found["high"],
            "consider": found["medium"], "blocked": found["blocked"]}


def parse_qa(text: str) -> dict:
    found = analyse(text)
    return {"blocking": found["blockers"], "strong": found["high"], "probing": found["medium"]}


# --- bucketing ----------------------------------------------------------------


def _when(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _period(at: datetime, by: str) -> str:
    if by == "week":
        year, week, _ = at.isocalendar()
        return f"{year}-W{week:02d}"
    return at.strftime("%Y-%m")


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


# --- the numbers ----------------------------------------------------------------


def collect(db: Database, config, members: set[str] | None, by: str, since: datetime | None):
    """Rows keyed by period; ``members`` None means everyone."""
    now = datetime.now(UTC)
    cal = config.calendar.calendar
    tz = config.calendar.tz_for(None)
    rows: dict[str, dict] = defaultdict(lambda: defaultdict(list))

    def keep(at: datetime | None) -> bool:
        return at is not None and (since is None or at >= since)

    snapshots = {(s["project_id"], s["mr_iid"]): s for s in db.all_snapshots()}
    for key, snap in snapshots.items():
        authored = members is None or (snap.get("author") or "") in members
        events = list(db.iter_events(*key))

        # Cycle: the author's wait, for MRs the team wrote.
        if authored:
            requested = next((e.occurred_at for e in events
                              if e.event_type == "review_requested"), None)
            merged = next((e.occurred_at for e in events if e.event_type == "mr_merged"), None)
            if requested and merged and merged > requested and keep(merged):
                row = rows[_period(merged, by)]
                row["cycle_h"].append(business_hours_between(requested, merged, cal, tz))
                row["cycle_days"].append((merged - requested).total_seconds() / 86400)

        # First response: the reviewer's side, for reviews the team was asked
        # for — whoever wrote the MR.
        for st in derive_mr(events, snap, config, now):
            if st.kind != KIND_REVIEW or st.first_response_hours is None:
                continue
            if members is not None and st.reviewer not in members:
                continue
            if keep(st.requested_at):
                rows[_period(st.requested_at, by)]["response_h"].append(st.first_response_hours)

    for plan in db.conn.execute(
        "SELECT project_id, mr_iid, kind, content, generated_at FROM test_plans"
    ):
        plan = dict(plan)
        snap = snapshots.get((plan["project_id"], plan["mr_iid"])) or {}
        if members is not None and (snap.get("author") or "") not in members:
            continue
        at = _when(plan["generated_at"])
        if not keep(at):
            continue
        row = rows[_period(at, by)]
        if plan["kind"] == "full-review":
            row["reviews"].append(parse_review(plan["content"]))
        elif plan["kind"] == "qa":
            row["plans"].append(parse_qa(plan["content"]))
    return rows


def summarise(rows: dict) -> list[dict]:
    out = []
    for period in sorted(rows):
        r = rows[period]
        reviews, plans = r["reviews"], r["plans"]
        found = {k: sum(x[k] for x in reviews) for k in ("blockers", "should_fix", "blocked")}
        tests = {k: sum(x[k] for x in plans) for k in ("blocking", "strong", "probing")}
        proposed = sum(tests.values())
        out.append({
            "period": period,
            "merged": len(r["cycle_h"]),
            "cycle_mean_h": _mean(r["cycle_h"]),
            "cycle_median_h": _median(r["cycle_h"]),
            "cycle_median_days": _median(r["cycle_days"]),
            "responses": len(r["response_h"]),
            "response_mean_h": _mean(r["response_h"]),
            "response_median_h": _median(r["response_h"]),
            "reviews": len(reviews),
            "blocked": found["blocked"],
            "blockers": found["blockers"],
            "high": found["should_fix"],
            "blockers_per_review": found["blockers"] / len(reviews) if reviews else None,
            "qa_plans": len(plans),
            "tests_proposed": proposed,
            "tests_blocking": tests["blocking"],
            "tests_strong": tests["strong"],
            "tests_probing": tests["probing"],
            "tests_per_plan": proposed / len(plans) if plans else None,
        })
    return out


# --- printing ---------------------------------------------------------------------

_COLUMNS = [
    ("period", "period"),
    ("merged", "MRs merged"),
    ("cycle_median_h", "cycle median (bh)"),
    ("cycle_mean_h", "cycle mean (bh)"),
    ("cycle_median_days", "cycle median (days)"),
    ("response_median_h", "1st response median (bh)"),
    ("response_mean_h", "1st response mean (bh)"),
    ("reviews", "AI reviews"),
    ("blocked", "verdict Blocked"),
    ("blockers", "blockers"),
    ("high", "high (should fix)"),
    ("qa_plans", "QA plans"),
    ("tests_proposed", "tests proposed"),
    ("tests_blocking", "of which blocking"),
]


def _cell(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def print_table(title: str, summary: list[dict]) -> None:
    print(f"\n## {title}\n")
    print("| " + " | ".join(label for _, label in _COLUMNS) + " |")
    print("|" + "|".join("---" for _ in _COLUMNS) + "|")
    for row in summary:
        print("| " + " | ".join(_cell(row[key]) for key, _ in _COLUMNS) + " |")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--team", help="one team from config.yaml (default: each of them)")
    parser.add_argument("--everyone", action="store_true", help="ignore teams: every MR")
    parser.add_argument("--by", choices=("month", "week"), default="month")
    parser.add_argument("--since", help="YYYY-MM-DD")
    parser.add_argument("--csv", help="also write the rows here")
    args = parser.parse_args()

    config = load_config(args.config)
    since = _when(args.since) if args.since else None
    if args.since and since is None:
        parser.error(f"--since {args.since!r}: expected YYYY-MM-DD")
    if args.everyone:
        groups = [("everyone", None)]
    else:
        teams = [t for t in config.teams if args.team in (None, t.name)]
        if not teams:
            parser.error(f"no team named {args.team!r} in {args.config}")
        groups = [(t.name, set(t.members)) for t in teams]

    written = []
    with Database(config.database_path) as db:
        for name, members in groups:
            summary = summarise(collect(db, config, members, args.by, since))
            print_table(name, summary)
            written += [{"team": name, **row} for row in summary]
    print(
        "\nbh = business hours on radar's SLA calendar. Cycle: first review request "
        "→ merge, MRs the team authored, by merge date. 1st response: review "
        "request → the reviewer's first comment or approval, reviews the team was "
        "asked for. AI columns: the latest saved result per MR, by the date it ran."
    )
    if args.csv and written:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            out = csv.DictWriter(fh, fieldnames=list(written[0]))
            out.writeheader()
            out.writerows(written)
        print(f"wrote {len(written)} rows to {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
