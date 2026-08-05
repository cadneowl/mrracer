"""Discussion threads: parsed from GitLab, cached, and shown on the board.

The board says a reviewer is waiting; these tests cover the part that says what
they are waiting *for* — the comments themselves, so nobody has to open GitLab
to read two sentences.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from radar.db import THREAD_COUNT_SQL, Database
from radar.gitlab_client import FixtureSource
from radar.poller import poll_once
from radar.service import build_dashboard, build_threads
from radar.threads import MAX_BODY_CHARS, Thread, threads_from_discussions
from radar.web.app import create_app
from tests.test_poller import PID, PROJECT, _mr, _sysnote

MR_URL = "https://gitlab.example.com/group/hub-backend/-/merge_requests/1"


def _note(note_id, author, body, at, **extra):
    note = {
        "id": note_id,
        "system": False,
        "author": {"username": author},
        "created_at": at,
        "body": body,
    }
    note.update(extra)
    return note


def _thread(discussion_id, notes):
    return {"id": discussion_id, "individual_note": False, "notes": notes}


# --- parsing ---------------------------------------------------------------


def test_a_diff_thread_keeps_who_said_what_and_where():
    disc = _thread(
        "d1",
        [
            _note(
                1002,
                "skatzman",
                "this drops the tenant filter",
                "2026-03-02T11:00:00Z",
                position={"new_path": "api/vex.py", "new_line": 42},
                resolvable=True,
            ),
            _note(1003, "irenez", "good catch, fixing", "2026-03-02T12:00:00Z", resolvable=True),
        ],
    )

    (t,) = threads_from_discussions(PID, 1, [disc])

    assert t.author == "skatzman"  # who opened it, not who spoke last
    assert t.file_path == "api/vex.py" and t.line == 42
    assert t.open is True
    assert [n["body"] for n in t.notes] == [
        "this drops the tenant filter",
        "good catch, fixing",
    ]


def test_a_thread_is_resolved_only_when_every_resolvable_note_is():
    half = _thread(
        "d2",
        [
            _note(1, "skatzman", "please rename", "2026-03-02T11:00:00Z",
                  resolvable=True, resolved=True, resolved_by={"username": "irenez"}),
            _note(2, "skatzman", "and this one too", "2026-03-02T11:30:00Z", resolvable=True),
        ],
    )
    done = _thread(
        "d3",
        [
            _note(3, "skatzman", "typo", "2026-03-02T11:00:00Z", resolvable=True,
                  resolved=True, resolved_by={"username": "irenez"},
                  resolved_at="2026-03-02T13:00:00Z"),
        ],
    )

    open_thread, resolved_thread = threads_from_discussions(PID, 1, [half, done])

    assert open_thread.resolved is False and open_thread.open is True
    assert resolved_thread.resolved is True and resolved_thread.open is False
    assert resolved_thread.resolved_by == "irenez"


def test_a_plain_comment_is_kept_but_is_not_something_to_resolve():
    """Nobody can resolve a general comment, so counting it as an open thread
    would show a number the reviewer can never work down."""
    disc = {
        "id": "d4",
        "individual_note": True,
        "notes": [_note(9, "jai", "LGTM overall", "2026-03-02T11:00:00Z")],
    }

    (t,) = threads_from_discussions(PID, 1, [disc])

    assert t.resolvable is False and t.open is False
    assert t.notes[0]["body"] == "LGTM overall"


def test_system_notes_never_become_conversation():
    """'requested review from @dan' is the audit trail events are built from;
    repeating it here would bury the sentence someone needs to read."""
    disc = {
        "id": "d5",
        "individual_note": True,
        "notes": [_sysnote(1001, "aviva", "requested review from @dan", "2026-03-02T09:05:00Z")],
    }
    assert threads_from_discussions(PID, 1, [disc]) == []


def test_a_mixed_discussion_keeps_only_the_human_notes():
    disc = _thread(
        "d6",
        [
            _note(1, "skatzman", "needs a test", "2026-03-02T11:00:00Z", resolvable=True),
            _sysnote(2, "irenez", "changed this line", "2026-03-02T11:05:00Z"),
        ],
    )
    (t,) = threads_from_discussions(PID, 1, [disc])
    assert len(t.notes) == 1 and t.notes[0]["author"] == "skatzman"


def test_a_giant_comment_is_cut_short_and_says_so():
    """A pasted stack trace must not make a board row megabytes wide; what is
    cut is still one click away on GitLab."""
    disc = _thread("d7", [_note(1, "jai", "x" * (MAX_BODY_CHARS + 500), "2026-03-02T11:00:00Z")])

    (t,) = threads_from_discussions(PID, 1, [disc])

    assert len(t.notes[0]["body"]) == MAX_BODY_CHARS
    assert t.notes[0]["truncated"] is True


def test_a_malformed_position_does_not_break_the_thread():
    disc = _thread(
        "d8",
        [_note(1, "jai", "hm", "2026-03-02T11:00:00Z", position={"new_path": "a.py",
                                                                "new_line": "not-a-line"})],
    )
    (t,) = threads_from_discussions(PID, 1, [disc])
    assert t.file_path == "a.py" and t.line is None


# --- storage ---------------------------------------------------------------


def _stored(**overrides) -> Thread:
    base = dict(
        project_id=PID, mr_iid=1, discussion_id="d1", author="skatzman",
        created_at="2026-03-02T11:00:00Z", updated_at="2026-03-02T11:00:00Z",
        resolvable=True, resolved=False, resolved_by=None, resolved_at=None,
        file_path="api/vex.py", line=42,
        notes=[{"id": 1, "author": "skatzman", "body": "hi", "created_at": "2026-03-02T11:00:00Z"}],
    )
    base.update(overrides)
    return Thread(**base)


def test_threads_round_trip(tmp_path):
    db = Database(tmp_path / "t.db")
    db.replace_threads(PID, 1, [_stored()])

    (row,) = db.threads_for(PID, 1)

    assert row["author"] == "skatzman" and row["resolvable"] is True and row["resolved"] is False
    assert row["notes"][0]["body"] == "hi"
    db.close()


def test_a_resolved_thread_stops_being_open_after_the_next_poll(tmp_path):
    """Threads are replaced, not merged: GitLab's answer is the whole answer, so
    a thread resolved (or deleted) there cannot linger here as still-waiting."""
    db = Database(tmp_path / "t.db")
    db.replace_threads(PID, 1, [_stored(), _stored(discussion_id="d2")])

    db.replace_threads(PID, 1, [_stored(resolved=True, resolved_by="irenez")])

    rows = db.threads_for(PID, 1)
    assert len(rows) == 1 and rows[0]["resolved"] is True
    assert db.thread_counts().get((PID, 1), {}).get("open", 0) == 0
    db.close()


def test_counts_separate_open_from_settled_and_attribute_by_opener(tmp_path):
    db = Database(tmp_path / "t.db")
    db.replace_threads(
        PID,
        1,
        [
            _stored(discussion_id="a", author="skatzman"),
            _stored(discussion_id="b", author="skatzman"),
            _stored(discussion_id="c", author="jai", resolved=True),
            _stored(discussion_id="d", author="jai", resolvable=False),
        ],
    )

    counts = db.thread_counts()[(PID, 1)]

    assert counts["total"] == 4
    assert counts["open"] == 2
    assert counts["by_author"] == {"skatzman": 2}  # jai has nothing outstanding
    db.close()


def test_the_board_tally_never_pages_in_a_comment_body(tmp_path):
    """This query runs on every board render, for every open browser. Answering
    it from the index costs nothing; answering it from the table means reading
    every stored comment to produce a handful of numbers. Only the query plan
    tells the two apart, so the plan is what gets asserted — adding a column the
    index lacks would silently turn it back into a full scan."""
    db = Database(tmp_path / "t.db")
    plan = " ".join(r[-1] for r in db.conn.execute("EXPLAIN QUERY PLAN " + THREAD_COUNT_SQL))
    assert "COVERING INDEX" in plan, plan
    db.close()


# --- through the poller and onto the board ---------------------------------


def _discussions_with_open_thread():
    return [
        {
            "id": "d0",
            "individual_note": True,
            "notes": [
                _sysnote(1001, "aviva", "requested review from @dan", "2026-03-02T09:05:00Z")
            ],
        },
        _thread(
            "d1",
            [
                _note(1002, "dan", "this drops the tenant filter", "2026-03-02T11:00:00Z",
                      position={"new_path": "api/vex.py", "new_line": 42}, resolvable=True)
            ],
        ),
    ]


def _source(discussions=None, updated_at="2026-03-02T11:05:00Z"):
    return FixtureSource(
        mrs_by_project={PROJECT: [_mr(["dan"], updated_at)]},
        discussions_by_mr={(PID, 1): discussions or _discussions_with_open_thread()},
    )


def test_polling_stores_the_conversation_alongside_the_events(config, tmp_path):
    db = Database(tmp_path / "r.db")
    poll_once(db, config, _source())

    (t,) = db.threads_for(PID, 1)

    assert t["author"] == "dan"
    assert t["notes"][0]["body"] == "this drops the tenant filter"
    db.close()


def test_resolving_on_gitlab_clears_the_board_count(config, tmp_path):
    db = Database(tmp_path / "r.db")
    poll_once(db, config, _source())
    assert build_dashboard(db, config)["rows"][0]["threads"]["open"] == 1

    resolved = _discussions_with_open_thread()
    resolved[1]["notes"][0].update(resolved=True, resolved_by={"username": "aviva"})
    poll_once(db, config, _source(resolved, updated_at="2026-03-03T09:00:00Z"))

    row = build_dashboard(db, config)["rows"][0]
    assert row["threads"] == {"total": 1, "open": 0}
    db.close()


def test_a_full_pass_backfills_an_mr_that_has_not_changed(config, tmp_path):
    """The upgrade case: an MR nobody has touched since is exactly the one whose
    stalled thread you want to read, and an ordinary poll skips it."""
    db = Database(tmp_path / "r.db")
    poll_once(db, config, _source())
    db.conn.execute("DELETE FROM mr_threads")  # as if polled before threads existed
    db.conn.commit()

    assert poll_once(db, config, _source()).mrs_seen == 0  # watermark skips it
    assert db.threads_for(PID, 1) == []

    poll_once(db, config, _source(), full=True)

    assert len(db.threads_for(PID, 1)) == 1
    db.close()


def test_a_full_pass_does_not_move_the_watermark_backwards(config, tmp_path):
    db = Database(tmp_path / "r.db")
    poll_once(db, config, _source(updated_at="2026-03-05T09:00:00Z"))
    mark = db.get_poll_state(PROJECT)["last_updated_after"]

    poll_once(db, config, _source(updated_at="2026-03-02T11:05:00Z"), full=True)

    assert db.get_poll_state(PROJECT)["last_updated_after"] == mark
    db.close()


def test_the_board_attributes_open_threads_to_the_reviewer_who_opened_them(config, tmp_path):
    db = Database(tmp_path / "r.db")
    poll_once(db, config, _source())

    row = build_dashboard(db, config)["rows"][0]
    dan = next(o for o in row["obligations"] if o["reviewer"] == "dan")

    assert dan["paused"] is True  # waiting on the author…
    assert dan["open_threads"] == 1  # …for this
    db.close()


# --- the view -------------------------------------------------------------


def test_unresolved_threads_are_listed_first(config, tmp_path):
    db = Database(tmp_path / "r.db")
    poll_once(db, config, _source())
    db.replace_threads(
        PID,
        1,
        [
            _stored(discussion_id="old", created_at="2026-01-01T09:00:00Z", resolved=True),
            _stored(discussion_id="new", created_at="2026-03-02T11:00:00Z"),
        ],
    )

    data = build_threads(db, PID, 1)

    assert [t["discussion_id"] for t in data["threads"]] == ["new", "old"]
    assert data["open_total"] == 1 and data["total"] == 2
    db.close()


def test_filtering_by_author_keeps_the_totals_honest(config, tmp_path):
    db = Database(tmp_path / "r.db")
    poll_once(db, config, _source())
    db.replace_threads(
        PID, 1, [_stored(discussion_id="a", author="skatzman"),
                 _stored(discussion_id="b", author="jai")]
    )

    data = build_threads(db, PID, 1, author="skatzman")

    assert [t["author"] for t in data["threads"]] == ["skatzman"]
    assert data["total"] == 2  # so the fragment can offer "show all"
    db.close()


def test_an_unknown_mr_has_no_threads(config, tmp_path):
    db = Database(tmp_path / "r.db")
    assert build_threads(db, PID, 999) is None
    db.close()


def test_a_thread_deep_links_to_its_first_note(config, tmp_path):
    db = Database(tmp_path / "r.db")
    poll_once(db, config, _source())

    (t,) = build_threads(db, PID, 1)["threads"]

    assert t["url"] == f"{MR_URL}#note_1002"
    assert t["location"] == "api/vex.py:42"
    db.close()


def _client(config, tmp_path, discussions=None):
    db_path = tmp_path / "web.db"
    db = Database(db_path)
    poll_once(db, config, _source(discussions))
    db.close()
    return TestClient(create_app(config, str(db_path)))


def test_the_board_offers_the_expander(config, tmp_path):
    resp = _client(config, tmp_path).get("/partials/board")
    assert 'id="threads-101-1"' in resp.text
    assert "data-thread-toggle" in resp.text
    assert 'data-thread-author="dan"' in resp.text


def test_an_mr_with_nothing_said_offers_no_expander(config, tmp_path):
    """A 💬 that opens onto nothing is worse than no 💬: it reads as "there is
    something here" on exactly the rows where there isn't."""
    only_system = [
        {"id": "s1", "individual_note": True,
         "notes": [_sysnote(1, "aviva", "requested review from @dan", "2026-03-02T09:05:00Z")]}
    ]
    text = _client(config, tmp_path, only_system).get("/partials/board").text

    assert "thread-btn" not in text
    assert "chip-threads" not in text
    assert 'id="threads-101-1"' in text  # the row the toggle would fill still exists


def test_the_fragment_renders_the_comment(config, tmp_path):
    resp = _client(config, tmp_path).get("/threads/101/1")
    assert resp.status_code == 200
    assert "this drops the tenant filter" in resp.text
    assert "api/vex.py:42" in resp.text
    assert "unresolved" in resp.text


def test_the_fragment_can_be_narrowed_to_one_person(config, tmp_path):
    client = _client(config, tmp_path)
    assert "this drops the tenant filter" in client.get("/threads/101/1?author=dan").text
    mine = client.get("/threads/101/1?author=someone-else")
    assert "this drops the tenant filter" not in mine.text
    assert "No comments from" in mine.text


def test_an_unknown_mr_is_a_404(config, tmp_path):
    assert _client(config, tmp_path).get("/threads/101/999").status_code == 404


def test_comment_markdown_is_rendered_but_never_trusted(config, tmp_path):
    """A comment body is written by anyone who can see the MR, so it takes the
    same render-then-sanitize path as skill output."""
    hostile = [
        _thread(
            "d1",
            [
                _note(7, "mallory",
                      "**bold** <script>alert(1)</script> [x](javascript:alert(1))",
                      "2026-03-02T11:00:00Z", resolvable=True)
            ],
        )
    ]
    resp = _client(config, tmp_path, hostile).get("/threads/101/1")

    assert "<strong>bold</strong>" in resp.text
    assert "<script>" not in resp.text
    assert "javascript:" not in resp.text
