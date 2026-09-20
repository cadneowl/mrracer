"""Reading a captured run back: what the events establish, and what they rule out.

The question this answers is the one the panel's charts raise and cannot settle
— waits are climbing, so is it the prompt growing, the provider, or the agent?
— and the answer has to be definitive enough to act on, because the next step
is either editing a skill or filing a ticket against a gateway.
"""

from __future__ import annotations

import json
import os

from radar.streamdiag import analyse, diagnose, load, report


def _capture(tmp_path, events, gap=2.0, name="capture.jsonl"):
    """A capture file: events, each stamped with when radar saw it."""
    path = tmp_path / name
    path.write_text("\n".join(
        json.dumps({"t": round((i + 1) * gap, 3), "event": event})
        for i, event in enumerate(events)
    ) + "\n", encoding="utf-8")
    return path


def _init(model="claude-opus-5"):
    return {"type": "system", "subtype": "init", "model": model,
            "claude_code_version": "2.1.278", "permissionMode": "dontAsk"}


def _assistant(message_id, *blocks, usage=None, model="claude-opus-5"):
    return {"type": "assistant", "message": {
        "id": message_id, "model": model, "content": list(blocks), "usage": usage}}


def _usage(fresh=10, cached=0, written=0):
    return {"input_tokens": fresh, "cache_read_input_tokens": cached,
            "cache_creation_input_tokens": written}


_THINK = {"type": "thinking", "thinking": "…"}
_TOOL = {"type": "tool_use", "name": "Bash", "input": {"command": "rg x"}}
_USER = {"type": "user", "message": {"role": "user", "content": []}}


def test_a_turn_is_one_request_however_many_events_it_arrives_in(tmp_path):
    path = _capture(tmp_path, [
        _init(), _assistant("m1", _THINK), _assistant("m1", _TOOL, usage=_usage(5, 900)),
        _USER, _assistant("m2", _TOOL, usage=_usage(5, 1200)),
    ])

    out = analyse(load(path))

    assert len(out.requests) == 2
    assert out.requests[0].events == 2 and out.requests[0].context == 905
    assert out.requests[0].first_block == "thinking"


def test_counts_arriving_on_a_later_block_are_found_and_named(tmp_path):
    """The shape that made a whole run look like it reported nothing: read the
    first event only and this request has no counts at all."""
    path = _capture(tmp_path, [
        _init(), _assistant("m1", _THINK), _assistant("m1", _TOOL, usage=_usage(40, 0, 31_000)),
    ])

    out = analyse(load(path))
    written = report(out)

    assert out.requests[0].context == 31_040
    assert out.requests[0].usage_on_event == 2
    assert "on a later block of the turn, not the first" in written


def test_a_provider_that_reports_nothing_is_said_so_outright(tmp_path):
    path = _capture(tmp_path, [
        _init(), _assistant("m1", _TOOL), _USER, _assistant("m2", _TOOL, usage={}),
    ])

    written = report(analyse(load(path)))

    assert "0 reported token counts" in written
    assert "the provider is not returning usage" in written


def test_two_backends_behind_one_model_name_are_pulled_apart(tmp_path):
    """A gateway may route concurrent sessions to different upstreams. The
    served model is on every assistant event, and two of them in one run —
    with different waits — is the whole diagnosis."""
    path = _capture(tmp_path, [
        _init("gw/deepseek-flash"),
        _assistant("m1", _TOOL, usage=_usage(), model="deepseek@a"), _USER,
        _assistant("m2", _TOOL, usage=_usage(), model="deepseek@b"), _USER,
        _assistant("m3", _TOOL, usage=_usage(), model="deepseek@b"),
    ])

    written = report(analyse(load(path)))

    assert "more than one model" in written
    assert "deepseek@a: 1 requests" in written and "deepseek@b: 2 requests" in written


def test_waits_that_track_the_prompt_are_blamed_on_the_prompt(tmp_path):
    """Context climbing in step with the waits is the skill accumulating
    context, and the fix is the skill."""
    events = [_init()]
    for n in range(1, 9):
        events += [_assistant(f"m{n}", _TOOL, usage=_usage(10, n * 20_000)), _USER]
    # Each turn waits longer, and carries proportionally more context.
    path = tmp_path / "grow.jsonl"
    at, lines = 0.0, []
    for event in events:
        at += 3.0 if event["type"] == "assistant" else 0.2
        if event["type"] == "assistant":
            at += event["message"]["usage"]["cache_read_input_tokens"] / 20_000
        lines.append(json.dumps({"t": round(at, 3), "event": event}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    written = report(analyse(load(path)))

    assert "they track the prompt size" in written
    assert "the context is the cause" in written


def test_waits_that_ignore_the_prompt_are_blamed_on_the_provider(tmp_path):
    """Climbing waits with a flat prompt is not something a skill can fix."""
    events = [_init()]
    for n in range(1, 9):
        events += [_assistant(f"m{n}", _TOOL, usage=_usage(10, 30_000)), _USER]
    at, lines = 0.0, []
    for event in events:
        at += (3.0 + len(lines) * 1.5) if event["type"] == "assistant" else 0.2
        lines.append(json.dumps({"t": round(at, 3), "event": event}))
    path = tmp_path / "flat.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    written = report(analyse(load(path)))

    assert "the prompt never changed size" in written
    assert "it cannot be the cause" in written


def test_long_silences_are_pointed_at(tmp_path):
    path = tmp_path / "stall.jsonl"
    path.write_text("\n".join([
        json.dumps({"t": 1.0, "event": _init()}),
        json.dumps({"t": 2.0, "event": _assistant("m1", _TOOL, usage=_usage())}),
        json.dumps({"t": 2.5, "event": _USER}),
        json.dumps({"t": 95.0, "event": _assistant("m2", _TOOL, usage=_usage())}),
    ]) + "\n", encoding="utf-8")

    written = report(analyse(load(path)))

    assert "model went quiet" in written and "92s" in written


def test_a_run_that_never_reported_a_result_says_so(tmp_path):
    """A killed or hung run is exactly the one worth reading back."""
    path = _capture(tmp_path, [_init(), _assistant("m1", _TOOL, usage=_usage())])

    assert "none — this run never reported one" in report(analyse(load(path)))


def test_a_plain_stream_json_dump_reads_too(tmp_path):
    """A skill someone ran by hand, redirected to a file: no arrival times, so
    no waits — and everything else still reads."""
    path = tmp_path / "raw.jsonl"
    path.write_text("\n".join(json.dumps(event) for event in [
        _init(), _assistant("m1", _TOOL, usage=_usage(10, 500)),
        {"type": "result", "subtype": "success", "num_turns": 1,
         "usage": {"output_tokens": 40}, "total_cost_usd": 0.01},
    ]) + "\n", encoding="utf-8")

    written = diagnose(str(path))

    assert "requests         1, of which 1 reported token counts" in written
    assert "completed" in written or "success" in written


def test_a_truncated_file_is_read_as_far_as_it_goes(tmp_path):
    """A capture from a run that was killed mid-line is the normal case."""
    path = tmp_path / "cut.jsonl"
    path.write_text(
        json.dumps({"t": 1.0, "event": _init()}) + "\n"
        + json.dumps({"t": 2.0, "event": _assistant("m1", _TOOL, usage=_usage())}) + "\n"
        + '{"t": 3.0, "event": {"type": "assis',
        encoding="utf-8",
    )

    out = analyse(load(path))

    assert out.unreadable == 1 and len(out.requests) == 1


def test_a_long_run_is_tabled_without_printing_every_line(tmp_path):
    events = [_init()]
    for n in range(1, 61):
        events += [_assistant(f"m{n}", _TOOL, usage=_usage()), _USER]

    written = report(analyse(load(_capture(tmp_path, events))), rows=10)

    assert "… 50 more" in written
    assert "requests         60" in written


def test_asking_for_one_row_does_not_print_every_row(tmp_path):
    """`rows // 2` twice over is `[-0:]` at --rows 1, which is the whole list —
    the one row asked for, printed as every row there is."""
    events = [_init()]
    for n in range(1, 21):
        events += [_assistant(f"m{n}", _TOOL, usage=_usage()), _USER]
    out = analyse(load(_capture(tmp_path, events)))

    assert "… " in report(out, rows=1), "the middle should still be elided"
    assert report(out, rows=1).count("\n") < report(out, rows=0).count("\n")
    # 0 is the way to ask for all of them, and says so in --help.
    assert "… " not in report(out, rows=0)


# --- reaching it from the command line -------------------------------------


def test_serve_capture_flag_turns_capturing_on(monkeypatch):
    """One flag on the command an operator already runs, rather than an
    environment variable they have to remember the name of."""
    from radar.__main__ import build_parser
    from radar.commands import DEFAULT_CAPTURE_DIR

    bare = build_parser().parse_args(["serve"])
    default = build_parser().parse_args(["serve", "--capture"])
    named = build_parser().parse_args(["serve", "--capture", "/tmp/elsewhere"])

    assert bare.capture is None
    assert default.capture == DEFAULT_CAPTURE_DIR
    assert named.capture == "/tmp/elsewhere"


def test_an_exported_variable_still_wins_over_the_flag(monkeypatch):
    """Whoever exported it said what they wanted; the flag is the convenient
    way to say the same thing, not a way to overrule them."""
    from radar.__main__ import _enable_capture
    from radar.commands import _CAPTURE_ENV

    monkeypatch.setenv(_CAPTURE_ENV, "/theirs")
    assert _enable_capture("mine") == "/theirs"

    monkeypatch.delenv(_CAPTURE_ENV)
    assert _enable_capture("mine") == "mine"
    assert os.environ[_CAPTURE_ENV] == "mine"

    monkeypatch.delenv(_CAPTURE_ENV)
    assert _enable_capture(None) == ""      # off unless asked for


def test_diagnose_reads_the_newest_run_when_asked_for_nothing(tmp_path, capsys):
    """The run someone wants to look at is almost always the one that just
    went wrong."""
    from radar.__main__ import main

    old = _capture(tmp_path, [_init(), _assistant("m1", _TOOL, usage=_usage(1, 100))],
                   name="review-aaa.jsonl")
    new = _capture(tmp_path, [_init("newer-model"),
                              _assistant("m1", _TOOL, usage=_usage(1, 200))],
                   name="qa-bbb.jsonl")
    os.utime(old, (1, 1))
    os.utime(new, (2_000_000_000, 2_000_000_000))

    assert main(["diagnose-stream", str(tmp_path)]) == 0

    printed = capsys.readouterr().out
    assert "reading the newest" in printed and "newer-model" in printed


def test_diagnose_compares_a_whole_directory(tmp_path, capsys):
    """A pipeline's steps, side by side: the shape of the question when three
    reviews are launched together and only one of them is slow."""
    from radar.__main__ import main

    _capture(tmp_path, [_init(), _assistant("m1", _TOOL, usage=_usage(1, 100))],
             name="review-aaa.jsonl")
    _capture(tmp_path, [_init(), _assistant("m1", _TOOL), _USER,
                        _assistant("m2", _TOOL)], name="qa-bbb.jsonl")

    assert main(["diagnose-stream", str(tmp_path), "--all"]) == 0

    printed = capsys.readouterr().out
    assert "2 captured runs" in printed
    assert "review-aaa.jsonl" in printed and "qa-bbb.jsonl" in printed
    # The column that separates "read nothing" from "was not told".
    assert "1/1" in printed and "0/2" in printed


def test_an_empty_capture_directory_says_what_to_do(tmp_path, capsys):
    from radar.__main__ import main

    assert main(["diagnose-stream", str(tmp_path)]) == 1
    assert "serve --capture" in capsys.readouterr().err


def test_a_missing_file_is_reported_rather_than_raised(tmp_path, capsys):
    from radar.__main__ import main

    assert main(["diagnose-stream", str(tmp_path / "nope.jsonl")]) == 1
    assert "cannot read" in capsys.readouterr().err


# --- the model working, or the model not starting --------------------------


def _thinking(tokens=200):
    return {"type": "system", "subtype": "thinking_tokens",
            "estimated_tokens": tokens, "estimated_tokens_delta": tokens}


def test_a_wait_full_of_reasoning_is_the_model_working(tmp_path):
    """The discriminator that settles a slow run: a provider that streams its
    reasoning says so continuously, so a wait full of thinking is generation
    and only a silent one can be a queue."""
    path = tmp_path / "thinking.jsonl"
    lines, at = [], 0.0
    lines.append(json.dumps({"t": at, "event": _init("deepseek")}))
    for n in range(1, 8):
        # One turn reasons for far longer than the rest — the spike a slow run
        # is asked about.
        for _ in range(60 if n == 4 else 4):
            at += 0.5
            lines.append(json.dumps({"t": round(at, 2), "event": _thinking(300)}))
        at += 0.3
        lines.append(json.dumps({"t": round(at, 2), "event": _assistant(
            f"m{n}", _THINK, usage=_usage(0, 0, 0))}))
        at += 0.5
        lines.append(json.dumps({"t": round(at, 2), "event": _USER}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    written = report(analyse(load(path)))

    assert "the model streamed" in written and "thinking tokens over" in written
    assert "none were silent" in written
    assert "That is the model thinking, not a queue" in written


def test_a_silent_wait_is_left_open_as_a_queue(tmp_path):
    """Nothing streamed while the request was outstanding: this is the only
    shape that a gateway could be responsible for, and the report says so
    rather than clearing the provider."""
    path = tmp_path / "quiet.jsonl"
    lines = [json.dumps({"t": 0.0, "event": _init("deepseek")})]
    at = 0.0
    for n in range(1, 8):
        if n != 3:
            # Every turn but one streams its reasoning while it works.
            for _ in range(4):
                at += 0.4
                lines.append(json.dumps({"t": round(at, 2), "event": _thinking(120)}))
            at += 0.4
        else:
            at += 40.0        # nothing at all, for forty seconds
        lines.append(json.dumps({"t": round(at, 2), "event": _assistant(f"m{n}", _THINK)}))
        at += 0.4
        lines.append(json.dumps({"t": round(at, 2), "event": _USER}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    written = report(analyse(load(path)))

    assert "was silent" in written
    assert "the only ones that can be a queue" in written


def test_time_in_tools_is_not_counted_against_the_model(tmp_path):
    """Most of a long review can be its own greps. A report that blames the
    model for them sends the reader to the wrong place."""
    path = tmp_path / "tools.jsonl"
    lines = [
        json.dumps({"t": 0.0, "event": _init()}),
        json.dumps({"t": 2.0, "event": _assistant("m1", _TOOL, usage=_usage())}),
        # 60s of the run's own tool, then the result comes back
        json.dumps({"t": 62.0, "event": _USER}),
        json.dumps({"t": 64.0, "event": _assistant("m2", _TOOL, usage=_usage())}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    written = report(analyse(load(path)))

    assert "waiting on the model" in written and "this run's own tools" in written
    assert "longest tool" in written and "not the model's" in written
    assert "model went quiet" not in written
