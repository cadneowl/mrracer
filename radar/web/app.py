"""FastAPI application factory for the dashboard.

A fresh SQLite connection is opened per request (cheap, and safe under
uvicorn's threadpool; WAL mode allows concurrent readers), so the web layer
holds no long-lived DB handle.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

import markdown as md
import nh3
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from ..charts import Series, chart
from ..coach import build_coach
from ..commands import (
    SNAPSHOT_KEYS,
    CommandJob,
    RunStats,
    job_health,
    stats_from_json,
    stats_from_mapping,
    stats_to_json,
)
from ..config import Config
from ..context import jenkins_stdin_provider_for, stdin_provider_for
from ..db import Database
from ..jenkins import JenkinsMonitor, analysable_build, strip_view
from ..jira import extract_keys
from ..pipeline import PipelineRunner, build_runners
from ..service import build_dashboard, build_threads

_BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_BASE / "templates"))


def _asset_version(path: Path) -> str:
    """A short digest of a static file, for cache-busting its URL.

    StaticFiles answers with an ETag and a Last-Modified and no Cache-Control,
    which leaves the browser to guess how long the file stays fresh — and the
    usual guess is a fraction of the file's age, so a stylesheet that has been
    on disk for months is reused for days without so much as a revalidation.
    The page then renders new markup against old rules: every class added in the
    upgrade has no styling at all, which looks like broken CSS rather than like
    a cache. Putting the digest in the URL makes an upgraded file a different
    URL, so it cannot be answered from a cache filled by the old one.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]

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


_STREAM_TICK = 0.4      # seconds between progress polls on the SSE stream
_CLOCK_EVERY = 12       # ticks between countdown re-anchors (~5s)

# How often the browser re-reads the CI strip. This is a read of an in-process
# cache, not a Jenkins call — Jenkins itself is polled on the scheduler's own
# `jenkins.poll_interval_seconds` — so it is cheap enough to keep well under
# that interval and leave the screen close to the cache.
_CI_TICK_S = 15

# How often the AI stats section redraws itself while a run works. Slower than
# the health rows above it: this is five charts and a page of numbers, and a
# trajectory does not change meaningfully between one second and the next.
_AI_TICK_S = 5


def _remaining_s(job: CommandJob | None, status: str | None = None) -> int | None:
    """Seconds left of a running job's budget, or None if it has no clock left
    to show (unknown job, or one that already finished).

    ``status`` is passed in by a caller that already read it, so a worker
    flipping the job mid-render can't have the panel take the running branch
    while this one decides there is no clock — the two would disagree and the
    countdown would silently not render. Measured on the monotonic clock the
    worker enforces the budget with, not on wall time.
    """
    if job is None or not job.budget_s:
        return None
    if (status if status is not None else job.status) != "running":
        return None
    return max(0, int(job.started_mono + job.budget_s - time.monotonic()))


def _clock_text(remaining_s: int | None) -> str:
    """"7:03 left", rendered server-side so the pill is never a blank box."""
    if remaining_s is None:
        return ""
    return f"{remaining_s // 60}:{remaining_s % 60:02d} left"


def _duration(seconds: int) -> str:
    """"45s", "10m", "1h 04m" — how long, at a glance."""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def _tokens(count: float) -> str:
    """"842", "8.5k", "1.25M" — a token count read at a glance.

    Rounded on purpose: what an operator does with these numbers is compare
    runs, and three significant figures answer that while a nine-digit total
    only pushes the rest of the row off the panel. Takes a float because a
    chart axis is scaled to round numbers, not to whole tokens.
    """
    count = int(round(count))
    if count < 1000:
        return str(count)
    if count < 999_500:
        text = f"{count / 1000:.1f}"
        return (text[:-2] if text.endswith(".0") else text) + "k"
    return f"{count / 1_000_000:.2f}M"


def _money(amount: float) -> str:
    """"$0.21", "$1.37", "<$0.01" — what a run cost, to the cent it matters at.

    A zero is nothing, not almost nothing. No run radar launches is free, so a
    zero here is a provider that did not price it — and rounding that up to a
    cent would read as "this cost next to nothing" where it means "nobody
    said". Callers get the empty string and put the absence into words.
    """
    if amount <= 0:
        return ""
    if amount < 0.01:
        return "<$0.01"
    return f"${amount:,.2f}" if amount >= 10 else f"${amount:.3f}".rstrip("0").rstrip(".")


def _secs(ms: int) -> str:
    """"940ms", "8.5s", "4m 12s" — a duration the run measured in milliseconds."""
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    return f"{ms // 60_000}m {ms % 60_000 // 1000:02d}s"


def _pct(share: float) -> str:
    """"88%", "0.4%" — a fraction, at the precision that still says something."""
    percent = share * 100
    return f"{percent:.1f}%" if 0 < percent < 10 else f"{percent:.0f}%"


def _top(counter: dict, limit: int = 6) -> str:
    """"Read ×12, Bash ×9, Grep ×6 (+2 more)" — a tally, busiest first."""
    ordered = sorted(counter.items(), key=lambda pair: (-pair[1], pair[0]))
    shown = ", ".join(f"{name} ×{count}" for name, count in ordered[:limit])
    rest = len(ordered) - limit
    return f"{shown} (+{rest} more)" if rest > 0 else shown


def _stats_view(stats: RunStats, details: bool = False) -> dict | None:
    """What a run spent, as the panel says it — or None if it said nothing.

    Two depths. The ``pills`` are the headline every panel and every step row
    carries; ``groups`` is everything else the run reported, which only the
    finished panel asks for (``details``) — it is a lot of numbers, and a step
    row refreshing every three seconds is the wrong place to read them.

    A command that does not speak stream-json reports no usage at all, and a row
    of zeros would read as a measurement rather than as the absence of one, so
    such a run gets no numbers on its panel (see ``RunStats.measured``).

    Every figure carries the words for what it is, including whether it is
    billed or still radar's own count: a thinking total nobody can tell apart
    from an estimate is worse than one labelled as an estimate.
    """
    if not stats.measured:
        return None
    pills: list[dict] = []

    def pill(label: str, value: str, title: str) -> None:
        pills.append({"label": label, "value": value, "title": title})

    if stats.turn_count:
        pill(
            "requests", str(stats.turn_count),
            "requests to the model. The API is stateless, so every tool call sends "
            "the whole conversation again and counts as another one — which is why "
            "almost all the input below is cache reads",
        )
    # Everything the model read, not the fraction of it that was billed at full
    # price: `input_tokens` alone counts only what was neither read from nor
    # written to the cache, and Claude Code caches nearly the whole prompt — so
    # that number is a handful of tokens beside a 74k prompt, and reads as if
    # the run had no input at all. The split is one hover away, and in the
    # details block, because it is what the bill is made of.
    read = stats.input_tokens + stats.cache_read_tokens + stats.cache_write_tokens
    if read or not stats.billed:
        pill(
            "in", _tokens(read),
            f"everything the model read: {_tokens(stats.input_tokens)} fresh, "
            f"{_tokens(stats.cache_read_tokens)} from the prompt cache, "
            f"{_tokens(stats.cache_write_tokens)} written to it. Only the fresh part "
            "is billed at full price",
        )
    hit = stats.cache_hit_rate
    if hit:
        pill(
            "cached", _pct(hit),
            f"of the input was served from the cache ({_tokens(stats.cache_read_tokens)} "
            f"of {_tokens(read)}), at about a tenth of the price",
        )
    if stats.output_tokens:
        pill(
            "out", _tokens(stats.output_tokens),
            "output tokens the model wrote, thinking included",
        )
    if stats.thinking_tokens:
        billed = stats.thinking_billed
        pill(
            "thinking", ("" if billed else "~") + _tokens(stats.thinking_tokens),
            "thinking tokens, part of the output above" if billed else
            "thinking tokens so far — the run's own live estimate, replaced by the "
            "billed count when it finishes",
        )
    if stats.cost_usd:
        pill("cost", _money(stats.cost_usd), "what this run was billed, as the CLI reports it")
    avg = stats.avg_response_s
    if avg is not None:
        pill(
            "avg", f"{avg:.1f}s",
            "average time one request took the model" if stats.billed else
            "average time one request took the model — radar's own measure while "
            "the run is in flight",
        )
    # Not a measurement but a warning, and the one figure here that changes what
    # the answer above it is worth: a denied tool is something the run wanted to
    # look at and was not allowed to.
    if stats.denied_calls:
        pill(
            "denied", str(stats.denied_calls),
            f"tool calls refused by the permission mode ({_top(stats.denials)}) — "
            "the run carried on without them",
        )
    view = {"model": stats.model, "pills": pills}
    if details:
        view["groups"] = _stats_groups(stats)
    return view


def _bytes(count: float) -> str:
    """"842 B", "8.5 kB", "1.25 MB" — a size, when tokens are not on offer."""
    count = float(count)
    if count < 1000:
        return f"{count:.0f} B"
    if count < 999_500:
        return f"{count / 1000:.1f} kB".replace(".0 kB", " kB")
    return f"{count / 1_000_000:.2f} MB"


def _whole(value: float) -> str:
    """A count for a chart axis, or nothing where the gridline falls between
    two of them — "0" twice over is worse than one unlabelled line."""
    return f"{value:.0f}" if float(value).is_integer() else ""


def _series(stats: RunStats, key: str, scale: float = 1.0) -> list[Series]:
    """One series per timeline, plotting ``key`` against seconds into the run.

    A pipeline has one timeline per step and a plain skill has one of its own
    (see ``RunStats.timelines``), so every chart below draws both shapes without
    knowing which it was given. A sample missing the value it is asked for —
    the first request has nothing to say about a wait nobody timed — is left
    out rather than plotted as zero.
    """
    out = []
    for line in stats.timelines():
        points = tuple(
            (float(sample.get("t") or 0.0), float(sample[key]) * scale)
            for sample in line["samples"]
            if isinstance(sample.get(key), (int, float))
            and not isinstance(sample[key], bool)
            # A zero here means "nothing reported this", not "this was nothing":
            # a provider that sends no token counts would otherwise be drawn as
            # a run that carried no context at all.
            and (sample[key] or key not in _UNREPORTED_AS_ZERO)
        )
        if points:
            out.append(Series(label=line.get("label") or "this run", points=points))
    return out


# Fields where a zero means the provider said nothing, rather than nothing
# happened. A context of zero is never real — every request carries a prompt —
# so it is the provider declining to say. A wait of zero, or no tool calls in a
# turn, are both things that genuinely happen and are plotted.
_UNREPORTED_AS_ZERO = frozenset({"context"})


def _stats_charts(stats: RunStats) -> list:
    """The run's shape: what a column of totals cannot say.

    Each one answers a question an operator actually asks of a slow or
    expensive review — is it getting slower as it goes, is it filling its
    context, is it grinding through tools, is it thinking harder — and they all
    share an x axis, so a spike in one can be read against the others.
    """
    charts = []
    waits = _series(stats, "wait")
    if waits:
        # The mean of the points drawn, not the headline average. Those are two
        # measures on two clocks — the headline is the run's own model time over
        # its own turn count once it reports them, this is radar's measure of
        # the same waits from outside — and a reference line that does not
        # average its own data is a line nobody can read.
        plotted = [y for one in waits for _, y in one.points]
        mean = sum(plotted) / len(plotted)
        charts.append(chart(
            waits, kind="line",
            title="Response time per request",
            caption="from the tool results going back to the next answer "
                    "starting; the first point includes starting the session",
            y_format=lambda v: f"{v:.0f}s" if v >= 1 or v == 0 else f"{v:.1f}s",
            reference=(mean, f"average {mean:.1f}s"),
        ))
    context = _series(stats, "context")
    if context:
        window = stats.context_window
        charts.append(chart(
            context, kind="line",
            title="Context carried per request",
            caption="the whole prompt of each request — a run that climbs and "
                    "never comes back down is filling up",
            y_format=_tokens,
            reference=(window, f"window {_tokens(window)}")
            if window and max(y for s in context for _, y in s.points) > window / 4
            else None,
        ))
    else:
        # This provider reports no prompt sizes, so the chart above would be a
        # flat line of zeros passing itself off as a measurement. The bytes of
        # conversation are not tokens, but they climb for the same reason and
        # answer the same question.
        weight = _series(stats, "bytes")
        if weight:
            charts.append(chart(
                weight, kind="line",
                title="Conversation size per request",
                caption="the provider reports no prompt sizes, so this is the "
                        "conversation the request had to carry, in bytes — it "
                        "grows for the same reason the prompt does",
                y_format=_bytes,
            ))
    tools = _series(stats, "tools")
    if tools:
        charts.append(chart(
            tools, kind="bars",
            title="Tool calls per request",
            caption="what the run did between answers; a tall bar with a long "
                    "wait beside it is an agent grinding, not a slow model",
            y_format=_whole,
        ))
    thinking = _series(stats, "thinking")
    if thinking and any(y for s in thinking for _, y in s.points):
        charts.append(chart(
            thinking, kind="bars",
            title="Thinking per request",
            caption="the run's own live estimate, in tokens",
            y_format=_tokens,
        ))
    # Only for tokens. The byte fallback above is already a running total of
    # the conversation, so adding it up again would draw a curve of sums of
    # sums — a bigger number every time and a meaning nobody could state.
    cumulative = []
    for line in (stats.timelines() if context else []):
        running, points = 0.0, []
        for sample in line["samples"]:
            value = sample.get("context")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                running += value
                points.append((float(sample.get("t") or 0.0), running))
        if points:
            cumulative.append(Series(label=line.get("label") or "this run",
                                     points=tuple(points)))
    if cumulative:
        charts.append(chart(
            cumulative, kind="line",
            title="Tokens read, cumulative",
            caption="every request re-sends the conversation, so this is the "
                    "shape of the bill",
            y_format=_tokens,
        ))
    return charts


def _cache_ttl_note(stats: RunStats) -> str:
    """How the cache writes split between the two TTLs, which are priced apart.

    The split comes from the run's own ``usage``, which covers its main loop;
    the total comes from the per-model breakdown, which also counts what its
    subagents wrote. So the two reconcile for a plain run and not for one that
    delegated — and when they don't, the note says which half it is describing
    rather than quietly looking like a rounding error.
    """
    split = [
        f"{_tokens(count)} at {ttl}"
        for count, ttl in (
            (stats.cache_write_5m_tokens, "5m"), (stats.cache_write_1h_tokens, "1h"),
        ) if count
    ]
    if not split:
        return ""
    note = ", ".join(split)
    counted = stats.cache_write_5m_tokens + stats.cache_write_1h_tokens
    if counted < stats.cache_write_tokens:
        note += " in the main loop; the rest was written by its subagents"
    return note


def _stats_groups(stats: RunStats) -> list[dict]:
    """Everything the run reported, grouped for the panel's details block.

    Deliberately exhaustive: this is the place to answer "why did that review
    cost four dollars", "was it the model or the tools that took ten minutes",
    "did it run out of context" and "was it refused something" without going to
    the session transcript.
    """
    groups: list[dict] = []

    def group(title: str, rows: list[tuple]) -> None:
        kept = [
            {"label": label, "value": value, "note": note}
            for label, value, note in rows
            if value
        ]
        if kept:
            groups.append({"title": title, "rows": kept})

    counted = "as the run was billed" if stats.billed else "radar's count so far"
    peak = ""
    if stats.peak_context_tokens:
        share = stats.context_share
        window = f" of {_tokens(stats.context_window)}" if stats.context_window else ""
        peak = f"{_tokens(stats.peak_context_tokens)}{window}"
        if share is not None:
            peak += f" ({_pct(share)})"
    hit = stats.cache_hit_rate
    group("Tokens", [
        ("read by the model", _tokens(
            stats.input_tokens + stats.cache_read_tokens + stats.cache_write_tokens
        ), "the whole prompt of every request, cache included"),
        ("fresh input", _tokens(stats.input_tokens),
         f"{counted} — the part of it that was not in the cache"),
        ("cache read", _tokens(stats.cache_read_tokens),
         f"{_pct(hit)} of all input was served from cache" if hit else ""),
        ("cache written", _tokens(stats.cache_write_tokens), _cache_ttl_note(stats)),
        ("output", _tokens(stats.output_tokens), ""),
        ("thinking", _tokens(stats.thinking_tokens),
         "part of the output" if stats.thinking_billed else "the run's own estimate"),
        ("total", _tokens(stats.total_tokens), "everything that went through the model"),
        ("biggest request", peak,
         "the most context any one request carried — how close this run came to "
         "filling the window"),
    ])

    # A model that reported no price still reports its tokens, so the row stays
    # and says which half is missing. "not priced" rather than a blank, because
    # the question this answers is "what did that cost" and silence is an
    # answer to it — just not the one a zero would imply.
    group("Money", [("cost", _money(stats.cost_usd), "")] + [
        (name, _money(use.get("cost_usd", 0.0)) or "not priced",
         f"in {_tokens(use.get('input', 0))} · cached {_tokens(use.get('cache_read', 0))} · "
         f"out {_tokens(use.get('output', 0))} · thinking {_tokens(use.get('thinking', 0))}"
         + ("" if use.get("cost_usd") else " — the provider reported no cost"))
        for name, use in sorted(stats.models.items())
    ])

    share = stats.model_share
    group("Time", [
        ("wall clock", _secs(stats.wall_ms) if stats.wall_ms else "", "as the run clocked itself"),
        ("waiting on the model", _secs(stats.api_ms) if stats.api_ms else "",
         f"{_pct(share)} of the run; the rest was tools and radar" if share else ""),
        ("first token", _secs(stats.first_token_ms) if stats.first_token_ms else "",
         "from launch to the first word of the answer"),
        ("per request", f"{stats.avg_response_s:.1f}s" if stats.avg_response_s else "",
         "on average" if stats.billed else "radar's measure, while it runs"),
        ("queued turns", str(stats.queued_turns) if stats.queued_turns else "", ""),
    ])

    subagents = [
        f"{count} {label}" for label, count in (
            ("spawned", stats.subagents_spawned), ("finished", stats.subagents_completed),
            ("failed", stats.subagents_failed), ("killed", stats.subagents_killed),
            ("refused", stats.subagents_refused),
        ) if count
    ]
    # Only when some request went unreported: an input figure of zero has two
    # very different causes — a run that read nothing, and a provider that does
    # not say what it read — and the operator who cannot tell them apart debugs
    # the wrong system.
    unreported = stats.turn_count and stats.requests_with_usage < stats.turn_count
    group("What it did", [
        ("requests", str(stats.turn_count) if stats.turn_count else "",
         f"the run's own count: {stats.reported_turns}"
         if stats.reported_turns and stats.reported_turns != stats.turn_count else ""),
        ("usage reported",
         f"{stats.requests_with_usage} of {stats.turn_count} requests" if unreported else "",
         # Two very different situations, and the operator acts on them
         # differently: totals that are missing a part, and totals that only
         # exist because the run summed them up itself at the end.
         "none of them carried counts as they went — these totals come from the "
         "run's own final report, so they are whole but only arrived at the end"
         if stats.billed and not stats.requests_with_usage else
         "the others came back with no token counts at all, so the totals here "
         "are only what the provider did report — not what the run actually read"),
        ("tool calls", str(stats.tool_calls) if stats.tool_calls else "", _top(stats.tools)),
        ("web searches", str(stats.web_searches) if stats.web_searches else "",
         "server-side tool, billed per request"),
        ("web fetches", str(stats.web_fetches) if stats.web_fetches else "",
         "server-side tool, billed per request"),
        ("subagents", ", ".join(subagents),
         _top(stats.subagent_types)
         + (f" · {stats.subagent_depth} deep" if stats.subagent_depth else "")),
    ])

    limits = ", ".join(
        f"{window.replace('_', ' ')} {_pct(used)}"
        for window, used in sorted(stats.rate_limits.items())
    )
    group("How it ended", [
        ("outcome", stats.outcome,
         f"stop reason: {stats.stop_reason}" if stats.stop_reason else ""),
        ("errors", str(stats.errors) if stats.errors else "",
         "results the run itself flagged as errors"),
        ("denied tool calls", str(stats.denied_calls) if stats.denied_calls else "",
         f"{_top(stats.denials)} — refused by the permission mode, and the run "
         "carried on without them"),
        ("rate limits", limits,
         f"of the account's budget used ({stats.rate_limit_status})"
         if stats.rate_limit_status else "of the account's budget used"),
    ])

    group("The run", [
        ("model", stats.model, f"window {_tokens(stats.context_window)} · output ceiling "
         f"{_tokens(stats.max_output_tokens)}" if stats.context_window else ""),
        ("claude code", stats.cli_version, ""),
        ("permission mode", stats.permission_mode,
         "anything off the allowlist is refused without asking"
         if stats.permission_mode == "dontAsk" else ""),
        ("output style", stats.output_style, ""),
        ("tools offered", str(stats.tools_offered) if stats.tools_offered else "",
         f"{stats.mcp_servers} MCP server(s)" if stats.mcp_servers else ""),
        ("service", " · ".join(part for part in (
            stats.service_tier, stats.speed, stats.inference_geo) if part), ""),
    ])
    return groups


def _health_row(job: CommandJob, name: str, label: str, step: str | None, now: float) -> dict:
    """One row of the panel's who-is-doing-what table (see _job_health.html)."""
    health = job_health(job, now)
    status, elapsed = health["status"], _duration(health["elapsed_s"])
    if status == "running":
        state_text = f"running {elapsed}"
        if health["stalled"]:
            note = (
                f"⚠ only waiting for {_duration(health['idle_s'])} — "
                f"last: {health['waiting_on']}"
            )
        elif health["waiting_on"]:
            note = f"waiting: {health['waiting_on']}"
        else:
            note = health["last_line"]
    elif status == "done":
        state_text, note = f"✓ done in {elapsed}", ""
    else:
        error = job.error.strip()
        state_text, note = f"✗ failed after {elapsed}", error.splitlines()[0] if error else ""
    session = health["session_id"]
    resume = f"claude --resume {session}"
    return {
        "name": name,
        "label": label or name,
        "step": step,
        "state": status,
        "state_text": state_text,
        "note": _short_note(note),
        "stalled": health["stalled"],
        "session_id": session,
        # A session is resumed from the directory it ran in, or it isn't found.
        "resume": f"cd {job.cwd} && {resume}" if job.cwd else resume,
        "can_stop": status == "running",
        # A failed step of a finished pipeline can be run again on its own;
        # the route decides for certain, this only offers it. What that would
        # cost beyond this step is filled in by `_health_rows`, which is the
        # one that can see the stages either side of it.
        "can_retry": status == "error" and step is not None,
        "retry_again": "",
        # What this step has spent so far. Here rather than only on the finished
        # panel because this fragment is the one that refreshes itself: the
        # counters move while the run works.
        "stats": _stats_view(job.stats),
    }


def _retry_again(runner, job: CommandJob, stage_index: int) -> str:
    """Which finished steps a retry of this stage would also pay for.

    Everything after the retried step runs again — its input is about to
    change, and a synthesis of a review that has been rewritten is a synthesis
    of nothing. So a step further down that already succeeded is work the
    retry buys twice, and on a review pipeline that step is the expensive one.
    Named rather than implied: the button is a single click on a job that has
    already spent real money.
    """
    return ", ".join(
        runner.steps[name].config.label or name
        for stage in runner.config.pipeline[stage_index + 1:]
        for name in stage
        if name in job.steps and job.steps[name].status == "done"
    )


def _short_note(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _health_rows(runner, job: CommandJob, now: float | None = None) -> list[dict]:
    """A row per step of a pipeline's job — started or not — or one row for a
    plain skill's job."""
    now = time.monotonic() if now is None else now
    if not isinstance(runner, PipelineRunner):
        return [_health_row(job, runner.kind, runner.config.label, None, now)]
    rows = []
    # What a retry replaced, kept beside the attempt that replaced it. The
    # total below these rows counts it — it was paid for — so leaving it out
    # would show steps adding up to less than the bill they sit under, with the
    # difference nowhere to be explained (see `CommandJob.retried_spend`).
    earlier: dict[str, list] = {}
    for record in job.retried_spend:
        if isinstance(record, dict) and record.get("name"):
            earlier.setdefault(str(record["name"]), []).append(record)
    for index, stage in enumerate(runner.config.pipeline):
        for name in stage:
            label = runner.steps[name].config.label
            child = job.steps.get(name)
            rows.extend(_spent_row(record) for record in earlier.get(name, ()))
            if child is not None:
                row = _health_row(child, name, label, name, now)
                if row["can_retry"]:
                    row["retry_again"] = _retry_again(runner, job, index)
                rows.append(row)
                continue
            rows.append({
                "name": name, "label": label or name, "step": name, "state": "pending",
                "state_text": "pending",
                "note": "" if job.status == "running" else "did not run",
                "stalled": False, "session_id": "", "resume": "", "can_stop": False,
                "can_retry": False, "retry_again": "", "stats": None,
            })
    return rows


def _spent_row(record: dict) -> dict:
    """One finished attempt as a health row: what it was, how long, what it cost.

    The shape of a row `_job_health.html` draws, with nothing live in it —
    nothing to stop, nothing to retry, no polling. Written once and used by
    both readers of these records: the breakdown of a stored result, and the
    superseded attempts a retry leaves behind on a live pipeline. They are the
    same record (see `CommandJob.retried_spend`), so they should read the same.

    ``stats`` is a `RunStats` while the job is in memory and a mapping once it
    has been through the database; either is taken.
    """
    status = str(record.get("status") or "")
    elapsed = record.get("elapsed_s")
    elapsed = _duration(elapsed) if isinstance(elapsed, int) and elapsed >= 0 else ""
    if status == "done":
        state_text = f"✓ done in {elapsed}" if elapsed else "✓ done"
    elif status:
        state_text = f"✗ failed after {elapsed}" if elapsed else "✗ failed"
    else:
        state_text = ""
    numbers = record.get("stats")
    if not isinstance(numbers, RunStats):
        numbers = stats_from_mapping(numbers)
    session = str(record.get("session_id") or "")
    return {
        "name": str(record.get("name") or ""),
        "label": str(record.get("label") or record.get("name") or ""),
        "step": None,
        "state": status or "done",
        "state_text": state_text,
        "note": "",
        "stalled": False,
        # A stored session id is a handle to a conversation that has very
        # likely been collected by now. Shown anyway, because `claude
        # --resume` either finds it or says it cannot, and the id is also
        # how a run is matched to a captured stream.
        "session_id": session,
        "resume": f"claude --resume {session}",
        "can_stop": False,
        "can_retry": False,
        "retry_again": "",
        "stats": _stats_view(numbers),
    }


def _stored_rows(stats: RunStats) -> list[dict]:
    """The per-step breakdown of a stored result, shaped like health rows.

    Same shape, so the same fragment draws it: a result re-opened next week
    reads exactly as the panel did when it finished, minus the parts that only
    mean something while a job is alive. What it keeps is the half that answers
    "and where did the half hour go": which step, how long, and what it cost.

    Empty for a plain skill (nothing to break down) and for a result stored
    before radar kept the breakdown, which then shows its total as it always
    did rather than a row of blanks.
    """
    return [_spent_row(record) for record in stats.steps if isinstance(record, dict)]


def _render_markdown(text: str) -> Markup:
    html = md.markdown(text, extensions=["fenced_code", "tables", "sane_lists"])
    clean = nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        url_schemes={"http", "https", "mailto"},
    )
    return Markup(clean)


def create_app(
    config: Config,
    db_path: str,
    poll_now: Callable[[], object] | None = None,
    jenkins: JenkinsMonitor | None = None,
) -> FastAPI:
    """Build the dashboard app.

    ``poll_now`` runs one GitLab polling pass and returns when it has stored
    what it found; ``radar serve`` passes the background poller's own pass.
    Without it (no GitLab credentials, or a test) the board is read-only over
    existing data and the refresh button is not offered.

    ``jenkins`` is the CI strip's ``JenkinsMonitor``, kept fresh by the
    background scheduler. The web layer only ever reads its snapshot: Jenkins is
    never fetched while a request is waiting. None means no jobs are configured,
    and no strip is rendered at all.
    """
    app = FastAPI(title="radar", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(_BASE / "static")), name="static")
    # Read once at startup: the file cannot change under a running server
    # without a restart bringing the new code that goes with it.
    templates.env.globals["css_version"] = _asset_version(_BASE / "static" / "radar.css")
    skills_by_name = {s.name: s for s in config.skills}
    runners = build_runners(config.skills)
    enabled = {s.name: s.enabled for s in config.skills}

    def _skill_view(s) -> dict:
        return {"name": s.name, "label": s.label, "button": s.button, "icon": s.icon}

    # Skills that persist output: the board shows a re-openable badge per skill
    # that has a stored result for a given MR (row.stored_kinds decides which).
    storing_skills = [
        _skill_view(s)
        for s in config.skills
        if s.stores_result and s is not config.analysis_skill
    ]

    def context(view: str | None) -> dict:
        with Database(db_path) as db:
            data = build_dashboard(db, config, view=view)
        data["poll_interval_minutes"] = config.gitlab.poll_interval_minutes
        data["can_refresh"] = poll_now is not None
        data["enabled_skills"] = [
            _skill_view(s)
            for s in config.skills
            if s.enabled and s is not config.analysis_skill
        ]
        data["storing_skills"] = storing_skills
        return data

    # Named in `jenkins.analysis.skill`, resolved once at load. Nothing here
    # infers it from a skill's name or contexts: which button exists is a fact
    # about the configuration, and the config says it in one place.
    build_skill = config.analysis_skill

    def _ci_view() -> dict | None:
        """The CI strip from cache — no I/O beyond the stored-analysis lookup."""
        if jenkins is None:
            return None
        analysed: set[tuple[str, int, str]] = set()
        if build_skill is not None:
            with Database(db_path) as db:
                analysed = db.analysed_builds()
        view = strip_view(
            jenkins.snapshot(),
            analysed=analysed,
            skill=build_skill.name if build_skill else "",
        )
        return view | {
            "tick_s": _CI_TICK_S,
            "skill": _skill_view(build_skill) if build_skill else None,
        }

    def _jenkins_job(job_name: str):
        """The configured job by that name, or a 404.

        Looked up rather than trusted: the name arrives in a URL, and every
        request that follows it reaches a URL built from the config entry.
        """
        job = next((j for j in config.jenkins.jobs if j.name == job_name), None)
        if job is None:
            raise HTTPException(status_code=404, detail=f"no Jenkins job named {job_name!r}")
        return job

    def _panel(request: Request, job, generated_at: str | None = None) -> HTMLResponse:
        skill = skills_by_name.get(job.kind)
        # Read the job's mutable state ONCE, and render from that read. The
        # worker thread flips status while this request is being served, so a
        # template that re-read `job.status` could take the "done" branch with
        # the output captured a moment earlier, while it was still running —
        # and the done fragment stops polling, so the panel would stay empty.
        # Status first: the runner publishes output and error *before* the
        # status that advertises them, so "done" here always has its output.
        status = job.status
        output, error = job.output, job.error
        remaining_s = _remaining_s(job, status)
        # Rows for a pipeline always — a finished one still says which step
        # failed and why — and for a plain skill while it runs. Never for a
        # re-opened stored result: it has no steps and nothing left to stop.
        runner = runners.get(job.kind)
        show_rows = (
            runner is not None
            and job.id != "stored"
            and (status == "running" or isinstance(runner, PipelineRunner))
        )
        if show_rows:
            health = _health_context(job.kind, job)
        else:
            # A finished plain skill has nothing to break down, and a
            # re-opened result has no live job to ask — but it carries the
            # breakdown of the run that wrote it (see `_stored_rows`). Drawn
            # through the same fragment, with nothing live in it: the reader
            # gets the same per-step line — which step, how long, what it cost
            # — instead of one total and four unexplained chart legends.
            rows = _stored_rows(job.stats)
            health = {
                "rows": rows,
                "live": False,
                "stop_all": False,
                "total": _stats_view(job.stats) if rows else None,
            }

        # What the run spent, at the top of the panel. Only once the run has
        # ended: this fragment is rendered when the panel opens and again when
        # the run finishes, so a running job's copy would be a snapshot taken
        # at nought — while it runs, the self-refreshing health rows carry the
        # counters instead. And only when the rows below do not already end in
        # a `total`, which is this same figure: a pipeline would otherwise
        # print its bill twice over, once unlabelled here and once as the
        # total, which reads as a mistake rather than as a summary.
        headline = None
        if status != "running" and not health.get("total"):
            headline = _stats_view(job.stats)

        # A build analysis — saved, finished or failed — can be run again over
        # the job's latest failed build; the analyse route replaces what was
        # saved. `title` is the Jenkins job's name for these jobs.
        rerun_url = (
            f"/jenkins/{quote(job.title, safe='')}/analyze"
            if build_skill is not None and skill is build_skill and job.title
            else None
        )
        return templates.TemplateResponse(
            request,
            "_command_panel.html",
            {
                **health,
                "rerun_url": rerun_url,
                "job": job,
                "status": status,
                "error": error,
                "kind": job.kind,
                "heading": skill.label if skill else job.kind,
                "icon": skill.icon if skill else "▶",
                "generated_at": generated_at,
                # Seconds left of the worker's budget, for the panel's
                # countdown; None once there is no clock left to show. Uses the
                # status read above, not a fresh one — see _remaining_s.
                "remaining_s": remaining_s,
                "clock_text": _clock_text(remaining_s),
                # The headline, once the run has ended (see `headline`).
                "stats": headline,
                # And the section under it, which refreshes on its own and so
                # is rendered whatever the job is doing.
                **(_ai_context(job.kind, job) or {"ai": None}),
                # Also rendered for a failed job: a run killed by the timeout
                # keeps whatever it had written, and half a review beats none.
                "output_html": _render_markdown(output) if output.strip() else None,
                # The markdown itself, for the copy button. Rendered into a
                # textarea, so Jinja's escaping is what keeps skill output —
                # which is untrusted — from breaking out of it.
                "output": output if output.strip() else "",
            },
        )

    def _ctx_for(snap: dict, project_id: int, mr_iid: int) -> tuple[dict, list[str]]:
        keys = extract_keys(
            [snap.get("title"), snap.get("source_branch"), snap.get("description")],
            config.jira.project_keys,
        )
        ctx = {"project_id": project_id, "mr_iid": mr_iid, "subject": f"!{mr_iid}"}
        ctx.update({k: snap.get(k, "") for k in SNAPSHOT_KEYS})
        ctx["jira_keys"] = " ".join(keys)
        ctx["jira_keys_csv"] = ",".join(keys)
        return ctx, keys

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, view: str | None = None):
        # `view` present -> explicit choice (empty string clears the filter);
        # absent -> fall back to the remembered cookie.
        cookie = request.cookies.get(COOKIE_NAME) or None
        token = (view or None) if view is not None else cookie

        resp = templates.TemplateResponse(
            request, "dashboard.html", context(token) | {"ci": _ci_view()}
        )
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

    @app.get("/partials/ci", response_class=HTMLResponse)
    def ci(request: Request):
        """The CI strip's own refresh, on its own cadence.

        Separate from the board partial on purpose: builds start and finish far
        faster than the board's 60s tick, and the strip must not be swept away
        with the board when someone changes the view filter.
        """
        view = _ci_view()
        if view is None:
            raise HTTPException(status_code=404, detail="no Jenkins jobs are configured")
        return templates.TemplateResponse(request, "_ci.html", {"ci": view})

    # Declared ahead of the /{kind}/... routes, like /partials and /threads: a
    # skill named "jenkins" cannot shadow these, and the order says so rather
    # than leaving it to chance.
    @app.post("/jenkins/{job_name}/analyze", response_class=HTMLResponse)
    def analyze_build(request: Request, job_name: str):
        """Run the build-analysis skill over the job's last failed build."""
        if build_skill is None or jenkins is None:
            raise HTTPException(status_code=404, detail="no build-analysis skill is enabled")
        job = _jenkins_job(job_name)
        status = next((s for s in jenkins.snapshot().jobs if s.name == job_name), None)
        build = analysable_build(status) if status else None
        if build is None:
            # The chip stopped being broken between the render and the click —
            # a build went green, or radar lost contact with Jenkins.
            raise HTTPException(
                status_code=409, detail=f"{job_name} has no failed build to analyse right now"
            )

        kind = build_skill.name
        ctx = {
            "jenkins_job": job.name,
            "build_number": build,
            "build_url": f"{job.url}/{build}/",
            "title": job.name,
            "subject": f"#{build}",
        }
        client = jenkins.client

        def on_success(finished) -> None:
            with Database(db_path) as db:
                db.save_build_analysis(
                    job.name, build, kind, finished.output, stats_to_json(finished.stats)
                )

        provider = jenkins_stdin_provider_for(kind, config, client, job, status, build)
        started = runners[kind].start(ctx, on_success=on_success, stdin_provider=provider)
        return _panel(request, started)

    @app.get("/jenkins/{job_name}/analysis/{build}", response_class=HTMLResponse)
    def stored_analysis(request: Request, job_name: str, build: int):
        """Re-open the analysis already stored for that exact build."""
        if build_skill is None:
            raise HTTPException(status_code=404, detail="no build-analysis skill is enabled")
        job = _jenkins_job(job_name)
        with Database(db_path) as db:
            stored = db.get_build_analysis(job.name, build, build_skill.name)
        if stored is None:
            raise HTTPException(status_code=404, detail="no stored analysis for that build")
        job_record = CommandJob(
            id="stored",
            kind=build_skill.name,
            subject=f"#{build}",
            title=job.name,
            status="done",
            output=stored["content"],
            # A stored answer keeps what its run cost: re-opening it a week later
            # answers "what did this take?" as well as "what did it say?".
            stats=stats_from_json(stored.get("stats")),
        )
        return _panel(request, job_record, generated_at=stored["generated_at"])

    @app.post("/refresh", response_class=HTMLResponse)
    def refresh(request: Request):
        """Poll GitLab now, then answer with the board built from what it stored.

        Synchronous on purpose. The click means "someone just asked me to review
        something — show it", so what gets swapped in has to be the board *after*
        the pass, not a promise that one is coming: a fire-and-forget kick would
        return the same stale board the button was pressed to escape.
        """
        if poll_now is None:
            raise HTTPException(status_code=404, detail="polling is not configured")
        poll_now()
        return board(request)

    # Declared ahead of the /{kind}/... routes: those all carry a literal second
    # segment ('status', 'stored', 'close') so they cannot shadow this, but the
    # order makes that independent of a skill ever being named "threads".
    @app.get("/threads/{project_id}/{mr_iid}", response_class=HTMLResponse)
    def threads(request: Request, project_id: int, mr_iid: int, author: str | None = None):
        with Database(db_path) as db:
            data = build_threads(db, project_id, mr_iid, author=author or None)
        if data is None:
            raise HTTPException(status_code=404, detail="unknown merge request")
        # Comment bodies are markdown written by anyone who can see the MR, so
        # they go through the same render-then-sanitize path as skill output.
        for thread in data["threads"]:
            for note in thread["notes"]:
                note["body_html"] = _render_markdown(note["body"])
        return templates.TemplateResponse(request, "_threads.html", data)

    def _ai_context(kind: str, job: CommandJob) -> dict | None:
        """The AI stats section: the run's numbers, and the shape of them.

        A job that has only just started has nothing to show yet and is given
        the section anyway: the panel around it is rendered once, so a section
        withheld now is one that never appears, and watching a run is exactly
        what the charts are for. None is for a finished run that reported
        nothing at all — a command that does not speak stream-json.
        """
        view = _stats_view(job.stats, details=True)
        if view is None:
            if job.status != "running":
                return None
            view = {"model": "", "pills": [], "groups": []}
        view["charts"] = _stats_charts(job.stats)
        # Named, not just written: the next step after seeing a bad
        # run is reading it back, and that takes the path.
        view["capture"] = job.capture_path
        return {
            "ai": view,
            "kind": kind,
            "job": job,
            "live": job.status == "running",
            "tick_s": _AI_TICK_S,
        }

    def _health_context(kind: str, job: CommandJob) -> dict:
        """What _job_health.html needs: the rows, and whether to keep refreshing."""
        runner = runners[kind]
        running = job.status == "running"
        return {
            "kind": kind,
            "job": job,
            "rows": _health_rows(runner, job),
            "live": running,
            "stop_all": running and isinstance(runner, PipelineRunner),
            # A pipeline's steps each say what they spent; this is the bill for
            # the whole run, and it moves with this fragment's own refresh.
            "total": _stats_view(job.stats) if isinstance(runner, PipelineRunner) else None,
        }

    def _running_job(kind: str, job_id: str) -> CommandJob:
        runner = runners.get(kind)
        job = runner.get(job_id) if runner is not None else None
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return job

    # Both declared ahead of POST /{kind}/{project_id}/{mr_iid}. Its segments are
    # untyped until validation, so "/review/stop/<id>" would match that route
    # and be refused as a bad merge-request number rather than reach this one.
    @app.get("/{kind}/health/{job_id}", response_class=HTMLResponse)
    def job_health_rows(request: Request, kind: str, job_id: str):
        job = _running_job(kind, job_id)
        return templates.TemplateResponse(
            request, "_job_health.html", _health_context(kind, job)
        )

    @app.get("/{kind}/stats/{job_id}", response_class=HTMLResponse)
    def ai_stats(request: Request, kind: str, job_id: str):
        """The AI stats section, redrawn — charts and all — while a run works."""
        job = _running_job(kind, job_id)
        context = _ai_context(kind, job)
        if context is None:
            # A command that reports no usage: answer with a fragment carrying
            # no trigger, so the browser stops asking.
            context = {
                "ai": {"pills": [], "groups": [], "charts": []}, "kind": kind,
                "job": job, "live": False, "tick_s": _AI_TICK_S,
            }
        return templates.TemplateResponse(request, "_ai_stats.html", context)

    @app.post("/{kind}/retry/{job_id}", response_class=HTMLResponse)
    def retry_step(request: Request, kind: str, job_id: str, step: str):
        """Run one failed step again and finish the pipeline from there.

        Answers with the whole panel rather than the row, because the job is
        running again: the panel is what carries the progress log, the
        countdown and the stream that redraws it when the run ends.
        """
        job = _running_job(kind, job_id)
        runner = runners[kind]
        if not isinstance(runner, PipelineRunner) or step not in runner.steps:
            raise HTTPException(status_code=404, detail=f"{kind} has no step named {step!r}")
        if not runner.retry_step(job_id, step):
            raise HTTPException(
                status_code=409,
                detail=f"{step} cannot be retried: it has to be a failed step of a "
                "finished run that this radar started",
            )
        return _panel(request, job)

    @app.post("/{kind}/stop/{job_id}", response_class=HTMLResponse)
    def stop_job(request: Request, kind: str, job_id: str, step: str | None = None):
        """Stop a job — or one step of a pipeline, which carries on without it."""
        job = _running_job(kind, job_id)
        runner = runners[kind]
        if step is not None and (
            not isinstance(runner, PipelineRunner) or step not in runner.steps
        ):
            raise HTTPException(status_code=404, detail=f"{kind} has no step named {step!r}")
        # False when it had already ended; the rows returned say so either way.
        runner.stop(job_id, step)
        return templates.TemplateResponse(
            request, "_job_health.html", _health_context(kind, job)
        )

    @app.post("/{kind}/{project_id}/{mr_iid}", response_class=HTMLResponse)
    def start_command(request: Request, kind: str, project_id: int, mr_iid: int):
        if kind not in runners or not enabled[kind]:
            raise HTTPException(status_code=404, detail=f"{kind} is not enabled")
        if skills_by_name[kind] is config.analysis_skill:
            # It is about a build, and this route is about a merge request: it
            # would run with no context at all and file the result where nothing
            # would ever show it. The board never offers this, but the URL is
            # guessable and the refusal belongs here rather than in the template.
            raise HTTPException(
                status_code=404,
                detail=f"{kind} analyses Jenkins builds, not merge requests",
            )
        with Database(db_path) as db:
            snap = db.get_snapshot(project_id, mr_iid)
        if snap is None:
            raise HTTPException(status_code=404, detail="unknown merge request")
        ctx, keys = _ctx_for(snap, project_id, mr_iid)

        def on_success_for(name: str):
            """What to do with a finished result of skill ``name`` for this MR."""
            if not skills_by_name[name].stores_result:
                return None
            csv = ",".join(keys)

            def on_success(job) -> None:
                with Database(db_path) as db:
                    db.save_test_plan(
                        project_id, mr_iid, name, csv, job.output, stats_to_json(job.stats)
                    )

            return on_success

        runner = runners[kind]
        if isinstance(runner, PipelineRunner):
            # Each step is handed what its own button would give it, and saves
            # what its own button would save.
            job = runner.start(
                ctx,
                on_success=on_success_for(kind),
                provider_for=lambda step: stdin_provider_for(
                    step, config, project_id, mr_iid, keys
                ),
                on_success_for=on_success_for,
            )
        else:
            stdin_provider = stdin_provider_for(kind, config, project_id, mr_iid, keys)
            job = runner.start(
                ctx, on_success=on_success_for(kind), stdin_provider=stdin_provider
            )
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

        # Outlive the job it is tailing: a skill given a long timeout_seconds
        # would otherwise have its stream cut mid-review and the panel would
        # reconnect for no reason. Still bounded, so a wedged job can't hold a
        # connection open forever.
        ticks = int((runner.config.timeout_seconds + 120) / _STREAM_TICK)

        async def gen():
            seen = 0
            for tick in range(ticks):
                snap = runner.progress_since(job_id, seen)
                if snap is None:
                    yield _sse("end", {"status": "error"})
                    return
                items, status = snap
                for item in items:
                    seen = max(seen, item.pop("rev"))
                    yield _sse("progress", item)
                # The countdown ticks in the browser every second; this only has
                # to correct it, so it goes out every few seconds rather than on
                # every poll — a long-running skill would otherwise spend
                # thousands of frames saying nothing new.
                if tick % _CLOCK_EVERY == 0 or status != "running":
                    yield _sse("clock", {"remaining_s": _remaining_s(runner.get(job_id))})
                if status != "running":
                    yield _sse("end", {"status": status})
                    return
                await asyncio.sleep(_STREAM_TICK)
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
        if skill is None or not skill.stores_result or skill is config.analysis_skill:
            raise HTTPException(status_code=404, detail=f"{kind} has no stored results")
        with Database(db_path) as db:
            plan = db.get_test_plan(project_id, mr_iid, kind)
        if plan is None:
            raise HTTPException(status_code=404, detail="no stored result")
        job = CommandJob(
            id="stored", kind=kind, project_id=project_id, mr_iid=mr_iid,
            subject=f"!{mr_iid}", title=f"{plan['jira_keys']}",
            status="done", output=plan["content"],
            stats=stats_from_json(plan.get("stats")),
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
