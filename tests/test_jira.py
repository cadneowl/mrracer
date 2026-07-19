"""Jira key extraction and link building."""

from __future__ import annotations

from radar.jira import browse_url, extract_keys


def test_extract_from_multiple_sources_dedup_order():
    keys = extract_keys(
        ["PROJ-12: add widget", "feature/PROJ-12-widget", "relates to BUG-7 and PROJ-12"]
    )
    assert keys == ["PROJ-12", "BUG-7"]


def test_extract_none_and_empty_safe():
    assert extract_keys([None, "", "no keys here"]) == []


def test_project_key_filter():
    keys = extract_keys(
        ["PROJ-1 and OPS-9 and BUG-2"], project_keys=["PROJ", "BUG"]
    )
    assert keys == ["PROJ-1", "BUG-2"]  # OPS filtered out


def test_does_not_match_lowercase_or_plain_numbers():
    assert extract_keys(["proj-1", "issue 123", "v2-3"]) == []


def test_browse_url():
    assert browse_url("https://x.atlassian.net", "PROJ-1") == "https://x.atlassian.net/browse/PROJ-1"
    assert browse_url("https://x.atlassian.net/", "PROJ-1") == "https://x.atlassian.net/browse/PROJ-1"
    assert browse_url(None, "PROJ-1") is None
