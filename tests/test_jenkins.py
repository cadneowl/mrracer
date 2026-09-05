"""Jenkins watching: the state table, the monitor's isolation, and the strip.

Nothing here touches the network — every fetch goes through an injected getter,
or a monkeypatched urlopen for the two tests that are *about* the HTTP layer.
"""

from __future__ import annotations

import base64
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest

from radar.config import JenkinsJob
from radar.jenkins import (
    ABORTED,
    FAILED,
    NEVER,
    RUNNING,
    SUCCESS,
    UNKNOWN,
    UNSTABLE,
    JenkinsClient,
    JenkinsError,
    JenkinsMonitor,
    fetch_all,
    job_view,
    map_status,
    strip_view,
)

JOB = JenkinsJob(name="backend-ci", url="https://jenkins.example.com/job/hub/job/backend/job/main")
OTHER = JenkinsJob(name="nightly-e2e", url="https://jenkins.example.com/job/e2e")
NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def _build(number=128, result="SUCCESS", *, building=False, ago_min=25, duration_s=192, est_s=None):
    started = NOW - timedelta(minutes=ago_min)
    build = {
        "number": number,
        "building": building,
        "result": result,
        "timestamp": int(started.timestamp() * 1000),
    }
    if duration_s is not None:
        build["duration"] = duration_s * 1000
    if est_s is not None:
        build["estimatedDuration"] = est_s * 1000
    return build


def _payload(last, completed=None):
    return {
        "_class": "org.jenkinsci.plugins.workflow.job.WorkflowJob",
        "lastBuild": last,
        "lastCompletedBuild": completed,
    }


def _client(answers: dict | None = None) -> JenkinsClient:
    """A client answering per job — the key matches a segment of the job's URL.

    Takes the dict itself rather than keywords, so a test can change what
    Jenkins says between two passes.
    """
    answers = {} if answers is None else answers

    def getter(url: str) -> dict:
        for key, answer in answers.items():
            if f"/job/{key}" in url:
                if isinstance(answer, BaseException):
                    raise answer
                return answer
        raise AssertionError(f"no fixture for {url}")

    return JenkinsClient(getter=getter)


# --- the state table -------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ("SUCCESS", SUCCESS),
        ("UNSTABLE", UNSTABLE),
        ("FAILURE", FAILED),
        ("ABORTED", ABORTED),
        ("NOT_BUILT", ABORTED),
    ],
)
def test_a_finished_build_maps_to_its_own_colour(result, expected):
    """UNSTABLE and ABORTED are deliberately not red: tests failing on a build
    that compiled, and a build nobody let finish, are not the same news."""
    status = map_status(_payload(_build(result=result)), JOB)
    assert status.state == expected
    assert status.build_number == 128


def test_a_running_build_keeps_the_previous_result_visible():
    """A build running over a red job spins in red — otherwise starting a build
    would make the breakage look resolved."""
    payload = _payload(
        _build(129, None, building=True, ago_min=2, duration_s=None, est_s=180),
        _build(128, "FAILURE"),
    )
    status = map_status(payload, JOB)

    assert (status.state, status.previous) == (RUNNING, FAILED)
    assert job_view(status, NOW)["ring"] == FAILED


def test_a_job_that_never_ran_is_not_a_failure():
    status = map_status(_payload(None, None), JOB)
    assert status.state == NEVER
    assert status.link == JOB.url  # nothing to link to but the job itself


def test_a_build_between_finishing_and_being_recorded_uses_the_completed_one():
    """Jenkins reports a just-stopped build with a null result for a moment;
    reading that as unknown would flicker the strip on every build."""
    payload = _payload(_build(130, None, duration_s=None), _build(129, "SUCCESS", duration_s=5))
    assert map_status(payload, JOB).state == SUCCESS


def test_a_folder_url_says_so_instead_of_going_grey():
    """The commonest way to get this config wrong is pointing at the multibranch
    project rather than one of its branch jobs."""
    payload = {"_class": "com.cloudbees.hudson.plugins.folder.Folder", "displayName": "hub"}
    status = map_status(payload, JOB)

    assert status.state == UNKNOWN
    assert "folder" in status.message and "branch job" in status.message


# --- links -----------------------------------------------------------------


def test_the_build_link_is_built_from_the_configured_url():
    """Never from Jenkins' own reported url: that comes from its "Jenkins URL"
    setting, which is routinely an internal host the browser cannot reach — and
    it would be a remote-controlled value landing in an href."""
    payload = _payload(_build())
    payload["url"] = "http://jenkins.internal:8080/job/hub/job/backend/job/main/128/"

    assert map_status(payload, JOB).link == (
        "https://jenkins.example.com/job/hub/job/backend/job/main/128/"
    )


# --- the monitor -----------------------------------------------------------


def test_one_unreachable_job_does_not_blank_the_others():
    monitor = JenkinsMonitor(
        [JOB, OTHER],
        _client({"hub": _payload(_build()), "e2e": JenkinsError("unreachable: timed out")}),
    )
    monitor.refresh()
    backend, e2e = monitor.snapshot().jobs

    assert (backend.name, backend.state) == ("backend-ci", SUCCESS)
    assert (e2e.state, e2e.stale) == (UNKNOWN, True)
    assert "timed out" in e2e.message


def test_a_failed_pass_keeps_the_last_known_state_but_marks_it_stale():
    """A network blip should not repaint a whole board grey — but the strip must
    not go on claiming the build is green either, so the chip is dimmed and the
    summary counts it as unreachable rather than as a pass."""
    answers = {"hub": _payload(_build(result="SUCCESS"))}
    monitor = JenkinsMonitor([JOB], _client(answers))
    monitor.refresh()
    assert monitor.snapshot().jobs[0].state == SUCCESS

    answers["hub"] = JenkinsError("unreachable: connection refused")
    monitor.refresh()
    status = monitor.snapshot().jobs[0]

    assert (status.state, status.stale) == (SUCCESS, True)
    assert strip_view(monitor.snapshot(), NOW)["summary"] == "1 unreachable"


def test_refresh_survives_an_error_no_one_predicted():
    """The scheduler calls this on a timer; it must never take the loop down."""
    monitor = JenkinsMonitor([JOB], _client({"hub": RuntimeError("boom")}))
    monitor.refresh()  # does not raise

    assert monitor.snapshot().jobs[0].state == UNKNOWN


def test_the_strip_reads_in_configured_order():
    monitor = JenkinsMonitor([JOB, OTHER], _client())
    assert [job["name"] for job in strip_view(monitor.snapshot(), NOW)["jobs"]] == [
        "backend-ci",
        "nightly-e2e",
    ]


def test_a_board_opened_before_the_first_pass_does_not_cry_outage():
    """The chips are unknown for the second before the first pass lands, but
    that is radar not having looked yet — announcing an outage on every freshly
    started board would teach people to ignore the real one."""
    view = strip_view(JenkinsMonitor([JOB, OTHER], _client()).snapshot(), NOW)

    assert view["summary"] == "checking…"
    assert "unreachable" not in view["summary"]


def test_jobs_are_fetched_together_rather_than_one_after_another():
    """Serially a pass costs one timeout per unreachable job: at the minimum 15s
    interval two dead jobs already overrun it, and the scheduler then drops the
    passes it cannot start while the board silently ages."""
    guard = threading.Lock()
    state = {"inside": 0, "most_at_once": 0}

    def getter(url: str) -> dict:
        with guard:
            state["inside"] += 1
            state["most_at_once"] = max(state["most_at_once"], state["inside"])
        try:
            time.sleep(0.05)
            return _payload(_build())
        finally:
            with guard:
                state["inside"] -= 1

    jobs = [JenkinsJob(f"j{i}", f"https://jenkins.example.com/job/j{i}") for i in range(4)]
    results = fetch_all(JenkinsClient(getter=getter), jobs)

    assert state["most_at_once"] > 1
    assert [r.state for r in results.values()] == [SUCCESS] * 4


def test_all_green_is_only_claimed_when_everything_is_green():
    monitor = JenkinsMonitor(
        [JOB, OTHER],
        _client({"hub": _payload(_build()), "e2e": _payload(_build(9, "UNSTABLE"))}),
    )
    monitor.refresh()
    view = strip_view(monitor.snapshot(), NOW)

    assert view["all_green"] is False
    assert view["summary"] == "1 unstable"

    monitor = JenkinsMonitor([JOB], _client({"hub": _payload(_build())}))
    monitor.refresh()
    assert strip_view(monitor.snapshot(), NOW)["summary"] == "all green"


# --- what the chip says ----------------------------------------------------


def test_the_tooltip_names_the_build_and_when_it_ran():
    status = map_status(_payload(_build(result="FAILURE")), JOB)
    detail = job_view(status, NOW)["detail"]

    assert detail == "#128 · failed 21m ago · took 3m12s"  # 25m ago, ran 3m12s


def test_a_running_build_shows_elapsed_against_the_usual_time():
    payload = _payload(_build(129, None, building=True, ago_min=1, duration_s=None, est_s=180))
    detail = job_view(map_status(payload, JOB), NOW)["detail"]

    assert detail == "#129 · running 1m00s · usually 3m00s"


def test_a_jenkins_clock_ahead_of_ours_never_renders_negative_elapsed():
    """Routine drift between two hosts, and guaranteed for the first seconds of
    a build when Jenkins' clock is a little fast. "running -42s" reads as a bug
    in radar, not as a fact about the build."""
    started_in_the_future = _build(129, None, building=True, ago_min=-2, duration_s=None)
    detail = job_view(map_status(_payload(started_in_the_future), JOB), NOW)["detail"]

    assert detail == "#129 · running 0s"


def test_the_tree_query_asks_for_every_field_the_fixtures_carry():
    """Jenkins returns exactly the fields `tree` names, so a fixture carrying one
    the query never asks for exercises a payload production cannot receive. That
    is how a missing `duration` hid: real tooltips lost "took 3m12s" and dated
    builds from their start rather than their finish, and every test passed."""
    from radar.jenkins import _TREE

    requested = set(_TREE.split("lastBuild[", 1)[1].split("]", 1)[0].split(","))
    assert set(_build(est_s=1)) <= requested


def test_a_folder_url_is_flagged_rather_than_left_looking_like_a_new_job():
    """Unknown and never-built both mean "no build to show", but only one of
    them means radar could not tell — a misconfigured URL must not read as a
    healthy job waiting for its first run."""
    folder = job_view(map_status({"_class": "...Folder"}, JOB), NOW)
    never = job_view(map_status(_payload(None), JOB), NOW)

    assert (folder["state"], folder["warn"]) == (UNKNOWN, True)
    assert (never["state"], never["warn"]) == (NEVER, False)


# --- the HTTP layer --------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _answer_with(monkeypatch, body: bytes | BaseException, seen: list | None = None):
    """Stand in for the network at the opener, which is the layer the client
    actually calls (it builds its own, to strip auth across a redirect)."""

    def fake_open(self, req, timeout=None):
        if seen is not None:
            seen.append(dict(req.headers))
        if isinstance(body, BaseException):
            raise body
        return _FakeResponse(body)

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", fake_open)


def test_credentials_are_sent_as_basic_auth_and_omitted_when_absent(monkeypatch):
    seen: list[dict] = []
    _answer_with(monkeypatch, b'{"lastBuild": null}', seen)

    JenkinsClient(credentials=("ci-bot", "shhh")).fetch(JOB)
    JenkinsClient().fetch(JOB)

    expected = "Basic " + base64.b64encode(b"ci-bot:shhh").decode()
    assert seen[0]["Authorization"] == expected
    assert "Authorization" not in seen[1]  # anonymous read, no header at all


def test_a_redirect_off_the_host_does_not_carry_the_token(monkeypatch):
    """urllib copies every header but content-length/content-type onto a
    redirect target, so a Jenkins fronted by a proxy that 302s /api/json to an
    SSO host would be handed radar's API token — for a host nobody named."""
    from radar.jenkins import _DropAuthOffHost

    handler = _DropAuthOffHost()
    req = urllib.request.Request(
        f"{JOB.url}/api/json", headers={"Authorization": "Basic zzz", "Accept": "*/*"}
    )

    off_host = handler.redirect_request(req, None, 302, "Found", {}, "https://sso.example.com/in")
    same_host = handler.redirect_request(
        req, None, 302, "Found", {}, "https://jenkins.example.com/job/hub/"
    )

    assert "Authorization" not in off_host.headers
    assert off_host.headers["Accept"] == "*/*"  # only the credential is dropped
    assert same_host.headers["Authorization"] == "Basic zzz"  # http->https still works


def test_certificates_are_verified_unless_the_config_turns_it_off():
    """Asserted on the SSL context the opener will actually use, rather than on
    the flag having been passed: the whole value of the default is that it is
    the connection that verifies, not that a boolean said so."""

    def https_context(client: JenkinsClient) -> ssl.SSLContext:
        handler = next(
            h for h in client._opener.handlers if isinstance(h, urllib.request.HTTPSHandler)
        )
        return handler._context

    default = https_context(JenkinsClient())
    assert (default.verify_mode, default.check_hostname) == (ssl.CERT_REQUIRED, True)

    unverified = https_context(JenkinsClient(verify_ssl=False))
    assert (unverified.verify_mode, unverified.check_hostname) == (ssl.CERT_NONE, False)


def test_a_login_page_is_reported_as_auth_rather_than_as_an_outage(monkeypatch):
    """An SSO proxy answers 200 with HTML, never a 4xx, so the status-code path
    never fires — and "unreachable: Expecting value: line 1 column 1" sends the
    operator hunting for a firewall."""
    _answer_with(monkeypatch, b"<html><body>Please sign in</body></html>")

    with pytest.raises(JenkinsError) as exc:
        JenkinsClient().fetch(JOB)
    assert "not JSON" in str(exc.value) and "JENKINS_USER" in str(exc.value)


def test_a_certificate_failure_names_its_own_fix(monkeypatch):
    """The host answered — calling it "unreachable" sends the reader hunting for
    a firewall, which is exactly the round trip this message exists to save."""
    verify_failed = ssl.SSLCertVerificationError(
        "certificate verify failed: self-signed certificate in certificate chain"
    )
    _answer_with(monkeypatch, urllib.error.URLError(verify_failed))

    with pytest.raises(JenkinsError) as exc:
        JenkinsClient().fetch(JOB)

    message = str(exc.value)
    assert "certificate not trusted" in message
    assert "SSL_CERT_FILE" in message and "verify_ssl" in message
    assert "unreachable" not in message
    message.encode("cp1252")  # it reaches `radar check`, which prints to one


def test_a_connection_dying_mid_body_is_still_a_jenkins_error(monkeypatch):
    """urlopen wraps connect-time failures in URLError, but the body is read
    after it returns: a reset there (routine through a proxy) would otherwise
    escape this module's contract as a raw traceback on every pass."""
    _answer_with(monkeypatch, ConnectionResetError(104, "Connection reset by peer"))

    with pytest.raises(JenkinsError, match="unreachable"):
        JenkinsClient().fetch(JOB)


def test_failure_messages_survive_a_windows_console():
    """`radar check` prints these straight to stdout, which is cp1252 on Windows.
    A message with, say, an arrow in it would not just look wrong — it would
    raise UnicodeEncodeError and take the whole check run down with it."""
    from radar.jenkins import _http_message

    for code in (401, 403, 404, 500):
        _http_message(code).encode("cp1252")
    map_status({"_class": "...Folder"}, JOB).message.encode("cp1252")


@pytest.mark.parametrize(
    ("code", "expected"),
    [(403, "JENKINS_USER"), (404, "not a build"), (500, "HTTP 500")],
)
def test_an_http_failure_says_what_to_do_about_it(monkeypatch, code, expected):
    _answer_with(monkeypatch, urllib.error.HTTPError(JOB.url, code, "nope", {}, None))

    with pytest.raises(JenkinsError) as exc:
        JenkinsClient().fetch(JOB)
    assert expected in str(exc.value)
