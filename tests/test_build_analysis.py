"""Analysing what broke a build: the commit walk, the log tail, and the button.

Every fetch goes through an injected getter, so nothing here touches a Jenkins.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from radar.config import JenkinsJob, load_config
from radar.context import build_jenkins_input
from radar.db import Database
from radar.jenkins import (
    FAILED,
    RUNNING,
    JenkinsClient,
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
    def text_getter(url):
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
        {"start=1125899906842624": ("", {"X-Text-Size": "9000000"}), "start=8744000": (body, {})},
        calls,
    )

    log = fetch_log_tail(client, JOB, 128, lines=5)

    assert log.text.splitlines() == [f"line {i}" for i in range(995, 1000)]
    assert (log.lines, log.truncated) == (5, True)
    assert log.total_bytes == 9000000
    # The probe, then the tail — and the tail is asked for by byte offset.
    assert len(calls) == 2 and "start=8744000" in calls[1]  # 9,000,000 - 256,000


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
        text_getter=lambda url: (
            ("", {"X-Text-Size": "40"}) if "1125899906842624" in url else ("boom\ntraceback", {})
        ),
    )


def test_the_bundle_names_the_build_the_range_and_the_log():
    status = map_status(_payload(_build(128, "FAILURE")), JOB)
    text = build_jenkins_input(_analysis_client(), JOB, status, log_lines=400)

    assert "# Jenkins build failure: backend-ci #128" in text
    assert "Result: FAILED" in text
    assert "## Commits since the last successful build (#127)" in text
    assert "Cache the resolver" in text and "src/app.py" in text
    assert "## Console log" in text and "traceback" in text


def test_a_build_with_no_commits_says_so_rather_than_staying_silent():
    """An empty list is evidence: it points at the environment rather than at
    the change, and a skill told nothing would have to guess which."""
    status = map_status(_payload(_build(128, "FAILURE")), JOB)
    text = build_jenkins_input(_analysis_client(items=()), JOB, status, log_lines=400)

    assert "recorded no source changes" in text
    assert "environmental" in text


# --- the web layer ---------------------------------------------------------

_ANALYZE_SKILL = """
skills:
  - name: analyze
    enabled: true
    command: python -c "import sys; sys.stdout.write(sys.stdin.read())"
"""


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
    broken = _app(tmp_path, _BASE_CONFIG + _JENKINS_JOBS + _ANALYZE_SKILL, _monitor("FAILURE"))[2]
    green = _app(tmp_path, _BASE_CONFIG + _JENKINS_JOBS + _ANALYZE_SKILL, _monitor("SUCCESS"))[2]

    assert "/jenkins/backend-ci/analyze" in broken.get("/partials/ci").text
    assert "/analyze" not in green.get("/partials/ci").text


def test_an_analysis_skill_never_appears_on_a_merge_request_row(tmp_path):
    _, _, client = _app(tmp_path, _BASE_CONFIG + _JENKINS_JOBS + _ANALYZE_SKILL, _monitor())
    page = client.get("/").text

    # The board's own buttons post to /{kind}/{project}/{mr}; the strip's posts
    # to /jenkins/... . The analysis skill must only ever be the second.
    assert "/jenkins/backend-ci/analyze" in page
    assert 'hx-post="/analyze/' not in page


def test_analysing_runs_the_skill_and_stores_the_result(tmp_path):
    config, db_path, client = _app(
        tmp_path, _BASE_CONFIG + _JENKINS_JOBS + _ANALYZE_SKILL, _monitor()
    )

    resp = client.post("/jenkins/backend-ci/analyze")
    assert resp.status_code == 200
    assert "Build failure analysis" in resp.text
    assert "#128" in resp.text  # the panel heads with the build, not an MR


def test_an_unknown_job_name_is_refused(tmp_path):
    _, _, client = _app(tmp_path, _BASE_CONFIG + _JENKINS_JOBS + _ANALYZE_SKILL, _monitor())

    assert client.post("/jenkins/not-a-job/analyze").status_code == 404
    assert client.get("/jenkins/not-a-job/analysis/1").status_code == 404


def test_a_green_job_cannot_be_analysed_even_by_url(tmp_path):
    """The chip stopped being broken between the render and the click."""
    _, _, client = _app(
        tmp_path, _BASE_CONFIG + _JENKINS_JOBS + _ANALYZE_SKILL, _monitor("SUCCESS")
    )
    resp = client.post("/jenkins/backend-ci/analyze")

    assert resp.status_code == 409
    assert "no failed build to analyse" in resp.text


def test_a_stored_analysis_re_opens_for_that_build(tmp_path):
    _, db_path, client = _app(tmp_path, _BASE_CONFIG + _JENKINS_JOBS + _ANALYZE_SKILL, _monitor())
    with Database(db_path) as db:
        db.save_build_analysis("backend-ci", 128, "analyze", "## It was the pyyaml bump")

    strip = client.get("/partials/ci").text
    assert "/jenkins/backend-ci/analysis/128" in strip  # the chip offers the saved one

    resp = client.get("/jenkins/backend-ci/analysis/128")
    assert resp.status_code == 200
    assert "It was the pyyaml bump" in resp.text
    assert client.get("/jenkins/backend-ci/analysis/999").status_code == 404
