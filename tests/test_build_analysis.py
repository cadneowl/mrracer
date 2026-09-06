"""Analysing what broke a build: the commit walk, the log tail, and the button.

Every fetch goes through an injected getter, so nothing here touches a Jenkins.
"""

from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

from radar.config import ConfigError, JenkinsJob, load_config
from radar.context import build_jenkins_input
from radar.db import Database
from radar.jenkins import (
    FAILED,
    RUNNING,
    JenkinsClient,
    JenkinsError,
    JenkinsMonitor,
    analysable_build,
    commit_range,
    fetch_builds,
    fetch_log_tail,
    map_status,
    strip_view,
)
from radar.web.app import create_app
from tests.conftest import _BASE_CONFIG, _JENKINS_JOBS
from tests.test_jenkins import _build, _payload

JOB = JenkinsJob(name="backend-ci", url="https://jenkins.example.com/job/hub/job/backend/job/main")


def _item(sha, msg, author="dana", files=("src/app.py",), date="2026-09-05T10:00:00Z"):
    return {
        "commitId": sha,
        "msg": msg,
        "date": date,
        "author": {"fullName": author},
        "affectedPaths": list(files),
    }


def _pipeline(number, result, items=()):
    """A Pipeline job's shape: changeSets, plural, one entry per SCM."""
    return {"number": number, "result": result, "changeSets": [{"items": list(items)}]}


def _freestyle(number, result, items=()):
    """A freestyle job's shape: changeSet, singular."""
    return {"number": number, "result": result, "changeSet": {"items": list(items)}}


# --- the commit walk -------------------------------------------------------


def test_the_range_stops_at_the_last_build_that_passed():
    builds = [
        _pipeline(130, "FAILURE", [_item("aaa", "Cache the resolver")]),
        _pipeline(129, "FAILURE", [_item("bbb", "Bump pyyaml", author="sam")]),
        _pipeline(128, "SUCCESS", [_item("ccc", "Older, already green")]),
    ]
    last_good, commits = commit_range(builds, 130)

    assert last_good == 128
    # Newest first: the most recent change is the one to look at first, and the
    # broken build's own commits are suspects rather than background.
    assert [c.sha for c in commits] == ["aaa", "bbb"]
    assert commits[1].author == "sam"


def test_both_jenkins_changeset_shapes_are_read():
    """A Pipeline job reports changeSets (a list), a freestyle job changeSet (an
    object). Reading only one finds no commits on half of all Jenkins — and
    finds them silently, since an empty list reads as "nothing changed"."""
    pipeline = commit_range([_pipeline(9, "FAILURE", [_item("aaa", "x")])], 9)[1]
    freestyle = commit_range([_freestyle(9, "FAILURE", [_item("bbb", "y")])], 9)[1]

    assert [c.sha for c in pipeline] == ["aaa"]
    assert [c.sha for c in freestyle] == ["bbb"]


def test_no_green_build_in_the_window_is_reported_as_such():
    builds = [_pipeline(n, "FAILURE", [_item(f"c{n}", "broken")]) for n in (12, 11, 10)]
    last_good, commits = commit_range(builds, 12)

    assert last_good is None  # the caller says so rather than implying a range
    assert len(commits) == 3


def test_builds_newer_than_the_one_analysed_are_left_out():
    """A build that started after the failure did not cause it."""
    builds = [
        {"number": 131, "building": True, "result": None},
        _pipeline(130, "FAILURE", [_item("aaa", "the suspect")]),
        _pipeline(129, "SUCCESS"),
    ]
    assert [c.sha for c in commit_range(builds, 130)[1]] == ["aaa"]


def test_a_commit_reported_by_two_builds_is_listed_once():
    same = _item("aaa", "retried")
    builds = [_pipeline(11, "FAILURE", [same]), _pipeline(10, "FAILURE", [same])]

    assert [c.sha for c in commit_range(builds, 11)[1]] == ["aaa"]


def test_the_builds_request_asks_for_both_shapes_and_a_window():
    seen = []

    def getter(url):
        seen.append(url)
        return {"builds": [_pipeline(9, "FAILURE")]}

    fetch_builds(JenkinsClient(getter=getter), JOB, limit=25)

    assert "changeSet[items[" in seen[0] and "changeSets[items[" in seen[0]
    assert "{0,25}" in seen[0]


# --- the log tail ----------------------------------------------------------


def _text_client(pages: dict, calls: list | None = None) -> JenkinsClient:
    def text_getter(url, headers=None):
        if calls is not None:
            calls.append(url)
        for fragment, answer in pages.items():
            if fragment in url:
                return answer
        raise AssertionError(f"no fixture for {url}")

    return JenkinsClient(getter=lambda url: {}, text_getter=text_getter)


def test_only_the_tail_of_a_huge_log_is_fetched():
    """Asking for the whole log would drag a hundred megabytes across the
    network to read its last few hundred lines."""
    calls: list[str] = []
    body = "\n".join(f"line {i}" for i in range(1000))
    client = _text_client(
        {"start=1125899906842624": ("", {"X-Text-Size": "9000000"}), "start=7000000": (body, {})},
        calls,
    )

    log = fetch_log_tail(client, JOB, 128, lines=5)

    assert log.text.splitlines() == [f"line {i}" for i in range(995, 1000)]
    assert (log.lines, log.truncated) == (5, True)
    assert log.total_bytes == 9000000
    # The probe, then the tail — and the tail is asked for by byte offset.
    assert len(calls) == 2 and "start=7000000" in calls[1]  # 9,000,000 - 2,000,000


def test_a_jenkins_that_will_not_report_a_size_falls_back_to_consoletext():
    client = _text_client(
        {"progressiveText": ("", {}), "consoleText": ("only\nthese\nlines", {})}
    )
    log = fetch_log_tail(client, JOB, 128, lines=10)

    assert log.text == "only\nthese\nlines"
    assert log.lines == 3


def test_a_short_log_is_not_called_truncated():
    client = _text_client(
        {"start=1125899906842624": ("", {"X-Text-Size": "3"}), "start=0": ("a\nb", {})}
    )
    log = fetch_log_tail(client, JOB, 128, lines=400)

    assert (log.text, log.truncated) == ("a\nb", False)


def test_a_log_whose_end_cannot_be_reached_is_not_passed_off_as_the_tail():
    """The read is capped from the *start* of the body, so slicing the last
    bytes off a capped prefix hands over the middle of the build and calls it
    the failure. Either the range works and the end really is here, or the
    bundle has to say the end is missing."""
    from radar.jenkins import _MAX_LOG_BYTES

    prefix = "x" * (_MAX_LOG_BYTES + 10)
    client = _text_client({"progressiveText": ("", {}), "consoleText": (prefix, {})})

    log = fetch_log_tail(client, JOB, 128, lines=5)

    assert log.has_end is False
    assert log.truncated is True


def test_a_log_of_enormous_lines_cannot_become_the_prompt(tmp_path):
    """A line count bounds nothing on its own. A build printing a JSON document
    or a base64 blob per line reaches megabytes in a hundred lines — which is
    the "prompt is too long" that sent the evidence to files in the first place,
    and which raising the slab size made worse before this cap existed."""
    log = "\n".join("x" * 20_000 for _ in range(200))
    client = _text_client(
        {"1125899906842624": ("", {"X-Text-Size": str(len(log))}), "start=": (log, {})}
    )

    result = fetch_log_tail(client, JOB, 93, lines=120, dest_dir=str(tmp_path))

    assert len(result.text) < 25_000, f"excerpt is {len(result.text):,} chars"
    assert "chars in the file" in result.text  # each clipped line says so
    # Nothing is lost: the file holds the lines whole.
    assert len(pathlib.Path(result.path).read_text(encoding="utf-8")) > 1_000_000


def test_a_ranged_fallback_really_is_the_end():
    """A server that honours `Range: bytes=-N` answers with Content-Range, and
    then what came back is the end of the log."""
    calls: list[str] = []
    client = _text_client(
        {
            "progressiveText": ("", {}),
            "consoleText": ("...\nFinished: FAILURE", {"Content-Range": "bytes 900-919/920"}),
        },
        calls,
    )

    log = fetch_log_tail(client, JOB, 128, lines=400)

    assert log.has_end is True
    assert log.text.endswith("Finished: FAILURE")
    assert log.truncated is True  # there was more before it


def test_a_build_list_too_large_to_be_json_says_that_rather_than_blaming_auth():
    """A truncated JSON body can never parse, and the not-JSON message sends the
    reader off to fix credentials that are fine."""
    from radar.jenkins import _MAX_JSON_BYTES

    huge = b'{"builds": [' + b'{"number": 1},' * _MAX_JSON_BYTES

    def fake_open(self, req, timeout=None):
        return _FakeResponseBytes(huge)

    import urllib.request as urlreq

    client = JenkinsClient()
    original = urlreq.OpenerDirector.open
    urlreq.OpenerDirector.open = fake_open
    try:
        with pytest.raises(JenkinsError) as exc:
            fetch_builds(client, JOB)
    finally:
        urlreq.OpenerDirector.open = original

    assert "larger than" in str(exc.value)
    assert "JENKINS_USER" not in str(exc.value)  # not an auth problem


class _FakeResponseBytes:
    def __init__(self, body: bytes):
        self._body = body
        self.headers = {}

    def read(self, amount=None):
        return self._body if amount is None else self._body[:amount]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# --- which chips offer the button ------------------------------------------


@pytest.mark.parametrize(
    ("result", "offered"),
    [("FAILURE", True), ("UNSTABLE", True), ("ABORTED", True), ("SUCCESS", False)],
)
def test_only_a_build_that_ran_and_did_not_pass_can_be_analysed(result, offered):
    status = map_status(_payload(_build(128, result)), JOB)
    assert (analysable_build(status) == 128) is offered


def test_a_build_running_over_a_red_one_still_offers_the_last_verdict():
    """The spinner's own build has no verdict yet; the breakage is still the
    news, and the build to explain is the last one that finished."""
    payload = _payload(
        _build(130, None, building=True, duration_s=None), _build(128, "FAILURE")
    )
    status = map_status(payload, JOB)

    assert (status.state, status.previous) == (RUNNING, FAILED)
    # 128, not 129: build numbers skip, so "the one before" is not minus one.
    assert analysable_build(status) == 128


def test_nothing_to_analyse_without_a_build_or_without_contact():
    never = map_status(_payload(None), JOB)
    assert analysable_build(never) is None

    client = JenkinsClient(getter=lambda url: _payload(_build(1, "FAILURE")))
    monitor = JenkinsMonitor([JOB], client)
    monitor.refresh()
    fresh = monitor.snapshot().jobs[0]
    assert analysable_build(fresh) == 1

    from dataclasses import replace

    # Stale: the state shown is the last thing radar knew, and analysing a build
    # it cannot currently reach would fail at the fetch anyway.
    assert analysable_build(replace(fresh, stale=True)) is None


def test_the_strip_marks_a_build_whose_analysis_is_already_stored():
    client = JenkinsClient(getter=lambda url: _payload(_build(128, "FAILURE")))
    monitor = JenkinsMonitor([JOB], client)
    monitor.refresh()

    plain = strip_view(monitor.snapshot(), analysed=set(), skill="analyze")["jobs"][0]
    marked = strip_view(
        monitor.snapshot(), analysed={("backend-ci", 128, "analyze")}, skill="analyze"
    )["jobs"][0]

    assert plain["analysed"] is False
    assert marked["analysed"] is True
    # The mark is per build: a new number is a new question.
    other = strip_view(
        monitor.snapshot(), analysed={("backend-ci", 127, "analyze")}, skill="analyze"
    )["jobs"][0]
    assert other["analysed"] is False


# --- the bundle the skill is handed ----------------------------------------


_ONE_COMMIT = (_item("aaa", "Cache the resolver"),)


def _analysis_client(items=_ONE_COMMIT):
    builds = {"builds": [_pipeline(128, "FAILURE", items), _pipeline(127, "SUCCESS")]}
    return JenkinsClient(
        getter=lambda url: builds,
        text_getter=lambda url, headers=None: (
            ("", {"X-Text-Size": "40"}) if "1125899906842624" in url else ("boom\ntraceback", {})
        ),
    )


def test_the_bundle_names_the_build_the_range_and_the_log():
    status = map_status(_payload(_build(128, "FAILURE")), JOB)
    text = build_jenkins_input(_analysis_client(), JOB, status, log_lines=400, number=128)

    assert "# Jenkins build failure: backend-ci #128" in text
    assert "Result: FAILED" in text
    assert "## Commits since the last successful build (#127)" in text
    assert "Cache the resolver" in text and "src/app.py" in text
    assert "## Console log" in text and "traceback" in text


def test_the_bundle_describes_the_build_being_analysed_not_the_one_now_running():
    """The case the README advertises. The chip's own number is the *running*
    build; the one being explained is the last that finished, and the header,
    the commit range and the log all have to be about that one — otherwise the
    skill reads a still-streaming log while the panel and the saved row say #128.
    """
    running_over_red = _payload(
        _build(130, None, building=True, duration_s=None), _build(128, "FAILURE")
    )
    status = map_status(running_over_red, JOB)
    assert (analysable_build(status), status.build_number) == (128, 130)

    fetched: list[str] = []
    client = JenkinsClient(
        getter=lambda url: {"builds": [_pipeline(128, "FAILURE", _ONE_COMMIT)]},
        text_getter=lambda url, headers=None: (
            fetched.append(url) or ("", {"X-Text-Size": "9"})
            if "1125899906842624" in url
            else (fetched.append(url) or ("log", {}))
        ),
    )
    text = build_jenkins_input(client, JOB, status, log_lines=400, number=128)

    assert "# Jenkins build failure: backend-ci #128" in text
    assert "#130" not in text  # never the build that has not finished
    assert "Result: FAILED" in text  # what #128 did, not "RUNNING"
    assert all("/128/" in url for url in fetched)  # the log fetched is #128's


def test_the_bundle_stays_small_however_ugly_the_build_is(tmp_path):
    """What this exists to stop: "Prompt is too long". A monorepo merge lists
    thousands of paths per commit and twenty-five builds of them ran to
    megabytes, all of it file names. The evidence goes to files; the prompt gets
    an excerpt and the paths."""
    commits = [
        {
            "commitId": f"{i:040x}",
            "msg": f"commit {i}",
            "date": "2026-09-06",
            "author": {"fullName": "dev"},
            "affectedPaths": [f"src/pkg{j}/module{j}.py" for j in range(4000)],
        }
        for i in range(25)
    ]
    huge_log = "\n".join(f"[{i:06d}] building" for i in range(200_000))
    client = JenkinsClient(
        getter=lambda url: {
            "builds": [_pipeline(93, "FAILURE", commits), _pipeline(92, "SUCCESS")]
        },
        text_getter=lambda url, headers=None: (
            ("", {"X-Text-Size": str(len(huge_log))})
            if "1125899906842624" in url
            else (huge_log[-2_000_000:], {})
        ),
    )
    status = map_status(_payload(_build(93, "FAILURE")), JOB)

    text = build_jenkins_input(client, JOB, status, 120, 93, dest_dir=str(tmp_path))

    # Inline: kilobytes, not megabytes. Unbounded, this was ~2.8 MB.
    assert len(text) < 40_000, f"bundle is {len(text):,} chars"
    assert "20 of 25 commits shown" in text and "100000 files touched in all" in text

    # And nothing is lost — the whole of both is on disk, named in the bundle.
    log_file = tmp_path / "backend-ci-93.log"
    commits_file = tmp_path / "backend-ci-93-commits.txt"
    assert str(log_file) in text and str(commits_file) in text
    assert log_file.stat().st_size > 1_000_000
    assert commits_file.read_text(encoding="utf-8").count("src/pkg0/module0.py") == 25
    # The excerpt is the end of the log, which is where the failure is.
    assert text.rstrip().endswith("[199999] building\n```")


def test_a_log_radar_only_has_the_start_of_is_never_described_as_the_end(tmp_path):
    """A Jenkins that reports no size and ignores a byte range leaves radar
    holding the build's *opening*. Saying "last N lines" over that, or naming
    its size as the whole log's, is how an agent comes to reason confidently
    about the wrong part of a run — and the file makes it worse, because now it
    can grep, find nothing, and conclude the error is not in the log at all."""
    from radar.jenkins import _MAX_LOG_BYTES

    prefix = "\n".join(f"[{i:06d}] starting up" for i in range(200_000))[: _MAX_LOG_BYTES + 10]
    client = JenkinsClient(
        getter=lambda url: {"builds": [_pipeline(93, "FAILURE")]},
        text_getter=lambda url, headers=None: (
            ("", {}) if "progressiveText" in url else (prefix, {})
        ),
    )
    status = map_status(_payload(_build(93, "FAILURE")), JOB)

    text = build_jenkins_input(client, JOB, status, 120, 93, dest_dir=str(tmp_path))

    assert "First 120 lines" in text and "Last 120 lines" not in text
    assert "the FIRST" in text  # what the file holds, not "the last X of Y"
    assert "end of this log could not be fetched" in text
    assert "is NOT below" in text


def test_the_documented_default_is_the_one_an_unset_config_gets(tmp_path):
    """Two copies of one number drifted the moment one was lowered: the README
    and the example promised 120 inline lines while every unset config got 400."""
    from radar.jenkins import DEFAULT_LOG_TAIL_LINES

    path = tmp_path / "plain.yaml"
    path.write_text(_BASE_CONFIG + _JENKINS_JOBS, encoding="utf-8")

    assert load_config(path).jenkins.log_tail_lines == DEFAULT_LOG_TAIL_LINES == 120


def test_a_build_with_no_commits_says_so_rather_than_staying_silent():
    """An empty list is evidence: it points at the environment rather than at
    the change, and a skill told nothing would have to guess which."""
    status = map_status(_payload(_build(128, "FAILURE")), JOB)
    text = build_jenkins_input(
        _analysis_client(items=()), JOB, status, log_lines=400, number=128
    )

    assert "recorded no source changes" in text
    assert "environmental" in text


# --- the web layer ---------------------------------------------------------

# The wiring under test: the jenkins block names the skill, and the skill is an
# ordinary skill. Nothing about its name or its contexts decides anything.
_WIRING = "  analysis: {enabled: true, skill: analyze}\n"
_ANALYZE_SKILL = """
skills:
  - name: analyze
    enabled: true
    command: python -c "import sys; sys.stdout.write(sys.stdin.read())"
"""
_WIRED = _JENKINS_JOBS + _WIRING + _ANALYZE_SKILL


def _app(tmp_path, config_text, monitor):
    path = tmp_path / "config.yaml"
    path.write_text(config_text, encoding="utf-8")
    config = load_config(path)
    db_path = tmp_path / "analysis.db"
    Database(db_path).close()
    return config, str(db_path), TestClient(create_app(config, str(db_path), jenkins=monitor))


def _monitor(result="FAILURE"):
    client = JenkinsClient(getter=lambda url: _payload(_build(128, result)))
    monitor = JenkinsMonitor([JOB], client)
    monitor.refresh()
    return monitor


def test_the_button_is_offered_only_where_there_is_something_to_explain(tmp_path):
    broken = _app(tmp_path, _BASE_CONFIG + _WIRED, _monitor("FAILURE"))[2]
    green = _app(tmp_path, _BASE_CONFIG + _WIRED, _monitor("SUCCESS"))[2]

    assert "/jenkins/backend-ci/analyze" in broken.get("/partials/ci").text
    assert "/analyze" not in green.get("/partials/ci").text


def test_an_analysis_skill_never_appears_on_a_merge_request_row(tmp_path):
    _, _, client = _app(tmp_path, _BASE_CONFIG + _WIRED, _monitor())
    page = client.get("/").text

    # The board's own buttons post to /{kind}/{project}/{mr}; the strip's posts
    # to /jenkins/... . The analysis skill must only ever be the second.
    assert "/jenkins/backend-ci/analyze" in page
    assert 'hx-post="/analyze/' not in page


def test_analysing_runs_the_skill_and_stores_the_result(tmp_path):
    config, db_path, client = _app(
        tmp_path, _BASE_CONFIG + _WIRED, _monitor()
    )

    resp = client.post("/jenkins/backend-ci/analyze")
    assert resp.status_code == 200
    assert "Build failure analysis" in resp.text
    assert "#128" in resp.text  # the panel heads with the build, not an MR


def test_the_analysis_skill_cannot_be_launched_against_a_merge_request(tmp_path):
    """Everywhere else keeps the two apart — the config refuses `checkout:
    worktree`, the board filters it off MR rows — but the URL is guessable, and
    the run would have no build context and file its result where nothing shows
    it."""
    _, _, client = _app(tmp_path, _BASE_CONFIG + _WIRED, _monitor())

    assert client.post("/analyze/101/1").status_code == 404
    assert client.get("/analyze/stored/101/1").status_code == 404


@pytest.mark.parametrize(
    ("wiring", "skills", "expected"),
    [
        # Every one of these used to render a strip that looked entirely normal
        # and simply offered no button — nothing to notice, nothing to search.
        (
            "  analysis: {enabled: true, skill: nope}\n",
            _ANALYZE_SKILL,
            "no skill named 'nope'",
        ),
        (
            "  analysis: {enabled: true, skill: analyze}\n",
            "skills:\n  - name: analyze\n    command: echo hi\n",
            "enabled: false",
        ),
        ("  analysis: {enabled: true}\n", _ANALYZE_SKILL, "no 'skill' is named"),
    ],
)
def test_a_wiring_that_would_show_no_button_is_refused_at_load(
    tmp_path, wiring, skills, expected
):
    path = tmp_path / "wiring.yaml"
    path.write_text(_BASE_CONFIG + _JENKINS_JOBS + wiring + skills, encoding="utf-8")

    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert expected in str(exc.value)


def test_radar_check_says_which_skill_the_button_runs(tmp_path):
    """"Why is there no button" should be answerable without reading the config
    file — the wiring lives in one place and was visible in none."""
    from radar.diagnostics import _check_jenkins

    wired = tmp_path / "wired.yaml"
    wired.write_text(_BASE_CONFIG + _WIRED, encoding="utf-8")
    unwired = tmp_path / "unwired.yaml"
    unwired.write_text(_BASE_CONFIG + _JENKINS_JOBS + _OTHER_SKILL, encoding="utf-8")

    import radar.jenkins as jenkins_mod

    monkey = jenkins_mod.JenkinsClient.fetch
    jenkins_mod.JenkinsClient.fetch = lambda self, job: {"lastBuild": None}
    try:
        on = {c.name: c for c in _check_jenkins(load_config(wired))}["jenkins.analysis"]
        off = {c.name: c for c in _check_jenkins(load_config(unwired))}["jenkins.analysis"]
    finally:
        jenkins_mod.JenkinsClient.fetch = monkey

    assert on.status == "ok" and "'analyze'" in on.detail
    assert off.status == "skip" and "jenkins.analysis.skill" in off.detail


_OTHER_SKILL = """
skills:
  - name: poke
    enabled: true
    command: echo hi
"""


def test_an_unwired_skill_is_just_a_skill(tmp_path):
    """The complaint this shape answers: a skill does not turn a button on by
    being named something, or by declaring anything. Only the wiring does — and
    an unwired skill is an ordinary merge-request one."""
    path = tmp_path / "unwired.yaml"
    path.write_text(_BASE_CONFIG + _JENKINS_JOBS + _OTHER_SKILL, encoding="utf-8")
    config = load_config(path)

    assert config.analysis_skill is None
    assert config.skill_by_name("poke") is not None  # declared, just not wired

    db_path = tmp_path / "unwired.db"
    Database(db_path).close()
    client = TestClient(create_app(config, str(db_path), jenkins=_monitor()))
    assert "/jenkins/" not in client.get("/partials/ci").text  # no strip button


def test_the_shape_the_previous_release_documented_is_refused_not_misplaced(tmp_path):
    """`skills: - name: analyze` used to inherit the build-analysis capability
    from its name. With the wiring explicit it inherits only a label and an
    icon, so left unwired it would render "🔎 analyse" on every merge-request
    row and run a build command with no build. Refused, with the fix quoted."""
    path = tmp_path / "old.yaml"
    path.write_text(_BASE_CONFIG + _JENKINS_JOBS + _ANALYZE_SKILL, encoding="utf-8")

    with pytest.raises(ConfigError) as exc:
        load_config(path)

    message = str(exc.value)
    assert "nothing wires it to the CI strip" in message
    assert "skill: analyze" in message  # the line to paste


@pytest.mark.parametrize(
    "setting", ["context: jira", "include_context: true", "stores_result: true"]
)
def test_merge_request_settings_on_the_wired_skill_are_refused_not_ignored(tmp_path, setting):
    """A value radar reads and disregards is indistinguishable from one it
    honours, until somebody depends on it."""
    skills = _ANALYZE_SKILL.replace(
        "    command:", f"    {setting}\n    command:"
    )
    path = tmp_path / "inert.yaml"
    path.write_text(_BASE_CONFIG + _JENKINS_JOBS + _WIRING + skills, encoding="utf-8")

    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "do nothing for a build analysis" in str(exc.value)


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        ("  analyse: {enabled: true, skill: analyze}\n", "unknown key(s) 'analyse'"),
        ("  analysis: {enabled: true, skil: analyze}\n", "unknown key(s) 'skil'"),
    ],
)
def test_a_misspelled_key_is_refused_rather_than_dropped(tmp_path, block, expected):
    """The button is spelled "analyse" in every sentence and the key is
    "analysis", so this is the likeliest way to write the block — and an ignored
    key here reproduces exactly the silence this wiring exists to remove."""
    path = tmp_path / "typo.yaml"
    path.write_text(_BASE_CONFIG + _JENKINS_JOBS + block + _ANALYZE_SKILL, encoding="utf-8")

    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert expected in str(exc.value)


def test_an_unknown_job_name_is_refused(tmp_path):
    _, _, client = _app(tmp_path, _BASE_CONFIG + _WIRED, _monitor())

    assert client.post("/jenkins/not-a-job/analyze").status_code == 404
    assert client.get("/jenkins/not-a-job/analysis/1").status_code == 404


def test_a_green_job_cannot_be_analysed_even_by_url(tmp_path):
    """The chip stopped being broken between the render and the click."""
    _, _, client = _app(
        tmp_path, _BASE_CONFIG + _WIRED, _monitor("SUCCESS")
    )
    resp = client.post("/jenkins/backend-ci/analyze")

    assert resp.status_code == 409
    assert "no failed build to analyse" in resp.text


def test_a_stored_analysis_re_opens_for_that_build(tmp_path):
    _, db_path, client = _app(tmp_path, _BASE_CONFIG + _WIRED, _monitor())
    with Database(db_path) as db:
        db.save_build_analysis("backend-ci", 128, "analyze", "## It was the pyyaml bump")

    strip = client.get("/partials/ci").text
    assert "/jenkins/backend-ci/analysis/128" in strip  # the chip offers the saved one

    resp = client.get("/jenkins/backend-ci/analysis/128")
    assert resp.status_code == 200
    assert "It was the pyyaml bump" in resp.text
    assert client.get("/jenkins/backend-ci/analysis/999").status_code == 404
