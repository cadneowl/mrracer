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

from ..coach import build_coach
from ..commands import PLACEHOLDER_KEYS, CommandJob, CommandRunner
from ..config import Config
from ..context import stdin_provider_for
from ..db import Database
from ..jira import extract_keys
from ..service import build_dashboard

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
    skills_by_name = {s.name: s for s in config.skills}
    runners = {s.name: CommandRunner(s, s.name) for s in config.skills}
    enabled = {s.name: s.enabled for s in config.skills}

    def _skill_view(s) -> dict:
        return {"name": s.name, "label": s.label, "button": s.button, "icon": s.icon}

    # Skills that persist output: the board shows a re-openable badge per skill
    # that has a stored result for a given MR (row.stored_kinds decides which).
    storing_skills = [_skill_view(s) for s in config.skills if s.stores_result]

    def context(view: str | None) -> dict:
        with Database(db_path) as db:
            data = build_dashboard(db, config, view=view)
        data["poll_interval_minutes"] = config.gitlab.poll_interval_minutes
        data["enabled_skills"] = [_skill_view(s) for s in config.skills if s.enabled]
        data["storing_skills"] = storing_skills
        return data

    def _panel(request: Request, job, generated_at: str | None = None) -> HTMLResponse:
        skill = skills_by_name.get(job.kind)
        return templates.TemplateResponse(
            request,
            "_command_panel.html",
            {
                "job": job,
                "kind": job.kind,
                "heading": skill.label if skill else job.kind,
                "icon": skill.icon if skill else "▶",
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

    @app.get("/{kind}/stored/{project_id}/{mr_iid}", response_class=HTMLResponse)
    def stored_plan(request: Request, kind: str, project_id: int, mr_iid: int):
        skill = skills_by_name.get(kind)
        if skill is None or not skill.stores_result:
            raise HTTPException(status_code=404, detail=f"{kind} has no stored results")
        with Database(db_path) as db:
            plan = db.get_test_plan(project_id, mr_iid, kind)
        if plan is None:
            raise HTTPException(status_code=404, detail="no stored result")
        job = CommandJob(
            id="stored", kind=kind, project_id=project_id, mr_iid=mr_iid,
            title=f"{plan['jira_keys']}", status="done", output=plan["content"],
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
