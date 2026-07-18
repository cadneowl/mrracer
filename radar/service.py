"""Read-side services: assemble the dashboard and run recompute.

These sit above the repository and derivation layers and produce
template-ready view data.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from .config import Config
from .db import Database
from .derive import (
    CHIP_AT_RISK,
    CHIP_BREACHED,
    CHIP_IN_SLA,
    CHIP_PENDING,
    CHIP_WAIVED,
    ObligationState,
    derive_mr,
)

_CHIP_ORDER = [CHIP_BREACHED, CHIP_AT_RISK, CHIP_IN_SLA, CHIP_PENDING, CHIP_WAIVED]


def _fmt_hours(hours: float) -> str:
    if hours >= 24:
        return f"{hours / 8:.1f} business-days"  # 8h workday-ish, display only
    return f"{hours:.1f}h"


def _remaining_label(o: ObligationState) -> str:
    if o.chip_state == CHIP_WAIVED:
        return "—"
    if o.phase == "resolved":
        return "done"
    if o.paused:
        return "paused"
    if o.remaining_hours >= 0:
        return f"{_fmt_hours(o.remaining_hours)} left"
    return f"{_fmt_hours(-o.remaining_hours)} over"


def _wall_age(created_at: str | None, now: datetime) -> str:
    if not created_at:
        return "—"
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return "—"
    delta = now - created.astimezone(UTC)
    days = delta.days
    hours = delta.seconds // 3600
    if days > 0:
        return f"{days}d {hours}h"
    minutes = (delta.seconds % 3600) // 60
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def _obligation_view(o: ObligationState) -> dict:
    return {
        "reviewer": o.reviewer,
        "round": o.round,
        "chip_state": o.chip_state,
        "status_text": o.status_text,
        "phase": o.phase,
        "remaining_label": _remaining_label(o),
        "budget_hours": o.budget_hours,
        "elapsed_hours": o.elapsed_hours,
        "remaining_hours": o.remaining_hours,
        "fraction_pct": min(100, round(o.fraction * 100)) if math.isfinite(o.fraction) else 100,
        "paused": o.paused,
        "reviewer_tz": o.reviewer_tz,
        "thread_count": o.thread_count,
        "urgency": o.urgency,
    }


def build_dashboard(db: Database, config: Config, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    rows = []
    summary = {chip: 0 for chip in _CHIP_ORDER}

    for snap in db.open_snapshots():
        events = list(db.iter_events(snap["project_id"], snap["mr_iid"]))
        obligations = derive_mr(events, snap, config, now)
        if not obligations:
            continue
        views = [_obligation_view(o) for o in obligations]
        for v in views:
            summary[v["chip_state"]] = summary.get(v["chip_state"], 0) + 1
        min_urgency = min((o.urgency for o in obligations), default=math.inf)
        rows.append(
            {
                "project_id": snap["project_id"],
                "mr_iid": snap["mr_iid"],
                "title": snap["title"],
                "web_url": snap["web_url"],
                "author": snap["author"],
                "target_branch": snap["target_branch"],
                "labels": snap["labels"],
                "draft": snap["draft"],
                "age": _wall_age(snap.get("created_at"), now),
                "obligations": views,
                "min_urgency": min_urgency,
            }
        )

    rows.sort(key=lambda r: r["min_urgency"])
    return {
        "rows": rows,
        "summary": summary,
        "breached": summary.get(CHIP_BREACHED, 0),
        "at_risk": summary.get(CHIP_AT_RISK, 0),
        "open_mrs": len(rows),
        "generated_at": now,
    }


def recompute(db: Database, config: Config, now: datetime | None = None) -> dict:
    """Re-derive every obligation from the event log and persist a snapshot."""
    now = now or datetime.now(UTC)
    records: list[dict] = []
    summary = {chip: 0 for chip in _CHIP_ORDER}
    for snap in db.all_snapshots():
        events = list(db.iter_events(snap["project_id"], snap["mr_iid"]))
        for o in derive_mr(events, snap, config, now):
            records.append(o.to_record())
            summary[o.chip_state] = summary.get(o.chip_state, 0) + 1
    count = db.replace_obligations(records)
    return {"obligations": count, "summary": summary}
