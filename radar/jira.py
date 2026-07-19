"""Jira issue-key extraction from merge-request metadata.

Each MR is associated with one or more Jira issues (a bug or an epic). We
recover the keys from the branch name, title, and description with the standard
``PROJ-123`` pattern, optionally filtered to a set of project keys from config.
The keys are shown as links on the board and passed to the QA test-plan command.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_KEY = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


def extract_keys(
    texts: Iterable[str | None], project_keys: Iterable[str] | None = None
) -> list[str]:
    """Return de-duplicated Jira keys found across ``texts`` (order preserved).

    If ``project_keys`` is given, only keys with those project prefixes are
    kept (case-insensitive).
    """
    allow = {p.upper() for p in project_keys} if project_keys else None
    seen: list[str] = []
    for text in texts:
        if not text:
            continue
        for key in _KEY.findall(text):
            prefix = key.split("-", 1)[0]
            if allow is not None and prefix not in allow:
                continue
            if key not in seen:
                seen.append(key)
    return seen


def browse_url(base_url: str | None, key: str) -> str | None:
    """Build a Jira browse URL for a key, or None if no base_url is configured."""
    if not base_url:
        return None
    return f"{base_url.rstrip('/')}/browse/{key}"
