"""Per-MR checkouts: `checkout: worktree` gives each job the merge request's code.

These drive real git — a local "remote" with a real ``refs/merge-requests/N/head``
ref, the way GitLab publishes one — because the whole feature is an assertion
about what git does, and a mocked git would only prove the mock agrees with itself.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from radar.config import ConfigError, load_config
from radar.db import Database
from radar.diagnostics import _check_skill_context
from radar.events import EventType as ET
from radar.web.app import create_app
from radar.worktree import MR_REF, WorktreeError, create_mr_worktree, is_git_repo
from tests.conftest import ev, ny

PY = f'"{sys.executable}"'

# Prints the working directory and the contents of marker.txt, so a test can see
# which commit's tree the command was actually run against.
SHOW_TREE = (
    f'{PY} -c "import os,pathlib; print(os.getcwd()); '
    f'print(pathlib.Path(chr(109)+chr(46)+chr(116)+chr(120)+chr(116)).read_text())"'
)

_BASE = """
gitlab: {{projects: [g/p]}}
calendar:
  workdays: [mon, tue, wed, thu, fri]
  work_hours: {{start: "09:00", end: "18:00"}}
  default_timezone: America/New_York
slas:
  - match: {{}}
    first_response_business_hours: 16
    approval_business_hours: 24
waive: {{draft: true}}
{extra}
"""


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    """An 'upstream' with an MR ref, and a clone of it — the operator's checkout.

    ``m.txt`` says which commit a tree is on, so a test can tell the merge
    request's code from the default branch's.
    """
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git("init", "-b", "main", cwd=upstream)
    _git("config", "user.email", "t@example.com", cwd=upstream)
    _git("config", "user.name", "t", cwd=upstream)
    (upstream / "m.txt").write_text("MAIN-BRANCH-CONTENT", encoding="utf-8")
    _git("add", ".", cwd=upstream)
    _git("commit", "-m", "main", cwd=upstream)

    # The MR's commit, published only under refs/merge-requests/7/head — exactly
    # like a GitLab MR from a fork, whose branch the target repo never has.
    _git("checkout", "-b", "feature", cwd=upstream)
    (upstream / "m.txt").write_text("MERGE-REQUEST-CONTENT", encoding="utf-8")
    _git("commit", "-am", "mr", cwd=upstream)
    head_sha = _git("rev-parse", "HEAD", cwd=upstream)
    _git("update-ref", MR_REF.format(iid=7), head_sha, cwd=upstream)
    _git("checkout", "main", cwd=upstream)
    _git("branch", "-D", "feature", cwd=upstream)

    clone = tmp_path / "clone"
    _git("clone", str(upstream), str(clone), cwd=tmp_path)
    return {"upstream": upstream, "clone": clone, "head_sha": head_sha}


def _config(tmp_path, extra):
    path = tmp_path / "config.yaml"
    path.write_text(_BASE.format(extra=extra), encoding="utf-8")
    return load_config(path)


def _seed(db):
    db.upsert_mr_snapshot(
        project_id=1, mr_iid=7, title="Add widget", author="aviva",
        web_url="https://gitlab.example.com/g/p/-/merge_requests/7",
        source_branch="feature", target_branch="main", description="", labels=[], draft=False,
        state="opened", reviewers=["dan"], created_at="2026-03-02T09:00:00Z",
        updated_at="2026-03-02T09:00:00Z",
    )
    db.insert_events([ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan", mr_iid=7)])


def _await_no_worktrees(repo, timeout=15.0):
    """Wait for the job's worktree to be removed.

    The worker tidies up in a `finally`, which runs *after* the job is marked
    done — so a test that reads `git worktree list` the instant the panel
    settles is racing the cleanup, not testing it.
    """
    deadline = time.time() + timeout
    listing = ""
    while time.time() < deadline:
        listing = _git("worktree", "list", cwd=repo)
        if "radar-mr-" not in listing:
            return
        time.sleep(0.05)
    raise AssertionError(f"worktree was not cleaned up:\n{listing}")


def _run(client, kind, timeout=30.0):
    start = client.post(f"/{kind}/1/7")
    assert start.status_code == 200
    match = re.search(r'data-job-id="([0-9a-f]+)"', start.text)
    if match is None:
        return start.text
    deadline = time.time() + timeout
    while time.time() < deadline:
        html = client.get(f"/{kind}/status/{match.group(1)}").text
        if "review-output" in html or "review-error" in html:
            return html
        time.sleep(0.05)
    raise AssertionError(f"{kind} job did not finish")


# --- the git layer ---------------------------------------------------------


def test_worktree_has_the_merge_requests_code(repos):
    clone = repos["clone"]
    assert (clone / "m.txt").read_text() == "MAIN-BRANCH-CONTENT"  # the checkout is on main

    wt = create_mr_worktree(clone, 7, repos["head_sha"])
    try:
        assert (wt.path / "m.txt").read_text() == "MERGE-REQUEST-CONTENT"
        # Detached: the operator's own checkout is untouched, still on main.
        assert (clone / "m.txt").read_text() == "MAIN-BRANCH-CONTENT"
        assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=clone) == "main"
        assert _git("rev-parse", "HEAD", cwd=wt.path) == repos["head_sha"]
    finally:
        wt.cleanup()
    assert not wt.path.exists()
    # And nothing is left registered against the repository.
    assert "radar-mr-" not in _git("worktree", "list", cwd=clone)


def test_two_worktrees_of_one_repo_are_independent(repos):
    a = create_mr_worktree(repos["clone"], 7, repos["head_sha"])
    b = create_mr_worktree(repos["clone"], 7, repos["head_sha"])
    try:
        assert a.path != b.path
        (a.path / "m.txt").write_text("SCRIBBLED", encoding="utf-8")
        assert (b.path / "m.txt").read_text() == "MERGE-REQUEST-CONTENT"
    finally:
        a.cleanup()
        b.cleanup()


def test_worktree_without_the_sha_falls_back_to_the_fetched_ref(repos):
    # radar has no head_sha (an MR polled before the column existed): the ref it
    # just fetched still names the right commit.
    wt = create_mr_worktree(repos["clone"], 7, None)
    try:
        assert (wt.path / "m.txt").read_text() == "MERGE-REQUEST-CONTENT"
    finally:
        wt.cleanup()


def test_unknown_mr_ref_is_an_error_not_a_silent_default_branch(repos):
    with pytest.raises(WorktreeError, match="could not get merge request 99"):
        create_mr_worktree(repos["clone"], 99, None)


def test_a_directory_that_is_not_a_repo_is_refused(tmp_path):
    (tmp_path / "plain").mkdir()
    assert not is_git_repo(tmp_path / "plain")
    with pytest.raises(WorktreeError, match="not a git repository"):
        create_mr_worktree(tmp_path / "plain", 7, None)


def test_cleanup_survives_a_worktree_removed_behind_gits_back(repos):
    import shutil

    wt = create_mr_worktree(repos["clone"], 7, repos["head_sha"])
    shutil.rmtree(wt.path)
    wt.cleanup()  # must not raise — the job is over either way
    assert "radar-mr-" not in _git("worktree", "list", cwd=repos["clone"])


# --- through a job ---------------------------------------------------------


def test_job_runs_in_the_merge_requests_worktree(repos, tmp_path):
    cfg = _config(
        tmp_path,
        "skills:\n"
        "  - name: dba\n"
        "    enabled: true\n"
        f"    command: '{SHOW_TREE}'\n"
        "    timeout_seconds: 60\n"
        "    checkout: worktree\n"
        f"    source: {repos['clone']}\n",
    )
    db_path = tmp_path / "r.db"
    db = Database(db_path)
    _seed(db)
    db.close()

    html = _run(TestClient(create_app(cfg, str(db_path))), "dba")
    assert "review-output" in html
    assert "MERGE-REQUEST-CONTENT" in html   # the command saw the MR's code...
    assert "MAIN-BRANCH-CONTENT" not in html  # ...not the checkout's branch
    assert str(repos["clone"]) not in html    # and did not run in the shared clone
    # The job's worktree is cleaned up, and the operator's checkout is as it was.
    _await_no_worktrees(repos["clone"])
    assert (repos["clone"] / "m.txt").read_text() == "MAIN-BRANCH-CONTENT"


def test_source_root_placeholder_and_bundle_name_the_worktree(repos, tmp_path):
    echo = (
        f'{PY} -c "import sys; print(sys.argv[1]); print(sys.stdin.read())" '
        "--root={source_root}"
    )
    cfg = _config(
        tmp_path,
        "skills:\n"
        "  - name: dba\n"
        "    enabled: true\n"
        f"    command: '{echo}'\n"
        "    timeout_seconds: 60\n"
        "    checkout: worktree\n"
        f"    source: {repos['clone']}\n",
    )
    db_path = tmp_path / "r.db"
    db = Database(db_path)
    _seed(db)
    db.close()

    html = _run(TestClient(create_app(cfg, str(db_path))), "dba")
    assert "review-output" in html
    assert "radar-mr-7-" in html          # {source_root} is the per-job worktree
    assert "worktree created for this run" in html  # and the bundle says so
    assert f"--root={repos['clone']}" not in html


def test_worktree_failure_fails_the_job(repos, tmp_path):
    """An MR whose ref the remote doesn't have must not review the wrong tree."""
    db_path = tmp_path / "r.db"
    db = Database(db_path)
    db.upsert_mr_snapshot(
        project_id=1, mr_iid=404, title="Ghost", author="aviva",
        web_url="https://gitlab.example.com/g/p/-/merge_requests/404",
        source_branch="nope", target_branch="main", description="", labels=[], draft=False,
        state="opened", reviewers=["dan"], created_at="2026-03-02T09:00:00Z",
        updated_at="2026-03-02T09:00:00Z",
    )
    db.insert_events([ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan", mr_iid=404)])
    db.close()

    cfg = _config(
        tmp_path,
        "skills:\n"
        "  - name: dba\n"
        "    enabled: true\n"
        f"    command: '{SHOW_TREE}'\n"
        "    timeout_seconds: 60\n"
        "    checkout: worktree\n"
        f"    source: {repos['clone']}\n",
    )
    client = TestClient(create_app(cfg, str(db_path)))
    start = client.post("/dba/1/404")
    job_id = re.search(r'data-job-id="([0-9a-f]+)"', start.text).group(1)
    html = ""
    for _ in range(600):
        html = client.get(f"/dba/status/{job_id}").text
        if "review-output" in html or "review-error" in html:
            break
        time.sleep(0.05)
    assert "review-error" in html
    assert "could not get merge request 404" in html


# --- regressions -----------------------------------------------------------


def test_concurrent_jobs_never_get_each_others_merge_request(tmp_path):
    """FETCH_HEAD is one file per repository.

    Fetching two MRs at once into it made each job's `worktree add FETCH_HEAD`
    resolve to whichever fetch finished last — measured at half of all pairs
    swapped, each producing a confident review of a change nobody asked about.
    Every job now fetches into a ref of its own.
    """
    import concurrent.futures as cf

    upstream = tmp_path / "up"
    upstream.mkdir()
    _git("init", "-b", "main", cwd=upstream)
    _git("config", "user.email", "t@example.com", cwd=upstream)
    _git("config", "user.name", "t", cwd=upstream)
    (upstream / "m.txt").write_text("base", encoding="utf-8")
    _git("add", ".", cwd=upstream)
    _git("commit", "-m", "base", cwd=upstream)
    for iid, content in ((7, "MR-SEVEN"), (9, "MR-NINE")):
        _git("checkout", "-q", "-B", f"f{iid}", "main", cwd=upstream)
        (upstream / "m.txt").write_text(content, encoding="utf-8")
        _git("commit", "-qam", content, cwd=upstream)
        _git("update-ref", MR_REF.format(iid=iid), _git("rev-parse", "HEAD", cwd=upstream),
             cwd=upstream)
    _git("checkout", "-q", "main", cwd=upstream)

    clone = tmp_path / "clone"
    _git("clone", "-q", str(upstream), str(clone), cwd=tmp_path)

    expected = {7: "MR-SEVEN", 9: "MR-NINE"}
    for _ in range(6):
        with cf.ThreadPoolExecutor(max_workers=2) as ex:
            futures = {i: ex.submit(create_mr_worktree, clone, i, None) for i in expected}
            trees = {i: f.result() for i, f in futures.items()}
        try:
            for iid, wt in trees.items():
                assert (wt.path / "m.txt").read_text().strip() == expected[iid]
        finally:
            for wt in trees.values():
                wt.cleanup()

    assert _git("for-each-ref", "--format=%(refname)", "refs/radar", cwd=clone) == ""


def test_a_freshly_pushed_commit_wins_over_the_last_polled_sha(repos):
    """The diff a skill is handed comes from GitLab live, but `head_sha` is only
    as new as the last poll — so pinning the tree to it would show the skill a
    diff of code it cannot see."""
    upstream, clone = repos["upstream"], repos["clone"]
    stale = repos["head_sha"]

    _git("checkout", "-q", "-B", "f2", stale, cwd=upstream)
    (upstream / "m.txt").write_text("FORCE-PUSHED-SINCE-THE-LAST-POLL", encoding="utf-8")
    _git("commit", "-qam", "amended", cwd=upstream)
    _git("update-ref", MR_REF.format(iid=7), _git("rev-parse", "HEAD", cwd=upstream), cwd=upstream)
    _git("checkout", "-q", "main", cwd=upstream)

    wt = create_mr_worktree(clone, 7, stale)  # radar still believes the old sha
    try:
        assert (wt.path / "m.txt").read_text() == "FORCE-PUSHED-SINCE-THE-LAST-POLL"
    finally:
        wt.cleanup()


def test_the_stale_sha_is_still_the_fallback_when_the_fetch_fails(repos):
    _git("fetch", "-q", "origin", MR_REF.format(iid=7), cwd=repos["clone"])
    _git("remote", "remove", "origin", cwd=repos["clone"])  # nothing to fetch from now
    wt = create_mr_worktree(repos["clone"], 7, repos["head_sha"])
    try:
        assert (wt.path / "m.txt").read_text() == "MERGE-REQUEST-CONTENT"
    finally:
        wt.cleanup()


def _radar_refs(repo):
    return _git("for-each-ref", "--format=%(refname)", "refs/radar", cwd=repo)


def test_a_leaked_worktree_and_ref_are_swept_once_the_ref_is_old(repos):
    """A killed radar leaves both behind — job threads are daemons, so nothing
    runs their cleanup — and a leaked ref pins the commit against gc forever."""
    import shutil

    from radar.worktree import _REF_GRACE_S, prune

    clone = repos["clone"]
    leaked = create_mr_worktree(clone, 7, None)
    shutil.rmtree(leaked._parent)  # what a killed radar leaves: the dir gone, git's records not
    assert "radar-mr-" in _git("worktree", "list", cwd=clone)

    prune(clone)
    assert "radar-mr-" not in _git("worktree", "list", cwd=clone)  # registration swept at once
    assert leaked.ref in _radar_refs(clone)  # ref held: this young it could still be fetching

    # The same ref, stamped as a job that died well before the grace period.
    aged = f"refs/radar/{int(time.time()) - _REF_GRACE_S - 60}-radar-mr-7-dead"
    _git("update-ref", aged, _git("rev-parse", leaked.ref, cwd=clone), cwd=clone)
    prune(clone)
    assert aged not in _radar_refs(clone)


def test_a_ref_that_is_still_being_fetched_is_not_swept(repos):
    """The window between a job's fetch and its `worktree add`: the ref exists
    with nothing registered against it, and deleting it would fail a review that
    was about to work — including one belonging to a second radar."""
    from radar.worktree import prune

    clone = repos["clone"]
    fresh = f"refs/radar/{int(time.time())}-radar-mr-7-inflight"
    _git("fetch", "-q", "origin", f"{MR_REF.format(iid=7)}:{fresh}", cwd=clone)

    prune(clone)
    assert fresh in _radar_refs(clone)


def test_a_live_worktrees_ref_survives_a_prune(repos):
    from radar.worktree import prune

    wt = create_mr_worktree(repos["clone"], 7, None)
    try:
        prune(repos["clone"])  # another job starting must not pull this one's ref
        assert wt.ref in _radar_refs(repos["clone"])
        assert (wt.path / "m.txt").read_text() == "MERGE-REQUEST-CONTENT"
    finally:
        wt.cleanup()


def test_working_dir_and_worktree_together_are_rejected(tmp_path, repos):
    with pytest.raises(ConfigError, match="contradict each other"):
        _config(
            tmp_path,
            "skills:\n  - name: dba\n    command: 'x'\n    checkout: worktree\n"
            f"    source: {repos['clone']}\n    working_dir: {repos['clone']}\n",
        )


# --- the commits radar knows about -----------------------------------------


def test_normalize_mr_keeps_the_head_sha():
    from radar.gitlab_client import normalize_mr

    mr = normalize_mr({"iid": 7, "project_id": 1, "sha": "abc123", "title": "t"})
    assert mr["head_sha"] == "abc123"  # free from the list endpoint, no extra call


def test_head_sha_round_trips_and_reaches_the_command(tmp_path):
    cfg = _config(
        tmp_path,
        "skills:\n"
        "  - name: dba\n"
        "    enabled: true\n"
        f"    command: '{PY} -c \"import sys; print(sys.argv[1])\" --sha={{head_sha}}'\n"
        "    timeout_seconds: 30\n",
    )
    db_path = tmp_path / "r.db"
    db = Database(db_path)
    _seed(db)
    db.upsert_mr_snapshot(
        project_id=1, mr_iid=7, title="Add widget", author="aviva",
        web_url="https://gitlab.example.com/g/p/-/merge_requests/7",
        source_branch="feature", target_branch="main", description="", labels=[], draft=False,
        state="opened", reviewers=["dan"], created_at="2026-03-02T09:00:00Z",
        updated_at="2026-03-02T09:00:00Z", head_sha="deadbeefcafe",
    )
    assert db.get_snapshot(1, 7)["head_sha"] == "deadbeefcafe"
    db.close()

    assert "--sha=deadbeefcafe" in _run(TestClient(create_app(cfg, str(db_path))), "dba")


def test_migration_adds_head_sha_to_an_older_database(tmp_path):
    import sqlite3

    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE mr_snapshots (
            project_id INTEGER NOT NULL, mr_iid INTEGER NOT NULL, title TEXT NOT NULL,
            author TEXT, web_url TEXT, source_branch TEXT, target_branch TEXT,
            labels TEXT NOT NULL DEFAULT '[]', draft INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'opened', reviewers TEXT NOT NULL DEFAULT '[]',
            created_at TEXT, updated_at TEXT, last_polled_at TEXT,
            PRIMARY KEY (project_id, mr_iid)
        );
        INSERT INTO mr_snapshots (project_id, mr_iid, title) VALUES (1, 7, 'Old MR');
        """
    )
    conn.commit()
    conn.close()

    db = Database(db_path)  # opening runs init_schema() -> _migrate()
    snap = db.get_snapshot(1, 7)
    assert snap["title"] == "Old MR"     # the row survives...
    assert snap["head_sha"] is None      # ...with no commit until its next poll
    db.close()


def test_review_bundle_names_the_commits():
    from radar.context import build_review_input
    from radar.gitlab_client import FixtureSource

    source = FixtureSource(
        mrs_by_project={}, discussions_by_mr={},
        mr_context_by_mr={
            (1, 7): {
                "title": "Add cache", "description": "", "diff": "@@ -1 +1 @@",
                "base_sha": "base111", "head_sha": "head222", "start_sha": "start333",
            }
        },
    )
    out = build_review_input(source, 1, 7)
    assert "## Commits" in out
    assert "base111" in out and "head222" in out and "start333" in out


# --- config + diagnostics --------------------------------------------------


def test_worktree_without_source_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="no 'source:'"):
        _config(
            tmp_path,
            "skills:\n  - name: dba\n    command: 'x'\n    checkout: worktree\n",
        )


def test_unknown_checkout_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown value"):
        _config(
            tmp_path,
            "skills:\n  - name: dba\n    command: 'x'\n    checkout: clone\n    source: /tmp\n",
        )


def test_checkout_defaults_to_none(tmp_path):
    cfg = _config(tmp_path, "skills:\n  - name: dba\n    command: 'x'\n")
    assert cfg.skill_by_name("dba").checkout == "none"
    assert cfg.skill_by_name("dba").remote == "origin"


def test_radar_check_reports_worktree_mode(repos, tmp_path):
    cfg = _config(
        tmp_path,
        "skills:\n  - name: dba\n    enabled: true\n    command: 'x'\n"
        "    checkout: worktree\n    remote: upstream\n"
        f"    source: {repos['clone']}\n",
    )
    check = {c.name: c for c in _check_skill_context(cfg)}["dba.context"]
    assert check.status == "ok"
    assert "worktree per job (fetching from 'upstream')" in check.detail


def test_radar_check_fails_when_the_source_is_not_a_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    cfg = _config(
        tmp_path,
        "skills:\n  - name: dba\n    enabled: true\n    command: 'x'\n"
        f"    checkout: worktree\n    source: {plain}\n",
    )
    check = {c.name: c for c in _check_skill_context(cfg)}["dba.context"]
    assert check.status == "fail" and "not a git repository" in check.detail
