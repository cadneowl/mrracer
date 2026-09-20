"""Read a captured run and say what happened to it.

The panel measures a run while it happens; this reads one back afterwards, from
the file ``RADAR_CAPTURE_STREAM`` wrote (see ``commands``). It exists for the
question the charts raise but cannot settle: *why* were the waits long, and why
did some requests report no tokens at all.

What it can settle, because the raw events carry it and the panel does not:

* **Which model actually served each request.** A gateway in front of a
  third-party model may route concurrent sessions to different backends, and
  the served model is on every assistant event. Two model strings in one run,
  with different waits, is the whole diagnosis.
* **Whether a request reported usage at all**, and on which of its events —
  a provider that attaches counts to the block after the thinking is a
  reporting quirk, and one that never sends them is a gateway gap.
* **Whether the waits track the prompt.** Correlating each wait against the
  context that request carried separates "this skill accumulates context" from
  "this provider is slow or queueing", which look identical on a clock.

Nothing here talks to a network or reads a config: it is a file, arithmetic and
a table, so it can be run on a stream someone emailed you.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field


@dataclass
class Request:
    """One request to the model, as the stream describes it."""

    index: int
    at: float                  # seconds into the run
    wait: float | None         # from the tool results going back, to this answer
    context: int | None        # None when nothing reported any
    model: str = ""
    thinking: int = 0          # the run's own estimate for this turn
    first_block: str = ""
    tool: str = ""
    stop_reason: str = ""
    usage_on_event: int | None = None   # which event of the turn carried counts
    events: int = 0
    # What the model was doing while this request was outstanding. A provider
    # that streams its reasoning says so continuously, so a wait full of
    # thinking is the model working and a silent one is the only kind that can
    # be a queue. This is the difference between "the model is slow" and "the
    # gateway is busy", and nothing else in the stream distinguishes them.
    thinking_in_wait: int = 0
    silent: bool = False
    convo_bytes: int = 0    # the conversation so far, when nothing reports sizes


@dataclass
class Analysis:
    events: int = 0
    unreadable: int = 0
    span_s: float = 0.0
    session_model: str = ""
    cli_version: str = ""
    permission_mode: str = ""
    requests: list = field(default_factory=list)
    models: dict = field(default_factory=dict)      # served model -> [waits]
    waiting_s: float = 0.0                          # time spent waiting on the model
    results: list = field(default_factory=list)     # the run's own summaries
    errors: list = field(default_factory=list)      # lines the run flagged
    denials: dict = field(default_factory=dict)
    # (at, seconds, what was happening): a long gap while a tool runs is not
    # the model going quiet, and calling both "a silence" sends the reader
    # after the wrong one.
    gaps: list = field(default_factory=list)

    @property
    def with_usage(self) -> int:
        return sum(1 for r in self.requests if r.context is not None)


def load(path: str) -> list:
    """The records ``RADAR_CAPTURE_STREAM`` wrote: (arrival, event) pairs.

    Also accepts a plain stream-json file — one a skill was run with by hand,
    redirected to disk. Those carry no arrival times, so the waits come out
    unknown and everything else still reads.
    """
    records = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                records.append((None, None))
                continue
            if isinstance(parsed, dict) and "t" in parsed and (
                "event" in parsed or "raw" in parsed
            ):
                records.append((parsed.get("t"), parsed.get("event")))
            else:
                records.append((None, parsed if isinstance(parsed, dict) else None))
    return records


def _blocks(event: dict) -> list:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, list) else []


def _tool_name(event: dict) -> str:
    for block in _blocks(event):
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = block.get("name")
            if isinstance(name, str):
                return name
    return ""


def analyse(records: list) -> Analysis:
    """Turn the captured events into one run's story."""
    out = Analysis(events=len(records))
    seen: dict = {}
    turn_started: float | None = None
    thinking = 0
    last_at = None
    last_kind = ""
    streamed: list = []      # (when, tokens) for every thinking event
    convo = 0                # bytes of conversation, as a stand-in for its size
    for at, event in records:
        if event is None:
            out.unreadable += 1
            continue
        kind = event.get("type")
        if at is not None:
            out.span_s = max(out.span_s, float(at))
            if last_at is not None and at - last_at > 30:
                # What was outstanding across it: a tool the model called, or
                # the model's own answer.
                doing = "a tool" if last_kind == "assistant" else "the model"
                out.gaps.append((round(last_at, 1), round(at - last_at, 1), doing))
            last_at = at
            last_kind = kind

        if kind == "system":
            subtype = event.get("subtype")
            if subtype == "init":
                out.session_model = str(event.get("model") or "")
                out.cli_version = str(event.get("claude_code_version") or "")
                out.permission_mode = str(event.get("permissionMode") or "")
                turn_started = at
            elif subtype == "thinking_tokens":
                delta = event.get("estimated_tokens_delta")
                if isinstance(delta, int):
                    thinking += delta
                    if at is not None:
                        streamed.append((at, delta))
            continue

        if kind == "user":
            turn_started = at
            convo += len(json.dumps(event))
            continue

        if kind == "result":
            out.results.append(event)
            for entry in event.get("permission_denials") or ():
                name = entry.get("tool_name") if isinstance(entry, dict) else None
                key = str(name or "a tool")
                out.denials[key] = out.denials.get(key, 0) + 1
            if event.get("is_error"):
                out.errors.append(str(event.get("terminal_reason") or "error"))
            continue

        if kind != "assistant":
            continue

        message = event.get("message")
        if not isinstance(message, dict):
            continue
        convo += len(json.dumps(event))
        key = message.get("id") or f"?{len(seen) + 1}"
        request = seen.get(key)
        if request is None:
            first = next(
                (b.get("type") for b in _blocks(event) if isinstance(b, dict)), ""
            )
            request = Request(
                index=len(seen) + 1,
                at=round(float(at), 1) if at is not None else 0.0,
                wait=(round(at - turn_started, 2)
                      if at is not None and turn_started is not None else None),
                context=None,
                model=str(message.get("model") or ""),
                thinking=thinking,
                first_block=str(first or ""),
                tool=_tool_name(event),
                stop_reason=str(message.get("stop_reason") or ""),
            )
            if request.wait:
                out.waiting_s += request.wait
                inside = [
                    tokens for when, tokens in streamed
                    if turn_started is not None and turn_started <= when <= at
                ]
                request.thinking_in_wait = sum(inside)
                request.silent = not inside
            request.convo_bytes = convo
            seen[key] = request
            out.requests.append(request)
            thinking = 0
            turn_started = None
            streamed = [pair for pair in streamed if at is None or pair[0] > at]
        request.events += 1
        request.tool = request.tool or _tool_name(event)
        usage = message.get("usage")
        if isinstance(usage, dict):
            carried = sum(
                value for value in (
                    usage.get("input_tokens"), usage.get("cache_read_input_tokens"),
                    usage.get("cache_creation_input_tokens"),
                ) if isinstance(value, int) and value > 0
            )
            if carried and request.context is None:
                request.context = carried
                # Which event of the turn carried the counts: the first, or a
                # later one. A provider that only ever answers on a later block
                # is the reason a naive reader records nothing.
                request.usage_on_event = request.events
    for request in out.requests:
        if request.wait is not None:
            out.models.setdefault(request.model or "unnamed", []).append(request.wait)
    return out


def _correlation(pairs: list) -> tuple[float, float] | None:
    """Least-squares slope and Pearson r for (x, y), or None if it says nothing."""
    pairs = [(float(x), float(y)) for x, y in pairs]
    n = len(pairs)
    if n < 4:
        return None
    mx = sum(x for x, _ in pairs) / n
    my = sum(y for _, y in pairs) / n
    sxx = sum((x - mx) ** 2 for x, _ in pairs)
    syy = sum((y - my) ** 2 for _, y in pairs)
    sxy = sum((x - mx) * (y - my) for x, y in pairs)
    if sxx == 0 or syy == 0:
        return None
    return sxy / sxx, sxy / math.sqrt(sxx * syy)


def _median(values: list) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if not ordered:
        return 0.0
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _tokens(count: object) -> str:
    """A token count out of the file, or "—" where there is not one."""
    if not isinstance(count, (int, float)) or isinstance(count, bool):
        return "—"
    if count < 1000:
        return str(count)
    if count < 999_500:
        return f"{count / 1000:.1f}k".replace(".0k", "k")
    return f"{count / 1_000_000:.2f}M"


def _number(value: object, default: str = "—") -> str:
    """A count the file chose, as text. Anything that is not one reads as "—".

    Everything in here comes out of a file this module invites you to bring from
    elsewhere — a captured run, or a `claude -p … > run.jsonl` produced by hand
    — so a field that should be a number and isn't is a thing to report, not a
    traceback to print instead of the report.
    """
    return str(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        else default


def _dollars(value: object) -> str:
    """What the run said it cost, to four decimal places, or "—"."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"${value:.4f}"
    return "—"


def _clock(seconds: float) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def _working_or_queued(out: Analysis) -> list:
    """Whether the waiting was the model working or the model not starting.

    A provider that streams its reasoning reports it as it goes, so a wait full
    of thinking tokens is generation and a silent one is the only kind that can
    be a queue. Separating them is what turns "this model is slow" into
    something to act on: reasoning is answered by asking for less of it or
    asking fewer times, and queueing is answered by whoever runs the gateway.
    """
    timed = [r for r in out.requests if r.wait]
    if not timed or not any(r.thinking_in_wait for r in timed):
        return []
    lines = []
    silent = [r for r in timed if r.silent]
    streamed = sum(r.thinking_in_wait for r in timed)
    waited = sum(r.wait for r in timed)
    lines.append(
        f"while waiting    the model streamed {_tokens(streamed)} thinking tokens over "
        f"{waited:.0f}s ({streamed / waited:.0f}/s)"
    )
    lines.append(
        f"                 it was working through {len(timed) - len(silent)} of "
        f"{len(timed)} waits"
        + ("; none were silent" if not silent else
           f"; {len(silent)} " + ("was" if len(silent) == 1 else "were") + " silent")
    )
    spikes = sorted(timed, key=lambda r: -r.wait)
    spikes = [r for r in spikes if r.wait >= max(10.0, 3 * _median([r.wait for r in timed]))]
    if spikes:
        spike_wait = sum(r.wait for r in spikes)
        spike_tokens = sum(r.thinking_in_wait for r in spikes)
        quiet = [r for r in spikes if r.silent]
        lines.append(
            f"the long ones    {len(spikes)} waits of {spike_wait:.0f}s in all"
            + (f", and the model was silent in {len(quiet)} of them — those are the "
               "only ones that can be a queue"
               if quiet else
               f", and the model was streaming reasoning in every one "
               f"({spike_tokens / spike_wait:.0f} tokens/s). That is the model "
               "thinking, not a queue")
        )
    return lines


def report(out: Analysis, rows: int = 40) -> str:
    """The analysis as text, ending in what it does and does not establish."""
    lines = [
        f"{out.events} events over {_clock(out.span_s)}"
        + (f" ({out.unreadable} unreadable)" if out.unreadable else ""),
    ]
    if out.session_model:
        lines.append(f"session          {out.session_model}"
                     + (f" · claude code {out.cli_version}" if out.cli_version else "")
                     + (f" · {out.permission_mode}" if out.permission_mode else ""))
    reported = out.with_usage
    total = len(out.requests)
    lines.append(f"requests         {total}, of which {reported} reported token counts")
    late = [r.usage_on_event for r in out.requests if (r.usage_on_event or 0) > 1]
    if late:
        lines.append(
            f"                 {len(late)} of those reported them on a later block of "
            "the turn, not the first"
        )
    if total and reported == 0:
        lines.append("                 ⚠ no request reported any: the provider is not "
                     "returning usage")
    elif reported < total:
        lines.append(f"                 ⚠ {total - reported} requests came back with none")

    if len(out.models) > 1:
        lines.append("served by        more than one model — this run was not answered "
                     "by one backend:")
        for name, waits in sorted(out.models.items()):
            lines.append(f"                 {name}: {len(waits)} requests, "
                         f"median {_median(waits):.1f}s, max {max(waits):.1f}s")
    elif out.models:
        served = next(iter(out.models))
        lines.append(f"served by        {served} — every request")

    lines.append("")
    lines.append("  #  at      wait   context  think  first block  tool")
    # Head and tail, with the middle elided. Spelled out rather than sliced by
    # `rows // 2` twice: at `--rows 1` that is `[-0:]`, which is the whole list
    # — the one row asked for, printed as every row there is.
    shown = out.requests
    if 0 < rows < len(out.requests):
        head = max(1, rows // 2)
        shown = out.requests[:head] + out.requests[-max(1, rows - head):]

    previous = None
    for request in shown:
        if previous is not None and request.index != previous + 1:
            lines.append(f"     … {request.index - previous - 1} more")
        previous = request.index
        wait = f"{request.wait:.1f}s" if request.wait is not None else "—"
        lines.append(
            f"  {request.index:<3} {_clock(request.at):<7} {wait:>6} "
            f"{_tokens(request.context):>8} {_tokens(request.thinking):>6}  "
            f"{request.first_block:<12} {request.tool}"
        )

    waits = [r.wait for r in out.requests if r.wait is not None]
    lines.append("")
    if out.waiting_s and out.span_s:
        share = min(1.0, out.waiting_s / out.span_s)
        lines.append(
            f"the wall clock   {out.waiting_s:.0f}s of {out.span_s:.0f}s waiting on the "
            f"model ({share:.0%}); the rest was this run's own tools"
        )
    if waits:
        lines.append(
            f"waits            min {min(waits):.1f}s · median {_median(waits):.1f}s · "
            f"max {max(waits):.1f}s"
        )
        trend = _correlation([(r.index, r.wait) for r in out.requests if r.wait is not None])
        if trend:
            slope, r = trend
            direction = "growing" if slope > 0 else "falling"
            strength = "steadily" if abs(r) >= 0.6 else "noisily"
            lines.append(
                f"                 {direction} {strength}: {slope:+.2f}s per request "
                f"(r={r:+.2f})"
            )
        sized = [(r.context, r.wait) for r in out.requests
                 if r.wait is not None and r.context]
        against = _correlation(sized)
        if against:
            _, r = against
            if abs(r) >= 0.6:
                lines.append(
                    f"                 and they track the prompt size (r={r:+.2f}) — "
                    "the context is the cause"
                )
            else:
                lines.append(
                    f"                 but they do NOT track the prompt size (r={r:+.2f})"
                    " — the prompt is not what is driving them"
                )
        elif not sized and any(r.convo_bytes for r in out.requests):
            # Nothing reported a prompt size, so use the one thing the stream
            # always carries: how much conversation had accumulated by then.
            # Bytes, not tokens, and said so — but the shape is the shape.
            estimated = _correlation([
                (r.convo_bytes, r.wait) for r in out.requests if r.wait
            ])
            if estimated:
                _, r = estimated
                verdict = ("and they track it — the conversation is the cause"
                           if abs(r) >= 0.6 else
                           "and they do NOT track it — the prompt is not what "
                           "is driving them")
                lines.append(
                    f"                 nothing reported a prompt size, so measured "
                    f"against the conversation's own length (r={r:+.2f}) {verdict}"
                )
        elif sized and len({context for context, _ in sized}) == 1:
            # No variance to correlate against, which is not "no answer": a
            # prompt that never changed size cannot be what made the waits
            # change, and that is the clearest reading of all.
            lines.append(
                f"                 while the prompt never changed size "
                f"({_tokens(sized[0][0])} every request) — it cannot be the cause"
            )
        elif not sized:
            lines.append("                 nothing to compare them against: no request "
                         "reported its size")
    lines += _working_or_queued(out)
    quiet = [entry for entry in out.gaps if entry[2] == "the model"]
    if quiet:
        worst = sorted(quiet, key=lambda entry: -entry[1])[:3]
        lines.append("model went quiet " + ", ".join(
            f"{value:.0f}s at {_clock(at)}" for at, value, _ in worst
        ))
    tools = [entry for entry in out.gaps if entry[2] == "a tool"]
    if tools:
        worst = max(tools, key=lambda entry: entry[1])
        lines.append(
            f"longest tool     {worst[1]:.0f}s at {_clock(worst[0])} — the run's own "
            "work, not the model's"
        )
    if out.denials:
        lines.append("denied           " + ", ".join(
            f"{name} ×{count}" for name, count in sorted(out.denials.items())
        ))
    for result in out.results:
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        lines.append(
            "result           "
            f"{result.get('terminal_reason') or result.get('subtype') or 'ended'}"
            f" · {_number(result.get('num_turns'))} turns"
            f" · {_tokens(usage.get('output_tokens'))} out"
            f" · {_dollars(result.get('total_cost_usd'))}"
            + (" · IS_ERROR" if result.get("is_error") else "")
        )
    if not out.results:
        lines.append("result           none — this run never reported one")
    return "\n".join(lines)


def diagnose(path, rows: int = 40) -> str:
    return report(analyse(load(str(path))), rows=rows)


def newest(files: list):
    """The most recently written capture — the run someone just watched fail."""
    return max(files, key=lambda path: path.stat().st_mtime)


def _trend(out: Analysis) -> str:
    """"+3.1s/req" when the waits are climbing steadily, "flat" when they are
    not, and nothing at all when there is too little to say."""
    waits = [(r.index, r.wait) for r in out.requests if r.wait is not None]
    measured = _correlation(waits)
    if measured is None:
        return "—"
    slope, r = measured
    if abs(r) < 0.5 or abs(slope) < 0.05:
        return "flat"
    return f"{slope:+.1f}s/req"


def compare(files: list) -> str:
    """Several runs side by side.

    The shape of the question when a pipeline's steps behave differently from
    each other: three reviews of one merge request, launched together, and only
    one of them slow. A column each for the things that tell those apart —
    whether the provider reported anything, how the waits moved, and which
    model actually answered.
    """
    rows = []
    for path in files:
        out = analyse(load(str(path)))
        waits = [r.wait for r in out.requests if r.wait is not None]
        served = sorted(out.models)
        rows.append((
            path.name,
            str(len(out.requests)),
            f"{out.with_usage}/{len(out.requests)}" if out.requests else "—",
            f"{_median(waits):.1f}s" if waits else "—",
            f"{max(waits):.1f}s" if waits else "—",
            _trend(out),
            ", ".join(served) if served else "—",
        ))
    header = ("run", "reqs", "usage", "median", "max", "trend", "served by")
    widths = [
        max(len(header[i]), max((len(row[i]) for row in rows), default=0))
        for i in range(len(header))
    ]
    lines = [
        f"{len(files)} captured runs in {files[0].parent}",
        "",
        "  ".join(name.ljust(widths[i]) for i, name in enumerate(header)),
        "  ".join("-" * widths[i] for i in range(len(header))),
    ]
    lines += ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in rows]
    lines.append("")
    lines.append("`usage` is how many requests came back with token counts; a run at "
                 "0 is not reading nothing,")
    lines.append("it is a provider that does not say. `trend` is how the wait per "
                 "request moved over the run.")
    lines.append("Read one in full with: radar diagnose-stream <file>")
    return "\n".join(lines)
