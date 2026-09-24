"""What a run spent: tokens in, cached, out and thinking, and how long the model
took to answer — counted from the events a run already emits.

The event shapes here are copied from a real ``claude -p --output-format
stream-json --verbose`` run, including the awkward part: a message's usage
arrives with its first content block, so its output count is a fragment of the
real one (16, 3, 2 against a billed 479) while its input side is already final.
That is why radar adds up only the input side as it goes and takes the totals
from the result event.
"""

from __future__ import annotations

import json
import re
import sys
import time

from radar.commands import (
    CommandJob,
    CommandRunner,
    RunStats,
    aggregate_stats,
    stats_from_json,
    stats_to_json,
)
from radar.config import SkillConfig
from radar.db import Database
from radar.web.app import _stats_view, _tokens


def _runner():
    return CommandRunner(SkillConfig(name="review", label="AI review", command="x"), "review")


def _job():
    return CommandJob(id="j", kind="review", started_mono=time.monotonic())


def _feed(runner, job, *events, stats=None):
    """Hand the events to the ingest the drain thread uses, in order.

    ``stats`` is the per-run scratch ``_execute`` keeps across the whole stream;
    pass one in to feed a stream in instalments.
    """
    stats = {} if stats is None else stats
    for event in events:
        line = event if isinstance(event, str) else json.dumps(event)
        runner._ingest(job, line, [], [], stats)
    return stats


def _assistant(message_id, *blocks, usage=None, parent=None):
    return {
        "type": "assistant",
        "parent_tool_use_id": parent,
        "message": {
            "id": message_id,
            "model": "claude-opus-5",
            "content": list(blocks),
            "usage": usage,
        },
    }


def _usage(fresh, cache_read, cache_write, output):
    return {
        "input_tokens": fresh,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write,
        "output_tokens": output,
        "service_tier": "standard",
    }


_TOOL = {"type": "tool_use", "name": "Bash", "input": {"command": "echo one"}}
_THINK = {"type": "thinking", "thinking": "…"}
_TEXT = {"type": "text", "text": "the answer"}

# The three requests of the recorded run, and what each carried.
_TURNS = (
    _assistant("msg_A", _TOOL, usage=_usage(2, 10653, 7498, 16)),
    _assistant("msg_B", _THINK, usage=_usage(2, 18151, 2399, 3)),
    _assistant("msg_B", _TOOL, usage=_usage(2, 18151, 2399, 3)),
    _assistant("msg_C", _THINK, usage=_usage(2, 20550, 154, 2)),
    _assistant("msg_C", _TEXT, usage=_usage(2, 20550, 154, 2)),
)

# What that run reported it was billed for when it finished.
_RESULT = {
    "type": "result",
    "subtype": "success",
    "result": "the answer",
    "num_turns": 3,
    "duration_ms": 8024,
    "duration_api_ms": 6895,
    "usage": {
        "input_tokens": 6,
        "cache_read_input_tokens": 49354,
        "cache_creation_input_tokens": 10051,
        "output_tokens": 479,
        "output_tokens_details": {"thinking_tokens": 134},
    },
}


def _thinking(estimated, delta):
    return {
        "type": "system",
        "subtype": "thinking_tokens",
        "estimated_tokens": estimated,
        "estimated_tokens_delta": delta,
    }


# --- counting what a run spends --------------------------------------------


def test_input_is_counted_once_per_request_not_once_per_event():
    """A turn reaches radar as one event per content block — the thinking, then
    the tool call it decided on — and every one of them repeats that request's
    usage. Counted per event, the three requests here would bill five."""
    runner, job = _runner(), _job()
    _feed(runner, job, *_TURNS)

    assert job.stats.turns == 3
    # Exactly the totals the run itself reported for those three requests.
    assert job.stats.input_tokens == 6
    assert job.stats.cache_read_tokens == 49354
    assert job.stats.cache_write_tokens == 10051
    # And nothing claims to know the output yet: a message's usage is emitted
    # before the turn has finished writing, so its count is a fragment.
    assert job.stats.output_tokens == 0
    assert not job.stats.billed


def test_a_subagents_requests_are_counted_too():
    """A subagent's turns arrive on the same stream under the tool call that
    started it, and its tokens are just as real as the parent's."""
    runner, job = _runner(), _job()
    _feed(
        runner, job,
        _assistant("msg_A", _TOOL, usage=_usage(2, 100, 50, 9)),
        _assistant("msg_sub", _TEXT, usage=_usage(3, 200, 60, 9), parent="toolu_1"),
    )

    assert job.stats.turns == 2
    assert job.stats.input_tokens == 5
    assert job.stats.cache_read_tokens == 300


def test_the_result_replaces_the_running_count_with_the_billed_totals():
    """The result event is the only place the run says what it was billed for,
    thinking included, so it wins over everything radar counted on the way."""
    runner, job = _runner(), _job()
    _feed(runner, job, *_TURNS, _RESULT)
    stats = job.stats

    assert stats.billed and stats.thinking_billed
    assert (stats.input_tokens, stats.cache_read_tokens, stats.cache_write_tokens) == (
        6, 49354, 10051,
    )
    assert stats.output_tokens == 479
    assert stats.thinking_tokens == 134
    # The model's own time over its own turn count — 6.895s across 3 requests.
    assert stats.reported_turns == 3
    assert round(stats.avg_response_s, 2) == 2.3


def test_a_run_reporting_two_results_adds_the_second_on():
    """Radar keeps every result's answer, because a run that reports more than
    one is adding a segment each time; its spend adds up the same way."""
    runner, job = _runner(), _job()
    second = json.loads(json.dumps(_RESULT))
    second["usage"]["output_tokens"] = 20
    second["num_turns"] = 1
    _feed(runner, job, *_TURNS, _RESULT, second)

    assert job.stats.output_tokens == 479 + 20
    assert job.stats.reported_turns == 4


# --- thinking ---------------------------------------------------------------


def test_thinking_is_the_runs_live_estimate_until_it_is_billed():
    """`estimated_tokens` restarts with every turn, so the deltas are what add
    up — and they are an estimate, which is why the panel marks them as one."""
    runner, job = _runner(), _job()
    _feed(
        runner, job,
        _thinking(50, 50), _thinking(183, 133),   # first turn
        _thinking(50, 50), _thinking(203, 153),   # second turn, counting afresh
    )

    assert job.stats.thinking_tokens == 386
    assert not job.stats.thinking_billed
    assert _stats_view(job.stats)["pills"][-1]["value"] == "~386"


def test_the_billed_thinking_count_replaces_the_estimate():
    runner, job = _runner(), _job()
    _feed(runner, job, _thinking(50, 50), _thinking(183, 133), *_TURNS, _RESULT)

    assert job.stats.thinking_tokens == 134 and job.stats.thinking_billed
    # A late estimate can no longer add to a billed figure.
    _feed(runner, job, _thinking(10, 10))
    assert job.stats.thinking_tokens == 134


def test_a_result_with_no_thinking_breakdown_leaves_the_estimate_alone():
    """An older CLI reports usage without the breakdown. Turning the estimate
    into a billed zero would be worse than keeping it labelled as an estimate."""
    runner, job = _runner(), _job()
    result = json.loads(json.dumps(_RESULT))
    del result["usage"]["output_tokens_details"]
    _feed(runner, job, _thinking(50, 50), *_TURNS, result)

    assert job.stats.thinking_tokens == 50
    assert not job.stats.thinking_billed
    assert job.stats.billed  # the rest of the totals are still the run's own


def test_thinking_events_are_counted_instead_of_logged():
    """They arrive several times a turn and each said "thinking tokens" and
    nothing more — enough of them to push the tool calls off the panel."""
    runner, job = _runner(), _job()
    _feed(runner, job, _thinking(50, 50), _thinking(183, 133))

    assert [item["text"] for item in job.progress] == []


# --- how long the model takes ----------------------------------------------


def test_the_wait_for_each_answer_is_measured_while_the_run_works(monkeypatch):
    """Until the result lands there is no `duration_api_ms` to divide, so radar
    times the wait itself: tool results going back, to the next answer starting."""
    # Held rather than ticked off a list: the ingest reads the clock for its own
    # bookkeeping too, and a test that counts those reads breaks on the next line
    # of code that takes a timestamp.
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    runner, job = _runner(), _job()
    scratch = {}
    _feed(
        runner, job,
        {"type": "system", "subtype": "init", "session_id": "s", "model": "claude-opus-5"},
        stats=scratch,
    )
    now[0] = 104.0  # the first answer took 4s
    _feed(runner, job, _assistant("msg_A", _TOOL, usage=_usage(2, 1, 1, 4)), stats=scratch)
    now[0] = 105.0
    _feed(runner, job, {"type": "user", "message": {"role": "user", "content": []}},
          stats=scratch)
    now[0] = 111.0  # the second took 6s
    _feed(runner, job, _assistant("msg_B", _TEXT, usage=_usage(2, 1, 1, 4)), stats=scratch)

    assert job.stats.latency_turns == 2
    assert job.stats.avg_response_s == 5.0
    assert job.stats.model == "claude-opus-5"


def test_the_runs_own_timing_wins_once_it_reports_it():
    runner, job = _runner(), _job()
    _feed(runner, job, *_TURNS, _RESULT)

    # 6895ms over 3 turns, not whatever radar clocked around its own overhead.
    assert round(job.stats.avg_response_s, 3) == round(6.895 / 3, 3)


# --- runs that measure nothing ---------------------------------------------


def test_a_command_that_speaks_no_stream_json_gets_no_numbers():
    """Its stdout lines are the progress log and nothing more. A row of zeros
    would read as a measurement rather than as the absence of one."""
    runner, job = _runner(), _job()
    _feed(runner, job, "checking out the branch…", "done")

    assert not job.stats.measured
    assert _stats_view(job.stats) is None


def test_a_malformed_usage_block_is_survived():
    """Every shape in these events is the child's to choose; a reader on the
    drain thread must not raise over one."""
    runner, job = _runner(), _job()
    _feed(
        runner, job,
        _assistant("msg_A", _TOOL, usage="not a dict"),
        _assistant("msg_B", _TOOL, usage={"input_tokens": None, "cache_read_input_tokens": -5}),
        {"type": "result", "usage": {"output_tokens": "many"}, "num_turns": None},
        {"type": "assistant", "message": "a string"},
    )

    assert job.stats.turns == 2
    assert job.stats.input_tokens == 0 and job.stats.output_tokens == 0


# --- a pipeline's total ----------------------------------------------------


def test_a_pipeline_adds_its_steps_numbers_up():
    steps = [
        RunStats(
            model="claude-opus-5", turns=3, input_tokens=6, cache_read_tokens=49354,
            cache_write_tokens=10051, output_tokens=479, thinking_tokens=134,
            billed=True, thinking_billed=True, api_ms=6895, reported_turns=3,
        ),
        RunStats(
            model="claude-opus-5", turns=1, input_tokens=4, cache_read_tokens=1000,
            cache_write_tokens=200, output_tokens=100, thinking_tokens=20,
            billed=True, thinking_billed=True, api_ms=2000, reported_turns=1,
        ),
    ]

    total = aggregate_stats(steps)

    assert total.input_tokens == 10 and total.output_tokens == 579
    assert total.thinking_tokens == 154 and total.reported_turns == 4
    assert total.model == "claude-opus-5"  # one model, named once
    assert total.billed and total.thinking_billed
    # Model time, summed: 8.895s over the 4 requests the two steps made.
    assert round(total.avg_response_s, 3) == round(8.895 / 4, 3)


def test_a_half_finished_pipeline_does_not_call_its_total_billed():
    """One step still estimating its thinking makes the total an estimate too,
    and the panel says so rather than showing it as a billed figure."""
    total = aggregate_stats([
        RunStats(turns=1, input_tokens=6, output_tokens=479, thinking_tokens=134,
                 billed=True, thinking_billed=True),
        RunStats(turns=1, input_tokens=4, thinking_tokens=50),
    ])

    assert not total.billed and not total.thinking_billed
    assert total.thinking_tokens == 184


def test_a_pipeline_with_nothing_started_is_not_measured():
    assert not aggregate_stats([]).measured


# --- numbers that outlive the job -----------------------------------------


def test_stored_numbers_survive_the_round_trip():
    """Tallies included — the tools it called, what it was refused, where the
    rate limits stood — so a re-opened result is the whole measurement."""
    stats = RunStats(
        model="claude-opus-5", turns=3, input_tokens=6, cache_read_tokens=49354,
        output_tokens=479, thinking_tokens=134, billed=True, thinking_billed=True,
        api_ms=6895, reported_turns=3, latency_s=7.5, latency_turns=3,
        cost_usd=0.2092675, wall_ms=8477, first_token_ms=1948, peak_context_tokens=20706,
        context_window=1_000_000, tool_calls=3, tools={"Bash": 2, "Read": 1},
        denials={"WebFetch": 1}, subagent_types={"dba": 1},
        models={"claude-opus-5": {"output": 479, "cost_usd": 0.209}},
        rate_limits={"five_hour": 0.13}, rate_limit_status="allowed",
        outcome="completed", cli_version="2.1.278", permission_mode="dontAsk",
    )

    assert stats_from_json(stats_to_json(stats)) == stats


def test_nothing_stored_reads_as_nothing_measured():
    """A result saved before radar measured runs, and a row whose column is
    empty, both re-open as a panel with no numbers rather than an error."""
    for text in ("", None, "not json", "[1, 2]"):
        assert not stats_from_json(text).measured


def test_a_stored_field_of_the_wrong_shape_is_dropped():
    """Written by another version of radar: what does not match the field it
    claims to be is left out, and the rest still renders."""
    stats = stats_from_json(json.dumps({
        "model": "claude-opus-5", "input_tokens": "lots", "output_tokens": 479,
        "billed": "yes", "latency_s": 3, "no_such_field": 1,
        "tools": {"Bash": 2}, "denials": ["WebFetch"],
    }))

    assert stats.model == "claude-opus-5" and stats.output_tokens == 479
    assert stats.input_tokens == 0 and stats.billed is False
    assert stats.latency_s == 3  # a whole number of seconds is still seconds
    assert stats.tools == {"Bash": 2} and stats.denials == {}  # a list is not a tally


def test_a_stored_tally_whose_counts_are_not_counts_is_dropped():
    """The keys of these mappings are names the run chose; the values are
    numbers this version does arithmetic on.

    Checking only the keys let a value of the wrong type through the read, and
    it then raised in the panel — rounding a string for a tooltip, or adding one
    to a pipeline's total — where the traceback is about formatting rather than
    about a row this version cannot read.
    """
    from radar.web.app import _stats_view

    stats = stats_from_json(json.dumps({
        "turns": 3, "cost_usd": 1.0,
        "tools": {"Read": "many"},              # not a count
        "denials": {"Bash": True},              # a bool is not a count either
        "rate_limits": {"5h": "high"},
        "models": {"opus": {"input": "lots"}},  # nor a nested one
        "subagent_types": {"reviewer": 2},      # this one is fine
    }))

    assert stats.tools == {} and stats.denials == {}
    assert stats.rate_limits == {} and stats.models == {}
    assert stats.subagent_types == {"reviewer": 2}
    # Which is the point of dropping them: the panel still renders.
    assert _stats_view(stats, details=True) is not None
    assert aggregate_stats([stats, stats]).turns == 6


def test_a_stored_tally_that_is_a_tally_survives():
    """The check above must not be a way of quietly losing good rows."""
    stats = stats_from_json(json.dumps({
        "turns": 2, "tools": {"Read": 3}, "rate_limits": {"5h": 0.4},
        "models": {"opus": {"input": 5, "cost_usd": 1.5}},
    }))

    assert stats.tools == {"Read": 3} and stats.rate_limits == {"5h": 0.4}
    assert stats.models == {"opus": {"input": 5, "cost_usd": 1.5}}


def test_a_stored_timeline_of_the_wrong_shape_draws_nothing_rather_than_breaking():
    """The validation above stops at "a list of dicts"; the charts read inside
    those dicts. A row this version cannot make sense of has to leave the
    charts blank, not take the panel down with it."""
    from radar.web.app import _stats_charts

    for series in ("not-a-list", [1, 2, 3], [{"no": "samples"}], None):
        stats = stats_from_json(json.dumps({
            "turns": 3, "series": [{"label": "arch", "samples": series}],
        }))
        assert _stats_charts(stats) == [], f"{series!r} should draw nothing"
    # A series that is not even a record of one.
    assert _stats_charts(stats_from_json('{"turns": 3, "series": ["arch"]}')) == []

    # And a series that *is* readable still draws, so this is not just off.
    good = stats_from_json(json.dumps({
        "turns": 3, "series": [{"label": "arch", "samples": [{"t": 1, "wait": 2.5}]}],
    }))
    assert len(good.timelines()) == 1 and _stats_charts(good)


def test_a_saved_result_keeps_what_its_run_cost(tmp_path):
    """Re-opening an answer a week later answers "what did this take?" as well
    as "what did it say?"."""
    stats = RunStats(turns=3, input_tokens=6, output_tokens=479, billed=True)
    db_path = tmp_path / "stats.db"
    with Database(db_path) as db:
        db.save_test_plan(101, 1, "qa", "PROJ-1", "## plan", stats_to_json(stats))
        db.save_build_analysis("backend-ci", 128, "doctor", "## cause", stats_to_json(stats))

    with Database(db_path) as db:
        assert stats_from_json(db.get_test_plan(101, 1, "qa")["stats"]) == stats
        assert stats_from_json(
            db.get_build_analysis("backend-ci", 128, "doctor")["stats"]
        ) == stats


def test_a_result_stored_without_numbers_still_reads_back(tmp_path):
    db_path = tmp_path / "old.db"
    with Database(db_path) as db:
        db.save_test_plan(101, 1, "qa", "PROJ-1", "## plan")
        assert db.get_test_plan(101, 1, "qa")["stats"] == ""


# --- how the panel says it ------------------------------------------------


def test_token_counts_are_rounded_for_reading():
    """What an operator does with these is compare runs; a nine-digit total only
    pushes the rest of the row off the panel."""
    assert _tokens(842) == "842"
    assert _tokens(8470) == "8.5k"
    assert _tokens(10_000) == "10k"
    assert _tokens(1_234_567) == "1.23M"


def test_the_strip_names_every_figure_it_shows():
    """`in` is everything the model read, not the sliver of it that missed the
    cache: Claude Code caches nearly the whole prompt, so the uncached remainder
    is 6 tokens beside a 59k prompt and reads as if the run had no input."""
    stats = RunStats(
        model="claude-opus-5", input_tokens=6, cache_read_tokens=49354,
        cache_write_tokens=10051, output_tokens=479, thinking_tokens=134,
        billed=True, thinking_billed=True, api_ms=6895, reported_turns=3,
    )

    view = _stats_view(stats)
    shown = {pill["label"]: pill["value"] for pill in view["pills"]}

    assert shown == {
        "requests": "3", "in": "59.4k", "cached": "83%", "out": "479",
        "thinking": "134", "avg": "2.3s", "speed": "69 tok/s",
    }
    # And the split the bill is actually made of is one hover away.
    hover = next(pill["title"] for pill in view["pills"] if pill["label"] == "in")
    assert "6 fresh" in hover and "49.4k from the prompt cache" in hover
    assert view["model"] == "claude-opus-5"
    # Each one carries the words for what it is: the numbers are dense, and a
    # cache figure nobody can read is a figure nobody trusts.
    assert all(pill["title"] for pill in view["pills"])


def test_a_figure_with_nothing_to_report_is_left_out():
    """A run that cached nothing, thought about nothing or has not said what it
    wrote shows no pill for it — an empty one is a claim of its own."""
    view = _stats_view(RunStats(turns=1, input_tokens=12))

    assert [pill["label"] for pill in view["pills"]] == ["requests", "in"]


# --- the whole way through -------------------------------------------------


_SKILL = """
skills:
  - name: qa
    enabled: true
    stores_result: true
    command: echo hi
"""


def test_a_real_run_is_measured_through_the_pipe(tmp_path):
    """Not `_ingest` on its own: a child process, its stdout drained line by
    line, and the numbers on the job the panel reads."""
    script = tmp_path / "emit.py"
    script.write_text(
        "import json\n"
        + "".join(f"print(json.dumps({event!r}))\n" for event in (*_TURNS, _RESULT)),
        encoding="utf-8",
    )
    runner = CommandRunner(
        SkillConfig(name="qa", command=f'"{sys.executable}" "{script}"', timeout_seconds=30),
        "qa",
    )
    job = runner.start({"project_id": 1, "mr_iid": 2})
    deadline = time.monotonic() + 30
    while job.status == "running":
        assert time.monotonic() < deadline, "the run never finished"
        time.sleep(0.05)

    assert job.status == "done"
    assert (job.stats.input_tokens, job.stats.output_tokens) == (6, 479)
    assert job.stats.thinking_tokens == 134 and job.stats.thinking_billed


def test_a_run_that_never_reported_a_result_still_keeps_its_last_request(tmp_path):
    """A request's entry on the timeline is closed by the next one, or by the
    result event. A run that was killed or stopped gets neither — and the
    request it died on is the most interesting point on the chart.
    """
    script = tmp_path / "half.py"
    script.write_text(
        "import json, sys\n"
        + "".join(f"print(json.dumps({event!r}))\n" for event in _TURNS[:1])
        + "sys.exit(1)\n",   # no result event, as a killed run has none
        encoding="utf-8",
    )
    runner = CommandRunner(
        SkillConfig(name="qa", command=f'"{sys.executable}" "{script}"', timeout_seconds=30),
        "qa",
    )
    job = runner.start({"project_id": 1, "mr_iid": 2})
    deadline = time.monotonic() + 30
    while job.status == "running":
        assert time.monotonic() < deadline, "the run never finished"
        time.sleep(0.05)

    assert job.status == "error"
    assert [sample["n"] for sample in job.stats.samples] == [1]


def test_a_reopened_result_shows_what_its_run_spent(tmp_path):
    """The numbers reach the panel, not just the job: this is the page an
    operator actually reads them off."""
    from fastapi.testclient import TestClient

    from radar.config import load_config
    from radar.web.app import create_app
    from tests.conftest import _BASE_CONFIG

    path = tmp_path / "qa.yaml"
    path.write_text(_BASE_CONFIG + _SKILL, encoding="utf-8")
    db_path = tmp_path / "panel.db"
    stats = RunStats(
        model="claude-opus-5", input_tokens=6, cache_read_tokens=49354, output_tokens=479,
        thinking_tokens=134, billed=True, thinking_billed=True, api_ms=6895, reported_turns=3,
    )
    with Database(db_path) as db:
        db.save_test_plan(101, 1, "qa", "PROJ-1", "## plan", stats_to_json(stats))

    html = TestClient(create_app(load_config(path), str(db_path))).get("/qa/stored/101/1").text

    assert 'class="run-stats"' in html
    for shown in ("claude-opus-5", ">479<", "49.4k", ">134<", "2.3s"):
        assert shown in html, shown


def test_a_running_panel_leaves_the_numbers_to_the_health_rows():
    """The panel is rendered when the run opens and again when it ends, so its
    own strip would be a snapshot taken at nought; the rows refresh themselves."""
    from radar.web.app import templates

    job = CommandJob(id="x", kind="qa", subject="!1", title="t", status="running")
    html = templates.env.get_template("_command_panel.html").render({
        "job": job, "status": "running", "error": "", "kind": "qa", "heading": "QA",
        "icon": "*", "generated_at": None, "remaining_s": 60, "clock_text": "1:00 left",
        "output_html": None, "output": "", "stats": None,
        "rows": [{
            "name": "qa", "label": "QA", "step": None, "state": "running",
            "state_text": "running 20s", "note": "", "stalled": False, "session_id": "",
            "resume": "", "can_stop": True, "stats": _stats_view(
                RunStats(turns=2, input_tokens=4, cache_read_tokens=18151, thinking_tokens=183)
            ),
        }],
        "live": True, "stop_all": False, "total": None,
    })

    assert html.count('class="run-stats"') == 1   # the row's, not the panel's
    assert "18.2k" in html and "~183" in html     # thinking still an estimate


def test_a_pipelines_own_numbers_are_its_steps_added_up(tmp_path):
    """Two steps run as one skill, and the panel's total is the bill for both —
    re-added as they go, so it grows with the run rather than arriving at the end."""
    from radar.pipeline import build_runners

    script = tmp_path / "step.py"
    script.write_text(
        "import json\n"
        + "".join(f"print(json.dumps({event!r}))\n" for event in (_TURNS[0], _RESULT)),
        encoding="utf-8",
    )
    command = f'"{sys.executable}" "{script}"'
    runners = build_runners([
        SkillConfig(name="a", command=command, timeout_seconds=30),
        SkillConfig(name="b", command=command, timeout_seconds=30),
        SkillConfig(name="full", pipeline=(("a", "b"),), timeout_seconds=60),
    ])
    job = runners["full"].start({"title": "Add widget", "subject": "!7"})
    deadline = time.monotonic() + 30
    while job.status == "running":
        assert time.monotonic() < deadline, "the pipeline never finished"
        time.sleep(0.05)

    assert job.status == "done"
    # Each step reported the recorded run's totals; the pipeline reports both.
    assert job.stats.output_tokens == 479 * 2
    assert job.stats.thinking_tokens == 134 * 2
    assert job.stats.reported_turns == 6 and job.stats.billed


def test_a_half_finished_pipeline_counts_the_requests_still_being_made():
    """One step has published its own `num_turns` and the other is still
    working; a total that reported only the first would say the pipeline had
    made fewer requests than the panel above it already lists."""
    total = aggregate_stats([
        RunStats(turns=31, reported_turns=31, input_tokens=14, billed=True),
        RunStats(turns=12, input_tokens=9),
    ])

    assert total.turn_count == 43


# --- everything else the same events report --------------------------------
#
# The event below is a real `result`, trimmed of its prose: a run that spawned
# one subagent, was refused a tool, and reported what it was billed. Its
# awkwardness is the point — `usage` covers the main loop (8.7k cache writes)
# while `modelUsage` carries the whole bill (22.5k), so the two disagree by
# exactly what the subagent spent.

_FULL_RESULT = {
    "type": "result",
    "subtype": "success",
    "result": "## Findings",
    "is_error": False,
    "num_turns": 3,
    "queued_turn_count": 2,
    "duration_ms": 8477,
    "duration_api_ms": 7478,
    "ttft_ms": 1948,
    "total_cost_usd": 0.2092675,
    "stop_reason": "end_turn",
    "terminal_reason": "completed",
    "api_error_status": None,
    "permission_denials": [
        {"tool_name": "Bash", "tool_use_id": "toolu_1", "tool_input": {"command": "ls"}},
        {"tool_name": "Bash", "tool_use_id": "toolu_2", "tool_input": {"command": "pwd"}},
        {"tool_name": "WebFetch", "tool_use_id": "toolu_3"},
    ],
    "usage": {
        "input_tokens": 6,
        "cache_read_input_tokens": 51115,
        "cache_creation_input_tokens": 8747,
        "output_tokens": 408,
        "output_tokens_details": {"thinking_tokens": 130},
        "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 8747},
        "server_tool_use": {"web_search_requests": 2, "web_fetch_requests": 1},
        "service_tier": "standard",
        "speed": "standard",
        "inference_geo": "not_available",
    },
    "modelUsage": {
        "claude-opus-5[1m]": {
            "inputTokens": 8, "outputTokens": 411, "cacheReadInputTokens": 51115,
            "cacheCreationInputTokens": 22495, "thinkingTokens": 139,
            "costUSD": 0.2092675, "contextWindow": 1000000, "maxOutputTokens": 64000,
        },
    },
    "subagent_stats": {
        "spawned": 1, "completed": 1, "failed": 0, "max_depth": 1,
        "killed": {"parent": 0, "user": 1, "system": 0},
        "refused": {"depth_limit": 0, "concurrency_limit": 2, "budget": 1},
        "by_type": {"general-purpose": 1},
    },
}

_INIT = {
    "type": "system", "subtype": "init", "session_id": "30cd2693-f926",
    "model": "claude-opus-5[1m]", "claude_code_version": "2.1.278",
    "permissionMode": "dontAsk", "output_style": "default",
    "tools": ["Read", "Grep", "Bash"], "mcp_servers": [{"name": "atlassian"}],
}

_RATE_LIMIT = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed",
        "unifiedWindows": {
            "five_hour": {"utilization": 0.13, "resetsAt": 1789845600},
            "seven_day": {"utilization": 0.24, "resetsAt": 1790056800},
        },
    },
}


def test_the_whole_bill_is_the_per_model_one_not_the_main_loops():
    """A run that delegates reports its main loop's usage at the top level and
    the complete bill per model: 8.7k cache writes against the 22.5k it paid
    for. The panel must show what was paid."""
    runner, job = _runner(), _job()
    _feed(runner, job, _FULL_RESULT)

    assert job.stats.cache_write_tokens == 22495     # not the 8747 of `usage`
    assert job.stats.output_tokens == 411            # not 408
    assert job.stats.thinking_tokens == 139          # not 130
    assert job.stats.models["claude-opus-5[1m]"]["cost_usd"] == 0.2092675
    # The TTL split is only the main loop's, and says so rather than looking
    # like a rounding error.
    assert job.stats.cache_write_1h_tokens == 8747
    note = next(
        row["note"] for group in _stats_view(job.stats, details=True)["groups"]
        for row in group["rows"] if row["label"] == "cache written"
    )
    assert "8.7k at 1h" in note and "subagents" in note


def test_money_is_reported():
    runner, job = _runner(), _job()
    _feed(runner, job, _FULL_RESULT)

    assert job.stats.cost_usd == 0.2092675
    shown = {pill["label"]: pill["value"] for pill in _stats_view(job.stats)["pills"]}
    assert shown["cost"] == "$0.209"


def test_where_the_time_went():
    """Wall clock against model time answers the question a slow review raises:
    was it the model, or was it the forty greps the skill ran?"""
    runner, job = _runner(), _job()
    _feed(runner, job, _FULL_RESULT)
    stats = job.stats

    assert (stats.wall_ms, stats.api_ms, stats.first_token_ms) == (8477, 7478, 1948)
    assert round(stats.model_share, 2) == 0.88
    assert stats.queued_turns == 2


def test_time_to_first_token_and_output_speed():
    """How long the model takes to start, and how fast it writes once it has —
    the two numbers that tell a slow provider from a long answer."""
    runner, job = _runner(), _job()
    _feed(runner, job, _FULL_RESULT)
    stats = job.stats

    assert stats.ttft_s == 1.948
    # 411 billed output tokens over 7.478s of model time.
    assert round(stats.output_tokens_per_s) == 55
    shown = {pill["label"]: pill["value"] for pill in _stats_view(stats)["pills"]}
    assert shown["speed"] == "55 tok/s · 1.9s ttft"


def test_output_speed_waits_for_the_billed_output():
    """Live, radar counts no output at all, so a speed then would read as zero
    tokens a second from a model that is busy writing."""
    stats = RunStats(turns=2, input_tokens=900, api_ms=5000)
    assert stats.output_tokens_per_s is None
    assert stats.ttft_s is None
    assert "speed" not in {pill["label"] for pill in _stats_view(stats)["pills"]}


def test_a_pipeline_averages_its_steps_time_to_first_token():
    """The slowest step alone would make every pipeline look like its worst
    provider; the average is what a step can expect."""
    fast = RunStats(turns=1, output_tokens=100, api_ms=1000,
                    first_token_ms=1000, ttft_ms_total=1000, ttft_reports=1)
    slow = RunStats(turns=1, output_tokens=300, api_ms=5000,
                    first_token_ms=3000, ttft_ms_total=3000, ttft_reports=1)
    total = aggregate_stats([fast, slow])

    assert total.ttft_s == 2.0
    assert total.first_token_ms == 3000
    assert round(total.output_tokens_per_s) == round(400 / 6)


def test_a_result_stored_before_the_average_still_has_a_time_to_first_token():
    assert stats_from_json(json.dumps({"turns": 1, "first_token_ms": 2500})).ttft_s == 2.5


def test_denied_tool_calls_are_surfaced():
    """Radar runs skills with `--permission-mode dontAsk`: a tool off the
    allowlist is refused without asking and the run writes its answer anyway.
    What it was refused is the difference between a finding and a guess."""
    runner, job = _runner(), _job()
    _feed(runner, job, _FULL_RESULT)

    assert job.stats.denials == {"Bash": 2, "WebFetch": 1}
    assert job.stats.denied_calls == 3
    denied = next(p for p in _stats_view(job.stats)["pills"] if p["label"] == "denied")
    assert denied["value"] == "3" and "Bash ×2" in denied["title"]


def test_subagents_are_counted_including_the_ones_refused():
    runner, job = _runner(), _job()
    _feed(runner, job, _FULL_RESULT)
    stats = job.stats

    assert (stats.subagents_spawned, stats.subagents_completed) == (1, 1)
    assert stats.subagents_killed == 1          # summed across the kill reasons
    assert stats.subagents_refused == 3         # concurrency 2 + budget 1
    assert stats.subagent_types == {"general-purpose": 1} and stats.subagent_depth == 1


def test_subagents_are_counted_live_before_any_result():
    """A run stopped or timed out still says how much it had handed off."""
    runner, job = _runner(), _job()
    _feed(runner, job, {
        "type": "system", "subtype": "task_started", "task_id": "t1",
        "description": "Check the migrations", "subagent_type": "dba", "spawn_depth": 1,
    })

    assert job.stats.subagents_spawned == 1
    assert job.stats.subagent_types == {"dba": 1} and job.stats.subagent_depth == 1


def test_a_result_with_no_subagent_tally_leaves_the_live_count_alone():
    """The same rule as the thinking estimate, for the same reason.

    A CLI that reports usage without breaking subagents out is not a run that
    started none — and believing it would wipe the only count radar had, while
    the log above still says a subagent started.
    """
    runner, job = _runner(), _job()
    _feed(runner, job, {
        "type": "system", "subtype": "task_started", "task_id": "t1",
        "description": "Check the migrations", "subagent_type": "dba",
    })
    result = json.loads(json.dumps(_RESULT))
    result.pop("subagent_stats", None)
    _feed(runner, job, result)

    assert job.stats.subagents_spawned == 1
    assert job.stats.subagent_types == {"dba": 1}
    assert not job.stats.subagents_billed
    assert job.stats.billed   # the token totals are still the run's own


def test_a_reported_subagent_tally_replaces_the_live_count():
    """It is the better number: it knows about the ones refused before they
    ever started, and radar only ever sees the ones that did."""
    runner, job = _runner(), _job()
    _feed(runner, job, {
        "type": "system", "subtype": "task_started", "task_id": "t1",
        "description": "Check the migrations", "subagent_type": "dba",
    })
    result = json.loads(json.dumps(_RESULT))
    result["subagent_stats"] = {
        "spawned": 2, "completed": 1, "refused": {"concurrency": 1},
        "by_type": {"dba": 2},
    }
    _feed(runner, job, result)

    assert job.stats.subagents_spawned == 2 and job.stats.subagents_refused == 1
    assert job.stats.subagent_types == {"dba": 2}   # replaced, not added to
    assert job.stats.subagents_billed


def test_server_side_tools_are_counted():
    """Web search and fetch are billed per request, not in tokens."""
    runner, job = _runner(), _job()
    _feed(runner, job, _FULL_RESULT)

    assert (job.stats.web_searches, job.stats.web_fetches) == (2, 1)


def test_tool_calls_are_tallied_by_name():
    """Forty Reads and one Bash is a different review from the other way round."""
    runner, job = _runner(), _job()
    _feed(
        runner, job,
        _assistant("m1", _TOOL, {"type": "tool_use", "name": "Read",
                                 "input": {"file_path": "/a"}}),
        _assistant("m2", {"type": "tool_use", "name": "Read", "input": {"file_path": "/b"}}),
    )

    assert job.stats.tool_calls == 3
    assert job.stats.tools == {"Bash": 1, "Read": 2}


def test_the_biggest_request_is_measured_against_the_window():
    """No total says how close a run came to running out of room — a run can
    spend millions of tokens fifty thousand at a time."""
    runner, job = _runner(), _job()
    _feed(runner, job, *_TURNS, _FULL_RESULT)

    assert job.stats.peak_context_tokens == 2 + 20550 + 154   # the largest request
    assert job.stats.context_window == 1_000_000
    assert round(job.stats.context_share, 4) == 0.0207


def test_how_the_run_ended_is_kept():
    runner, job = _runner(), _job()
    _feed(runner, job, _FULL_RESULT)

    assert job.stats.outcome == "completed" and job.stats.stop_reason == "end_turn"
    assert job.stats.errors == 0


def test_an_api_error_is_named_as_the_outcome():
    runner, job = _runner(), _job()
    _feed(runner, job, {
        "type": "result", "is_error": True, "terminal_reason": "api_error",
        "stop_reason": "stop_sequence", "num_turns": 1, "usage": {},
    })

    assert job.stats.outcome == "api_error" and job.stats.errors == 1


def test_what_the_session_was_given_is_recorded():
    """The numbers mean different things under a different model, a different
    permission mode or a different tool surface."""
    runner, job = _runner(), _job()
    _feed(runner, job, _INIT)
    stats = job.stats

    assert stats.model == "claude-opus-5[1m]" and stats.cli_version == "2.1.278"
    assert stats.permission_mode == "dontAsk" and stats.output_style == "default"
    assert (stats.tools_offered, stats.mcp_servers) == (3, 1)


def test_rate_limits_are_kept_at_their_tightest():
    """What is left of the account's budget is what the next review gets."""
    runner, job = _runner(), _job()
    quieter = json.loads(json.dumps(_RATE_LIMIT))
    quieter["rate_limit_info"]["unifiedWindows"]["five_hour"]["utilization"] = 0.02
    _feed(runner, job, _RATE_LIMIT, quieter)

    assert job.stats.rate_limits == {"five_hour": 0.13, "seven_day": 0.24}
    assert job.stats.rate_limit_status == "allowed"
    assert not job.progress  # counted, not logged


def test_the_details_block_covers_every_group():
    runner, job = _runner(), _job()
    _feed(runner, job, _INIT, _RATE_LIMIT, *_TURNS, _FULL_RESULT)

    view = _stats_view(job.stats, details=True)
    titles = [group["title"] for group in view["groups"]]
    assert titles == ["Tokens", "Money", "Time", "What it did", "How it ended", "The run"]
    rows = {
        row["label"]: row["value"]
        for group in view["groups"] for row in group["rows"]
    }
    assert rows["read by the model"] == "73.6k" and rows["fresh input"] == "8"
    assert rows["cost"] == "$0.209"
    assert rows["total"] == "74k"
    assert rows["waiting on the model"] == "7.5s"
    assert rows["denied tool calls"] == "3"
    assert rows["rate limits"] == "five hour 13%, seven day 24%"
    assert rows["permission mode"] == "dontAsk"
    assert rows["subagents"] == "1 spawned, 1 finished, 1 killed, 3 refused"


def test_a_model_that_reported_no_price_says_so_rather_than_almost_nothing():
    """The gateway radar is pointed at reports tokens and no cost.

    Rounding that up to "<$0.01" reads as "this cost next to nothing" where it
    means "nobody said" — and the whole point of these numbers is telling those
    two apart.
    """
    stats = RunStats(
        turns=3, models={"deepseek-v4p1-flash": {"input": 1200, "output": 400,
                                                 "thinking": 900, "cost_usd": 0.0}},
    )
    rows = {
        row["label"]: row
        for group in _stats_view(stats, details=True)["groups"]
        if group["title"] == "Money"
        for row in group["rows"]
    }

    assert rows["deepseek-v4p1-flash"]["value"] == "not priced"
    assert "no cost" in rows["deepseek-v4p1-flash"]["note"]
    # The tokens it did report are still there to read.
    assert "1.2k" in rows["deepseek-v4p1-flash"]["note"]
    assert "cost" not in rows, "a run nobody priced has no total to show"


def test_the_headline_stays_a_headline():
    """The strip answers the usual question; the details answer the next one.
    A step row asks for the strip alone — it redraws every three seconds."""
    runner, job = _runner(), _job()
    _feed(runner, job, _INIT, *_TURNS, _FULL_RESULT)

    assert "groups" not in _stats_view(job.stats)
    assert len(_stats_view(job.stats)["pills"]) <= 9


def test_a_pipelines_total_adds_the_new_numbers_up_too():
    left, right = _runner(), _runner()
    a, b = _job(), _job()
    _feed(left, a, _INIT, _RATE_LIMIT, _FULL_RESULT)
    _feed(right, b, _INIT, _FULL_RESULT)

    total = aggregate_stats([a.stats, b.stats])

    assert round(total.cost_usd, 4) == round(0.2092675 * 2, 4)
    assert total.denials == {"Bash": 4, "WebFetch": 2}
    assert total.web_searches == 4 and total.subagents_spawned == 2
    # Not summed: a context window is not shared, so the peak is the worst of
    # them, and one model named twice is one model.
    assert total.context_window == 1_000_000
    assert total.model == "claude-opus-5[1m]"
    assert total.models["claude-opus-5[1m]"]["output"] == 822
    assert total.rate_limits == {"five_hour": 0.13, "seven_day": 0.24}


# --- the shape of a run, not just its totals -------------------------------


def _wait_then(runner, job, scratch, clock, seconds, event):
    """Advance the fake clock by `seconds`, then feed one event."""
    clock[0] += seconds
    _feed(runner, job, event, stats=scratch)


def test_a_timeline_is_recorded_request_by_request(monkeypatch):
    """Totals cannot show a run whose waits are climbing or whose context
    doubled halfway through: the charts are drawn from this."""
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    runner, job, scratch = _runner(), _job(), {}
    job.started_mono = 100.0

    _feed(runner, job, {"type": "system", "subtype": "init", "model": "m"}, stats=scratch)
    _wait_then(runner, job, scratch, clock, 2.0,
               _assistant("m1", _TOOL, usage=_usage(2, 10_000, 500, 9)))
    _feed(runner, job, {"type": "user", "message": {"role": "user", "content": []}},
          stats=scratch)
    _wait_then(runner, job, scratch, clock, 5.0,
               _assistant("m2", _THINK, _TOOL, usage=_usage(2, 30_000, 500, 9)))
    _feed(runner, job, _RESULT, stats=scratch)

    first, second = job.stats.samples
    assert (first["n"], first["wait"], first["context"]) == (1, 2.0, 10_502)
    assert (second["n"], second["wait"], second["context"]) == (2, 5.0, 30_502)
    # A request's tool calls happen after the model answered it, so they belong
    # to that request and not to the one before.
    assert (first["tools"], second["tools"]) == (1, 1)
    assert first["t"] == 2.0 and second["t"] == 7.0


def test_thinking_is_attributed_to_the_request_it_preceded(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    runner, job, scratch = _runner(), _job(), {}
    job.started_mono = 100.0

    _feed(runner, job, _assistant("m1", _TOOL, usage=_usage(2, 10, 0, 9)), stats=scratch)
    _feed(runner, job, _thinking(50, 50), _thinking(300, 250), stats=scratch)
    _feed(runner, job, _assistant("m2", _TEXT, usage=_usage(2, 20, 0, 9)), stats=scratch)
    _feed(runner, job, _RESULT, stats=scratch)

    assert [sample["thinking"] for sample in job.stats.samples] == [300, 0]


def test_a_long_run_keeps_its_shape_rather_than_its_tail():
    """Past the cap the timeline halves its resolution instead of dropping the
    beginning — what a long run is asked about is its shape, and the shape
    starts at the start."""
    from radar.commands import _MAX_SAMPLES

    runner, job, scratch = _runner(), _job(), {}
    for n in range(_MAX_SAMPLES + 40):
        _feed(runner, job, _assistant(f"m{n}", _TEXT, usage=_usage(1, 10, 0, 1)), stats=scratch)

    assert len(job.stats.samples) <= _MAX_SAMPLES
    assert job.stats.samples[0]["n"] == 1        # the run's first request is still there
    assert job.stats.samples[-1]["n"] > _MAX_SAMPLES // 2


def test_the_timeline_survives_being_stored():
    stats = RunStats(
        label="AI review",
        samples=[{"n": 1, "t": 2.0, "wait": 2.0, "context": 10_502, "tools": 1,
                  "thinking": 0}],
    )

    assert stats_from_json(stats_to_json(stats)).timelines() == [
        {"label": "AI review", "samples": stats.samples}
    ]


def test_a_pipeline_keeps_one_line_per_step():
    """Two steps of a stage run at once; a single line stitched from both would
    zigzag between two runs' waits and mean nothing."""
    a = RunStats(label="AI review", turns=1, input_tokens=1,
                 samples=[{"n": 1, "t": 1.0, "wait": 2.0, "context": 900}])
    b = RunStats(label="DBA review", turns=1, input_tokens=1,
                 samples=[{"n": 1, "t": 1.5, "wait": 4.0, "context": 700}])

    total = aggregate_stats([a, b])

    assert [line["label"] for line in total.timelines()] == ["AI review", "DBA review"]


# --- the charts themselves -------------------------------------------------


def test_every_chart_is_drawn_from_the_timeline():
    from radar.web.app import _stats_charts

    runner, job, scratch = _runner(), _job(), {}
    _feed(runner, job, {"type": "system", "subtype": "init", "model": "m"}, stats=scratch)
    for n in range(1, 5):
        _feed(runner, job, _thinking(60, 60), stats=scratch)
        _feed(runner, job, _assistant(f"m{n}", _TOOL, usage=_usage(2, 1000 * n, 0, 9)),
              stats=scratch)
    _feed(runner, job, _RESULT, stats=scratch)

    titles = [str(one).split("</figcaption>")[0] for one in _stats_charts(job.stats)]
    assert len(titles) == 5
    assert "Response time per request" in titles[0]
    assert "Context carried per request" in titles[1]
    assert "Tool calls per request" in titles[2]
    assert "Thinking per request" in titles[3]
    assert "Tokens read, cumulative" in titles[4]
    # Drawn, not described: one point per request on the line charts.
    assert str(_stats_charts(job.stats)[1]).count("<circle") == 4


def test_a_long_run_draws_the_line_without_a_dot_per_request():
    """The dots are most of the markup, and the section re-sends all of it.

    A circle per request per series per chart is what the payload is made of: a
    four-step pipeline at the sample cap came to 600 KB of SVG, re-sent every
    few seconds to every open panel while the run worked. Past a point every two
    pixels a row of dots is a thick line anyway, so the line draws alone and the
    shape — which is what the chart is for — is unchanged.
    """
    from radar.charts import _MAX_DOTS
    from radar.web.app import _stats_charts

    def drawn(samples: int) -> str:
        stats = RunStats(turns=samples)
        stats.samples = [{"t": float(n), "wait": 2.0} for n in range(samples)]
        return str(_stats_charts(stats)[0])

    short = drawn(_MAX_DOTS)
    long = drawn(_MAX_DOTS + 1)
    assert short.count("<circle") == _MAX_DOTS
    assert long.count("<circle") == 0
    # The line itself is still every point.
    assert long.count(",") >= _MAX_DOTS and "<polyline" in long
    assert len(long) < len(short), "and the markup got smaller, not bigger"


def test_a_run_with_no_timeline_draws_nothing():
    from radar.web.app import _stats_charts

    assert _stats_charts(RunStats(turns=3, input_tokens=6, billed=True)) == []


def test_a_chart_names_its_series_when_there_is_more_than_one():
    """A pipeline's steps share an axis, so the legend is what tells them
    apart; one skill's own chart is about itself and needs none."""
    from radar.charts import Series, chart

    one = chart([Series("AI review", ((0.0, 1.0), (1.0, 2.0)))], title="Waits")
    two = chart(
        [Series("AI review", ((0.0, 1.0),)), Series("DBA review", ((0.0, 3.0),))],
        title="Waits",
    )

    assert "chart-legend" not in str(one)
    assert "chart-legend" in str(two)
    assert "AI review" in str(two) and "DBA review" in str(two)


def test_a_chart_escapes_what_it_is_given():
    """A series is named after a skill, and a skill is named in config."""
    from radar.charts import Series, chart

    drawn = str(chart(
        [Series("<img src=x onerror=alert(1)>", ((0.0, 1.0),)),
         Series("second", ((0.0, 2.0),))],
        title="</svg><script>alert(1)</script>",
    ))

    assert "<script>" not in drawn and "<img" not in drawn
    assert "&lt;script&gt;" in drawn and "&lt;img" in drawn


def test_an_axis_says_nothing_rather_than_something_untrue():
    """A count axis whose middle gridline falls between two whole numbers had
    two labels reading 0."""
    from radar.charts import Series, chart
    from radar.web.app import _whole

    drawn = str(chart([Series("a", ((0.0, 1.0),))], title="Tools", y_format=_whole))

    assert drawn.count(">0<") == 1          # the baseline, and nothing pretending
    assert ">1<" in drawn


# --- the section it all lives in -------------------------------------------


def _app_with_a_streaming_skill(tmp_path, script_events, timeout=30):
    """A dashboard whose one skill replays these stream-json events for !1."""
    from fastapi.testclient import TestClient

    from radar.config import load_config
    from radar.db import Database as DB
    from radar.gitlab_client import FixtureSource
    from radar.poller import poll_once
    from radar.web.app import create_app
    from tests.conftest import _BASE_CONFIG
    from tests.test_poller import PID, PROJECT, _discussions, _mr

    script = tmp_path / "emit.py"
    script.write_text(
        "import json\n"
        + "".join(f"print(json.dumps({event!r}), flush=True)\n" for event in script_events),
        encoding="utf-8",
    )
    path = tmp_path / "config.yaml"
    path.write_text(_BASE_CONFIG + f"""
skills:
  - name: qa
    label: QA plan
    enabled: true
    stores_result: true
    command: '"{sys.executable}" "{script}"'
    timeout_seconds: {timeout}
""", encoding="utf-8")
    config = load_config(path)
    db_path = tmp_path / "panel.db"
    with DB(db_path) as db:
        poll_once(db, config, FixtureSource(
            mrs_by_project={PROJECT: [_mr(["dan"])]},
            discussions_by_mr={(PID, 1): _discussions()},
        ))
    return TestClient(create_app(config, str(db_path))), PID


def test_the_panel_carries_a_collapsible_ai_stats_section(tmp_path):
    """Folded away by default — the strip above answers the usual question —
    and holding the charts and every measurement for when it doesn't."""
    client, pid = _app_with_a_streaming_skill(tmp_path, [_INIT, *_TURNS, _RESULT])

    html = client.post(f"/qa/{pid}/1").text

    assert '<details class="ai-stats"' in html and "<summary>AI stats</summary>" in html
    # And it is the section that refreshes itself, not the panel around it.
    assert 'class="ai-stats-body"' in html


def test_a_folded_away_section_does_not_keep_redrawing_itself(tmp_path):
    """Five charts and a page of numbers, drawn server-side for nobody.

    The filter is on the trigger rather than the fragment, so htmx keeps the
    timer running and opening the section picks the refresh back up without a
    reload.
    """
    client, pid = _app_with_a_streaming_skill(tmp_path, [_INIT, *_TURNS, _RESULT])
    job_id = re.search(r'plog-([0-9a-f]+)', client.post(f"/qa/{pid}/1").text).group(1)

    live = client.get(f"/qa/stats/{job_id}").text
    trigger = re.search(r'hx-trigger="([^"]*)"', live)
    assert trigger, "a running section should still poll"
    assert "closest('details')?.open !== false" in trigger.group(1)


def test_the_section_redraws_itself_until_the_run_ends(tmp_path):
    """The charts have to move while the run works, and the polling has to stop
    when it doesn't — a finished fragment carries no trigger."""
    client, pid = _app_with_a_streaming_skill(tmp_path, [_INIT, *_TURNS, _RESULT])
    job_id = re.search(r'plog-([0-9a-f]+)', client.post(f"/qa/{pid}/1").text).group(1)

    deadline = time.monotonic() + 30
    while "hx-trigger" in client.get(f"/qa/stats/{job_id}").text:
        assert time.monotonic() < deadline, "the run never finished"
        time.sleep(0.1)

    done = client.get(f"/qa/stats/{job_id}").text
    assert "Response time per request" in done   # the charts are there
    assert "Tokens read, cumulative" in done
    assert "hx-get" not in done                  # and the browser stops asking


def test_the_live_section_says_so_before_the_first_answer(tmp_path):
    """An empty frame that fills in later looks broken; this says what it is
    waiting for."""
    from radar.commands import CommandRunner
    from radar.web.app import _stats_view, templates

    runner = CommandRunner(SkillConfig(name="qa", command="x"), "qa")
    job = runner._admit({"project_id": 1, "mr_iid": 2})
    _feed(runner, job, _INIT)

    html = templates.env.get_template("_ai_stats.html").render({
        "ai": _stats_view(job.stats, details=True) or {"pills": [], "groups": [], "charts": []},
        "kind": "qa", "job": job, "live": True, "tick_s": 5,
    })

    assert "charts start" in html


def test_the_reference_line_averages_the_points_it_is_drawn_against():
    """The headline average is the run's own model time over its own turn
    count; the chart's points are radar's measure of the same waits from
    outside. A line taken from one and drawn across the other cannot be read."""
    from radar.web.app import _stats_charts

    stats = RunStats(
        label="AI review",
        samples=[
            {"n": 1, "t": 1.0, "wait": 2.0, "context": 100},
            {"n": 2, "t": 5.0, "wait": 6.0, "context": 200},
        ],
        # The run's own figures say something different, and this is the line
        # that must not follow them.
        api_ms=1000, reported_turns=2, billed=True, turns=2, input_tokens=4,
    )

    drawn = str(_stats_charts(stats)[0])

    assert "average 4.0s" in drawn      # the mean of 2s and 6s
    assert "average 0.5s" not in drawn  # not api_ms / num_turns


# --- providers that report usage differently -------------------------------


def test_usage_attached_to_a_later_block_is_not_lost():
    """The shape a reasoning model behind a gateway produced: the turn opens
    with a thinking block carrying no counts, and the token counts arrive on
    the tool call that follows. Reading only the first event of a request
    recorded zero for the whole run, which looked like a provider that reports
    nothing at all."""
    runner, job = _runner(), _job()
    _feed(
        runner, job,
        _assistant("msg_A", _THINK, usage=None),
        _assistant("msg_A", _TOOL, usage=_usage(12, 4000, 800, 40)),
    )

    assert job.stats.turns == 1                    # still one request
    assert job.stats.input_tokens == 12
    assert job.stats.cache_read_tokens == 4000
    assert job.stats.cache_write_tokens == 800
    assert job.stats.requests_with_usage == 1


def test_a_usage_block_that_grows_through_a_turn_is_counted_once():
    """Some providers fill the counts in as the turn goes. Each field is
    tracked per request, so what is added is the increase and never the total
    again."""
    runner, job = _runner(), _job()
    _feed(
        runner, job,
        _assistant("msg_A", _THINK, usage=_usage(12, 4000, 0, 1)),
        _assistant("msg_A", _TEXT, usage=_usage(12, 4000, 800, 40)),
        _assistant("msg_A", _TOOL, usage=_usage(12, 4000, 800, 60)),
    )

    assert (job.stats.input_tokens, job.stats.cache_read_tokens) == (12, 4000)
    assert job.stats.cache_write_tokens == 800
    assert job.stats.turns == 1


def test_requests_with_no_usage_at_all_are_counted_and_said_out_loud():
    """An input figure of zero has two very different causes — a run that read
    nothing, and a provider that does not report what it read. The panel has to
    say which, or the operator debugs the wrong system."""
    runner, job = _runner(), _job()
    _feed(
        runner, job,
        _assistant("msg_A", _THINK, usage=None),
        _assistant("msg_B", _TOOL, usage={}),
        _assistant("msg_C", _TEXT, usage=_usage(5, 100, 0, 9)),
    )

    assert job.stats.turns == 3 and job.stats.requests_with_usage == 1
    rows = {
        row["label"]: (row["value"], row["note"])
        for group in _stats_view(job.stats, details=True)["groups"]
        for row in group["rows"]
    }
    value, note = rows["usage reported"]
    assert value == "1 of 3 requests"
    assert "no token counts" in note


def test_nothing_is_said_when_every_request_reported(tmp_path):
    runner, job = _runner(), _job()
    _feed(runner, job, *_TURNS, _RESULT)

    labels = [
        row["label"] for group in _stats_view(job.stats, details=True)["groups"]
        for row in group["rows"]
    ]
    assert "usage reported" not in labels


def test_the_context_chart_still_lands_on_the_right_request():
    """A late usage block belongs to the request it came in on, not to the one
    radar happened to be holding open."""
    from radar.web.app import _stats_charts

    runner, job, scratch = _runner(), _job(), {}
    _feed(
        runner, job,
        _assistant("msg_A", _THINK, usage=None),
        _assistant("msg_A", _TOOL, usage=_usage(2, 5000, 0, 9)),
        _assistant("msg_B", _TOOL, usage=_usage(2, 9000, 0, 9)),
        _RESULT,
        stats=scratch,
    )

    assert [sample["context"] for sample in job.stats.samples] == [5002, 9002]
    assert _stats_charts(job.stats)   # and it draws


def test_a_run_can_be_asked_to_keep_its_raw_stream(tmp_path, monkeypatch):
    """The numbers can only say *that* a provider reported nothing or answered
    slowly; the next question is always what it actually sent. Off by default,
    because this is a forensic tool and not a log."""
    from radar.commands import _CAPTURE_ENV, CommandRunner

    events = [_INIT, *_TURNS, _RESULT]
    script = tmp_path / "emit.py"
    script.write_text(
        "import json\n"
        + "".join(f"print(json.dumps({event!r}))\n" for event in events),
        encoding="utf-8",
    )
    out = tmp_path / "captured"
    monkeypatch.setenv(_CAPTURE_ENV, str(out))
    runner = CommandRunner(
        SkillConfig(name="qa", command=f'"{sys.executable}" "{script}"', timeout_seconds=30),
        "qa",
    )

    job = runner.start({"project_id": 1, "mr_iid": 2})
    deadline = time.monotonic() + 30
    while job.status == "running":
        assert time.monotonic() < deadline, "the run never finished"
        time.sleep(0.05)

    written = list(out.glob("qa-*.jsonl"))
    assert len(written) == 1
    records = [json.loads(line) for line in written[0].read_text().splitlines()]
    assert len(records) == len(events)
    # Every line arrives stamped with when radar saw it — the half no event
    # carries, and the half a latency question needs.
    assert all(isinstance(record["t"], (int, float)) for record in records)
    assert records[-1]["event"]["type"] == "result"
    # And the panel says where it went, so nobody has to guess.
    assert any("capturing this run's raw stream" in item["text"] for item in job.progress)


def test_nothing_is_captured_unless_it_is_asked_for(tmp_path, monkeypatch):
    from radar.commands import _CAPTURE_ENV, CommandRunner

    monkeypatch.delenv(_CAPTURE_ENV, raising=False)
    script = tmp_path / "emit.py"
    script.write_text("print('hello')\n", encoding="utf-8")
    runner = CommandRunner(
        SkillConfig(name="qa", command=f'"{sys.executable}" "{script}"', timeout_seconds=30),
        "qa",
    )

    job = runner.start({"project_id": 1, "mr_iid": 2})
    deadline = time.monotonic() + 30
    while job.status == "running":
        assert time.monotonic() < deadline
        time.sleep(0.05)

    assert list(tmp_path.glob("*.jsonl")) == []


def test_the_panel_prints_the_command_that_reads_the_run_back(tmp_path):
    """Capturing is only half of it: the next step after seeing a bad run is
    reading it back, and that takes the path."""
    from radar.commands import CommandRunner
    from radar.web.app import _stats_view, templates

    runner = CommandRunner(SkillConfig(name="qa", label="QA", command="x"), "qa")
    job = runner._admit({"project_id": 1, "mr_iid": 2})
    job.capture_path = "/work/radar-streams/qa-9f2c1ab4e7d1.jsonl"
    _feed(runner, job, _INIT, *_TURNS, _RESULT)

    view = _stats_view(job.stats, details=True)
    view["capture"] = job.capture_path
    html = templates.env.get_template("_ai_stats.html").render(
        {"ai": view, "kind": "qa", "job": job, "live": False, "tick_s": 5}
    )

    assert "radar diagnose-stream /work/radar-streams/qa-9f2c1ab4e7d1.jsonl" in html


def test_a_run_that_was_not_captured_says_nothing_about_it():
    from radar.commands import CommandRunner
    from radar.web.app import _stats_view, templates

    runner = CommandRunner(SkillConfig(name="qa", command="x"), "qa")
    job = runner._admit({"project_id": 1, "mr_iid": 2})
    _feed(runner, job, _INIT, *_TURNS, _RESULT)

    html = templates.env.get_template("_ai_stats.html").render({
        "ai": _stats_view(job.stats, details=True) | {"capture": job.capture_path},
        "kind": "qa", "job": job, "live": False, "tick_s": 5,
    })

    assert "diagnose-stream" not in html


def test_a_reported_zero_does_not_wipe_a_thinking_estimate_that_was_watched():
    """A gateway that does not report thinking sends a zero rather than
    nothing: one streamed 50k reasoning tokens and then reported
    `thinkingTokens: 0`. Believing it would claim the model never reasoned and
    throw away the only figure radar had."""
    runner, job = _runner(), _job()
    silent_about_thinking = json.loads(json.dumps(_FULL_RESULT))
    silent_about_thinking["usage"]["output_tokens_details"]["thinking_tokens"] = 0
    silent_about_thinking["modelUsage"]["claude-opus-5[1m]"]["thinkingTokens"] = 0

    _feed(runner, job, _thinking(900, 900), _thinking(2000, 1100), silent_about_thinking)

    assert job.stats.thinking_tokens == 2000        # what was watched, not the zero
    assert not job.stats.thinking_billed            # and still labelled an estimate
    assert job.stats.output_tokens == 411           # the rest of the totals stand


def test_a_reported_zero_is_believed_when_nothing_was_seen_thinking():
    """A model that really did not think reports zero, and radar saw nothing
    either — there is no estimate to protect."""
    runner, job = _runner(), _job()
    quiet = json.loads(json.dumps(_FULL_RESULT))
    quiet["usage"]["output_tokens_details"]["thinking_tokens"] = 0
    quiet["modelUsage"]["claude-opus-5[1m]"]["thinkingTokens"] = 0

    _feed(runner, job, quiet)

    assert job.stats.thinking_tokens == 0 and job.stats.thinking_billed


def test_totals_that_only_arrive_at_the_end_are_described_as_such():
    """A gateway that reports nothing per request but sums up at the end gives
    whole totals, late — which is a different thing from partial totals, and
    the operator acts on them differently."""
    runner, job = _runner(), _job()
    _feed(
        runner, job,
        _assistant("m1", _TOOL, usage={"input_tokens": 0, "cache_read_input_tokens": 0}),
        _assistant("m2", _TOOL, usage={"input_tokens": 0, "cache_read_input_tokens": 0}),
        _FULL_RESULT,
    )

    assert job.stats.requests_with_usage == 0 and job.stats.billed
    note = next(
        row["note"] for group in _stats_view(job.stats, details=True)["groups"]
        for row in group["rows"] if row["label"] == "usage reported"
    )
    assert "only arrived at the end" in note


def test_a_provider_that_reports_no_sizes_gets_no_flat_zero_chart():
    """Drawing the zeros as a measurement is how a chart lies: it showed a run
    carrying no context at all, when the truth is that nobody said."""
    from radar.web.app import _stats_charts

    runner, job, scratch = _runner(), _job(), {}
    _feed(runner, job, _INIT, stats=scratch)
    for n in range(1, 6):
        # Usage present on every message and zero in every field — the shape
        # this gateway actually sends.
        _feed(runner, job, _assistant(f"m{n}", _TOOL, usage=_usage(0, 0, 0, 0)),
              stats=scratch)
        _feed(runner, job, {"type": "user", "message": {"role": "user", "content": []}},
              stats=scratch)

    titles = [str(one).split("</figcaption>")[0] for one in _stats_charts(job.stats)]

    assert not any("Context carried per request" in t for t in titles)
    assert any("Conversation size per request" in t for t in titles)
    # No cumulative chart in this mode: the series above is already a running
    # total, and summing it again would mean nothing.
    assert not any("cumulative" in t for t in titles)
    # And the substitute is drawn from something real: the conversation grows.
    sizes = [sample["bytes"] for sample in job.stats.samples]
    assert sizes == sorted(sizes) and sizes[-1] > sizes[0]


def test_a_provider_that_does_report_sizes_gets_the_token_chart():
    from radar.web.app import _stats_charts

    runner, job, scratch = _runner(), _job(), {}
    _feed(runner, job, _INIT, *_TURNS, _RESULT, stats=scratch)

    titles = [str(one).split("</figcaption>")[0] for one in _stats_charts(job.stats)]

    assert any("Context carried per request" in t for t in titles)
    assert any("Tokens read, cumulative" in t for t in titles)
    assert not any("Conversation size" in t for t in titles)


# --- surviving a network that blinks ---------------------------------------


def test_a_context_fetch_that_fails_transiently_is_tried_again(monkeypatch, tmp_path):
    """The failure that costs most is the cheapest to survive: a connection
    reset a minute into a pipeline, on the step that merges half an hour of
    reviews."""
    from radar.commands import CommandRunner

    monkeypatch.setattr(time, "sleep", lambda _s: None)   # no real waiting
    script = tmp_path / "emit.py"
    script.write_text("import sys; print(sys.stdin.read().strip())\n", encoding="utf-8")
    runner = CommandRunner(
        SkillConfig(name="qa", command=f'"{sys.executable}" "{script}"', timeout_seconds=30),
        "qa",
    )
    tries = []

    def flaky(*_args, **_kwargs):
        tries.append(1)
        if len(tries) < 3:
            raise ConnectionResetError(54, "Connection reset by peer")
        return "the context, eventually"

    job = runner.start({"project_id": 1, "mr_iid": 2}, stdin_provider=flaky)
    deadline = time.monotonic() + 30
    while job.status == "running":
        assert time.monotonic() < deadline, "the run never finished"
        time.sleep(0.05)

    assert job.status == "done" and len(tries) == 3
    assert "the context, eventually" in job.output
    assert any("trying again" in item["text"] for item in job.progress)


def test_a_context_fetch_that_keeps_failing_reports_the_real_fault(monkeypatch, tmp_path):
    """Three resets is a broken thing, not a blink — and the panel should name
    what actually went wrong, not that radar retried."""
    from radar.commands import CommandRunner

    monkeypatch.setattr(time, "sleep", lambda _s: None)
    runner = CommandRunner(
        SkillConfig(name="qa", command=f'"{sys.executable}" -c "pass"', timeout_seconds=30),
        "qa",
    )
    tries = []

    def broken(*_args, **_kwargs):
        tries.append(1)
        raise ConnectionResetError(54, "Connection reset by peer")

    job = runner.start({"project_id": 1, "mr_iid": 2}, stdin_provider=broken)
    deadline = time.monotonic() + 30
    while job.status == "running":
        assert time.monotonic() < deadline
        time.sleep(0.05)

    assert job.status == "error" and len(tries) == 3
    assert "Connection reset by peer" in job.error


def test_a_misconfiguration_is_not_retried(monkeypatch, tmp_path):
    """A required input that is unset will be unset next time too; retrying it
    just delays the answer."""
    from radar.commands import CommandRunner
    from radar.skillcontext import SkillContextError

    monkeypatch.setattr(time, "sleep", lambda _s: None)
    runner = CommandRunner(
        SkillConfig(name="qa", command=f'"{sys.executable}" -c "pass"', timeout_seconds=30),
        "qa",
    )
    tries = []

    def refuses(*_args, **_kwargs):
        tries.append(1)
        raise SkillContextError("JIRA_BASE_URL is not set")

    job = runner.start({"project_id": 1, "mr_iid": 2}, stdin_provider=refuses)
    deadline = time.monotonic() + 30
    while job.status == "running":
        assert time.monotonic() < deadline
        time.sleep(0.05)

    assert job.status == "error" and len(tries) == 1
    assert "JIRA_BASE_URL" in job.error


def test_a_slow_failure_does_not_buy_a_retry_it_cannot_afford(tmp_path):
    """The budget is read after the attempt, not before it.

    A fetch that fails *slowly* has already spent the time the decision to
    retry is about. Reading the budget from before it ran let a job sleep, try
    again with nothing left, and report the timeout that followed instead of
    the connection reset that started it — the one thing the panel needed to
    say. `time.sleep` is deliberately not stubbed here: the clock is the point.
    """
    from radar.commands import CommandRunner

    runner = CommandRunner(
        SkillConfig(name="qa", command=f'"{sys.executable}" -c "pass"', timeout_seconds=5),
        "qa",
    )

    def slow_and_broken(*_args, **_kwargs):
        time.sleep(2)
        raise ConnectionResetError(54, "Connection reset by peer")

    began = time.monotonic()
    job = runner.start({"project_id": 1, "mr_iid": 2}, stdin_provider=slow_and_broken)
    deadline = began + 20
    while job.status == "running":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    elapsed = time.monotonic() - began

    # Measured before the attempt there is room for the 3s wait; measured after
    # it has eaten 2s of a 5s budget there is not.
    assert "Connection reset by peer" in job.error, job.error
    assert elapsed < 4.5, f"the fetch ran {elapsed:.1f}s of a 5s budget and retried anyway"


# --- what the provider said, when it refused -------------------------------


def _retry(status: int, reason: str, attempt: int = 1, total: int = 10) -> dict:
    return {
        "type": "system", "subtype": "api_retry", "error_status": status,
        "error": reason, "attempt": attempt, "max_retries": total,
        "retry_delay_ms": 37312,
    }


def test_a_provider_refusal_is_read_from_the_stream(tmp_path):
    """The reason a run died is in its own event stream and nowhere else: the
    CLI retries a 401 quietly and then exits non-zero having printed only
    whatever unrelated warning it happened to emit. Radar used to report that
    warning as the cause."""
    from radar.commands import CommandJob, CommandRunner
    from radar.config import SkillConfig

    runner = CommandRunner(SkillConfig(name="review", command="x"), "review")
    job = CommandJob(id="j", kind="review")
    stats: dict = {}
    for attempt in (1, 2, 10):
        runner._ingest(job, json.dumps(_retry(401, "authentication_failed", attempt)),
                       [], [], stats)

    assert job.stats.api_errors == {"HTTP 401 authentication_failed": 3}
    assert job.stats.last_api_error == "HTTP 401 authentication_failed"
    # And it reaches the panel's live log while it is happening, not only after.
    said = " ".join(item["text"] for item in job.progress)
    assert "the model provider refused" in said and "401" in said
    assert "attempt 10/10" in said


def test_a_refused_run_still_shows_the_refusal_though_it_measured_nothing():
    """A run the provider turned down has no tokens, no cost and no turns, so
    the "did this report anything?" gate would drop the one fact worth having."""
    from radar.web.app import _stats_view

    stats = RunStats()
    stats.api_errors = {"HTTP 401 authentication_failed": 10}
    stats.last_api_error = "HTTP 401 authentication_failed"
    assert not stats.measured          # nothing was spent — nothing got through

    view = _stats_view(stats)
    assert view is not None
    labels = {p["label"]: p["value"] for p in view["pills"]}
    assert labels["provider refused"] == "HTTP 401 authentication_failed ×10"
