"""Poller idempotency and reconciliation, using fixture GitLab payloads.

These never touch a real GitLab; FixtureSource serves recorded-shape JSON.
"""

from __future__ import annotations

import copy

from radar.db import Database
from radar.events import EventType as ET
from radar.gitlab_client import FixtureSource
from radar.poller import poll_once
from radar.service import build_dashboard

PROJECT = "group/hub-backend"
PID = 101


def _sysnote(note_id, author, body, at):
    return {
        "id": note_id,
        "system": True,
        "author": {"username": author},
        "created_at": at,
        "body": body,
    }


def _diff_thread(note_id, author, body, at):
    return {
        "id": f"disc-{note_id}",
        "individual_note": False,
        "notes": [
            {
                "id": note_id,
                "system": False,
                "author": {"username": author},
                "created_at": at,
                "body": body,
                "position": {"new_path": "a.py", "new_line": 10},
                "resolvable": True,
            }
        ],
    }


def _mr(reviewers, updated_at="2026-03-02T11:05:00Z", state="opened"):
    return {
        "project_id": PID,
        "iid": 1,
        "title": "Add widget",
        "author": {"username": "aviva"},
        "web_url": "https://gitlab.example.com/group/hub-backend/-/merge_requests/1",
        "source_branch": "feature/widget",
        "target_branch": "main",
        "labels": [],
        "draft": False,
        "state": state,
        "reviewers": [{"username": r} for r in reviewers],
        "created_at": "2026-03-02T09:00:00Z",
        "updated_at": updated_at,
    }


def _discussions():
    return [
        {
            "id": "d1",
            "individual_note": True,
            "notes": [
                _sysnote(1001, "aviva", "requested review from @dan", "2026-03-02T09:05:00Z")
            ],
        },
        _diff_thread(1002, "dan", "please rename this", "2026-03-02T11:00:00Z"),
    ]


def _source(reviewers=("dan",), updated_at="2026-03-02T11:05:00Z"):
    return FixtureSource(
        mrs_by_project={PROJECT: [_mr(list(reviewers), updated_at)]},
        discussions_by_mr={(PID, 1): _discussions()},
    )


def test_poll_emits_events(config, tmp_path):
    db = Database(tmp_path / "r.db")
    result = poll_once(db, config, _source())
    assert result.mrs_seen == 1
    assert result.new_events > 0
    types = {e.event_type for e in db.iter_events(PID, 1)}
    assert ET.REVIEW_REQUESTED in types
    assert ET.NOTE_ADDED in types
    # snapshot stored
    snap = db.get_snapshot(PID, 1)
    assert snap["author"] == "aviva"
    db.close()


def test_poll_is_idempotent_on_repeat(config, tmp_path):
    db = Database(tmp_path / "r.db")
    poll_once(db, config, _source())
    count_after_first = db.event_count()

    # Re-poll with the same data but a *newer* updated_at so the MR is fetched
    # again (defeating the updated_after skip) — dedup must still add nothing.
    r2 = poll_once(db, config, _source(updated_at="2026-03-02T12:00:00Z"))
    assert r2.new_events == 0
    assert db.event_count() == count_after_first
    db.close()


def test_updated_after_skips_unchanged(config, tmp_path):
    db = Database(tmp_path / "r.db")
    poll_once(db, config, _source())
    # Same updated_at -> FixtureSource filters it out -> nothing seen.
    r2 = poll_once(db, config, _source())
    assert r2.mrs_seen == 0
    assert r2.new_events == 0
    db.close()


def test_reviewer_reconciliation(config, tmp_path):
    db = Database(tmp_path / "r.db")
    # maya is a current reviewer but has no "requested review" system note.
    poll_once(db, config, _source(reviewers=("dan", "maya")))
    reqs = [
        e
        for e in db.iter_events(PID, 1)
        if e.event_type == ET.REVIEW_REQUESTED and e.reviewer == "maya"
    ]
    assert len(reqs) == 1
    assert reqs[0].payload.get("source") == "reviewer_snapshot"

    # Re-poll: no duplicate synthetic event.
    poll_once(db, config, _source(reviewers=("dan", "maya"), updated_at="2026-03-02T12:00:00Z"))
    reqs = [
        e
        for e in db.iter_events(PID, 1)
        if e.event_type == ET.REVIEW_REQUESTED and e.reviewer == "maya"
    ]
    assert len(reqs) == 1
    db.close()


def test_merged_mr_resolves_and_leaves_board(config, tmp_path):
    db = Database(tmp_path / "r.db")
    # First poll: MR is open (sets the high-water mark).
    poll_once(db, config, _source())
    assert build_dashboard(db, config)["open_mrs"] == 1

    # Next poll sees it merged (state='all' fetch): a terminal event is emitted,
    # the snapshot flips to 'merged', and it drops off the board.
    merged = FixtureSource(
        mrs_by_project={PROJECT: [_mr(["dan"], updated_at="2026-03-03T09:00:00Z", state="merged")]},
        discussions_by_mr={(PID, 1): _discussions()},
    )
    poll_once(db, config, merged)

    assert db.get_snapshot(PID, 1)["state"] == "merged"
    types = {e.event_type for e in db.iter_events(PID, 1)}
    assert ET.MR_MERGED in types
    assert build_dashboard(db, config)["open_mrs"] == 0
    db.close()


def test_dashboard_integration(config, tmp_path):
    db = Database(tmp_path / "r.db")
    poll_once(db, config, _source())
    data = build_dashboard(db, config)
    assert data["open_mrs"] == 1
    row = data["rows"][0]
    assert row["mr_iid"] == 1
    # dan opened a diff thread -> first response given -> approval phase, paused
    dan = next(o for o in row["obligations"] if o["reviewer"] == "dan")
    assert dan["phase"] == "approval"
    assert dan["paused"] is True
    db.close()


def test_poll_result_shape_stable_across_runs(config, tmp_path):
    """Guard against snapshot mutation leaking between polls."""
    db = Database(tmp_path / "r.db")
    src = _source()
    before = copy.deepcopy(src._mrs)
    poll_once(db, config, src)
    assert src._mrs == before  # source data not mutated
    db.close()
