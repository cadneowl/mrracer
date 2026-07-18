"""SLA rule matching and waiver evaluation — pure functions over config."""

from __future__ import annotations

import fnmatch

from .config import Config, SLARule, WaiveConfig


def match_sla(config: Config, target_branch: str | None, labels: list[str]) -> SLARule:
    """Return the first SLA rule that matches, top to bottom.

    A rule matches when its target_branch glob (if set) matches and all of its
    required labels (if any) are present. The default rule (match: {}) always
    matches and is validated to be last.
    """
    label_set = set(labels)
    for rule in config.slas:
        m = rule.match
        if m.target_branch is not None and not fnmatch.fnmatch(
            target_branch or "", m.target_branch
        ):
            continue
        if m.labels and not set(m.labels).issubset(label_set):
            continue
        return rule
    return config.slas[-1]  # guaranteed default by config validation


def is_waived_by_mr(waive: WaiveConfig, draft: bool, labels: list[str]) -> str | None:
    """Return a waiver reason if the MR itself waives its obligations, else None."""
    if waive.draft and draft:
        return "draft"
    overlap = set(waive.labels) & set(labels)
    if overlap:
        return f"label:{sorted(overlap)[0]}"
    return None
