"""The coach view: per-reviewer breach detail and chronic patterns.

This is the manager-oriented counterpart to the main board. Per the project's
"no surveillance" principle, individual breach lists live *only* here (behind an
unlinked/obscure URL), never on the shared board.

Everything is derived live from the event log — current open breaches from open
MRs, and historical compliance from resolved obligations — so no separate stats
store is required. Waived obligations are excluded (as everywhere).
"""

from __future__ import annotations

from datetime import UTC, datetime

from .config import Config
from .db import Database
from .derive import CHIP_AT_RISK, CHIP_BREACHED, CHIP_WAIVED, derive_mr

# A reviewer is flagged "chronic" with enough volume and a high breach rate.
_CHRONIC_MIN_BREACHES = 3
_CHRONIC_MIN_RATE = 1 / 3


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 1)
    k = (len(ordered) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    interp = ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)
    return round(interp, 1)


def _new_reviewer(username: str) -> dict:
    return {
        "username": username,
        "open_breaches": [],
        "open_at_risk": 0,
        "open_load": 0,
        "resolved": 0,
        "resolved_within": 0,
        "breach_total": 0,
        "_fr_times": [],
    }


def build_coach(db: Database, config: Config, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    reviewers: dict[str, dict] = {}

    for snap in db.all_snapshots():
        events = list(db.iter_events(snap["project_id"], snap["mr_iid"]))
        is_open = snap.get("state") == "opened"
        for st in derive_mr(events, snap, config, now):
            if st.chip_state == CHIP_WAIVED:
                continue  # waived obligations are excluded from stats
            r = reviewers.setdefault(st.reviewer, _new_reviewer(st.reviewer))
            if st.first_response_hours is not None:
                r["_fr_times"].append(st.first_response_hours)

            if st.phase == "resolved":
                r["resolved"] += 1
                if st.within_sla:
                    r["resolved_within"] += 1
                else:
                    r["breach_total"] += 1
            elif is_open and st.phase in ("first_response", "approval"):
                r["open_load"] += 1
                if st.chip_state == CHIP_BREACHED:
                    r["breach_total"] += 1
                    r["open_breaches"].append(
                        {
                            "mr_iid": st.mr_iid,
                            "title": snap["title"],
                            "web_url": snap.get("web_url"),
                            "overdue_hours": round(-st.remaining_hours, 1),
                            "phase": st.phase,
                        }
                    )
                elif st.chip_state == CHIP_AT_RISK:
                    r["open_at_risk"] += 1

    rows = [_finalize(r) for r in reviewers.values()]
    rows.sort(key=lambda r: (-len(r["open_breaches"]), -r["breach_total"], r["username"]))

    team = {
        "reviewers": len(rows),
        "open_breaches": sum(len(r["open_breaches"]) for r in rows),
        "open_at_risk": sum(r["open_at_risk"] for r in rows),
        "chronic": sum(1 for r in rows if r["chronic"]),
    }
    total_resolved = sum(r["resolved"] for r in rows)
    total_within = sum(r["resolved_within"] for r in rows)
    team["compliance_pct"] = round(100 * total_within / total_resolved) if total_resolved else None

    return {"reviewers": rows, "team": team, "generated_at": now}


def _finalize(r: dict) -> dict:
    fr = r.pop("_fr_times")
    r["open_breach_count"] = len(r["open_breaches"])
    r["median_first_response"] = _percentile(fr, 0.5)
    r["p90_first_response"] = _percentile(fr, 0.9)
    r["compliance_pct"] = (
        round(100 * r["resolved_within"] / r["resolved"]) if r["resolved"] else None
    )
    considered = r["resolved"] + r["open_breach_count"]
    rate = r["breach_total"] / considered if considered else 0.0
    r["breach_rate_pct"] = round(100 * rate)
    r["chronic"] = r["breach_total"] >= _CHRONIC_MIN_BREACHES and rate >= _CHRONIC_MIN_RATE
    return r
