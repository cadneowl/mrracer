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


def create_app(config: Config, db_path: str) -> FastAPI:
    app = FastAPI(title="radar", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(_BASE / "static")), name="static")

    def context() -> dict:
        with Database(db_path) as db:
            data = build_dashboard(db, config)
        data["poll_interval_minutes"] = config.gitlab.poll_interval_minutes
        return data

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(request, "dashboard.html", context())

    @app.get("/partials/board", response_class=HTMLResponse)
    def board(request: Request):
        return templates.TemplateResponse(request, "_board.html", context())

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app
