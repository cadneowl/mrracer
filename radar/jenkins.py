"""Jenkins job status for the board's CI strip.

radar watches the jobs a team keeps an eye on — builds, test suites — and shows
one dot each above the board: green, amber, red, grey, or spinning while a build
runs. Read-only; radar never triggers or cancels anything.

Two rules shape this module.

**Nothing here runs on a request path.** The board partial re-renders every 60
seconds and ``/refresh`` renders it synchronously, so a Jenkins fetch inside
either would make the whole board as slow as the slowest Jenkins. A background
pass refreshes ``JenkinsMonitor``'s in-memory cache on its own interval (see
``scheduler.add_jenkins_job``) and every request renders from that cache. The
cache is deliberately not persisted: a build status is worthless once
superseded, and the first pass runs at startup.

**Links are derived, never taken from Jenkins.** ``lastBuild.url`` is built from
the instance's "Jenkins URL" setting, which is routinely an internal hostname a
browser cannot reach — and it would be a remote-controlled value landing in an
href. The build link is the configured job URL plus the build number instead.

Auth is optional HTTP Basic (JENKINS_USER / JENKINS_TOKEN); unset means an
anonymous read, which is how most internal instances are configured. stdlib
urllib, like ``jira_client``, so there is no new dependency and the CA bundle
``tls.sync_ca_bundle`` propagates already applies.
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
import threading
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from .config import JenkinsJob

log = logging.getLogger("radar.jenkins")

# The board's colour language, in Jenkins' vocabulary: only a real FAILURE is
# red. UNSTABLE (built fine, tests failed) is amber, and a build nobody let
# finish is grey — neither is the same news as a broken build.
SUCCESS = "success"
UNSTABLE = "unstable"
FAILED = "failed"
ABORTED = "aborted"
NEVER = "never"
RUNNING = "running"
UNKNOWN = "unknown"

_RESULT_STATE = {
    "SUCCESS": SUCCESS,
    "UNSTABLE": UNSTABLE,
    "FAILURE": FAILED,
    "ABORTED": ABORTED,
    "NOT_BUILT": ABORTED,
}

_VERB = {SUCCESS: "passed", UNSTABLE: "unstable", FAILED: "failed", ABORTED: "aborted"}

# Named in the strip's one-line summary; anything not here is either green or
# already obvious from its dot.
_SUMMARY_WORD = (
    (FAILED, "failed"),
    (UNSTABLE, "unstable"),
    (RUNNING, "running"),
    (ABORTED, "aborted"),
    (NEVER, "not built"),
    (UNKNOWN, "unreachable"),
)

# One request per job, carrying only what the strip renders. Every field
# `map_status`/`_status` reads has to be named here: Jenkins returns exactly
# what the tree asks for, so a field left out arrives as absent rather than as
# an error, and the chip quietly loses whatever it was for.
_TREE = (
    "displayName,lastBuild[number,building,result,timestamp,duration,estimatedDuration],"
    "lastCompletedBuild[number,result,timestamp,duration]"
)

# Plain prose on purpose: `radar check` prints this straight to stdout, which is
# cp1252 on Windows, and a character outside that set takes the whole run down
# with a UnicodeEncodeError rather than reporting anything.
_AUTH_HINT = "set JENKINS_USER and JENKINS_TOKEN (an API token from your Jenkins user page)"


class JenkinsError(Exception):
    """A Jenkins request failed: network, HTTP status, or an unreadable body."""


@dataclass(frozen=True)
class JobStatus:
    """One watched job as the strip shows it.

    ``previous`` is the last completed state of a job that is building now, so a
    build running over a red job spins in red rather than looking neutral.
    ``stale`` means the last fetch failed and everything else here is the last
    thing radar did know — shown dimmed, never as fresh truth.
    """

    name: str
    url: str  # the job page
    state: str
    previous: str | None = None  # RUNNING only: what the last build did
    build_number: int | None = None
    build_url: str | None = None  # derived from url + number, never from Jenkins
    started_at: datetime | None = None
    duration_s: int | None = None  # a finished build's run time
    estimated_s: int | None = None  # a running build's expected run time
    message: str = ""  # why it is unknown, or why the last check failed
    stale: bool = False

    @property
    def link(self) -> str:
        """Where the chip goes: the build if there is one, else the job page."""
        return self.build_url or self.url


@dataclass(frozen=True)
class Snapshot:
    jobs: tuple[JobStatus, ...]
    checked_at: datetime | None  # None until the first pass completes


# --- fetching --------------------------------------------------------------


class _DropAuthOffHost(urllib.request.HTTPRedirectHandler):
    """Strip the Authorization header from a redirect that leaves the host.

    urllib copies every header but content-length/content-type onto the redirect
    target, so a Jenkins fronted by a proxy that 302s ``/api/json`` to an SSO
    host would be handed radar's API token — silently, and for a host the
    operator never named. Same-host redirects (http -> https, a trailing slash)
    keep it, which is the only case that needs it.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urlparse(newurl).netloc != urlparse(req.full_url).netloc:
            new.headers.pop("Authorization", None)
        return new


class JenkinsClient:
    """Reads one job's status. ``getter`` is injectable so tests never use the network."""

    HTTP_TIMEOUT_S = 10  # a hung Jenkins must not hold the refresh thread

    def __init__(
        self,
        credentials: tuple[str, str] | None = None,
        getter: Callable[[str], dict] | None = None,
        timeout: int | None = None,
    ):
        self._auth_header = None
        if credentials:
            user, secret = credentials
            encoded = base64.b64encode(f"{user}:{secret}".encode()).decode()
            self._auth_header = f"Basic {encoded}"
        self._getter = getter or self._http_get
        self._timeout = timeout or self.HTTP_TIMEOUT_S
        self._opener = urllib.request.build_opener(_DropAuthOffHost())

    @classmethod
    def from_env(cls) -> JenkinsClient:
        from .config import jenkins_credentials

        return cls(credentials=jenkins_credentials())

    def fetch(self, job: JenkinsJob) -> dict:
        return self._getter(f"{job.url}/api/json?tree={_TREE}")

    def _http_get(self, url: str) -> dict:
        headers = {"Accept": "application/json"}
        if self._auth_header:
            headers["Authorization"] = self._auth_header
        req = urllib.request.Request(url, headers=headers)
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:  # noqa: S310
                body = resp.read()
        except urllib.error.HTTPError as exc:  # a subclass of OSError: keep it first
            raise JenkinsError(_http_message(exc.code)) from None
        except (OSError, http.client.HTTPException) as exc:
            # OSError covers URLError and TimeoutError. HTTPException covers a
            # response that dies *after* the headers (IncompleteRead,
            # RemoteDisconnected) — routine through a proxy, and not something
            # urlopen wraps, so without it a mid-body reset would escape this
            # method's contract and reach the caller as a raw traceback.
            # Never the URL: it is long, and the reason is the useful half.
            raise JenkinsError(f"unreachable: {exc}") from None

        try:
            return json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            # A 200 that is not JSON is almost always an SSO login page, which
            # never returns a 4xx — so without this the one diagnosis that
            # actually helps would be reported as a network outage.
            raise JenkinsError(
                "answered with something that is not JSON — the URL may be reaching a "
                f"login page rather than Jenkins itself; if so, {_AUTH_HINT}"
            ) from None


def _http_message(code: int) -> str:
    """An HTTP status as something a reader can act on."""
    if code in (401, 403):
        return f"HTTP {code}: not permitted — {_AUTH_HINT}"
    if code == 404:
        return "HTTP 404: no such job — the URL should be the job's page, not a build"
    return f"HTTP {code}"


# --- interpreting ----------------------------------------------------------


def _epoch_ms(value: object) -> datetime | None:
    """Jenkins timestamps are epoch milliseconds, UTC."""
    if not isinstance(value, int | float) or value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _seconds(value: object) -> int | None:
    if not isinstance(value, int | float) or value <= 0:
        return None
    return int(value / 1000)


def map_status(payload: dict, job: JenkinsJob) -> JobStatus:
    """One Jenkins ``api/json`` payload as a ``JobStatus``. Pure: no I/O, no clock."""
    klass = str(payload.get("_class", ""))
    if "lastBuild" not in payload:
        # Folders and multibranch projects have no builds of their own, and
        # pointing at one is the commonest way to get this config wrong.
        hint = (
            " — that URL is a folder or a multibranch project; point at a branch "
            "job inside it, e.g. .../job/main/"
            if "Folder" in klass or "MultiBranch" in klass
            else " — that URL does not look like a job"
        )
        return JobStatus(name=job.name, url=job.url, state=UNKNOWN, message=f"no builds{hint}")

    last = payload.get("lastBuild") or None
    completed = payload.get("lastCompletedBuild") or None
    if last is None:
        return JobStatus(name=job.name, url=job.url, state=NEVER, message="no builds yet")

    if last.get("building"):
        return _status(
            job,
            RUNNING,
            last,
            previous=_RESULT_STATE.get(str((completed or {}).get("result"))),
            estimated_s=_seconds(last.get("estimatedDuration")),
        )

    # A build that has just stopped can be reported with no result for a moment;
    # lastCompletedBuild is the honest answer in that window.
    build = last if last.get("result") else (completed or last)
    return _status(job, _RESULT_STATE.get(str(build.get("result")), UNKNOWN), build)


def _status(job: JenkinsJob, state: str, build: dict, **extra) -> JobStatus:
    number = build.get("number") if isinstance(build.get("number"), int) else None
    return JobStatus(
        name=job.name,
        url=job.url,
        state=state,
        build_number=number,
        build_url=f"{job.url}/{number}/" if number else None,
        started_at=_epoch_ms(build.get("timestamp")),
        duration_s=_seconds(build.get("duration")),
        **extra,
    )


# --- fetching every job ----------------------------------------------------

_MAX_PARALLEL = 8


def fetch_all(
    client: JenkinsClient, jobs: Iterable[JenkinsJob]
) -> dict[str, JobStatus | Exception]:
    """Every job's status, fetched concurrently, keyed by job name.

    Values are a ``JobStatus`` or the exception that job failed with — the
    caller decides what a failure should look like (a dimmed chip here, a failed
    check in ``radar check``), so nothing is swallowed on the way.

    Concurrent because a pass costs one timeout per unreachable job when it is
    serial: at the minimum 15s poll interval two dead jobs already overrun it,
    and APScheduler then drops the firings it cannot start while the board keeps
    ageing. These are independent GETs, so a pass now costs roughly one timeout
    however many jobs there are.
    """
    jobs = list(jobs)
    if not jobs:
        return {}

    def one(job: JenkinsJob) -> JobStatus | Exception:
        try:
            return map_status(client.fetch(job), job)
        except Exception as exc:  # noqa: BLE001 - handed to the caller intact
            return exc

    with ThreadPoolExecutor(
        max_workers=min(_MAX_PARALLEL, len(jobs)), thread_name_prefix="jenkins"
    ) as pool:
        return dict(zip((job.name for job in jobs), pool.map(one, jobs), strict=True))


# --- the cache the board reads --------------------------------------------


class JenkinsMonitor:
    """The watched jobs' latest status, refreshed in the background.

    ``refresh`` isolates one job's failure from the rest, the way ``poll_once``
    isolates one project's: a job radar cannot reach keeps its last known state,
    dimmed, while every other chip still updates.
    """

    def __init__(self, jobs: Iterable[JenkinsJob], client: JenkinsClient):
        self._jobs = tuple(jobs)
        self._client = client
        self._lock = threading.Lock()
        self._statuses = {
            job.name: JobStatus(name=job.name, url=job.url, state=UNKNOWN, message="checking…")
            for job in self._jobs
        }
        self._checked_at: datetime | None = None

    def refresh(self) -> None:
        """One pass over every watched job. Never raises: a bad pass must take
        down neither the scheduler nor the board it feeds."""
        with self._lock:
            previous = dict(self._statuses)

        fetch_all_result = fetch_all(self._client, self._jobs)
        fetched: dict[str, JobStatus] = {}
        for job in self._jobs:
            outcome = fetch_all_result[job.name]
            if isinstance(outcome, JobStatus):
                fetched[job.name] = outcome
                continue
            # One job's outage is its own: it goes stale, the rest still update.
            if isinstance(outcome, JenkinsError):
                reason = str(outcome)
                log.warning("jenkins job %s: %s", job.name, reason)
            else:
                reason = repr(outcome)
                log.error("jenkins job %s failed", job.name, exc_info=outcome)
            fetched[job.name] = _stale(previous.get(job.name), job, reason)

        # Swapped in one go, so a pass in flight is never half on screen.
        with self._lock:
            self._statuses = fetched
            self._checked_at = datetime.now(UTC)

    def snapshot(self) -> Snapshot:
        with self._lock:
            return Snapshot(
                jobs=tuple(self._statuses[job.name] for job in self._jobs),
                checked_at=self._checked_at,
            )


def _stale(previous: JobStatus | None, job: JenkinsJob, reason: str) -> JobStatus:
    """What to show for a job this pass could not reach.

    The last known state, dimmed and carrying the reason — a blip should not
    blank a board, and the summary counts it as unreachable either way, so a
    real outage still says so rather than quietly reading as green.
    """
    if previous is None or previous.state == UNKNOWN:
        return JobStatus(name=job.name, url=job.url, state=UNKNOWN, message=reason, stale=True)
    return replace(previous, message=reason, stale=True)


# --- rendering -------------------------------------------------------------


def _ago(when: datetime | None, now: datetime) -> str:
    """"25m ago" / "3h ago" — the strip is glanced at, not studied."""
    if when is None:
        return "—"
    seconds = max(0, int((now - when).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _dur(seconds: int | None) -> str:
    if seconds is None:
        return ""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _detail(status: JobStatus, now: datetime) -> str:
    """The chip's tooltip: "#128 · failed 25m ago · took 3m12s"."""
    parts: list[str] = []
    if status.build_number:
        parts.append(f"#{status.build_number}")

    if status.state == RUNNING:
        running = "running"
        if status.started_at:
            # Clamped like _ago: Jenkins' clock being a little ahead of ours is
            # routine, and "running -42s" reads as a bug in radar.
            elapsed = max(0, int((now - status.started_at).total_seconds()))
            running += f" {_dur(elapsed)}"
        parts.append(running)
        if status.estimated_s:
            parts.append(f"usually {_dur(status.estimated_s)}")
    elif status.state in _VERB:
        parts.append(f"{_VERB[status.state]} {_ago(_finished_at(status), now)}")
        if status.duration_s:
            parts.append(f"took {_dur(status.duration_s)}")
    elif status.message and not status.stale:
        parts.append(status.message)

    if status.stale:
        parts.append(f"last check failed: {status.message}")
    return " · ".join(p for p in parts if p)


def _finished_at(status: JobStatus) -> datetime | None:
    """When a completed build ended — Jenkins reports its start plus a duration."""
    if status.started_at is None:
        return None
    if not status.duration_s:
        return status.started_at
    return status.started_at + timedelta(seconds=status.duration_s)


def job_view(status: JobStatus, now: datetime) -> dict:
    """One chip, ready for the template."""
    return {
        "name": status.name,
        "link": status.link,
        "state": status.state,
        # A running build spins in the last result's colour, so a red job under
        # a running build still reads as red.
        "ring": (status.previous or UNKNOWN) if status.state == RUNNING else status.state,
        "stale": status.stale,
        # Marked whenever radar does not actually know: a stale chip, and an
        # unknown one (a folder URL, say) that never had a state to go stale
        # from. Without it a misconfigured job looks like one awaiting its
        # first build.
        "warn": status.stale or status.state == UNKNOWN,
        "detail": _detail(status, now),
    }


def strip_view(snapshot: Snapshot, now: datetime | None = None) -> dict:
    """The whole CI strip: chips, a one-line summary, and how fresh it all is."""
    now = now or datetime.now(UTC)
    jobs = [job_view(s, now) for s in snapshot.jobs]

    if snapshot.checked_at is None:
        # No pass has finished yet (the board opened in the second before the
        # first one lands). Every chip is unknown, but that is radar not having
        # looked rather than Jenkins being down, and announcing an outage on
        # every freshly started board would train people to ignore the one real
        # outage it is there to report.
        return {"jobs": jobs, "summary": "checking…", "all_green": False, "checked": "checking…"}

    # A job radar could not reach counts as unreachable whatever it last said —
    # otherwise a total outage would leave the summary claiming "all green".
    counts = Counter(UNKNOWN if s.stale else s.state for s in snapshot.jobs)
    trouble = [f"{counts[state]} {word}" for state, word in _SUMMARY_WORD if counts[state]]
    return {
        "jobs": jobs,
        "summary": " · ".join(trouble) if trouble else "all green",
        "all_green": not trouble,
        "checked": _ago(snapshot.checked_at, now),
    }
