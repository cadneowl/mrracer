"""The MR diff a review reads says where it is incomplete.

GitLab lists a file that is over its diff limits with an empty ``diff``. Joined
as-is, that is a bare ``diff --git`` header — which a reviewer reads as "touched,
no changes", and then reports the MR's tests as missing when they were simply
too large to be shown. Radar asks for the full diffs, and marks whatever still
comes back empty so it cannot be mistaken for an unchanged file.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from radar import gitlab_client
from radar.context import build_review_input
from radar.gitlab_client import FixtureSource, GitLabSource, changes_to_diff

TEST_FILE = "src/integtest/DigestIntegTest.java"


def _change(path, diff="", **flags):
    return {"old_path": path, "new_path": path, "diff": diff, "a_mode": "100644",
            "b_mode": "100644", **flags}


def _review(changes, **ctx):
    diff, omitted = changes_to_diff(changes)
    source = FixtureSource(
        {}, {}, {(1, 7): {"title": "t", "diff": diff, "omitted": omitted, **ctx}}
    )
    return build_review_input(source, 1, 7)


def test_a_file_with_a_diff_is_joined_unchanged():
    diff, omitted = changes_to_diff([_change("a.py", "@@ -1 +1 @@\n-x\n+y\n")])
    assert diff == "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    assert omitted == []


@pytest.mark.parametrize("flag, why", [("too_large", "too large"), ("collapsed", "collapsed")])
def test_a_blanked_file_is_marked_and_reported(flag, why):
    diff, omitted = changes_to_diff([_change(TEST_FILE, **{flag: True})])
    assert f"GitLab omitted its diff ({why}); its changes are NOT shown here" in diff
    assert omitted == [
        {"path": TEST_FILE, "why": f"GitLab omitted its diff ({why})", "at": "head"}
    ]


def test_an_empty_diff_with_no_reason_is_still_not_read_as_unchanged():
    diff, omitted = changes_to_diff([_change(TEST_FILE)])
    assert "NOT shown here" in diff
    assert [o["path"] for o in omitted] == [TEST_FILE]


def test_what_an_empty_diff_legitimately_means_is_said_in_git_s_own_terms():
    rename = {**_change("bin/run.sh", b_mode="100755"), "old_path": "run.sh",
              "renamed_file": True}
    created = _change("pkg/__init__.py", a_mode="0", new_file=True)
    deleted = _change("old.txt", b_mode="0", deleted_file=True)
    diff, omitted = changes_to_diff([rename, created, deleted])
    assert "old mode 100644\nnew mode 100755\nrename from run.sh\nrename to bin/run.sh" in diff
    assert "new file mode 100644" in diff
    assert "deleted file mode 100644" in diff
    assert "mode 0" not in diff
    assert omitted == []


def test_a_blanked_deleted_file_is_read_at_the_base():
    text = _review([_change("big.sql", b_mode="0", deleted_file=True, too_large=True)],
                   base_sha="b1", head_sha="h1")
    assert "`git show b1:big.sql` (the MR deletes it)" in text


def test_a_file_over_radar_s_own_cap_is_left_out_not_inlined(monkeypatch):
    monkeypatch.setattr(gitlab_client, "MAX_FILE_DIFF_CHARS", 10)
    diff, omitted = changes_to_diff([_change("yarn.lock", "+" * 50), _change("a.py", "+x")])
    assert "+" * 50 not in diff and "+x" in diff
    assert [o["path"] for o in omitted] == ["yarn.lock"]


def test_files_past_the_total_budget_are_left_out_and_smaller_ones_still_fit(monkeypatch):
    monkeypatch.setattr(gitlab_client, "MAX_DIFF_CHARS", 20)
    diff, omitted = changes_to_diff(
        [_change("a.py", "+" * 15), _change("b.py", "-" * 15), _change("c.py", "+c")]
    )
    assert "-" * 15 not in diff and "+c" in diff
    assert [o["path"] for o in omitted] == ["b.py"]
    assert "budget was spent" in omitted[0]["why"]


def test_the_review_input_leads_with_what_the_diff_leaves_out():
    text = _review([_change("a.py", "+x\n"), _change(TEST_FILE, too_large=True)],
                   head_sha="h1")
    assert text.index("## Incomplete diff") < text.index("## Diff")
    assert f"`git show h1:{TEST_FILE}`" in text


def test_a_truncated_file_list_says_how_many_and_how_to_find_them():
    text = _review([_change("a.py", "+x\n")], base_sha="b1", head_sha="h1",
                   truncated={"shown": 1000, "total": "1000+"})
    assert "only 1000 of 1000+ changed files" in text
    assert "`git diff --name-only b1..h1`" in text


def test_a_complete_diff_has_no_incomplete_section():
    assert "Incomplete diff" not in _review([_change("a.py", "+x")])


class _GitLab(BaseHTTPRequestHandler):
    """Just enough of the MR API: the MR itself and its changes."""

    seen: list = []

    def do_GET(self) -> None:
        url = urlparse(self.path)
        self.seen.append((url.path, parse_qs(url.query)))
        if url.path.endswith("/changes"):
            body = {"changes": [_change(TEST_FILE, too_large=True)],
                    "overflow": True, "changes_count": "1000+"}
        else:
            body = {"iid": 7, "title": "t", "description": "",
                    "diff_refs": {"base_sha": "b", "head_sha": "h", "start_sha": "s"}}
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def gitlab():
    _GitLab.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GitLab)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def test_the_live_source_asks_for_raw_diffs_and_reports_the_gaps(gitlab):
    ctx = GitLabSource(gitlab, "token").get_mr_context(1, 7)
    changes_calls = [q for path, q in _GitLab.seen if path.endswith("/changes")]
    assert changes_calls == [{"access_raw_diffs": ["true"]}]
    assert [o["path"] for o in ctx["omitted"]] == [TEST_FILE]
    assert ctx["truncated"] == {"shown": 1, "total": "1000+"}
    assert ctx["head_sha"] == "h"
