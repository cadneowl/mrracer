"""In-process polling loop via APScheduler.

Used by ``radar serve`` to poll GitLab on the configured interval while the
web server runs in the same process. Each run opens its own DB connection and
data source so nothing long-lived is shared across threads.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime

from apscheduler.schedulers.background import BackgroundScheduler

from .config import Config
from .db import Database
from .gitlab_client import MRSource
from .poller import PollResult, poll_once

log = logging.getLogger("radar.scheduler")


class PollRunner:
    """One polling pass, shared by the interval job and the board's refresh button.

    A lock serialises passes: two running at once would race on the per-project
    high-water mark, and the pass that writes second could push the mark past
    MRs the other one never stored. Waiting for the in-flight pass and then
    running is the honest behaviour for the button — a click asks for a pass
    that *ends* after the click, not for whatever a pass already underway
    happened to see before it.
    """

    def __init__(
        self,
        config: Config,
        db_path: str,
        source_factory: Callable[[], MRSource],
    ) -> None:
        self._config = config
        self._db_path = db_path
        self._source_factory = source_factory
        self._lock = threading.Lock()

    def run(self) -> PollResult | None:
        """Run one pass. Returns None if it failed — logged, never raised, so a
        bad pass takes down neither the scheduler nor the request that asked."""
        with self._lock:
            try:
                with Database(self._db_path) as db:
                    result = poll_once(db, self._config, self._source_factory())
            except Exception:  # noqa: BLE001 — keep the loop alive across failures
                log.exception("poll failed")
                return None
        log.info(
            "poll complete: %d MRs seen, %d new events",
            result.mrs_seen,
            result.new_events,
        )
        return result


def make_scheduler(runner: PollRunner, minutes: int) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        runner.run,
        trigger="interval",
        minutes=minutes,
        id="poll",
        next_run_time=datetime.now(UTC),  # run once immediately
        max_instances=1,
        coalesce=True,
    )
    return scheduler
