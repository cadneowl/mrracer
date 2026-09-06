"""FastAPI application factory for the dashboard.

A fresh SQLite connection is opened per request (cheap, and safe under
uvicorn's threadpool; WAL mode allows concurrent readers), so the web layer
holds no long-lived DB handle.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path

import markdown as md
import nh3
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from ..coach import build_coach
from ..commands import SNAPSHOT_KEYS, CommandJob, CommandRunner
from ..config import Config
from ..context import jenkins_stdin_provider_for, stdin_provider_for
from ..db import Database
from ..jenkins import JenkinsMonitor, analysable_build, strip_view
from ..jira import extract_keys
from ..service import build_dashboard, build_threads

_BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_BASE / "templates"))


def _asset_version(path: Path) -> str:
    """A short digest of a static file, for cache-busting its URL.

    StaticFiles answers with an ETag and a Last-Modified and no Cache-Control,
    which leaves the browser to guess how long the file stays fresh — and the
    usual guess is a fraction of the file's age, so a stylesheet that has been
    on disk for months is reused for days without so much as a revalidation.
    The page then renders new markup against old rules: every class added in the
    upgrade has no styling at all, which looks like broken CSS rather than like
    a cache. Putting the digest in the URL makes an upgraded file a different
    URL, so it cannot be answered from a cache filled by the old one.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]

COOKIE_NAME = "radar_view"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year


# Review output is untrusted HTML: it comes from an external command whose
# input includes attacker-influenceable MR content (diffs, titles, comments).
# So we render markdown, then sanitize the resulting HTML against a strict
# allowlist before marking it safe — no <script>, event handlers, or js: URLs.
_ALLOWED_TAGS = {
    "a", "p", "br", "hr", "pre", "code", "blockquote", "em", "strong", "del", "ins",
    "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "tr", "th", "td", "span",
}
_ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "code": {"class"},
    "span": {"class"},
    "pre": {"class"},
    "th": {"align"},
    "td": {"align"},
}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


_STREAM_TICK = 0.4      # seconds between progress polls on the SSE stream
_CLOCK_EVERY = 12       # ticks between countdown re-anchors (~5s)

# How often the browser re-reads the CI strip. This is a read of an in-process
# cache, not a Jenkins call — Jenkins itself is polled on the scheduler's own
# `jenkins.poll_interval_seconds` — so it is cheap enough to keep well under
# that interval and leave the screen close to the cache.
_CI_TICK_S = 15


def _remaining_s(job: CommandJob | None, status: str | None = None) -> int | None:
    """Seconds left of a running job's budget, or None if it has no clock left
    to show (unknown job, or one that already finished).

    ``status`` is passed in by a caller that already read it, so a worker
    flipping the job mid-render can't have the panel take the running branch
    while this one decides there is no clock — the two would disagree and the
    countdown would silently not render. Measured on the monotonic clock the
    worker enforces the budget with, not on wall time.
    """
    if job is None or not job.budget_s:
        return None
    if (status if status is not None else job.status) != "running":
        return None
    return max(0, int(job.started_mono + job.budget_s - time.monotonic()))


def _clock_text(remaining_s: int | None) -> str:
    """"7:03 left", rendered server-side so the pill is never a blank box."""
    if remaining_s is None:
        return ""
    return f"{remaining_s // 60}:{remaining_s % 60:02d} left"


def _render_markdown(text: str) -> Markup:
    html = md.markdown(text, extensions=["fenced_code", "tables", "sane_lists"])
    clean = nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        url_schemes={"http", "https", "mailto"},
    )
    return Markup(clean)


def create_app(
    config: Config,
    db_path: str,
    poll_now: Callable[[], object] | None = None,
    jenkins: JenkinsMonitor | None = None,
) -> FastAPI:
    """Build the dashboard app.

    ``poll_now`` runs one GitLab polling pass and returns when it has stored
    what it found; ``radar serve`` passes the background poller's own pass.
    Without it (no GitLab credentials, or a test) the board is read-only over
    existing data and the refresh button is not offered.

    ``jenkins`` is the CI strip's ``JenkinsMonitor``, kept fresh by the
    background scheduler. The web layer only ever reads its snapshot: Jenkins is
    never fetched while a request is waiting. None means no jobs are configured,
    and no strip is rendered at all.
    """
    app = FastAPI(title="radar", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(_BASE / "static")), name="static")
    # Read once at startup: the file cannot change under a running server
    # without a restart bringing the new code that goes with it.
    templates.env.globals["css_version"] = _asset_version(_BASE / "static" / "radar.css")
    skills_by_name = {s.name: s for s in config.skills}
    runners = {s.name: CommandRunner(s, s.name) for s in config.skills}
    enabled = {s.name: s.enabled for s in config.skills}

    def _skill_view(s) -> dict:
        return {"name": s.name, "label": s.label, "button": s.button, "icon": s.icon}

    # Skills that persist output: the board shows a re-openable badge per skill
    # that has a stored result for a given MR (row.stored_kinds decides which).
    storing_skills = [
        _skill_view(s)
        for s in config.skills
        if s.stores_result and s is not config.analysis_skill
    ]

    def context(view: str | None) -> dict:
        with Database(db_path) as db:
            data = build_dashboard(db, config, view=view)
        data["poll_interval_minutes"] = config.gitlab.poll_interval_minutes
        data["can_refresh"] = poll_now is not None
        data["enabled_skills"] = [
            _skill_view(s)
            for s in config.skills
            if s.enabled and s is not config.analysis_skill
        ]
        data["storing_skills"] = storing_skills
        return data

    # Named in `jenkins.analysis.skill`, resolved once at load. Nothing here
    # infers it from a skill's name or contexts: which button exists is a fact
    # about the configuration, and the config says it in one place.
    build_skill = config.analysis_skill

    def _ci_view() -> dict | None:
        """The CI strip from cache — no I/O beyond the stored-analysis lookup."""
        if jenkins is None:
            return None
        analysed: set[tuple[str, int, str]] = set()
        if build_skill is not None:
            with Database(db_path) as db:
                analysed = db.analysed_builds()
        view = strip_view(
            jenkins.snapshot(),
            analysed=analysed,
            skill=build_skill.name if build_skill else "",
        )
        return view | {
            "tick_s": _CI_TICK_S,
            "skill": _skill_view(build_skill) if build_skill else None,
        }

    def _jenkins_job(job_name: str):
        """The configured job by that name, or a 404.

        Looked up rather than trusted: the name arrives in a URL, and every
        request that follows it reaches a URL built from the config entry.
        """
        job = next((j for j in config.jenkins.jobs if j.name == job_name), None)
        if job is None:
            raise HTTPException(status_code=404, detail=f"no Jenkins job named {job_name!r}")
        return job

    def _panel(request: Request, job, generated_at: str | None = None) -> HTMLResponse:
        skill = skills_by_name.get(job.kind)
        # Read the job's mutable state ONCE, and render from that read. The
        # worker thread flips status while this request is being served, so a
        # template that re-read `job.status` could take the "done" branch with
        # the output captured a moment earlier, while it was still running —
        # and the done fragment stops polling, so the panel would stay empty.
        # Status first: the runner publishes output and error *before* the
        # status that advertises them, so "done" here always has its output.
        status = job.status
        output, error = job.output, job.error
        remaining_s = _remaining_s(job, status)
        return templates.TemplateResponse(
            request,
            "_command_panel.html",
            {
                "job": job,
                "status": status,
                "error": error,
                "kind": job.kind,
                "heading": skill.label if skill else job.kind,
                "icon": skill.icon if skill else "▶",
                "generated_at": generated_at,
                # Seconds left of the worker's budget, for the panel's
                # countdown; None once there is no clock left to show. Uses the
                # status read above, not a fresh one — see _remaining_s.
                "remaining_s": remaining_s,
                "clock_text": _clock_text(remaining_s),
                # Also rendered for a failed job: a run killed by the timeout
                # keeps whatever it had written, and half a review beats none.
                "output_html": _render_markdown(output) if output.strip() else None,
                # The markdown itself, for the copy button. Rendered into a
                # textarea, so Jinja's escaping is what keeps skill output —
                # which is untrusted — from breaking out of it.
                "output": output if output.strip() else "",
            },
        )

    def _ctx_for(snap: dict, project_id: int, mr_iid: int) -> tuple[dict, list[str]]:
        keys = extract_keys(
            [snap.get("title"), snap.get("source_branch"), snap.get("description")],
            config.jira.project_keys,
        )
        ctx = {"project_id": project_id, "mr_iid": mr_iid, "subject": f"!{mr_iid}"}
        ctx.update({k: snap.get(k, "") for k in SNAPSHOT_KEYS})
        ctx["jira_keys"] = " ".join(keys)
        ctx["jira_keys_csv"] = ",".join(keys)
        return ctx, keys

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, view: str | None = None):
        # `view` present -> explicit choice (empty string clears the filter);
        # absent -> fall back to the remembered cookie.
        cookie = request.cookies.get(COOKIE_NAME) or None
        token = (view or None) if view is not None else cookie

        resp = templates.TemplateResponse(
            request, "dashboard.html", context(token) | {"ci": _ci_view()}
        )
        if view is not None:
            if token:
                resp.set_cookie(COOKIE_NAME, token, max_age=COOKIE_MAX_AGE, samesite="lax")
            else:
                resp.delete_cookie(COOKIE_NAME)
        return resp

    @app.get("/partials/board", response_class=HTMLResponse)
    def board(request: Request):
        # Auto-refresh preserves the remembered filter via the cookie.
        token = request.cookies.get(COOKIE_NAME) or None
        return templates.TemplateResponse(request, "_board.html", context(token))

    @app.get("/partials/ci", response_class=HTMLResponse)
    def ci(request: Request):
        """The CI strip's own refresh, on its own cadence.

        Separate from the board partial on purpose: builds start and finish far
        faster than the board's 60s tick, and the strip must not be swept away
        with the board when someone changes the view filter.
        """
        view = _ci_view()
        if view is None:
            raise HTTPException(status_code=404, detail="no Jenkins jobs are configured")
        return templates.TemplateResponse(request, "_ci.html", {"ci": view})

    # Declared ahead of the /{kind}/... routes, like /partials and /threads: a
    # skill named "jenkins" cannot shadow these, and the order says so rather
    # than leaving it to chance.
    @app.post("/jenkins/{job_name}/analyze", response_class=HTMLResponse)
    def analyze_build(request: Request, job_name: str):
        """Run the build-analysis skill over the job's last failed build."""
        if build_skill is None or jenkins is None:
            raise HTTPException(status_code=404, detail="no build-analysis skill is enabled")
        job = _jenkins_job(job_name)
        status = next((s for s in jenkins.snapshot().jobs if s.name == job_name), None)
        build = analysable_build(status) if status else None
        if build is None:
            # The chip stopped being broken between the render and the click —
            # a build went green, or radar lost contact with Jenkins.
            raise HTTPException(
                status_code=409, detail=f"{job_name} has no failed build to analyse right now"
            )

        kind = build_skill.name
        ctx = {
            "jenkins_job": job.name,
            "build_number": build,
            "build_url": f"{job.url}/{build}/",
            "title": job.name,
            "subject": f"#{build}",
        }
        client = jenkins.client

        def on_success(finished) -> None:
            with Database(db_path) as db:
                db.save_build_analysis(job.name, build, kind, finished.output)

        provider = jenkins_stdin_provider_for(kind, config, client, job, status, build)
        started = runners[kind].start(ctx, on_success=on_success, stdin_provider=provider)
        return _panel(request, started)

    @app.get("/jenkins/{job_name}/analysis/{build}", response_class=HTMLResponse)
    def stored_analysis(request: Request, job_name: str, build: int):
        """Re-open the analysis already stored for that exact build."""
        if build_skill is None:
            raise HTTPException(status_code=404, detail="no build-analysis skill is enabled")
        job = _jenkins_job(job_name)
        with Database(db_path) as db:
            stored = db.get_build_analysis(job.name, build, build_skill.name)
        if stored is None:
            raise HTTPException(status_code=404, detail="no stored analysis for that build")
        job_record = CommandJob(
            id="stored",
            kind=build_skill.name,
            subject=f"#{build}",
            title=job.name,
            status="done",
            output=stored["content"],
        )
        return _panel(request, job_record, generated_at=stored["generated_at"])

    @app.post("/refresh", response_class=HTMLResponse)
    def refresh(request: Request):
        """Poll GitLab now, then answer with the board built from what it stored.

        Synchronous on purpose. The click means "someone just asked me to review
        something — show it", so what gets swapped in has to be the board *after*
        the pass, not a promise that one is coming: a fire-and-forget kick would
        return the same stale board the button was pressed to escape.
        """
        if poll_now is None:
            raise HTTPException(status_code=404, detail="polling is not configured")
        poll_now()
        return board(request)

    # Declared ahead of the /{kind}/... routes: those all carry a literal second
    # segment ('status', 'stored', 'close') so they cannot shadow this, but the
    # order makes that independent of a skill ever being named "threads".
    @app.get("/threads/{project_id}/{mr_iid}", response_class=HTMLResponse)
    def threads(request: Request, project_id: int, mr_iid: int, author: str | None = None):
        with Database(db_path) as db:
            data = build_threads(db, project_id, mr_iid, author=author or None)
        if data is None:
            raise HTTPException(status_code=404, detail="unknown merge request")
        # Comment bodies are markdown written by anyone who can see the MR, so
        # they go through the same render-then-sanitize path as skill output.
        for thread in data["threads"]:
            for note in thread["notes"]:
                note["body_html"] = _render_markdown(note["body"])
        return templates.TemplateResponse(request, "_threads.html", data)

    @app.post("/{kind}/{project_id}/{mr_iid}", response_class=HTMLResponse)
    def start_command(request: Request, kind: str, project_id: int, mr_iid: int):
        if kind not in runners or not enabled[kind]:
            raise HTTPException(status_code=404, detail=f"{kind} is not enabled")
        if skills_by_name[kind] is config.analysis_skill:
            # It is about a build, and this route is about a merge request: it
            # would run with no context at all and file the result where nothing
            # would ever show it. The board never offers this, but the URL is
            # guessable and the refusal belongs here rather than in the template.
            raise HTTPException(
                status_code=404,
                detail=f"{kind} analyses Jenkins builds, not merge requests",
            )
        with Database(db_path) as db:
            snap = db.get_snapshot(project_id, mr_iid)
        if snap is None:
            raise HTTPException(status_code=404, detail="unknown merge request")
        ctx, keys = _ctx_for(snap, project_id, mr_iid)

        on_success = None
        if skills_by_name[kind].stores_result:
            csv = ",".join(keys)

            def on_success(job) -> None:
                with Database(db_path) as db:
                    db.save_test_plan(project_id, mr_iid, kind, csv, job.output)

        stdin_provider = stdin_provider_for(kind, config, project_id, mr_iid, keys)
        job = runners[kind].start(ctx, on_success=on_success, stdin_provider=stdin_provider)
        return _panel(request, job)

    @app.get("/{kind}/status/{job_id}", response_class=HTMLResponse)
    def command_status(request: Request, kind: str, job_id: str):
        if kind not in runners:
            raise HTTPException(status_code=404, detail="unknown kind")
        job = runners[kind].get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return _panel(request, job)

    @app.get("/{kind}/stream/{job_id}")
    def command_stream(kind: str, job_id: str):
        # Server-Sent Events: tail the job's progress log live, then a single
        # `end` event carrying the terminal status. The browser renders the
        # final result by re-fetching /status on `end`.
        if kind not in runners:
            raise HTTPException(status_code=404, detail="unknown kind")
        runner = runners[kind]

        # Outlive the job it is tailing: a skill given a long timeout_seconds
        # would otherwise have its stream cut mid-review and the panel would
        # reconnect for no reason. Still bounded, so a wedged job can't hold a
        # connection open forever.
        ticks = int((runner.config.timeout_seconds + 120) / _STREAM_TICK)

        async def gen():
            seen = 0
            for tick in range(ticks):
                snap = runner.progress_since(job_id, seen)
                if snap is None:
                    yield _sse("end", {"status": "error"})
                    return
                items, status = snap
                for item in items:
                    seen = max(seen, item.pop("rev"))
                    yield _sse("progress", item)
                # The countdown ticks in the browser every second; this only has
                # to correct it, so it goes out every few seconds rather than on
                # every poll — a long-running skill would otherwise spend
                # thousands of frames saying nothing new.
                if tick % _CLOCK_EVERY == 0 or status != "running":
                    yield _sse("clock", {"remaining_s": _remaining_s(runner.get(job_id))})
                if status != "running":
                    yield _sse("end", {"status": status})
                    return
                await asyncio.sleep(_STREAM_TICK)
            yield _sse("end", {"status": "timeout"})

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/{kind}/close", response_class=HTMLResponse)
    def command_close(kind: str):
        return HTMLResponse("")  # htmx swaps this empty content in to dismiss

    @app.get("/{kind}/stored/{project_id}/{mr_iid}", response_class=HTMLResponse)
    def stored_plan(request: Request, kind: str, project_id: int, mr_iid: int):
        skill = skills_by_name.get(kind)
        if skill is None or not skill.stores_result or skill is config.analysis_skill:
            raise HTTPException(status_code=404, detail=f"{kind} has no stored results")
        with Database(db_path) as db:
            plan = db.get_test_plan(project_id, mr_iid, kind)
        if plan is None:
            raise HTTPException(status_code=404, detail="no stored result")
        job = CommandJob(
            id="stored", kind=kind, project_id=project_id, mr_iid=mr_iid,
            subject=f"!{mr_iid}", title=f"{plan['jira_keys']}",
            status="done", output=plan["content"],
        )
        return _panel(request, job, generated_at=plan["generated_at"])

    @app.get("/coach", response_class=HTMLResponse)
    def coach(request: Request):
        with Database(db_path) as db:
            data = build_coach(db, config)
        data["poll_interval_minutes"] = config.gitlab.poll_interval_minutes
        return templates.TemplateResponse(request, "coach.html", data)

    @app.get("/coach/partial", response_class=HTMLResponse)
    def coach_partial(request: Request):
        with Database(db_path) as db:
            data = build_coach(db, config)
        return templates.TemplateResponse(request, "_coach.html", data)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app
