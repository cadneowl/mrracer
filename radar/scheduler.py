"""In-process polling loop via APScheduler.

Used by ``radar serve`` to poll GitLab on the configured interval while the
web server runs in the same process. Each run opens its own DB connection and
data source so nothing long-lived is shared across threads.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from apscheduler.schedulers.background import BackgroundScheduler

from .config import Config
from .db import Database
from .gitlab_client import MRSource
from .poller import poll_once

log = logging.getLogger("radar.scheduler")


def make_scheduler(
    config: Config,
    db_path: str,
    source_factory: Callable[[], MRSource],
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")

    def job() -> None:
        try:
            with Database(db_path) as db:
                result = poll_once(db, config, source_factory())
            log.info(
                "poll complete: %d MRs seen, %d new events",
                result.mrs_seen,
                result.new_events,
            )
        except Exception:  # noqa: BLE001 — keep the loop alive across failures
            log.exception("poll failed")

    scheduler.add_job(
        job,
        trigger="interval",
        minutes=config.gitlab.poll_interval_minutes,
        id="poll",
        next_run_time=datetime.now(UTC),  # run once immediately
        max_instances=1,
        coalesce=True,
    )
    return scheduler
