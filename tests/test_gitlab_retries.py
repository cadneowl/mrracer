"""Transient HTTP failures on the way to GitLab are retried.

A connection that dies mid-request is not an answer, and everything radar asks
GitLab for is an idempotent read — so the poller losing a pass, or a review
losing its diff, to a reset peer or a proxy recycling a connection is a failure
worth surviving rather than reporting. The retries live on the adapter of the
session python-gitlab talks over, so urllib3 retries the one request that
failed instead of radar re-running a fetch that had already pulled a large diff.

Exercised against a local server rather than a mock: what is being tested is
that the policy is really installed on the session python-gitlab uses, which a
stubbed adapter would assert about itself instead.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from radar.gitlab_client import GitLabSource


class _Flaky(BaseHTTPRequestHandler):
    """Fails the first two requests, then answers. Counts every hit."""

    fail_times = 2
    hits: dict = {}

    def _respond(self) -> None:
        seen = self.hits[self.command] = self.hits.get(self.command, 0) + 1
        code = 502 if seen <= self.fail_times else 200
        body = b'{"ok": true}'
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _respond

    def log_message(self, *args) -> None:
        pass          # the test's own output is the assertions


@pytest.fixture
def flaky():
    _Flaky.hits = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Flaky)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def no_backoff(monkeypatch):
    """The real policy sleeps between attempts; the test only needs the count.

    Patched on the class the way the policy is read, so what is under test is
    still the code that builds it.
    """
    monkeypatch.setattr(GitLabSource, "RETRY_BACKOFF_S", 0)


def test_a_read_that_fails_twice_is_retried_until_it_answers(flaky, no_backoff):
    source = GitLabSource(flaky, "token")

    response = source._gl.session.get(f"{flaky}/api/v4/projects")

    assert response.status_code == 200
    assert _Flaky.hits["GET"] == 3, "the first two 502s should have been retried"


def test_a_write_is_not_retried(flaky, no_backoff):
    """Only reads. A POST that may or may not have landed is not ours to send
    again — and radar makes exactly one kind of write, a note on an MR."""
    source = GitLabSource(flaky, "token")

    response = source._gl.session.post(f"{flaky}/api/v4/projects/1/notes")

    assert response.status_code == 502
    assert _Flaky.hits["POST"] == 1


def test_the_status_codes_worth_retrying_are_the_transient_ones(flaky):
    source = GitLabSource(flaky, "token")
    retry = source._gl.session.get_adapter(flaky).max_retries

    assert retry.total == GitLabSource.HTTP_RETRIES
    # A rate limit is transient and polite to wait on; a 404 or a 403 is an
    # answer, and asking three times does not change it.
    assert 429 in retry.status_forcelist and 503 in retry.status_forcelist
    assert 404 not in retry.status_forcelist and 403 not in retry.status_forcelist
    assert "POST" not in retry.allowed_methods and "GET" in retry.allowed_methods


class _Throttled(BaseHTTPRequestHandler):
    """Answers the first read with a 503 and a long `Retry-After`, then 200."""

    hits: dict = {}
    retry_after = "10"

    def do_GET(self) -> None:
        seen = self.hits["GET"] = self.hits.get("GET", 0) + 1
        body = b'{"ok": true}'
        self.send_response(503 if seen == 1 else 200)
        if seen == 1:
            self.send_header("Retry-After", self.retry_after)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def throttled():
    _Throttled.hits = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Throttled)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def test_the_backoff_is_radars_own_not_whatever_the_server_asks_for(throttled, no_backoff):
    """urllib3 obeys `Retry-After` to the second by default — up to six hours.

    Nothing here bounds that: `HTTP_TIMEOUT_S` is a socket timeout and this is a
    sleep. A GitLab or a proxy answering a throttled read with `Retry-After: 600`
    would park whichever thread asked — the poller's, whose pass then stops for
    ten minutes, or a skill's context fetch, which radar has its own deadline
    for and would have given up on long before the sleep ended.
    """
    source = GitLabSource(throttled, "token")

    started = time.monotonic()
    response = source._gl.session.get(f"{throttled}/api/v4/projects")
    took = time.monotonic() - started

    assert response.status_code == 200 and _Throttled.hits["GET"] == 2
    assert took < 3, f"slept {took:.0f}s on the server's word instead of radar's"
    assert source._gl.session.get_adapter(throttled).max_retries \
        .respect_retry_after_header is False


def test_a_client_with_no_session_still_builds(monkeypatch, caplog):
    """Best effort: a python-gitlab that stops exposing a requests session
    costs radar the retries, not the poller."""
    import gitlab

    class Sessionless:
        session = None

        def __init__(self, *args, **kwargs) -> None:
            pass

    monkeypatch.setattr(gitlab, "Gitlab", Sessionless)
    source = GitLabSource("http://gitlab.invalid", "token")   # must not raise

    assert isinstance(source._gl, Sessionless)
