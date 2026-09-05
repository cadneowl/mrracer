"""Web layer smoke tests: templates render and the board reflects DB state."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from fastapi.testclient import TestClient

from radar import jenkins
from radar.db import Database
from radar.gitlab_client import FixtureSource
from radar.jenkins import JenkinsClient, JenkinsMonitor
from radar.poller import poll_once
from radar.web.app import create_app
from tests.test_jenkins import _build, _payload
from tests.test_poller import PID, PROJECT, _discussions, _mr


def _seed(db):
    src = FixtureSource(
        mrs_by_project={PROJECT: [_mr(["dan"])]},
        discussions_by_mr={(PID, 1): _discussions()},
    )
    return src


def test_dashboard_renders(config, tmp_path):
    db_path = tmp_path / "web.db"
    db = Database(db_path)
    poll_once(db, config, _seed(db))
    db.close()

    app = create_app(config, str(db_path))
    client = TestClient(app)

    resp = client.get("/")
    assert resp.status_code == 200
    assert "review SLA board" in resp.text
    assert "Add widget" in resp.text
    assert "dan" in resp.text

    partial = client.get("/partials/board")
    assert partial.status_code == 200
    assert "chip" in partial.text

    assert client.get("/healthz").json() == {"status": "ok"}


def test_empty_dashboard(config, tmp_path):
    db_path = tmp_path / "empty.db"
    Database(db_path).close()
    app = create_app(config, str(db_path))
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "No open merge requests" in resp.text


def test_refresh_polls_then_returns_the_new_board(config, tmp_path):
    """The whole point of the button: an MR that appears between polls is on the
    board in the response to the click, not one auto-refresh later."""
    db_path = tmp_path / "refresh.db"
    Database(db_path).close()

    # GitLab's state, as the fixture serves it — mutated below to stand for an
    # MR opened after the last scheduled pass.
    mrs, discussions = {PROJECT: []}, {}
    source = FixtureSource(mrs_by_project=mrs, discussions_by_mr=discussions)
    polls = []

    def poll_now():
        polls.append(1)
        with Database(db_path) as db:
            poll_once(db, config, source)

    client = TestClient(create_app(config, str(db_path), poll_now=poll_now))
    poll_now()  # the scheduled pass, before the MR exists
    assert "No open merge requests" in client.get("/").text
    assert "refresh now" in client.get("/").text

    mrs[PROJECT] = [_mr(["dan"])]
    discussions[(PID, 1)] = _discussions()

    resp = client.post("/refresh")
    assert resp.status_code == 200
    assert len(polls) == 2
    assert "Add widget" in resp.text
    assert "dan" in resp.text


def _jenkins(config, calls=None):
    """A monitor over the fixture's two jobs, already refreshed once: backend-ci
    red, nightly-e2e green."""
    answers = {
        "hub": _payload(_build(128, "FAILURE"), _build(128, "FAILURE")),
        "e2e": _payload(_build(9, "SUCCESS"), _build(9, "SUCCESS")),
    }

    def getter(url: str) -> dict:
        if calls is not None:
            calls.append(url)
        return next(payload for key, payload in answers.items() if f"/job/{key}" in url)

    monitor = JenkinsMonitor(config.jenkins.jobs, JenkinsClient(getter=getter))
    monitor.refresh()
    return monitor


def test_the_ci_strip_shows_a_dot_per_job_that_opens_the_build(jenkins_config, tmp_path):
    db_path = tmp_path / "ci.db"
    Database(db_path).close()
    client = TestClient(
        create_app(jenkins_config, str(db_path), jenkins=_jenkins(jenkins_config))
    )

    page = client.get("/").text
    assert 'id="ci-strip"' in page
    assert "backend-ci" in page and "nightly-e2e" in page
    assert "dot-failed" in page and "dot-success" in page
    # The link is the build, built from the configured job URL — click it and
    # you land on the run that broke, not on a job page to hunt through.
    assert "https://jenkins.example.com/job/hub/job/backend/job/main/128/" in page
    assert "1 failed" in page  # the summary says so without reading the dots

    partial = client.get("/partials/ci")
    assert partial.status_code == 200
    assert "nightly-e2e" in partial.text


def test_the_strip_is_served_from_cache_never_a_live_fetch(jenkins_config, tmp_path):
    """The guarantee the whole design rests on: a Jenkins call on the request
    path would make every board render as slow as the slowest Jenkins."""
    db_path = tmp_path / "ci-cache.db"
    Database(db_path).close()
    calls: list[str] = []
    monitor = _jenkins(jenkins_config, calls)
    assert len(calls) == 2  # the one background pass, one call per job

    client = TestClient(create_app(jenkins_config, str(db_path), jenkins=monitor))
    client.get("/")
    client.get("/partials/ci")
    client.get("/partials/board")

    assert len(calls) == 2  # no request asked Jenkins anything


def test_without_jenkins_jobs_there_is_no_strip(config, tmp_path):
    db_path = tmp_path / "no-ci.db"
    Database(db_path).close()
    client = TestClient(create_app(config, str(db_path)))

    assert 'id="ci-strip"' not in client.get("/").text
    assert client.get("/partials/ci").status_code == 404


def _css() -> str:
    return (Path(__file__).resolve().parent.parent / "radar/web/static/radar.css").read_text(
        encoding="utf-8"
    )


def test_every_state_the_strip_can_render_has_a_rule():
    """The strip builds its classes at runtime — `dot-{{ job.state }}` — so a
    state added to jenkins.py with no rule beside it renders as an invisible
    zero-size span, and no assertion on the HTML would notice: the markup is
    exactly right, it is the styling that is missing."""
    css = _css()
    states = [
        jenkins.SUCCESS,
        jenkins.UNSTABLE,
        jenkins.FAILED,
        jenkins.ABORTED,
        jenkins.NEVER,
        jenkins.UNKNOWN,
    ]

    assert [s for s in states if f".dot-{s}" not in css] == []
    # RUNNING draws a spinner rather than a dot, ringed in the previous state.
    assert [s for s in [*states, jenkins.UNKNOWN] if f".ring-{s}" not in css] == []


def test_the_templates_use_no_class_the_stylesheet_never_heard_of():
    """Catches the rename half-done in one file — the markup keeps saying
    `ci-job` while the rule became something else, or the reverse."""
    css_rules = set(re.findall(r"\.([a-zA-Z][\w-]*)", _css()))
    # Hooks that deliberately carry no styling of their own: JS toggles them, or
    # a parent selector reaches them, or they exist only to be found in a test.
    styleless = {"chip-name", "ci-name", "col-mr", "col-obl", "mr-title", "stat-open"}

    templates_dir = Path(__file__).resolve().parent.parent / "radar/web/templates"
    unknown: dict[str, str] = {}
    for template in sorted(templates_dir.glob("*.html")):
        for attr in re.findall(r'class="([^"]*)"', template.read_text(encoding="utf-8")):
            # Drop Jinja tags and interpolations; what is left is literal.
            for token in re.sub(r"\{%.*?%\}|\{\{.*?\}\}", " ", attr, flags=re.S).split():
                if re.fullmatch(r"[a-z][a-z0-9-]*[a-z0-9]", token) and token not in css_rules:
                    unknown.setdefault(token, template.name)

    assert {k: v for k, v in unknown.items() if k not in styleless} == {}


def test_the_stylesheet_url_changes_when_the_stylesheet_does(config, tmp_path):
    """Static files go out with an ETag and no Cache-Control, so the browser is
    left to guess how long they stay fresh — and for a file months old it
    guesses days, serving new markup against the rules from before the upgrade.
    The digest in the URL is what stops that, so it has to actually be there."""
    db_path = tmp_path / "assets.db"
    Database(db_path).close()
    client = TestClient(create_app(config, str(db_path)))

    href = re.search(r'href="(/static/radar\.css[^"]*)"', client.get("/").text).group(1)
    assert re.fullmatch(r"/static/radar\.css\?v=[0-9a-f]{12}", href)
    assert client.get(href).status_code == 200  # the query does not break serving

    digest = hashlib.sha256(_css().encode("utf-8")).hexdigest()[:12]
    assert href.endswith(digest)  # this file's content, not a build number


def test_refresh_is_absent_without_a_poller(config, tmp_path):
    """Read-only mode (no GitLab credentials): nothing to refresh with, so the
    board neither offers the button nor accepts the request behind it."""
    db_path = tmp_path / "readonly.db"
    Database(db_path).close()
    client = TestClient(create_app(config, str(db_path)))

    assert "refresh now" not in client.get("/").text
    assert client.post("/refresh").status_code == 404
