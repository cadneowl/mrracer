"""SLA rule matching and waiver evaluation."""

from __future__ import annotations

from radar.slas import is_waived_by_mr, match_sla


def test_branch_glob_is_case_sensitive(config):
    # release/* -> 4h rule; default -> 16h. Git branches are case-sensitive, so
    # 'Release/...' must NOT match the release rule (regression: fnmatch would
    # match case-insensitively on Windows).
    assert match_sla(config, "release/2026.7", []).first_response_business_hours == 4
    assert match_sla(config, "Release/2026.7", []).first_response_business_hours == 16


def test_label_rule_and_default(config):
    assert match_sla(config, "main", ["hotfix"]).first_response_business_hours == 4
    assert match_sla(config, "main", []).first_response_business_hours == 16


def test_first_match_wins(config):
    # A release branch that also has a hotfix label still hits the release rule
    # first (it is listed above hotfix), but both are 4h here.
    assert match_sla(config, "release/x", ["hotfix"]).approval_business_hours == 8


def test_waiver(config):
    assert is_waived_by_mr(config.waive, True, []) == "draft"
    assert is_waived_by_mr(config.waive, False, ["blocked"]) is not None
    assert is_waived_by_mr(config.waive, False, ["do-not-review"]) is not None
    assert is_waived_by_mr(config.waive, False, ["something-else"]) is None
    assert is_waived_by_mr(config.waive, False, []) is None
