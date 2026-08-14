"""PollRunner: the polling pass the interval job and the refresh button share."""

from __future__ import annotations

import threading
import time

from radar.gitlab_client import FixtureSource
from radar.scheduler import PollRunner
from tests.test_poller import PID, PROJECT, _discussions, _mr


class _SlowSource(FixtureSource):
    """A fetch slow enough to overlap with a second pass, if one were allowed."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.inside = 0
        self.most_at_once = 0
        self._guard = threading.Lock()

    def list_merge_requests(self, project, updated_after, state):
        with self._guard:
            self.inside += 1
            self.most_at_once = max(self.most_at_once, self.inside)
        try:
            time.sleep(0.05)
            return super().list_merge_requests(project, updated_after, state)
        finally:
            with self._guard:
                self.inside -= 1


def _source_args():
    return {
        "mrs_by_project": {PROJECT: [_mr(["dan"])]},
        "discussions_by_mr": {(PID, 1): _discussions()},
    }


def test_passes_do_not_overlap(config, tmp_path):
    """A click landing on top of the scheduled tick must not run a second pass
    alongside it: both write the same per-project high-water mark."""
    source = _SlowSource(**_source_args())
    runner = PollRunner(config, str(tmp_path / "s.db"), lambda: source)

    threads = [threading.Thread(target=runner.run) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert source.most_at_once == 1


def test_a_failed_pass_reports_nothing_and_frees_the_next(config, tmp_path):
    """A GitLab outage must not wedge the button — the next click still polls."""
    attempts = []
    source = FixtureSource(**_source_args())

    def factory():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("gitlab unreachable")
        return source

    runner = PollRunner(config, str(tmp_path / "s.db"), factory)
    assert runner.run() is None
    assert runner.run().mrs_seen == 1
