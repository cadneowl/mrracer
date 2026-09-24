"""FastAPI application factory for the dashboard.

A fresh SQLite connection is opened per request (cheap, and safe under
uvicorn's threadpool; WAL mode allows concurrent readers), so the web layer
holds no long-lived DB handle.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
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
from ..context import (
    deslop_stdin_provider_for,
    jenkins_stdin_provider_for,
    stdin_provider_for,
)
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
# The event that redraws the board at once (see dashboard.html).
_BOARD_REFRESH = "board-refresh"

# How often the polish section re-reads a polish run of its own. Faster than the
# AI stats below it: this is a handful of lines and a spinner, and the thing it
# is watching for is the moment the answer arrives.
_DESLOP_TICK_S = 2

# Where a polished answer is filed: 'mr' for a merge request, 'build' for a
# Jenkins build. The two carry different coordinates, so the kind travels with
# them everywhere rather than being guessed from what is or is not set.
_MR, _BUILD = "mr", "build"


# A rewriting skill worth the name does not answer with a message: it answers
# with the message, the claims that still need checking, and what it cut. All
# three belong on the panel — but only the first belongs on the clipboard, and
# pasting the checklist into the merge request is exactly the thing the whole
# feature exists to stop. So radar finds the sendable part.
#
# Matched on the heading the output format asks for ("Ready to send"), in the
# shapes it actually comes back as: a markdown heading, a bold run-in, numbered
# or not, with or without a colon.
_SENDABLE_RE = re.compile(
    r"^[ \t]{0,3}(?:#{1,6}[ \t]*)?(?:\*\*|__)?[ \t]*(?:\d[.)][ \t]*)?"
    r"ready to send\b[ \t]*[:.]?[ \t]*(?:\*\*|__)?[ \t]*:?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
# Where it ends. Named sections only, and only when the name is the whole line:
# an earlier version ended the message at any markdown heading and at any line
# beginning "notes", which quietly cut a message at its own "### Blocking"
# sub-heading and at the sentence "Notes on the second finding: …". It took the
# disclosure line and half the findings with it, and produced a confident
# partial comment that neither the sender nor the reader could tell was partial.
#
# So the bias here is deliberate and one-way: a boundary this fails to find
# means the checklist is offered *with* the message, which is untidy and
# obvious. A boundary it finds too early means a finding silently disappears.
_AFTER_SENDABLE_RE = re.compile(
    r"^[ \t]{0,3}(?:#{1,6}[ \t]*)?(?:\*\*|__)?[ \t]*(?:\d[.)][ \t]*)?"
    r"(?:check before sending|checks? before sending|notes)\b"
    r"[ \t]*[:.]?[ \t]*(?:\*\*|__)?[ \t]*:?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def _unwrap(text: str) -> str:
    """Strip the fence or the quote marks a message was handed over in.

    The skill is told to put the message in a code block or a blockquote "so it
    can be copied cleanly" — which it is, right up until the backticks go into
    the merge request with it.

    Only a message that is *one* fenced block is unfenced. Comparing the first
    line with the last was enough to strip the opening fence of the first block
    and the closing fence of the last, leaving every fence in between — so a
    rewrite that quoted two snippets pasted stray backticks into the comment,
    which is the one thing this function exists to prevent.
    """
    lines = text.strip().splitlines()
    fences = [line for line in lines if line.lstrip().startswith("```")]
    if (
        len(fences) == 2
        and len(lines) >= 2
        and lines[0].lstrip().startswith("```")
        and lines[-1].strip() == "```"
    ):
        return "\n".join(lines[1:-1]).strip()
    body = [line for line in lines if line.strip()]
    if body and all(line.lstrip().startswith(">") for line in body):
        return "\n".join(
            line.lstrip()[1:].removeprefix(" ") if line.strip() else ""
            for line in lines
        ).strip()
    return text.strip()


def _sendable_part(text: str) -> str:
    """The message inside a rewrite, or "" if there is no telling which it is.

    Empty rather than a guess: copying the wrong half of an answer into a merge
    request is worse than copying all of it, because the reader cannot tell that
    anything is missing. When this finds nothing the panel simply offers the
    whole rewrite, which is what it would have offered anyway.
    """
    start = _SENDABLE_RE.search(text)
    if start is None:
        return ""
    rest = text[start.end():]
    end = _AFTER_SENDABLE_RE.search(rest)
    if end is None:
        # Both edges or nothing. Without the section that follows it there is no
        # telling where the message stops, and the honest answer is to offer the
        # whole rewrite — which, for an answer that is only a message, is the
        # message. Guessing the other way is what drops a finding.
        return ""
    message = _unwrap(rest[: end.start()])
    if not message:
        return ""   # a heading with nothing under it is not a message
    # Last line of defence against the failure this function must not have. If
    # what came out still reads like it contains the start of a later section,
    # the boundary was not found where it looked — so say nothing and let the
    # panel offer the whole rewrite, which is what it would have offered anyway.
    if _AFTER_SENDABLE_RE.search(message):
        return ""
    return message


def _digest(text: str) -> str:
    """A fingerprint of the exact text a polished answer was made from.

    Short on purpose: it is compared for equality and shown to nobody. Its only
    job is to notice that the run has since been run again, so a polished
    version of the answer it replaced is not offered as if it were current.
    """
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


# What one click of "+ more time" is worth. Ten minutes rather than the budget
# again: a review whose budget is forty minutes does not need another forty to
# finish its last finding, the button can be clicked twice, and a number the
# reader can predict beats one that scales with something they would have to
# look up. The route takes any figure, so a bookmark can grant a different one.
_EXTEND_S = 600

# The most one request may grant. A ceiling rather than no ceiling because this
# is a URL: the button posts ten minutes, and a hand-written request should not
# be able to turn a run's budget into a week by mistyping a number.
_MAX_EXTEND_S = 24 * 3600


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
    # `extra_s` included: a run that has been given more time has more time, and
    # a countdown that still showed the original budget would have the panel
    # saying 0:00 through work somebody deliberately paid to continue.
    return max(0, int(job.started_mono + job.budget_s + job.extra_s - time.monotonic()))


def _clock_text(remaining_s: int | None) -> str:
    """"7:03 left", rendered server-side so the pill is never a blank box."""
    if remaining_s is None:
        return ""
    if remaining_s <= 0:
        # Not "0:00 left", which reads as a clock that has stopped ticking on a
        # run that is still going. The rows say what is actually happening and
        # what the choice is; this only has to stop contradicting them.
        return "out of time"
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
    # A run being refused by the provider has usually measured nothing at all —
    # no tokens, no cost, no turns — so the `measured` gate below would drop the
    # one fact worth showing. It goes first, and on its own if need be.
    refused = sum(stats.api_errors.values())
    if not stats.measured and not refused:
        return None
    pills: list[dict] = []

    def pill(label: str, value: str, title: str) -> None:
        pills.append({"label": label, "value": value, "title": title})

    if refused:
        pill(
            "provider refused", f"{stats.last_api_error} ×{refused}",
            "the model provider turned these calls down and the run retried. "
            "401 is authentication, 429 a rate limit, 5xx the gateway itself — "
            "none of it reaches the command's stderr, so radar reads it from "
            "the run's own event stream",
        )
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
    # One pill for both, because they answer one question — is the provider
    # slow to start, or slow to write? — and the strip is kept to a headline.
    ttft, speed = stats.ttft_s, stats.output_tokens_per_s
    if ttft is not None or speed is not None:
        parts, notes = [], []
        if speed is not None:
            parts.append(f"{speed:.0f} tok/s")
            notes.append(
                f"output tokens per second of model time ({_tokens(stats.output_tokens)} "
                f"in {_secs(stats.api_ms)}), time to first token included, tools not"
            )
        if ttft is not None:
            parts.append(f"{ttft:.1f}s ttft")
            notes.append(
                "time to first token: how long the model took to start answering"
                + (", averaged over the steps" if stats.ttft_reports > 1 else "")
            )
        pill("speed", " · ".join(parts), "; ".join(notes))
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
        ("time to first token", f"{stats.ttft_s:.1f}s" if stats.ttft_s is not None else "",
         f"averaged over {stats.ttft_reports} steps; the slowest took "
         f"{_secs(stats.first_token_ms)}" if stats.ttft_reports > 1 else
         "from the request to the first word of the answer"),
        ("output speed",
         f"{stats.output_tokens_per_s:.0f} tok/s" if stats.output_tokens_per_s else "",
         "output tokens over the time spent waiting on the model, time to first "
         "token included"),
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


def _health_row(
    job: CommandJob, name: str, label: str, step: str | None, now: float, grace: float = 0.0
) -> dict:
    """One row of the panel's who-is-doing-what table (see _job_health.html)."""
    health = job_health(job, now, grace)
    status, elapsed = health["status"], _duration(health["elapsed_s"])
    if status == "running":
        state_text = f"running {elapsed}"
        if health["out_of_time"]:
            # The one state the panel exists to be read in. Said over the stall
            # warning and over whatever the run last did, because it is the only
            # one with a deadline of its own: when `decide_s` runs out the run is
            # killed and everything it has done goes with it.
            state_text = f"⏳ out of time after {elapsed}"
            note = (
                f"still running — {_duration(health['decide_s'])} to give it more "
                "time before it is stopped and its work is lost"
            )
        elif health["stalled"]:
            note = (
                f"⚠ only waiting for {_duration(health['idle_s'])} — "
                f"last: {health['waiting_on']}"
            )
        elif health["waiting_on"]:
            note = f"waiting: {health['waiting_on']}"
        else:
            note = health["last_line"]
        if health["granted_s"] and not health["out_of_time"]:
            state_text += f" (+{_duration(health['granted_s'])} given)"
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
        # Anything still running can be given more time, not only a run that has
        # already run out: a countdown getting short while a review is plainly
        # mid-thought is exactly when to say "carry on", and saying it early
        # costs nothing — an unused grant is unused time.
        "can_extend": status == "running",
        "out_of_time": health["out_of_time"],
        "extend_minutes": _EXTEND_S // 60,
        # Any step of a finished pipeline that actually ran can be run again —
        # the one that failed, and the one that succeeded and answered badly.
        # A review that missed the point is not a failure radar can detect, and
        # a synthesis of three reviews is worth re-running the moment one of
        # them is re-run. The route decides for certain, this only offers it;
        # `_health_rows` is what can see whether the pipeline is still going
        # and what else a retry would pay for.
        "can_retry": status in ("done", "error") and step is not None,
        # "retry" for a step that failed, "run again" for one that did not:
        # the same button, but ↻ retry on a green row reads as if radar thought
        # something had gone wrong.
        "retry_text": "↻ retry" if status == "error" else "↻ run again",
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
        return [_health_row(job, runner.kind, runner.config.label, None, now,
                            runner.config.timeout_grace_seconds)]
    rows = []
    # What a retry replaced, kept beside the attempt that replaced it. The
    # total below these rows counts it — it was paid for — so leaving it out
    # would show steps adding up to less than the bill they sit under, with the
    # difference nowhere to be explained (see `CommandJob.retried_spend`).
    earlier: dict[str, list] = {}
    for record in job.retried_spend:
        if isinstance(record, dict) and record.get("name"):
            earlier.setdefault(str(record["name"]), []).append(record)
    # A retry resumes the run from the step it is given, so it can only be
    # offered once the run it would resume has ended. While the pipeline works,
    # a step that has already finished is part of a run still in flight.
    running = job.status == "running"
    for index, stage in enumerate(runner.config.pipeline):
        for name in stage:
            label = runner.steps[name].config.label
            child = job.steps.get(name)
            rows.extend(_spent_row(record) for record in earlier.get(name, ()))
            if child is not None:
                row = _health_row(child, name, label, name, now,
                                  runner.steps[name].config.timeout_grace_seconds)
                row["can_retry"] = row["can_retry"] and not running
                if row["can_retry"]:
                    row["retry_again"] = _retry_again(runner, job, index)
                rows.append(row)
                continue
            rows.append({
                "name": name, "label": label or name, "step": name, "state": "pending",
                "state_text": "pending",
                "note": "" if running else "did not run",
                "stalled": False, "session_id": "", "resume": "", "can_stop": False,
                "can_extend": False, "out_of_time": False, "extend_minutes": 0,
                "can_retry": False, "retry_text": "", "retry_again": "", "stats": None,
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
        "can_extend": False,
        "out_of_time": False,
        "extend_minutes": 0,
        "can_retry": False,
        "retry_text": "",
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
            and s is not config.deslopify_skill
        ]
        data["storing_skills"] = storing_skills
        # The badge that re-opens a saved sendable version, and the names to put
        # on it. Every declared skill, not only the enabled ones: a polished
        # answer for a skill since switched off is still a saved answer, and a
        # badge with no label would be a button with no meaning.
        data["polish"] = _skill_view(config.deslopify_skill) if config.deslopify_skill else None
        data["skill_labels"] = {s.name: _skill_view(s) for s in config.skills}
        return data

    # Named in `jenkins.analysis.skill`, resolved once at load. Nothing here
    # infers it from a skill's name or contexts: which button exists is a fact
    # about the configuration, and the config says it in one place.
    build_skill = config.analysis_skill

    # Named in `deslopify.skill`, resolved the same way and for the same reason.
    # It is not a board skill: it never runs for a merge request of its own, so
    # it is kept out of `enabled_skills` above and refused by `start_command`.
    deslop_skill = config.deslopify_skill

    # The polish run most recently started for one answer, keyed by the
    # coordinates that answer is filed under, with the fingerprint of the text
    # it was handed. Per process and deliberately small: once a polish run has
    # finished its answer is in the database, and this is only what lets the
    # section show a run that is still going — and tell a finished one's numbers
    # apart from the saved row's.
    polished_by: dict[tuple[str, str, str, str], tuple[str, str]] = {}
    # Claimed under this before a polish starts, so two clicks on one answer
    # cannot become two runs — the same rule `pipeline.retry_step` applies to a
    # retry, and for the same reason: the second click is a second bill. The
    # button disables itself, which covers the double-click and nothing else;
    # two tabs, or two people looking at the same merge request, are the case
    # this is here for.
    polish_lock = threading.Lock()

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

    def _polish_source(job: CommandJob, skill) -> tuple[str, str, str] | None:
        """Where a polish of this panel's answer would be filed, or None.

        None for a run radar cannot key. A polished answer is kept against the
        run it was made from, and a button that could not save what it produced
        is worse than no button.
        """
        if build_skill is not None and skill is build_skill:
            if not job.title or job.build_number is None:
                return None
            return (_BUILD, job.title, str(job.build_number))
        if job.project_id is not None and job.mr_iid is not None:
            return (_MR, str(job.project_id), str(job.mr_iid))
        return None

    def _deslop_url(source: tuple[str, str, str], kind: str, job_id: str) -> str:
        """The polish section's URL for one answer.

        It carries the job id while the panel is showing a live run, so a polish
        rewrites exactly the text on screen rather than whatever the database
        holds. The two differ more often than it sounds: a skill that stores
        nothing has no row at all, and a run killed by the timeout keeps a
        partial answer that was never saved.
        """
        path = "/".join(quote(part, safe="") for part in (*source, kind))
        query = f"?job={quote(job_id, safe='')}" if job_id and job_id != "stored" else ""
        return f"/deslop/{path}{query}"

    def _deslop_view(source: tuple[str, str, str], kind: str, url: str, output: str) -> dict:
        """The polish section for one answer: what exists, and what is running.

        Precedence is the order a reader would want: a run happening now, then
        the answer it is about to replace. A polish that failed puts its message
        above whichever of those is shown — a failed retry must not quietly look
        like the saved version was never there.
        """
        digest = _digest(output)
        runner = runners[deslop_skill.name]
        record = polished_by.get((*source, kind))
        live = runner.get(record[0]) if record else None
        view = {
            "url": url,
            "kind": deslop_skill.name,
            "label": deslop_skill.label or deslop_skill.name,
            "button": deslop_skill.button or "polish",
            "icon": deslop_skill.icon,
            "tick_s": _DESLOP_TICK_S,
            "state": "none",
            "job": live,
            "rows": [],
            "remaining_s": None,
            "clock_text": "",
            "content": "",
            "content_html": None,
            "generated_at": "",
            "stale": False,
            "stats": None,
            "sendable": "",
            "error": live.error if live is not None and live.status == "error" else "",
            "persist_error": "",
        }
        if live is not None and live.status == "running":
            view["state"] = "running"
            view["rows"] = _health_rows(runner, live)
            view["remaining_s"] = _remaining_s(live)
            view["clock_text"] = _clock_text(view["remaining_s"])
            return view

        with Database(db_path) as db:
            stored = db.get_deslopified(*source, kind)

        if live is not None and live.status == "done":
            # The run that just finished. Its text is what was saved a moment
            # ago — read from the job rather than from the row so a save that
            # failed still shows the answer it produced, with the warning.
            view.update(
                state="done",
                content=live.output,
                stats=_stats_view(live.stats),
                stale=record[1] != digest,
                persist_error=live.persist_error,
                generated_at=stored["generated_at"] if stored else "",
            )
        elif stored is not None:
            view.update(
                state="done",
                content=stored["content"],
                stats=_stats_view(stats_from_json(stored.get("stats"))),
                stale=stored.get("source_digest", "") != digest,
                generated_at=stored["generated_at"],
            )
        elif view["error"]:
            # Nothing saved and the run failed: whatever it wrote before it did
            # is still worth showing, exactly as a failed review's is.
            view.update(state="error", content=live.output)

        view["content_html"] = (
            _render_markdown(view["content"]) if view["content"].strip() else None
        )
        # The part of the rewrite that is actually the message. Everything is
        # shown — the checks and the notes are the half that makes the rewrite
        # trustworthy — but this is what the copy button puts on the clipboard.
        view["sendable"] = _sendable_part(view["content"])
        return view

    def _deslop_for(job: CommandJob, skill, status: str, output: str) -> dict | None:
        """The polish section for a panel, or None when it has nothing to offer.

        Nothing while the run is still going: the answer is still being written,
        and rewriting half of one is paying to polish a draft about to change.
        Nothing on the polish skill's own panel either — a polish of a polish is
        a second rewrite of the same text, and the button that made this one is
        already on the section it came from.
        """
        if deslop_skill is None or skill is deslop_skill or not output.strip():
            return None
        if status == "running":
            return None
        source = _polish_source(job, skill)
        if source is None:
            return None
        return _deslop_view(source, job.kind, _deslop_url(source, job.kind, job.id), output)

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
            health = _health_context(job.kind, job, watch=(status == "running"))
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
                "extend_all": False,
                "extend_minutes": 0,
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
        response = templates.TemplateResponse(
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
                # The polish section: a rewrite of this answer fit to send, on
                # demand, kept beside the answer rather than replacing it. None
                # when this panel has nothing to offer one (see `_deslop_for`).
                "deslop": _deslop_for(job, skill, status, output),
            },
        )
        if status != "running" and job.id != "stored":
            # A run that has just ended may have saved a result, and its row
            # on the board should say so now rather than at the next tick.
            response.headers["HX-Trigger"] = _BOARD_REFRESH
        return response

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
            build_number=build,
            subject=f"#{build}",
            title=job.name,
            status="done",
            output=stored["content"],
            # A stored answer keeps what its run cost: re-opening it a week later
            # answers "what did this take?" as well as "what did it say?".
            stats=stats_from_json(stored.get("stats")),
        )
        return _panel(request, job_record, generated_at=stored["generated_at"])

    # Declared here, with /jenkins and /threads, so they sit ahead of the
    # /{kind}/... routes: those match on segment count alone until validation,
    # and a five-segment path must not be read as a skill's own namespace.
    def _polish_key(source_kind: str, a: str, b: str) -> tuple[str, str, str]:
        """Validate the coordinates a polish is filed under, from a URL.

        Checked rather than trusted: every one of these ends up as an int() or
        as a database key, and a path someone typed is not the place to find out
        which.
        """
        if source_kind not in (_MR, _BUILD):
            raise HTTPException(
                status_code=404,
                detail=f"unknown source {source_kind!r} (expected {_MR} or {_BUILD})",
            )
        # ASCII digits, not `str.isdigit()`: that is true of '\u00b2' and of the
        # Arabic-Indic digits, and int() accepts the second and raises on the
        # first — so the check would pass a value that crashes the lookup two
        # lines later, turning a bad URL into a 500.
        def numeric(value: str) -> bool:
            return value.isascii() and value.isdigit()

        if not (numeric(b) and (source_kind == _BUILD or numeric(a))):
            raise HTTPException(status_code=404, detail="bad coordinates for that source")
        return (source_kind, a, b)

    def _polish_target(
        source: tuple[str, str, str], kind: str, job_id: str | None
    ) -> tuple[str, str, str]:
        """The run a polish is about: its subject, its heading, and the exact
        text to rewrite.

        Resolved here rather than posted by the browser. The text is what an
        agent is handed and what gets stored against this run, and a request is
        not where either should come from. A job id names a run this process
        still holds, which is the only way to reach the answer of a skill that
        stores nothing, or the half a run wrote before it failed; without one,
        the saved answer is the answer.
        """
        skill = skills_by_name.get(kind)
        if skill is None:
            raise HTTPException(status_code=404, detail=f"no skill named {kind!r}")
        if skill is deslop_skill:
            raise HTTPException(
                status_code=404,
                detail=f"{kind} is the polish skill — its own answer is not polished again",
            )
        heading = skill.label or kind
        source_kind, a, b = source
        if job_id:
            runner = runners.get(kind)
            job = runner.get(job_id) if runner is not None else None
            if job is None:
                raise HTTPException(status_code=404, detail="unknown job")
            # The id came out of a URL, so it is checked against the coordinates
            # beside it: without this, one merge request's answer could be filed
            # against another's row.
            if job.status == "running" or _polish_source(job, skill) != source:
                raise HTTPException(
                    status_code=409,
                    detail=f"that {heading} run is not a finished answer for this one",
                )
            if not job.output.strip():
                raise HTTPException(
                    status_code=409, detail=f"that {heading} run wrote nothing to polish"
                )
            # Named, not numbered: a rewrite is asked what question it serves,
            # and "!7" is not one. The title is what says what the change is
            # about, and the job has been carrying it all along.
            return f"{job.subject} {job.title}".strip(), heading, job.output

        with Database(db_path) as db:
            if source_kind == _BUILD:
                stored = db.get_build_analysis(a, int(b), kind)
                subject = f"{a} #{b}"
            else:
                stored = db.get_test_plan(int(a), int(b), kind)
                snap = db.get_snapshot(int(a), int(b))
                subject = f"!{b} {snap['title']}".strip() if snap else f"!{b}"
        if stored is None:
            raise HTTPException(
                status_code=409,
                detail=f"there is no saved {heading} answer for {subject} to polish — "
                "radar no longer holds the run that produced this one",
            )
        # Same refusal as the live branch above, for the same reason: a paid
        # model run over an empty draft answers with something, and that
        # something gets filed as the sendable version of this answer.
        if not stored["content"].strip():
            raise HTTPException(
                status_code=409,
                detail=f"the saved {heading} answer for {subject} is empty, so there is "
                "nothing to rewrite",
            )
        return subject, heading, stored["content"]

    def _polish_ctx(source: tuple[str, str, str], subject: str) -> dict:
        """What the polish command's template is filled from.

        The same placeholders its subject's own skill would get, so a polish
        command can name `{web_url}` or `{build_url}` and mean it. A field radar
        cannot resolve — a Jenkins job dropped from the config since the answer
        was saved — substitutes to empty, which is what an absent placeholder
        has always done.
        """
        source_kind, a, b = source
        if source_kind == _BUILD:
            job = next((j for j in config.jenkins.jobs if j.name == a), None)
            return {
                "jenkins_job": a,
                "build_number": int(b),
                "build_url": f"{job.url}/{b}/" if job else "",
                "title": a,
                "subject": subject,
            }
        with Database(db_path) as db:
            snap = db.get_snapshot(int(a), int(b))
        if snap is None:
            return {"project_id": int(a), "mr_iid": int(b), "subject": subject}
        return _ctx_for(snap, int(a), int(b))[0]

    @app.post("/deslop/{source_kind}/{source_a}/{source_b}/{kind}", response_class=HTMLResponse)
    def start_deslop(
        request: Request, source_kind: str, source_a: str, source_b: str, kind: str,
        job: str | None = None,
    ):
        """Rewrite one finished answer into something fit to send.

        On demand, never automatically: it is another model run over work that
        has already been paid for, and the original answer is the one radar
        keeps. What it produces is stored beside that answer, not over it.
        """
        if deslop_skill is None:
            raise HTTPException(status_code=404, detail="no polish skill is configured")
        source = _polish_key(source_kind, source_a, source_b)
        subject, heading, text = _polish_target(source, kind, job)
        digest = _digest(text)
        url = _deslop_url(source, kind, job or "")

        def on_success(finished: CommandJob) -> None:
            with Database(db_path) as db:
                db.save_deslopified(
                    *source, kind, finished.output, digest, stats_to_json(finished.stats)
                )

        provider = deslop_stdin_provider_for(
            deslop_skill.name, config, heading, subject, text,
            config.deslopify.destination_for(source[0]),
        )
        runner = runners[deslop_skill.name]
        key = (*source, kind)
        # Checked and claimed without letting go, because everything before this
        # only *read*: two clicks, or two tabs, would otherwise both find
        # nothing running and both start a run — and a run is a bill. The loser
        # is answered with the section, not an error: it wants to see the
        # rewrite, and one is already on its way.
        #
        # `start` is admitted, resolved and handed to a thread; it does no I/O
        # worth holding a lock across, and nothing it takes is held by anything
        # that wants this one.
        with polish_lock:
            previous = polished_by.get(key)
            running = runner.get(previous[0]) if previous else None
            if running is None or running.status != "running":
                started = runner.start(
                    _polish_ctx(source, subject),
                    on_success=on_success,
                    stdin_provider=provider,
                )
                # Claimed before the fragment is drawn, so the section this
                # answers with is already the one following the new run.
                polished_by[key] = (started.id, digest)
        return templates.TemplateResponse(
            request,
            "_deslop.html",
            {"deslop": _deslop_view(source, kind, url, text), "oob": True},
        )

    @app.get(
        "/deslop/{source_kind}/{source_a}/{source_b}/{kind}/saved",
        response_class=HTMLResponse,
    )
    def saved_deslop(
        request: Request, source_kind: str, source_a: str, source_b: str, kind: str
    ):
        """Re-open a saved sendable version on its own.

        Its own door, because a polished answer can outlive the answer it was
        made from: a review skill stores nothing, so once radar has been
        restarted the panel that offered the rewrite is gone and the rewrite is
        not. Shown through the ordinary panel — it is a saved answer like any
        other, with the same copy button and the same ✕.
        """
        if deslop_skill is None:
            raise HTTPException(status_code=404, detail="no polish skill is configured")
        source = _polish_key(source_kind, source_a, source_b)
        with Database(db_path) as db:
            row = db.get_deslopified(*source, kind)
        if row is None:
            raise HTTPException(status_code=404, detail="no saved sendable version")
        skill = skills_by_name.get(kind)
        heading = skill.label if skill else kind
        job = CommandJob(
            id="stored",
            kind=deslop_skill.name,
            status="done",
            output=row["content"],
            stats=stats_from_json(row.get("stats")),
        )
        # The panel is headed with the polish skill's own name, so `title` is
        # where it says what this is a rewrite *of* — which is the first thing
        # the reader needs, and the one thing "sendable version" does not say.
        if source[0] == _BUILD:
            job.build_number = int(source[2])
            job.subject = f"#{source[2]}"
            job.title = f"{source[1]} · {heading}"
        else:
            job.project_id, job.mr_iid = int(source[1]), int(source[2])
            job.subject = f"!{source[2]}"
            job.title = heading
        return _panel(request, job, generated_at=row["generated_at"])

    @app.get("/deslop/{source_kind}/{source_a}/{source_b}/{kind}", response_class=HTMLResponse)
    def deslop_section(
        request: Request, source_kind: str, source_a: str, source_b: str, kind: str,
        job: str | None = None,
    ):
        """The polish section on its own — what a run of it refreshes into."""
        if deslop_skill is None:
            raise HTTPException(status_code=404, detail="no polish skill is configured")
        source = _polish_key(source_kind, source_a, source_b)
        _, _, text = _polish_target(source, kind, job)
        return templates.TemplateResponse(
            request,
            "_deslop.html",
            {
                "deslop": _deslop_view(
                    source, kind, _deslop_url(source, kind, job or ""), text
                ),
                "oob": True,
            },
        )

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

    def _health_context(kind: str, job: CommandJob, watch: bool = False) -> dict:
        """What _job_health.html needs: the rows, and whether to keep refreshing.

        ``watch`` marks a fragment that was drawn into a *running* panel, and it
        is what lets the panel notice that the run has ended. The event stream
        was the only thing doing that, and a stream is exactly what a browser
        throws away when it freezes a background tab — leaving a finished run
        under a spinner, with its answer already on disk. These rows keep
        arriving because they are ordinary polled requests, so when they come
        back saying the run is over, they say so loudly enough to redraw the
        panel around them.
        """
        runner = runners[kind]
        running = job.status == "running"
        return {
            "kind": kind,
            "job": job,
            # Only in a panel that is still showing the run as going: rendered
            # into a finished one it would ask for a redraw of what is already
            # there, once every time, forever.
            "redraw_when_done": watch and not running,
            "watch": watch,
            "rows": _health_rows(runner, job),
            "live": running,
            "stop_all": running and isinstance(runner, PipelineRunner),
            # One decision for a stage of three reviews that ran out together.
            "extend_all": running and isinstance(runner, PipelineRunner),
            "extend_minutes": _EXTEND_S // 60,
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
    def job_health_rows(request: Request, kind: str, job_id: str, watch: int = 0):
        job = _running_job(kind, job_id)
        return templates.TemplateResponse(
            request, "_job_health.html", _health_context(kind, job, watch=bool(watch))
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
                detail=f"{step} cannot be run again: it has to be a step that ran, in "
                "a finished run that this radar started",
            )
        return _panel(request, job)

    @app.post("/{kind}/extend/{job_id}", response_class=HTMLResponse)
    def extend_job(
        request: Request, kind: str, job_id: str,
        step: str | None = None, seconds: int = _EXTEND_S,
    ):
        """Give a running job more time — one step of a pipeline, or all of them.

        Answers with the health rows, which is where the countdown and the state
        that changed both live, and which the browser is refreshing anyway. The
        grant is a number of seconds so a bookmark can ask for a different one;
        the panel's button asks for `_EXTEND_S`.
        """
        job = _running_job(kind, job_id)
        runner = runners[kind]
        if step is not None and (
            not isinstance(runner, PipelineRunner) or step not in runner.steps
        ):
            raise HTTPException(status_code=404, detail=f"{kind} has no step named {step!r}")
        if seconds <= 0 or seconds > _MAX_EXTEND_S:
            raise HTTPException(
                status_code=400,
                detail=f"seconds must be between 1 and {_MAX_EXTEND_S}",
            )
        # False when it had already ended; the rows returned say so either way,
        # exactly as they do for a stop that arrived a moment too late.
        runner.extend(job_id, seconds, step)
        return templates.TemplateResponse(
            request, "_job_health.html", _health_context(kind, job)
        )

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
        if skills_by_name[kind] is deslop_skill:
            # It rewrites an answer that already exists, and this route has
            # only a merge request: it would run with an empty draft and file
            # its result nowhere. The board never offers it, but the URL is
            # guessable and the refusal belongs here rather than in the template.
            raise HTTPException(
                status_code=404,
                detail=f"{kind} polishes an answer another run produced, not a merge "
                "request — it is offered on the panel of a run that has finished",
            )
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
        # htmx swaps this empty content in to dismiss — and the board is
        # redrawn, because closing a panel is when its result is looked for.
        return HTMLResponse("", headers={"HX-Trigger": _BOARD_REFRESH})

    @app.get("/{kind}/stored/{project_id}/{mr_iid}", response_class=HTMLResponse)
    def stored_plan(request: Request, kind: str, project_id: int, mr_iid: int):
        skill = skills_by_name.get(kind)
        if skill is None or not skill.stores_result or skill is config.analysis_skill:
            raise HTTPException(status_code=404, detail=f"{kind} has no stored results")
        with Database(db_path) as db:
            plan = db.get_test_plan(project_id, mr_iid, kind)
        if plan is None:
            raise HTTPException(status_code=404, detail="no stored result")
        # If the run that wrote it is still in this process's memory, show that
        # job instead. The answer is the same text — it is what was saved — but
        # the job knows which step wrote what, and a step can be run again from
        # it. Without this the buttons last only as long as the panel stays
        # open: closing it and re-opening the saved answer from the board is the
        # ordinary way to read a review, and it would quietly take away the only
        # way to re-run a step of it.
        runner = runners.get(kind)
        live = runner.finished_for(project_id, mr_iid) if runner is not None else None
        if live is not None:
            return _panel(request, live, generated_at=plan["generated_at"])
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
