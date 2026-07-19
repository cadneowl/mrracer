"""FastAPI application factory for the dashboard.

A fresh SQLite connection is opened per request (cheap, and safe under
uvicorn's threadpool; WAL mode allows concurrent readers), so the web layer
holds no long-lived DB handle.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import markdown as md
import nh3
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from ..commands import PLACEHOLDER_KEYS, CommandJob, CommandRunner
from ..config import Config
from ..context import stdin_provider_for
from ..db import Database
from ..jira import extract_keys
from ..service import build_dashboard

_KINDS = {"review": "AI review", "qa": "QA test plan"}

_BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_BASE / "templates"))

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


def _render_markdown(text: str) -> Markup:
    html = md.markdown(text, extensions=["fenced_code", "tables", "sane_lists"])
    clean = nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        url_schemes={"http", "https", "mailto"},
    )
    return Markup(clean)


def create_app(config: Config, db_path: str) -> FastAPI:
    app = FastAPI(title="radar", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(_BASE / "static")), name="static")
    runners = {
        "review": CommandRunner(config.review, "review"),
        "qa": CommandRunner(config.qa, "qa"),
    }
    enabled = {"review": config.review.enabled, "qa": config.qa.enabled}

    def context(view: str | None) -> dict:
        with Database(db_path) as db:
            data = build_dashboard(db, config, view=view)
        data["poll_interval_minutes"] = config.gitlab.poll_interval_minutes
        data["review_enabled"] = config.review.enabled
        data["qa_enabled"] = config.qa.enabled
        return data

    def _panel(request: Request, job, generated_at: str | None = None) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "_command_panel.html",
            {
                "job": job,
                "kind": job.kind,
                "heading": _KINDS.get(job.kind, job.kind),
                "generated_at": generated_at,
                "output_html": _render_markdown(job.output) if job.status == "done" else None,
            },
        )

    def _ctx_for(snap: dict, project_id: int, mr_iid: int) -> tuple[dict, list[str]]:
        keys = extract_keys(
            [snap.get("title"), snap.get("source_branch"), snap.get("description")],
            config.jira.project_keys,
        )
        ctx = {"project_id": project_id, "mr_iid": mr_iid}
        ctx.update({k: snap.get(k, "") for k in PLACEHOLDER_KEYS})
        ctx["jira_keys"] = " ".join(keys)
        ctx["jira_keys_csv"] = ",".join(keys)
        return ctx, keys

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, view: str | None = None):
        # `view` present -> explicit choice (empty string clears the filter);
        # absent -> fall back to the remembered cookie.
        cookie = request.cookies.get(COOKIE_NAME) or None
        token = (view or None) if view is not None else cookie

        resp = templates.TemplateResponse(request, "dashboard.html", context(token))
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

    @app.post("/{kind}/{project_id}/{mr_iid}", response_class=HTMLResponse)
    def start_command(request: Request, kind: str, project_id: int, mr_iid: int):
        if kind not in runners or not enabled[kind]:
            raise HTTPException(status_code=404, detail=f"{kind} is not enabled")
        with Database(db_path) as db:
            snap = db.get_snapshot(project_id, mr_iid)
        if snap is None:
            raise HTTPException(status_code=404, detail="unknown merge request")
        ctx, keys = _ctx_for(snap, project_id, mr_iid)

        on_success = None
        if kind == "qa":
            csv = ",".join(keys)

            def on_success(job) -> None:
                with Database(db_path) as db:
                    db.save_test_plan(project_id, mr_iid, csv, job.output)

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

        async def gen():
            sent = 0
            for _ in range(1500):  # ~10min safety cap at 0.4s/iteration
                snap = runner.progress_since(job_id, sent)
                if snap is None:
                    yield _sse("end", {"status": "error"})
                    return
                items, status = snap
                for item in items:
                    yield _sse("progress", item)
                sent += len(items)
                if status != "running":
                    yield _sse("end", {"status": status})
                    return
                await asyncio.sleep(0.4)
            yield _sse("end", {"status": "timeout"})

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/{kind}/close", response_class=HTMLResponse)
    def command_close(kind: str):
        return HTMLResponse("")  # htmx swaps this empty content in to dismiss

    @app.get("/qa/stored/{project_id}/{mr_iid}", response_class=HTMLResponse)
    def stored_plan(request: Request, project_id: int, mr_iid: int):
        with Database(db_path) as db:
            plan = db.get_test_plan(project_id, mr_iid)
        if plan is None:
            raise HTTPException(status_code=404, detail="no stored test plan")
        job = CommandJob(
            id="stored", kind="qa", project_id=project_id, mr_iid=mr_iid,
            title=f"{plan['jira_keys']}", status="done", output=plan["content"],
        )
        return _panel(request, job, generated_at=plan["generated_at"])

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app
