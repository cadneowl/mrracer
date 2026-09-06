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
import re
import ssl
import threading
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from .config import DEFAULT_LOG_TAIL_LINES, JenkinsJob

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

# States worth asking an agent about: a build that ran and did not pass. A job
# that never built, or one radar could not reach, has no log to read.
_ANALYSABLE = frozenset({FAILED, UNSTABLE, ABORTED})

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
    previous_number: int | None = None  # RUNNING only: which build that was
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
        verify_ssl: bool = True,
        text_getter: Callable[[str], tuple[str, dict[str, str]]] | None = None,
    ):
        self._auth_header = None
        if credentials:
            user, secret = credentials
            encoded = base64.b64encode(f"{user}:{secret}".encode()).decode()
            self._auth_header = f"Basic {encoded}"
        self._getter = getter or self._http_get
        self._text_getter = text_getter or self._http_get_text
        self._timeout = timeout or self.HTTP_TIMEOUT_S

        handlers: list[urllib.request.BaseHandler] = [_DropAuthOffHost()]
        if not verify_ssl:
            # For an internal Jenkins whose chain cannot be trusted any other
            # way (see JenkinsConfig.verify_ssl). check_hostname must go first:
            # setting CERT_NONE while it is on raises.
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self._opener = urllib.request.build_opener(*handlers)

    @classmethod
    def from_env(cls, verify_ssl: bool = True) -> JenkinsClient:
        from .config import jenkins_credentials

        return cls(credentials=jenkins_credentials(), verify_ssl=verify_ssl)

    def fetch(self, job: JenkinsJob) -> dict:
        return self._getter(f"{job.url}/api/json?tree={_TREE}")

    def fetch_json(self, url: str) -> dict:
        """Any other JSON endpoint on the same Jenkins (the builds list)."""
        return self._getter(url)

    def fetch_text(self, url: str, headers: dict[str, str] | None = None) -> tuple:
        """A text endpoint, with its response headers.

        The headers matter in both directions: a console log is fetched by
        asking Jenkins how big it is (``X-Text-Size``) and then for the tail, and
        the fallback asks for a byte range — so the body is never the whole of a
        very large log, and the answer says which part of it arrived.
        """
        return self._text_getter(url, headers or {})

    def _http_get(self, url: str) -> dict:
        body, _ = self._http_get_raw(url, "application/json")
        return body

    def _http_get_text(self, url: str, extra: dict[str, str] | None = None) -> tuple:
        return self._http_get_raw(url, "text/plain", extra)

    def _http_get_raw(self, url: str, accept: str, extra: dict[str, str] | None = None) -> tuple:
        headers = {"Accept": accept, **(extra or {})}
        if self._auth_header:
            headers["Authorization"] = self._auth_header
        req = urllib.request.Request(url, headers=headers)
        try:
            # A ceiling on both paths, but a different one and for different
            # reasons: a log is *expected* to be longer than radar wants and is
            # cut deliberately, while a JSON body that gets cut can only fail to
            # parse — so that one is read with room to spare and refused loudly
            # rather than silently truncated into a syntax error.
            limit = _MAX_LOG_BYTES if accept == "text/plain" else _MAX_JSON_BYTES
            with self._opener.open(req, timeout=self._timeout) as resp:  # noqa: S310
                body = resp.read(limit + 1)
                response_headers = {k: v for k, v in resp.headers.items()}
            if accept != "text/plain" and len(body) > limit:
                raise JenkinsError(
                    f"the response was larger than {limit // 1_000_000} MB, which is more "
                    "than any job's build list should be — check the URL points at a job"
                )
        except urllib.error.HTTPError as exc:  # a subclass of OSError: keep it first
            raise JenkinsError(_http_message(exc.code)) from None
        except (OSError, http.client.HTTPException) as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(exc, ssl.SSLError) or isinstance(reason, ssl.SSLError):
                raise JenkinsError(_tls_message(reason or exc)) from None
            # OSError covers URLError and TimeoutError. HTTPException covers a
            # response that dies *after* the headers (IncompleteRead,
            # RemoteDisconnected) — routine through a proxy, and not something
            # urlopen wraps, so without it a mid-body reset would escape this
            # method's contract and reach the caller as a raw traceback.
            # Never the URL: it is long, and the reason is the useful half.
            raise JenkinsError(f"unreachable: {exc}") from None

        if accept == "text/plain":
            return body.decode("utf-8", "replace"), response_headers

        try:
            return json.loads(body.decode("utf-8")), response_headers
        except (json.JSONDecodeError, UnicodeDecodeError):
            # A 200 that is not JSON is almost always an SSO login page, which
            # never returns a 4xx — so without this the one diagnosis that
            # actually helps would be reported as a network outage.
            raise JenkinsError(
                "answered with something that is not JSON — the URL may be reaching a "
                f"login page rather than Jenkins itself; if so, {_AUTH_HINT}"
            ) from None


def _tls_message(exc: BaseException) -> str:
    """A certificate failure as the two things that actually fix it.

    Reported apart from the other network errors because it is not one: the host
    answered, and "unreachable" sends the reader looking for a firewall. Plain
    ASCII, like every message that can reach a cp1252 console.
    """
    return (
        f"certificate not trusted: {exc} - point SSL_CERT_FILE at the CA that signed it "
        "(radar copies it to the other TLS variables at startup, and 'radar check' shows "
        "what each stack trusts), or set jenkins.verify_ssl: false to stop verifying"
    )


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
        finished = completed or {}
        finished_number = finished.get("number")
        return _status(
            job,
            RUNNING,
            last,
            previous=_RESULT_STATE.get(str(finished.get("result"))),
            # Kept rather than derived: build numbers skip (a deleted build, a
            # renumbered job), so "the one before this" is not this one minus 1.
            previous_number=finished_number if isinstance(finished_number, int) else None,
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


# --- what broke it: commits and the console log ----------------------------

# Both spellings on purpose. A Pipeline job (WorkflowRun) reports `changeSets`,
# a list, one entry per SCM; a freestyle job reports `changeSet`, a single
# object. Asking for only one of them finds no commits at all on half the
# Jenkins installations in existence, and finds them silently — an empty commit
# list reads as "nothing changed", which is a conclusion rather than a gap.
_CHANGE_FIELDS = "commitId,msg,comment,date,authorEmail,affectedPaths,author[fullName]"
_BUILDS_TREE = (
    "builds[number,result,building,timestamp,duration,"
    f"changeSet[items[{_CHANGE_FIELDS}]],changeSets[items[{_CHANGE_FIELDS}]]]"
)

# How far back to look for the last green build. A job that has been broken for
# more than this many builds reports the commits it can see and says so.
BUILDS_WINDOW = 25

# The tail actually pulled back, and how much of it is shown. The byte cap
# bounds what crosses the network; the line count is what the skill reads.
LOG_TAIL_BYTES = 2_000_000
# Ceilings on what goes *inline*. The line count says how much of the end to
# show; these say how big that is allowed to get, because a line count on its
# own bounds nothing — one build printing a JSON document per line reaches
# megabytes in a hundred of them.
_EXCERPT_CHARS = 20_000
_EXCERPT_LINE_CHARS = 2_000
_MAX_LOG_BYTES = 4_000_000  # ceiling on a console-log read
_MAX_JSON_BYTES = 8_000_000  # ceiling on an api/json read, refused rather than cut

# Asking for a start beyond the end returns an empty body and, in the header,
# the log's real size — which is how the tail is fetched without pulling the
# whole log across the network first.
_SIZE_PROBE_OFFSET = 1 << 50


@dataclass(frozen=True)
class Commit:
    sha: str
    author: str
    when: str
    message: str
    files: tuple[str, ...] = ()

    @property
    def short_sha(self) -> str:
        return self.sha[:12]


@dataclass(frozen=True)
class BuildLog:
    text: str  # the inline excerpt: the last `lines` lines
    lines: int  # lines in `text`
    total_bytes: int  # size of the whole log, 0 when Jenkins would not say
    truncated: bool
    # Where the whole slab radar pulled back was written, if anywhere. A build
    # log runs to tens of megabytes and an agent cannot be handed that as prompt
    # text, but it can open a file and search it — so the excerpt is what a
    # small failure needs, and this is what a real one does.
    path: str | None = None
    slab_bytes: int = 0  # how much of the log the file holds
    slab_lines: int = 0
    # Whether `text` reaches the end of the log. False means radar could only
    # get the beginning — which is where a build says what it is about to do,
    # not why it failed. The bundle says so rather than presenting the opening
    # of a build as the reason it broke.
    has_end: bool = True


def _changeset_items(build: dict) -> list[dict]:
    """The SCM entries of one build, whichever shape this Jenkins reports."""
    sets = build.get("changeSets")
    if isinstance(sets, list):
        return [
            item
            for changeset in sets
            if isinstance(changeset, dict)
            for item in (changeset.get("items") or [])
        ]
    return list((build.get("changeSet") or {}).get("items") or [])


def _commit(item: dict) -> Commit:
    author = (item.get("author") or {}).get("fullName") or item.get("authorEmail") or "unknown"
    # `msg` is the subject line; `comment` is the whole message. The subject is
    # what a reader scans, so it leads, and the body is left to the log.
    message = str(item.get("msg") or item.get("comment") or "").strip().splitlines()
    return Commit(
        sha=str(item.get("commitId") or ""),
        author=str(author),
        when=str(item.get("date") or ""),
        message=message[0] if message else "",
        files=tuple(str(p) for p in (item.get("affectedPaths") or [])),
    )


def commit_range(builds: list[dict], broken_number: int) -> tuple[int | None, list[Commit]]:
    """What landed between the last successful build and this broken one.

    Pure, like ``map_status``: the walk is where this feature is most likely to
    be wrong, and it should be testable without a Jenkins.

    Returns the last green build's number (None if none is in the window) and
    its commits, newest build first — the most recent change being the one most
    worth looking at first. The broken build's own changes are included: they
    are the prime suspects, not context.
    """
    numbered = [b for b in builds if isinstance(b.get("number"), int)]
    seen: set[str] = set()
    commits: list[Commit] = []
    last_good: int | None = None

    for build in sorted(numbered, key=lambda b: b["number"], reverse=True):
        if build["number"] > broken_number:
            continue  # a newer build than the one being analysed
        if build.get("result") == "SUCCESS":
            last_good = build["number"]
            break
        for item in _changeset_items(build):
            commit = _commit(item)
            # The same commit can be reported by two builds (a retry, or a
            # build that picked up an unchanged head); say it once.
            if commit.sha and commit.sha in seen:
                continue
            seen.add(commit.sha)
            commits.append(commit)
    return last_good, commits


def fetch_builds(client: JenkinsClient, job: JenkinsJob, limit: int = BUILDS_WINDOW) -> list[dict]:
    """The job's recent builds with their changesets, in one request."""
    url = f"{job.url}/api/json?tree={_BUILDS_TREE}{{0,{limit}}}"
    return list(client.fetch_json(url).get("builds") or [])


def _excerpt(lines: list[str], keep_last: bool, have_file: bool) -> list[str]:
    """Bound the inline excerpt by bytes as well as by line count.

    A line count alone bounds nothing: a build that prints a JSON document or a
    base64 blob per line puts megabytes into the prompt in a hundred lines, and
    "prompt is too long" is what comes back. So each line is clipped, and lines
    are then dropped from the far end until the whole thing fits. Everything
    dropped is still in the file the bundle names.
    """
    def clip(line: str) -> str:
        if len(line) <= _EXCERPT_LINE_CHARS:
            return line
        elided = len(line) - _EXCERPT_LINE_CHARS
        where = " in the file" if have_file else ""
        return line[:_EXCERPT_LINE_CHARS] + f"… (+{elided} chars{where})"

    clipped = [clip(line) for line in lines]
    ordered = list(reversed(clipped)) if keep_last else clipped
    out: list[str] = []
    size = 0
    for line in ordered:
        size += len(line) + 1
        if size > _EXCERPT_CHARS and out:
            break
        out.append(line)
    return list(reversed(out)) if keep_last else out


def slug(text: str) -> str:
    """A job name as a filename component: no separators, no surprises.

    Job names carry spaces and slashes ("hub/e2e/nightly build"), and these
    names reach the filesystem.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")
    return cleaned or "job"


def fetch_log_tail(
    client: JenkinsClient,
    job: JenkinsJob,
    number: int,
    lines: int = DEFAULT_LOG_TAIL_LINES,
    dest_dir: str | None = None,
) -> BuildLog:
    """The end of a build's console log — where the failure is.

    Two small requests rather than one large one: Jenkins is asked for a slice
    past the end, which answers with the log's size, and then for the last
    ``LOG_TAIL_BYTES`` of it. Fetching ``consoleText`` instead would drag a
    hundred-megabyte log across the network to read its last few hundred lines.

    With ``dest_dir``, that slab is written there and the returned excerpt is
    only its last ``lines`` lines. A log of few, very long lines — a JSON dump,
    a base64 blob — would otherwise put a quarter of a megabyte into a prompt on
    its own, and an agent can do nothing with a window it cannot move.
    """
    base = f"{job.url}/{number}"
    text, total = "", 0
    try:
        _, headers = client.fetch_text(
            f"{base}/logText/progressiveText?start={_SIZE_PROBE_OFFSET}"
        )
        total = int(headers.get("X-Text-Size") or headers.get("x-text-size") or 0)
    except (JenkinsError, ValueError):
        total = 0

    # Whether anything was left behind is tracked as it happens, rather than
    # inferred afterwards by comparing the decoded text's length against the
    # size Jenkins reported: decoding and line-splitting both change that
    # length, so the comparison would call a complete short log truncated.
    from_offset = False
    has_end = True
    if total > 0:
        start = max(0, total - LOG_TAIL_BYTES)
        from_offset = start > 0
        text, _ = client.fetch_text(f"{base}/logText/progressiveText?start={start}")
    else:
        # No size to work from, so ask for the last bytes directly. A server that
        # honours the range gives the true end; one that ignores it gives the
        # beginning, and a read of the beginning must NOT be dressed up as a
        # tail — slicing the last bytes off a capped prefix would hand over the
        # middle of the build and call it the failure.
        text, response = client.fetch_text(
            f"{base}/consoleText", {"Range": f"bytes=-{LOG_TAIL_BYTES}"}
        )
        ranged = any(key.lower() == "content-range" for key in response)
        size = len(text.encode("utf-8", "replace"))
        total = size
        if ranged:
            from_offset = True
        elif size > _MAX_LOG_BYTES:  # the read hit its ceiling: a prefix, not all
            from_offset, has_end = True, False
            total = 0  # how much more there is, nobody said
        # else: the whole log came back and it is short — nothing was left out.

    kept = text.splitlines()
    # With no end in hand the opening lines are at least coherent; the tail of a
    # prefix is an arbitrary point in the middle.
    shown = _excerpt(
        kept[-lines:] if has_end else kept[:lines], keep_last=has_end, have_file=bool(dest_dir)
    )

    path = None
    if dest_dir:
        # The whole slab, for an agent to search. Written before the excerpt is
        # cut, so what it reads is a superset of what it was shown.
        target = Path(dest_dir) / f"{slug(job.name)}-{number}.log"
        target.write_text(text, encoding="utf-8", errors="replace")
        path = str(target)

    return BuildLog(
        text="\n".join(shown),
        lines=len(shown),
        total_bytes=total,
        truncated=from_offset or len(shown) < len(kept),
        has_end=has_end,
        path=path,
        slab_bytes=len(text.encode("utf-8", "replace")),
        slab_lines=len(kept),
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

    @property
    def client(self) -> JenkinsClient:
        """The configured client, for the one-off fetches an analysis makes.

        Shared deliberately: it carries the credentials, the TLS decision and
        the timeout the operator configured, and a second client built beside it
        would be a second place for those to be got wrong.
        """
        return self._client

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


def analysable_build(status: JobStatus) -> int | None:
    """The build this chip would analyse, or None if there is nothing to explain.

    One rule: the last *completed* build was not a success. That covers a build
    running over a red job — where the breakage is still the news — and excludes
    a job that has never built or that radar could not reach, neither of which
    has a build to read a log from.
    """
    if status.stale:
        return None
    if status.state == RUNNING:
        # The spinner's own build has no verdict yet; the question is about the
        # last one that finished, whose result the ring is showing.
        return status.previous_number if status.previous in _ANALYSABLE else None
    if status.build_number is None:
        return None
    return status.build_number if status.state in _ANALYSABLE else None


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
        # The build the analyse button would explain, or None for a chip with
        # nothing to explain. The template decides what to draw; this decides
        # whether there is anything to draw it for.
        "analysable_build": analysable_build(status),
    }


def strip_view(
    snapshot: Snapshot,
    now: datetime | None = None,
    analysed: set[tuple[str, int, str]] | None = None,
    skill: str = "",
) -> dict:
    """The whole CI strip: chips, a one-line summary, and how fresh it all is.

    ``analysed`` is the set of (job, build, skill) that already have a stored
    analysis, so a chip can offer to re-open one instead of paying for it again.
    """
    now = now or datetime.now(UTC)
    jobs = [job_view(s, now) for s in snapshot.jobs]
    for view in jobs:
        build = view["analysable_build"]
        view["analysed"] = bool(
            skill and build is not None and (view["name"], build, skill) in (analysed or set())
        )

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
