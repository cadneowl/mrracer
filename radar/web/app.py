"""FastAPI application factory for the dashboard.

A fresh SQLite connection is opened per request (cheap, and safe under
uvicorn's threadpool; WAL mode allows concurrent readers), so the web layer
holds no long-lived DB handle.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import Config
from ..db import Database
from ..service import build_dashboard

_BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_BASE / "templates"))

COOKIE_NAME = "radar_view"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year


def create_app(config: Config, db_path: str) -> FastAPI:
    app = FastAPI(title="radar", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(_BASE / "static")), name="static")

    def context(reviewer: str | None) -> dict:
        with Database(db_path) as db:
            data = build_dashboard(db, config, reviewer=reviewer)
        data["poll_interval_minutes"] = config.gitlab.poll_interval_minutes
        return data

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

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app
