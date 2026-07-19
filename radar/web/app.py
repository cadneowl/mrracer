"""FastAPI application factory for the dashboard.

A fresh SQLite connection is opened per request (cheap, and safe under
uvicorn's threadpool; WAL mode allows concurrent readers), so the web layer
holds no long-lived DB handle.
"""

from __future__ import annotations

from pathlib import Path

import markdown as md
import nh3
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from ..config import Config
from ..db import Database
from ..review import PLACEHOLDER_KEYS, ReviewRunner
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
    runner = ReviewRunner(config.review)

    def context(reviewer: str | None) -> dict:
        with Database(db_path) as db:
            data = build_dashboard(db, config, reviewer=reviewer)
        data["poll_interval_minutes"] = config.gitlab.poll_interval_minutes
        data["review_enabled"] = config.review.enabled
        return data

    def _job_panel(request: Request, job) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "_review_panel.html",
            {
                "job": job,
                "output_html": _render_markdown(job.output) if job.status == "done" else None,
            },
        )

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, view: str | None = None):
        # `view` present -> explicit choice (empty string clears the personal
        # view); absent -> fall back to the remembered cookie.
        cookie = request.cookies.get(COOKIE_NAME) or None
        reviewer = (view or None) if view is not None else cookie

        resp = templates.TemplateResponse(request, "dashboard.html", context(reviewer))
        if view is not None:
            if reviewer:
                resp.set_cookie(COOKIE_NAME, reviewer, max_age=COOKIE_MAX_AGE, samesite="lax")
            else:
                resp.delete_cookie(COOKIE_NAME)
        return resp

    @app.get("/partials/board", response_class=HTMLResponse)
    def board(request: Request):
        # Auto-refresh preserves the remembered personal view via the cookie.
        reviewer = request.cookies.get(COOKIE_NAME) or None
        return templates.TemplateResponse(request, "_board.html", context(reviewer))

    @app.post("/review/{project_id}/{mr_iid}", response_class=HTMLResponse)
    def start_review(request: Request, project_id: int, mr_iid: int):
        if not config.review.enabled:
            raise HTTPException(status_code=404, detail="review is not enabled")
        with Database(db_path) as db:
            snap = db.get_snapshot(project_id, mr_iid)
        if snap is None:
            raise HTTPException(status_code=404, detail="unknown merge request")
        ctx = {"project_id": project_id, "mr_iid": mr_iid}
        ctx.update({k: snap.get(k, "") for k in PLACEHOLDER_KEYS})
        job = runner.start(ctx)
        return _job_panel(request, job)

    @app.get("/review/status/{job_id}", response_class=HTMLResponse)
    def review_status(request: Request, job_id: str):
        job = runner.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown review job")
        return _job_panel(request, job)

    @app.get("/review/close", response_class=HTMLResponse)
    def review_close():
        return HTMLResponse("")  # htmx swaps this empty content in to dismiss

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app
