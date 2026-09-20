"""Launch an external command for an MR, stream its progress, and track the job.

Shared by the code-review and QA-test-plan features: both take a command
template from config (e.g. ``claude -p "/code-review {web_url}"``), fill in the
MR's context, and run it as a background subprocess.

The child's stdout is read line-by-line as it runs so the dashboard can show
live progress (an SSE endpoint tails ``job.progress``). If the command speaks
Claude Code's ``--output-format stream-json`` (line-delimited JSON events), we
turn tool_use / assistant events into friendly progress lines and take the final
answer from the ``result`` event. A run can report more than one of those, so the
answer is every result event in order — which is also why the child is launched
with background work turned off (see ``_DEFAULT_CHILD_ENV``): an agent that
defers work to a background subagent reports a placeholder first and its actual
findings only later, if at all. Any other command works too: its stdout lines
become the progress log and the accumulated text becomes the output.

The same events also say what the run *spent* — tokens in, out and thinking, and
how long the model took to answer — which radar keeps per job as a ``RunStats``
and the panel shows next to the progress log (see ``RunStats``).

Before launching, the skill's declared context bag is resolved for this MR (see
``skillcontext``): ``{source_root}`` becomes a placeholder like any other, and
— unless ``working_dir`` overrides it — the child runs *in* that checkout, so an
agent's own file tools land on the right tree. A required input that is unset,
or a root that is not a directory, refuses the job instead of launching one that
would review nothing.

Safety: the template is split into argv with ``shlex`` *before* substitution and
run with ``shell=False``, so an MR field can't inject shell metacharacters or
extra arguments; a substituted value that would make a token start with ``-`` is
refused. The child never inherits our GitLab PAT (see ``_ENV_DENYLIST``).
"""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, fields, replace

from .config import SECRET_ENV_NAMES, CommandConfig
from .skillcontext import SkillContextError, job_context
from .worktree import WorktreeError, create_mr_worktree

log = logging.getLogger("radar.commands")

# Env vars never exported to the skill subprocess. The child is an LLM agent fed
# attacker-influenceable MR content; it must not inherit radar's GitLab PAT or
# Jira credentials. When a skill needs context, radar fetches it and pipes it on
# stdin (see context.py); a skill that fetches on its own must carry its own
# credentials (e.g. an MCP server), not borrow radar's.
_ENV_DENYLIST = SECRET_ENV_NAMES

# Exported to every skill, on top of radar's own environment and before the
# skill's `env:` block, which can override or drop any of it.
#
# CLAUDE_CODE_DISABLE_BACKGROUND_TASKS: a headless `claude -p` that spawns a
# background subagent answers the turn immediately with a placeholder ("I'll
# report the findings when it completes") and emits that as a result, landing
# the real answer in a later one — if it is waited for at all. Radar captures
# every result event, so at best the placeholder is glued to the front of the
# output and at worst it is the whole of it. Forcing subagents to run inline
# makes the run's last word its actual work. A skill that would rather keep its
# parallelism can drop the variable with `env_unset:`.
#
# CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS: how long `claude -p` waits for still-
# running background agents before killing them and exiting with whatever it
# has (0 = wait forever; the CLI's default gives up after 10 minutes). Inert
# while the variable above keeps everything inline, but a skill can always
# escape to the background anyway — by opting out, or by shelling out to
# another `claude` itself — and then this is the difference between findings
# and a "still running" note. With the ceiling gone, the only clock left is
# radar's own `timeout_seconds`, which the operator sizes to the skill.
_DEFAULT_CHILD_ENV = {
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0",
}


def _is_secret_env(name: str) -> bool:
    """Whether a variable name is one of radar's own credentials.

    Case-insensitive, because Windows resolves environment variables that way:
    a child handed ``gitlab_token`` reads it back as ``GITLAB_TOKEN``.
    """
    return name.upper() in _ENV_DENYLIST


def _unset_env(env: dict, name: str) -> None:
    """Drop a variable from a child environment, matching the platform's own
    name rules (Windows ignores case, so the uppercase twin must go too)."""
    if os.name == "nt":
        for key in [k for k in env if k.upper() == name.upper()]:
            del env[key]
    else:
        env.pop(name, None)

_MAX_OUTPUT = 200_000    # cap captured output to bound memory / stored plan size
_MAX_JOBS = 256          # bound the in-memory job registry (evict oldest)
_MAX_PROGRESS = 500      # cap the per-job progress log
_MAX_SAMPLES = 400       # cap a run's timeline (halved, not truncated, past it)

# How hard radar tries to fetch a skill's context before giving the job up.
# The fetches are idempotent reads and the thing they most often hit is a
# connection reset, so a couple of retries a few seconds apart turn a lost
# pipeline into a slower one. More than that is waiting on something broken.
_CONTEXT_ATTEMPTS = 3
_CONTEXT_RETRY_WAIT_S = 3.0

PLACEHOLDER_KEYS = (
    "web_url",
    "mr_iid",
    "project_id",
    "source_branch",
    "target_branch",
    "title",
    "author",
    "jira_keys",       # space-separated, e.g. "PROJ-1 PROJ-2"
    "jira_keys_csv",   # comma-separated, e.g. "PROJ-1,PROJ-2"
    "head_sha",        # the MR's head commit, as of the last poll
    "source_root",     # the checkout for this job (see skillcontext / worktree)
    # Filled for the skill `jenkins.analysis.skill` names, instead of the MR
    # fields above. A skill only ever sees one set; the other substitutes to
    # empty, which is what an absent placeholder has always done.
    "jenkins_job",
    "build_number",
    "build_url",
)

# Placeholders filled from the MR snapshot; the rest are computed per job.
SNAPSHOT_KEYS = tuple(k for k in PLACEHOLDER_KEYS if k != "source_root")


class CommandError(ValueError):
    """The command template + MR context can't be turned into a safe argv."""


# The line `claude -p` prints on stderr when its background-agent wait ceiling
# elapses and it kills agents that were still working. Radar's default ceiling
# is 0 (wait forever), so seeing this means something overrode it — and that the
# run's output is a deferral note, not the findings.
#
# Deliberately specific: a false positive throws away a finished review (the
# branch skips `on_success`, so nothing is stored), while a false negative only
# leaves today's behaviour of storing the note. So this matches the CLI's own
# sentence at the start of a line, not the phrase wherever it appears — a review
# that happens to discuss background tasks, or an MCP server logging about them,
# must not cost the operator their result. The tail is left loose because the
# sentence is undocumented and a release may reword it.
_BG_SWEPT_RE = re.compile(r"(?im)^\s*background tasks still running after\b.*?\bterminat")

# The status the Agent tool reports for a subagent dispatched to the background.
_ASYNC_LAUNCH_STATUS = "async_launched"

# How long to keep reading a pipe after the child itself has exited. Only in
# play when something the child spawned inherited the pipe and outlived it.
_DRAIN_GRACE = 5.0

# Every live skill process, so radar can take them down when it exits: with the
# wait ceiling at 0 the CLI never reaps its own background agents, and a job's
# timeout dies with the process running it. Registered while a child is alive,
# swept by the atexit hook below (which covers a clean exit and Ctrl-C; a
# SIGKILLed radar can't run code at all).
_LIVE: dict[int, tuple[subprocess.Popen, int | None]] = {}
_LIVE_LOCK = threading.Lock()


def _kill_tree(proc: subprocess.Popen, pgid: int | None) -> None:
    """Kill the child and everything it spawned, not just the child.

    ``proc.kill()`` alone ends the direct child while a backgrounded agent it
    launched keeps running (and keeps spending API tokens) — and if that agent
    inherited our stdout, keeps the pipe's write end open. On POSIX the child
    leads its own process group (``start_new_session`` at launch) so the group
    can be signalled as a unit; on Windows ``taskkill /T`` walks the tree.

    Both are best effort, and neither reaches a descendant that put *itself* in
    a new session or was re-parented after its own parent died. The direct
    ``kill()`` stays as a backstop, which is why every step here is guarded: a
    tree kill that raises must not skip it.

    ``pgid`` is captured at spawn rather than read from ``proc.pid`` here,
    because a caller may signal after the child was reaped and its pid could
    then belong to somebody else. Callers only reach that path while a group
    member is provably still alive, which keeps the id allocated.
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            pass  # missing from PATH, or wedged — fall through to kill()
    elif pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:  # group already empty
            pass
    try:
        proc.kill()
    except OSError:  # pragma: no cover - already reaped
        pass


def _join_all(threads: list[threading.Thread], grace: float) -> bool:
    """Wait up to ``grace`` for all of them together; True if any is still
    running. One shared deadline, not one each: the point is to bound how long
    a job can be held up by a pipe nobody is going to close."""
    deadline = time.monotonic() + grace
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    return any(thread.is_alive() for thread in threads)


def _sweep_live_children() -> None:
    """Kill any skill still running when radar exits, so nothing outlives the
    only supervisor it had."""
    with _LIVE_LOCK:
        live = list(_LIVE.values())
    for proc, pgid in live:
        if proc.poll() is None:
            log.warning("radar is exiting; stopping skill process %s", proc.pid)
            _kill_tree(proc, pgid)


atexit.register(_sweep_live_children)


def _content_blocks(obj: dict) -> list:
    """The content blocks of a stream-json message event, or nothing.

    Every shape here is the child's to choose and radar's to survive, so a
    message that is a string, or content that is not a list, reads as empty
    rather than raising on a reader thread.
    """
    message = obj.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    return content if isinstance(content, list) else []


def _async_agent_launched(obj: dict) -> bool:
    """Whether this stream-json event reports a subagent sent to the background.

    Structural on purpose: the marker is advisory (it only sharpens a timeout
    message), so matching the word anywhere in the line would be all cost and
    no benefit — a diff, a tool result, or a review discussing this very code
    would trip it and misdirect the operator.
    """
    if obj.get("status") == _ASYNC_LAUNCH_STATUS:
        return True
    for block in _content_blocks(obj):
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        content = block.get("content")
        for part in content if isinstance(content, list) else [content]:
            if isinstance(part, dict):
                part = part.get("text")
            if not isinstance(part, str) or _ASYNC_LAUNCH_STATUS not in part:
                continue
            try:
                payload = json.loads(part)
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict) and payload.get("status") == _ASYNC_LAUNCH_STATUS:
                return True
    return False


# Which of a tool's inputs actually says what the agent is doing. "Bash" alone,
# forty times over, tells an operator watching a long review nothing; the command
# it ran tells them where the review has got to. First key present wins, so the
# human-written description beats the machinery when a tool offers both.
_TOOL_DETAIL_KEYS = (
    "description", "command", "file_path", "notebook_path", "pattern",
    "query", "url", "prompt", "path", "skill", "subagent_type",
    "to",  # SendMessage: which session a run is talking to is the whole story
)


def _tool_detail(name: str, tool_input: object) -> str:
    """One line naming what this tool call is doing, e.g. ``Bash: git diff``."""
    if not isinstance(tool_input, dict):
        return name
    for key in _TOOL_DETAIL_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            detail = _short(value, 90)
            # Grep/Glob read as a pattern applied somewhere; the path is the
            # half that says which part of the tree is being looked at.
            where = tool_input.get("path") or tool_input.get("glob")
            if key == "pattern" and isinstance(where, str) and where.strip():
                detail = f"{detail} in {_short(where, 40)}"
            return f"{name}: {detail}"
    return name


def build_argv(command: str, ctx: dict) -> list[str]:
    """Split a command template into argv, then substitute placeholders into
    each token (never re-splitting substituted values).

    On Windows we split with ``posix=False`` so backslash paths survive, then
    strip the quotes shlex leaves on quoted tokens. Substituting after the
    split means an MR field can never inject extra args (run with shell=False).

    Argument-injection guard: if substitution makes a token *start* with ``-``
    when its template didn't (e.g. template ``tool {title}`` with a title of
    ``--upload-file``), the injected value would be read as a flag by the target
    tool. We refuse rather than smuggle a flag.
    """
    posix = os.name != "nt"
    tokens = shlex.split(command, posix=posix)
    argv = []
    for token in tokens:
        if not posix and len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
            token = token[1:-1]
        template_token = token
        for key in PLACEHOLDER_KEYS:
            # A snapshot column is nullable (an MR polled before `head_sha`
            # existed, an MR with no author), and `str(None)` is the four-letter
            # string "None" — which a skill would take for a real ref, branch or
            # URL. An absent value is an empty one.
            value = ctx.get(key)
            token = token.replace("{" + key + "}", "" if value is None else str(value))
        if token.startswith("-") and not template_token.startswith("-"):
            raise CommandError(
                "refusing to run: a substituted MR value would start with '-' and "
                f"be read as a flag (token {template_token!r} -> {token!r}). "
                "Embed the placeholder after a fixed prefix, e.g. --arg={placeholder}."
            )
        argv.append(token)
    return argv


def _positive_int(obj: dict, key: str) -> int:
    """One number out of a child-controlled object, or zero.

    Every shape in a stream-json event is the run's to choose and radar's to
    survive: a field that is missing, null, a string or negative reads as
    nothing rather than raising on the thread draining the pipe.
    """
    value = obj.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _positive_float(obj: dict, key: str) -> float:
    """As ``_positive_int``, for a value the run reports with decimals (money)."""
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if value > 0 else 0.0


def _text(obj: dict, key: str, limit: int = 60) -> str:
    """A short string out of a child-controlled object, or empty."""
    value = obj.get(key)
    return _short(value, limit) if isinstance(value, str) and value.strip() else ""


def _bump(counter: dict, key: str, by: int = 1) -> None:
    """Add to a counter keyed by something the run named (a tool, a subagent)."""
    if by:
        counter[key] = counter.get(key, 0) + by


@dataclass
class RunStats:
    """What one run spent and did, as the run itself reported it.

    Tokens have two sources, and the second one wins. While the run is in flight
    radar counts what each request carries: an ``assistant`` event names the
    message it belongs to, and the input side of a message's usage is settled
    before generation starts, so the input columns are exact from the first
    turn. The output side is not — the usage arrives with the turn's first
    content block, long before the turn has finished writing, and reads 16 or 3
    tokens against a real 479 — so radar never adds those up. Thinking meanwhile
    comes from the CLI's own running estimate (a ``system``/``thinking_tokens``
    event).

    When the run reports its ``result`` it publishes the totals it was billed
    for — thinking, money, per-model breakdown and all — and those replace
    everything counted until then (``billed``, ``thinking_billed``). So the
    panel's numbers grow while a run works and are settled the moment it
    finishes, without ever mixing an estimate into a billed total.

    The rest is what the same events say about the run rather than its bill:
    what it was allowed to do and what it was refused, the subagents it started,
    how much of its context window it reached, how much of the wall clock was
    the model rather than tools, and where the account's rate limits stood. A
    command that speaks no stream-json reports none of it, which is what
    ``measured`` is for: an unmeasured run gets no numbers on its panel rather
    than a row of confident zeros.
    """

    # --- what answered -----------------------------------------------------
    model: str = ""
    # Per model, for a run whose subagents answer on a cheaper one than its main
    # loop: name -> {input, output, cache_read, cache_write, thinking, cost_usd}.
    models: dict = field(default_factory=dict)
    cli_version: str = ""
    permission_mode: str = ""
    output_style: str = ""
    service_tier: str = ""
    speed: str = ""            # "standard" or "fast" (fast mode is priced apart)
    inference_geo: str = ""
    tools_offered: int = 0     # how many tools the session was given
    mcp_servers: int = 0

    # --- tokens ------------------------------------------------------------
    turns: int = 0                 # requests to the model, one per assistant message
    # How many of those requests came with token counts attached. Fewer than
    # `turns` means the provider is not reporting usage for some of them, and
    # the totals below are only what it did report — which is worth knowing
    # before reading an input figure of zero as "this run read nothing".
    requests_with_usage: int = 0
    input_tokens: int = 0          # fresh input — everything not served from cache
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_write_5m_tokens: int = 0  # the two cache TTLs are priced differently
    cache_write_1h_tokens: int = 0
    output_tokens: int = 0         # only known once the run reports its result
    thinking_tokens: int = 0       # part of the output; an estimate until billed
    billed: bool = False           # the totals are the run's own, not radar's count
    thinking_billed: bool = False
    # As `thinking_billed`, for the subagent counts: set once a result has
    # actually reported a tally of them, so a result that reports none is read
    # as a CLI that does not break them out rather than a run that started none.
    subagents_billed: bool = False

    # Radar's own: the largest single request this run made, against the window
    # it had. How close a skill came to running out of room, which no single
    # total says — a run can spend millions of tokens 50k at a time.
    peak_context_tokens: int = 0
    context_window: int = 0
    max_output_tokens: int = 0

    # --- money -------------------------------------------------------------
    cost_usd: float = 0.0

    # --- time --------------------------------------------------------------
    api_ms: int = 0            # time the run spent waiting on the model
    wall_ms: int = 0           # how long the run took, as the CLI clocked it
    first_token_ms: int = 0    # from launch to the first token of the answer
    reported_turns: int = 0    # the run's own turn count (``num_turns``)
    queued_turns: int = 0
    # Radar's own measure of how long the model takes to start answering, summed
    # over the turns it could time: the live stand-in for api_ms/reported_turns,
    # which only arrives with the result (see ``CommandRunner._count_request``).
    latency_s: float = 0.0
    latency_turns: int = 0

    # --- what it did -------------------------------------------------------
    tool_calls: int = 0
    tools: dict = field(default_factory=dict)   # tool name -> calls
    web_searches: int = 0                       # server-side tools, billed apart
    web_fetches: int = 0
    subagents_spawned: int = 0
    subagents_completed: int = 0
    subagents_failed: int = 0
    subagents_killed: int = 0
    subagents_refused: int = 0                  # depth, concurrency or budget
    subagent_depth: int = 0
    subagent_types: dict = field(default_factory=dict)

    # --- how it ended, and what it was refused -----------------------------
    # Radar launches skills with `--permission-mode dontAsk`, where anything off
    # the allowlist is denied without asking and the run carries on regardless.
    # A review that could not read the file it wanted still writes an answer, so
    # this is the difference between a finding and a guess.
    denials: dict = field(default_factory=dict)  # tool name -> denied calls
    errors: int = 0                              # results the run flagged as errors
    outcome: str = ""                            # the run's own terminal_reason
    stop_reason: str = ""

    # --- the account's rate limits, as of the last the run heard -----------
    rate_limits: dict = field(default_factory=dict)  # window -> utilisation, 0..1
    rate_limit_status: str = ""

    # --- the shape of the run, request by request --------------------------
    # One entry per request: when it was made (seconds into the run), how long
    # the model took to start answering, how much context it carried, and what
    # came of it (tool calls, thinking). Totals cannot show a run whose waits
    # are climbing or whose context doubled halfway through and stayed there —
    # this is what the panel's charts are drawn from.
    samples: list = field(default_factory=list)
    label: str = ""   # whose timeline it is, for a pipeline's shared axis
    # Per step, once a pipeline adds its steps up: [{"label", "samples"}].
    series: list = field(default_factory=list)
    # What each step of a pipeline cost and how long it took. The totals above
    # are the steps added together, and added together they no longer say that
    # the synthesis took two minutes and the QA plan twenty-four — which is the
    # first thing anyone asks of a run that took half an hour. Kept per step so
    # a result re-opened next week shows the same breakdown the panel did while
    # it ran. One record per step: `name`, `label`, `status`, `elapsed_s`,
    # `session_id`, and `stats`, that step's own numbers without its timeline
    # (the charts read that from `series`, and storing it twice doubles the row).
    steps: list = field(default_factory=list)

    def timelines(self) -> list:
        """Every timeline this covers — a pipeline's steps, or just its own.

        Checked on the way out rather than taken on trust. These come back from
        a stored row too, which another version of radar may have written in
        another shape (see ``stats_from_json``) — and a series whose samples
        are not samples has to leave the charts blank, not take the panel down
        with it. A row that cannot be read is simply not a line.
        """
        lines = self.series or [{"label": self.label, "samples": self.samples}]
        out = []
        for line in lines:
            if not isinstance(line, dict):
                continue
            samples = [s for s in line.get("samples") or () if isinstance(s, dict)]
            if samples:
                out.append({"label": line.get("label") or "", "samples": samples})
        return out

    @property
    def turn_count(self) -> int:
        """How many times the model was asked.

        Radar's own count, because it is the one that is live and the one that
        adds up: a pipeline with one step finished and one still working would
        otherwise report only the finished step's ``num_turns``. The run's own
        count stands in for it if radar saw no assistant events at all.
        """
        return self.turns or self.reported_turns

    @property
    def total_tokens(self) -> int:
        """Everything that went through the model, cache included."""
        return (
            self.input_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
            + self.output_tokens
        )

    @property
    def avg_response_s(self) -> float | None:
        """Average time one request took the model, or None if nothing timed it.

        The run's own ``duration_api_ms`` over its own turn count when it has
        reported both — that is the model's time and nothing else. Until then,
        radar's measure: tool results going back, to the next answer starting,
        which is the same wait with radar's own overhead inside it.
        """
        if self.api_ms and self.reported_turns:
            return self.api_ms / 1000 / self.reported_turns
        if self.latency_turns:
            return self.latency_s / self.latency_turns
        return None

    @property
    def model_share(self) -> float | None:
        """What fraction of the run was the model thinking, 0..1.

        The rest was radar's own preparation and the skill's tools — a review
        that spends four minutes of five running greps is a review to give
        better context, not a slower model.
        """
        if not self.wall_ms or not self.api_ms:
            return None
        return min(1.0, self.api_ms / self.wall_ms)

    @property
    def cache_hit_rate(self) -> float | None:
        """What fraction of the input the cache served, 0..1."""
        total = self.input_tokens + self.cache_read_tokens + self.cache_write_tokens
        return self.cache_read_tokens / total if total else None

    @property
    def context_share(self) -> float | None:
        """How close the biggest request came to filling the window, 0..1."""
        if not self.peak_context_tokens or not self.context_window:
            return None
        return min(1.0, self.peak_context_tokens / self.context_window)

    @property
    def denied_calls(self) -> int:
        return sum(self.denials.values())

    @property
    def measured(self) -> bool:
        """Whether this run reported anything worth showing."""
        return bool(
            self.turn_count
            or self.total_tokens
            or self.thinking_tokens
            or self.tool_calls
            or self.cost_usd
        )


# How a pipeline's total is made from its steps' numbers. Spelled out per field
# rather than guessed, because the right answer differs: tokens and money add
# up, a context peak is the worst any one step reached (they do not share a
# window), and a name is a name.
_SUM_FIELDS = (
    "turns", "requests_with_usage", "input_tokens", "cache_read_tokens", "cache_write_tokens",
    "cache_write_5m_tokens", "cache_write_1h_tokens", "output_tokens",
    "thinking_tokens", "cost_usd", "api_ms", "wall_ms", "reported_turns",
    "queued_turns", "latency_s", "latency_turns", "tool_calls", "web_searches",
    "web_fetches", "subagents_spawned", "subagents_completed", "subagents_failed",
    "subagents_killed", "subagents_refused", "errors", "tools_offered",
    "mcp_servers",
)
_MAX_FIELDS = (
    "peak_context_tokens", "context_window", "max_output_tokens",
    "first_token_ms", "subagent_depth",
)
_COUNTER_FIELDS = ("tools", "subagent_types", "denials")
_TEXT_FIELDS = (
    "model", "cli_version", "permission_mode", "output_style", "service_tier",
    "speed", "inference_geo", "outcome", "stop_reason", "rate_limit_status",
)


def aggregate_stats(parts: Iterable[RunStats]) -> RunStats:
    """One pipeline's numbers: its steps', added up.

    ``api_ms`` and ``wall_ms`` sum even across steps that ran at once — they are
    time spent, not time elapsed, and the panel already says how long each step
    took. A total is ``billed`` only when every step that has numbers has
    published its own, so a half-finished pipeline never claims a billed figure
    it is still estimating part of.
    """
    total = RunStats()
    measured: list[RunStats] = []
    texts: dict[str, list[str]] = {name: [] for name in _TEXT_FIELDS}
    for part in parts:
        for name in _SUM_FIELDS:
            setattr(total, name, getattr(total, name) + getattr(part, name))
        for name in _MAX_FIELDS:
            setattr(total, name, max(getattr(total, name), getattr(part, name)))
        for name in _COUNTER_FIELDS:
            counter = getattr(total, name)
            for key, count in getattr(part, name).items():
                _bump(counter, key, count)
        for name, value in part.models.items():
            into = total.models.setdefault(name, {})
            for key, number in value.items():
                if isinstance(number, (int, float)) and not isinstance(number, bool):
                    into[key] = into.get(key, 0) + number
        for window, used in part.rate_limits.items():
            # The tightest any step saw: what is left is what the next run gets.
            total.rate_limits[window] = max(total.rate_limits.get(window, 0.0), used)
        for name in _TEXT_FIELDS:
            value = getattr(part, name)
            if value and value not in texts[name]:
                texts[name].append(value)
        for line in part.timelines():
            # Kept apart rather than merged: two steps of a stage run at once,
            # and a single line stitched from both would zigzag between two
            # runs' waits and mean nothing. One line per step, one axis.
            total.series.append(line)
        if part.measured:
            measured.append(part)
    for name, values in texts.items():
        setattr(total, name, ", ".join(values))
    total.billed = bool(measured) and all(part.billed for part in measured)
    # Only over the steps the flag is about. `all()` of nothing is True, and a
    # total with no thinking in it at all would otherwise claim to be a billed
    # figure — a claim about a number that does not exist.
    for flag, counted in (("thinking_billed", "thinking_tokens"),
                          ("subagents_billed", "subagents_spawned")):
        counting = [part for part in measured if getattr(part, counted)]
        setattr(total, flag,
                bool(counting) and all(getattr(part, flag) for part in counting))
    return total


def _take_billed_thinking(stats: RunStats, billed: int) -> None:
    """Replace the live estimate with the billed count — unless the billed
    count is zero and the run plainly did think.

    A provider that does not report thinking sends a zero rather than nothing:
    one gateway streamed fifty thousand reasoning tokens and then reported
    ``thinkingTokens: 0``. Taking that literally would wipe the only figure
    radar had and claim the model never reasoned, so a zero is only believed
    when nothing was seen streaming either.
    """
    if not billed and stats.thinking_tokens:
        return
    if not stats.thinking_billed:
        stats.thinking_billed = True
        stats.thinking_tokens = 0
    stats.thinking_tokens += billed


def stats_to_json(stats: RunStats) -> str:
    """The numbers, for a result that outlives the job that produced it."""
    return json.dumps(asdict(stats))


def stats_to_record(stats: RunStats) -> dict:
    """One step's numbers, for nesting inside a pipeline's own (``steps``).

    Only what was measured: a field still at its default is dropped, because
    `stats_from_mapping` fills it back in from the same default and a stored
    row is easier to read without forty zeroes per step. The timeline goes too
    — the charts read it from `series`, and a second copy would double the row
    for a picture already drawn.
    """
    reference = RunStats()
    return {
        name: value
        for name, value in asdict(replace(stats, samples=[], series=[], steps=[])).items()
        if value != getattr(reference, name)
    }


def stats_from_json(text: str | None) -> RunStats:
    """Stored numbers, read back defensively.

    A row written by another version of radar may be missing a field or carry
    one this version no longer has, and a re-opened result must render either
    way: anything that does not match the field it claims to be is dropped.
    """
    try:
        data = json.loads(text) if text else None
    except (ValueError, TypeError):
        data = None
    return stats_from_mapping(data)


def _is_number(value: object) -> bool:
    """A number to do arithmetic on — and not a bool, which is one in Python."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _mapping_ok(name: str, value: object) -> bool:
    """Whether a stored mapping is one this version can add up.

    The keys are names the run chose — a tool, a model, a rate-limit window —
    and every value is a number, except ``models``, whose values are a mapping
    of numbers each. Checking the keys alone is not enough: a value of the
    wrong type survives the read and raises later, in the panel, where the
    stack trace is about rounding a string rather than about a row this version
    cannot read. Dropped whole rather than value by value — half a tally is a
    number nobody can interpret.
    """
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return False
    if name == "models":
        return all(
            isinstance(inner, dict)
            and all(isinstance(key, str) and _is_number(n) for key, n in inner.items())
            for inner in value.values()
        )
    return all(_is_number(number) for number in value.values())


def stats_from_mapping(data: object) -> RunStats:
    """As ``stats_from_json``, for numbers already parsed.

    Split out for the per-step records inside a stored total (``RunStats.steps``),
    which arrive as dicts nested in the same document: re-serialising one just to
    read it back through the same checks would be a round trip to nowhere.
    """
    if not isinstance(data, dict):
        return RunStats()
    reference = RunStats()

    kept = {}
    for spec in fields(RunStats):
        value = data.get(spec.name)
        default = getattr(reference, spec.name)
        if isinstance(default, bool):
            ok = isinstance(value, bool)
        elif isinstance(default, int):
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif isinstance(default, float):
            ok = _is_number(value)
        elif isinstance(default, dict):
            ok = _mapping_ok(spec.name, value)
        elif isinstance(default, list):
            # A run's timeline, stored so a re-opened result still draws its
            # charts. Entries are plain records of numbers; anything else in
            # there was not written by this.
            ok = isinstance(value, list) and all(isinstance(item, dict) for item in value)
        else:
            ok = isinstance(value, str)
        if ok:
            kept[spec.name] = value
    return RunStats(**kept)


@dataclass
class CommandJob:
    id: str
    kind: str  # the skill's name
    # A job is about a merge request or about a Jenkins build, never both, so
    # the coordinates of the other one are absent rather than zero.
    project_id: int | None = None
    mr_iid: int | None = None
    subject: str = ""  # "!123" or "backend-ci #128" — what the panel heads with
    title: str = ""
    status: str = "running"  # running / done / error
    output: str = ""
    error: str = ""
    persist_error: str = ""  # set if the result was produced but couldn't be saved
    returncode: int | None = None
    progress: list[dict] = field(default_factory=list)  # live log: {kind, text}
    # The panel's countdown, measured on the same clock the worker enforces the
    # budget with. Wall clock would drift from it across a host suspend or an
    # NTP step and show time the run does not actually have.
    started_mono: float = 0.0
    budget_s: int = 0
    # Progress items carry a stable `id` (identity, so the browser can update a
    # line it already drew) and a `rev` from this counter (bumped on append AND
    # on a collapse, so "what changed since?" has one answer covering both).
    progress_next_id: int = 0
    progress_rev: int = 0
    # Where the child runs. Per job, not per skill: the source root is resolved
    # from the MR's project, so two projects reviewed by one skill run in their
    # own checkouts (see `CommandRunner.start`).
    cwd: str | None = None
    # What the panel needs to tell a working run from a stuck one (see
    # `job_health`): the Claude session to resume for the whole conversation,
    # when the run last did something other than wait, how many waits since
    # then, and the latest of them.
    session_id: str = ""
    last_work_mono: float = 0.0
    waits_since_work: int = 0
    waiting_on: str = ""
    ended_mono: float = 0.0
    # Set from the panel; only the worker that owns the process acts on it.
    stop_requested: threading.Event = field(default_factory=threading.Event)
    # A pipeline's steps as they start, by step name (see `pipeline`).
    steps: dict[str, CommandJob] = field(default_factory=dict)
    # What earlier attempts of a step spent, kept when a retry replaces the
    # step's job. The money was spent either way, and a pipeline total that
    # went *down* when a step was run again would be the one number here that
    # nobody could trust (see `pipeline.PipelineRunner._roll_up`).
    #
    # A record per superseded attempt, shaped like the ones in `RunStats.steps`
    # — `name`, `label`, `status`, `elapsed_s`, `session_id` and `stats` (a
    # `RunStats`) — so it can be shown as a line of its own beside the attempt
    # that replaced it. The total counts it, so the breakdown has to as well:
    # rows that add up to less than the total they sit under are worse than no
    # rows, because the missing money has nowhere to be explained.
    retried_spend: list = field(default_factory=list)

    # What a retry of one step would need: the context and callbacks the job
    # was started with. A pipeline fills it (see `pipeline.retry_step`); a
    # plain skill leaves it empty, because its board button starts a new job.
    retry_with: dict = field(default_factory=dict)
    # Where this run's raw stream was kept, when it was (see `_open_capture`).
    # On the job rather than in the stats: it is a fact about this process on
    # this machine, not a measurement to store with the result.
    capture_path: str = ""
    # What this run spent. Written only by the thread draining the child's
    # stdout, read by whichever request is rendering the panel — so a reader
    # can catch it mid-turn and see one counter ahead of another, never a
    # torn number. A pipeline's own is its steps' added up (see `pipeline`).
    stats: RunStats = field(default_factory=RunStats)


def _fail(job: CommandJob, message: str) -> None:
    """Move a job to a terminal error state (error text set before status)."""
    job.error = message[:8000]
    job.status = "error"


# Tools a run calls to wait for something rather than to do anything: polling
# other sessions or background tasks. A run that calls nothing else for long
# enough is stuck — the case this exists for is a review that messaged a
# sibling pipeline step and then polled it for half an hour.
_WAIT_TOOLS = frozenset({"ListAgents", "SendMessage", "TaskOutput", "Monitor"})
_SLEEP_RE = re.compile(r"^\s*sleep\s+\d")

# A run is stalled after this long with nothing but waiting, and at least this
# many waits: one long sleep before a check is patience, not a loop.
STALL_AFTER_S = 300
_STALL_MIN_WAITS = 3


def is_wait(name: str, tool_input: object) -> bool:
    """Whether a tool call waits for something instead of doing work."""
    if name in _WAIT_TOOLS:
        return True
    return (
        name == "Bash"
        and isinstance(tool_input, dict)
        and bool(_SLEEP_RE.match(str(tool_input.get("command") or "")))
    )


def request_stop(job: CommandJob) -> bool:
    """Ask the worker running ``job`` to stop it; False if it has already ended."""
    if job.status != "running":
        return False
    job.stop_requested.set()
    return True


def job_health(job: CommandJob, now: float | None = None) -> dict:
    """How a job is doing, as numbers the panel phrases (see ``web.app``)."""
    now = time.monotonic() if now is None else now
    running = job.status == "running"
    idle = max(0.0, now - (job.last_work_mono or job.started_mono)) if running else 0.0
    return {
        "status": job.status,
        "elapsed_s": int(max(0.0, (job.ended_mono or now) - job.started_mono)),
        "idle_s": int(idle),
        "stalled": running and job.waits_since_work >= _STALL_MIN_WAITS and idle >= STALL_AFTER_S,
        "waiting_on": job.waiting_on if job.waits_since_work else "",
        "session_id": job.session_id,
        "last_line": job.progress[-1]["text"] if job.progress else "",
    }


def _with_deadline(fn: Callable[[], object], seconds: float, what: str) -> object:
    """Run ``fn`` on a helper thread and give up on it after ``seconds``.

    Preparing a job means calling out to GitLab or Jira, and a socket with no
    answer coming back has no timeout of its own — the job would sit in
    "running" forever and the panel would tail it just as long. There is no way
    to interrupt a blocking read from outside, so the thread is abandoned
    (daemon, so it cannot hold up shutdown) and the job is failed. The work it
    was doing is a read; nothing is left half-written.
    """
    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=max(0.0, seconds))
    if worker.is_alive():
        raise TimeoutError(what)
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box.get("value")


# Set to a directory and every run writes its raw event stream there, one file
# per job, each line `{"t": <seconds into the run>, "event": {...}}`. Off by
# default: this is a forensic tool, not a log. It exists because a run's numbers
# can only say *that* a provider reported no tokens or answered slowly, and the
# next question is always what it actually sent — which radar parses and throws
# away. Arrival times are radar's own, which is the half no event carries.
_CAPTURE_ENV = "RADAR_CAPTURE_STREAM"

# Where `radar serve --capture` puts them when it is given no directory of its
# own. Beside the database rather than in a temp dir: these are files someone
# reads a day later, or sends to whoever runs the gateway, and a path that the
# operating system empties overnight is not that.
DEFAULT_CAPTURE_DIR = "radar-streams"


def _open_capture(job: CommandJob) -> object | None:
    """The file this run's raw stream is copied to, or None if unasked for."""
    directory = os.environ.get(_CAPTURE_ENV, "").strip()
    if not directory:
        return None
    try:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{job.kind}-{job.id}.jsonl")
        handle = open(path, "w", encoding="utf-8", errors="replace")  # noqa: SIM115
        job.capture_path = os.path.abspath(path)
        return handle
    except OSError as exc:
        log.warning("%s: cannot capture the stream: %s", job.kind, exc)
        return None


def _capture(handle, job: CommandJob, line: str) -> None:
    """One raw line, stamped with when it arrived. Never fails a run: a full
    disk costs the evidence, not the review."""
    try:
        text = line.strip()
        try:
            record = {"t": round(time.monotonic() - job.started_mono, 3),
                      "event": json.loads(text)}
        except (ValueError, TypeError):
            record = {"t": round(time.monotonic() - job.started_mono, 3), "raw": text}
        handle.write(json.dumps(record) + "\n")
        handle.flush()   # a run that hangs is exactly when the file is read
    except (OSError, ValueError, TypeError) as exc:  # pragma: no cover - defensive
        log.warning("%s: cannot write the captured stream: %s", job.kind, exc)


def _feed_stdin(proc, text: str) -> None:
    try:
        proc.stdin.write(text)
        proc.stdin.close()
    except (OSError, ValueError):  # child exited / pipe closed early
        pass


def _short(text: str, limit: int = 140) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class CommandRunner:
    """Owns background command jobs for the process lifetime (in-memory)."""

    def __init__(self, config: CommandConfig, kind: str):
        self.config = config
        self.kind = kind
        self._jobs: dict[str, CommandJob] = {}
        self._lock = threading.Lock()

    @property
    def checkout(self) -> str:
        """"none" (run in the configured source) or "worktree" (one per job)."""
        return getattr(self.config, "checkout", "none")

    def _admit(self, ctx: dict) -> CommandJob:
        """A new running job for this context, registered so the panel finds it."""
        project_id = ctx.get("project_id")
        mr_iid = ctx.get("mr_iid")
        job = CommandJob(
            id=uuid.uuid4().hex[:12],
            kind=self.kind,
            project_id=int(project_id) if project_id not in (None, "") else None,
            mr_iid=int(mr_iid) if mr_iid not in (None, "") else None,
            subject=str(ctx.get("subject", "")),
            title=str(ctx.get("title", "")),
            started_mono=time.monotonic(),
            budget_s=self.config.timeout_seconds,
        )
        # Whose timeline this is, for a pipeline drawing its steps on one axis.
        job.stats.label = self.config.label or self.kind
        with self._lock:
            self._jobs[job.id] = job
            while len(self._jobs) > _MAX_JOBS:  # evict oldest so serve doesn't leak
                self._jobs.pop(next(iter(self._jobs)))
        return job

    def start(
        self,
        ctx: dict,
        on_success: Callable[[CommandJob], None] | None = None,
        stdin_provider: Callable[[str], str] | None = None,
    ) -> CommandJob:
        job = self._admit(ctx)
        try:
            # Resolve the skill's declared context first: a required var that is
            # unset, or a source root that is not a directory, refuses the job
            # here rather than launching an agent that would review nothing.
            # Cheap (env reads), so it stays on the request thread and the button
            # reports a misconfiguration immediately.
            # A build analysis has no GitLab project, so a `source:` mapping keyed
            # by one cannot match; such a skill declares a `default:` or no
            # source at all, and resolve_source falls back to it.
            resolved = job_context(
                self.config, job.project_id if job.project_id is not None else "",
                str(ctx.get("web_url") or ""),
            )
            resolved.raise_for_problems()
            if self.checkout == "worktree" and not resolved.source_root:
                raise SkillContextError(
                    "checkout: worktree needs a 'source:' that resolves to this project's "
                    "checkout — there is nothing to make a worktree of"
                )
        except SkillContextError as exc:
            job.status, job.error = "error", str(exc)
            return job
        threading.Thread(
            target=self._run,
            args=(job, ctx, resolved, on_success, stdin_provider),
            daemon=True,
        ).start()
        return job

    def get(self, job_id: str) -> CommandJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def finished_for(self, project_id: int, mr_iid: int) -> CommandJob | None:
        """The most recent run of this skill for one merge request that worked.

        For re-opening a saved answer: the row in the database is the answer,
        but the job that produced it is the only thing that still knows which
        step wrote what, and the only thing a step can be run again from. While
        this process is alive, that job is right here — so a saved result can be
        shown as the panel that produced it rather than as text with no history.

        Newest first, and only a run that finished successfully: a later run
        that failed did not replace what was saved, so its panel is not the
        saved answer. Insertion order is age order (see ``_admit``), so the
        scan runs backwards.
        """
        with self._lock:
            jobs = list(self._jobs.values())
        for job in reversed(jobs):
            if (job.project_id == project_id and job.mr_iid == mr_iid
                    and job.status == "done"):
                return job
        return None

    def progress_since(self, job_id: str, after_rev: int) -> tuple[list[dict], str] | None:
        """Progress items changed since ``after_rev``, plus the job's status, or
        None if the job is unknown.

        Filtering on the revision rather than on the id covers both things that
        can happen to the log: a new line appended, and an existing line's count
        going up when its event repeats. A reader tracking ids alone would never
        learn about the second, and the line it drew would sit there stale.

        ``id`` identifies the line across those updates — the log is capped by
        dropping from the front, so list positions shift and ids don't. The
        bookkeeping a collapse needs stays server-side; a reader gets only what
        it draws.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            items = [
                {"id": item["id"], "rev": item["rev"], "kind": item["kind"], "text": item["text"]}
                for item in job.progress
                if item["rev"] > after_rev
            ]
            return items, job.status

    def _add(self, job: CommandJob, kind: str, text: str) -> None:
        with self._lock:
            # Collapse an immediate repeat into a count on the line already
            # there. A run that reads twenty files says twenty different things
            # now, so a true repeat is a loop worth seeing as one line rather
            # than as twenty identical ones scrolling the rest off the panel.
            job.progress_rev += 1
            last = job.progress[-1] if job.progress else None
            if last is not None and last["kind"] == kind and last["base"] == text:
                last["count"] += 1
                last["text"] = f"{text} (×{last['count']})"
                last["rev"] = job.progress_rev
                return
            job.progress.append({
                "id": job.progress_next_id,
                "rev": job.progress_rev,
                "kind": kind,
                "text": text,
                "base": text,   # what to compare a repeat against
                "count": 1,
            })
            job.progress_next_id += 1
            overflow = len(job.progress) - _MAX_PROGRESS
            if overflow > 0:
                del job.progress[:overflow]

    # --- execution ---------------------------------------------------------

    def stop(self, job_id: str, step: str | None = None) -> bool:
        """Stop a running job from the panel; False if it is unknown or ended.

        Only asks: the worker that owns the process does the kill (see
        ``_execute``), so a stop never races the reap of the child it targets.
        ``step`` names a pipeline's step, and a plain skill has none.
        """
        job = self.get(job_id)
        return job is not None and step is None and request_stop(job)

    def _run(self, job: CommandJob, ctx: dict, resolved, on_success, stdin_provider=None) -> None:
        # Catch-all guarantees a terminal state; a worker crash must never leave
        # the job "running" (the UI would tail it forever).
        #
        # `timeout_seconds` is the budget for the whole job, not just for the
        # command: fetching a checkout and fetching the MR's context happen
        # before the command starts, and both talk to the network. Bounding only
        # the child would leave the two phases most likely to hang unbounded.
        deadline = time.monotonic() + self.config.timeout_seconds
        worktree = None
        scratch = ""
        try:
            # Somewhere for this run's evidence to live. A build log runs to tens
            # of megabytes: it is handed over as a file the skill can search, not
            # as prompt text, and it lives exactly as long as the run that reads
            # it — the `finally` below owns its removal the way it owns the
            # worktree's. Inside the try, because this function is a thread
            # target with nothing above it: a mkdtemp that raises (no TMPDIR, a
            # full disk) would kill the worker before the catch-all runs and
            # leave the job "running" for a panel that tails it forever.
            scratch = tempfile.mkdtemp(prefix="radar-job-")
            if self.checkout == "worktree":
                self._add(job, "log", "preparing this merge request's worktree…")
                worktree = create_mr_worktree(
                    resolved.source_root,
                    job.mr_iid,
                    str(ctx.get("head_sha") or "") or None,
                    remote=getattr(self.config, "remote", "origin"),
                    timeout_s=deadline - time.monotonic(),
                )
            source_root = str(worktree.path) if worktree else (resolved.source_root or "")
            self._execute(
                job, ctx, source_root, resolved, deadline, on_success, stdin_provider, scratch
            )
        except WorktreeError as exc:
            _fail(job, str(exc))
        except TimeoutError as exc:
            _fail(job, f"{self.kind} timed out after {self.config.timeout_seconds}s ({exc})")
        except Exception as exc:  # noqa: BLE001 - last-resort terminal state
            log.exception("%s worker crashed", self.kind)
            _fail(job, f"unexpected error: {exc}")
        finally:
            # The panel's elapsed time stops here rather than counting on.
            job.ended_mono = time.monotonic()
            if scratch:
                shutil.rmtree(scratch, ignore_errors=True)
            if worktree is not None:
                worktree.cleanup()

    def _execute(
        self,
        job: CommandJob,
        ctx: dict,
        source_root: str,
        resolved,
        deadline: float,
        on_success,
        stdin_provider=None,
        scratch: str = "",
    ) -> None:
        # Built here, not in `start`, because a worktree's path is only known
        # once the worker has made it — and `{source_root}` must name the tree
        # the skill will actually read.
        try:
            argv = build_argv(self.config.command, {**ctx, "source_root": source_root})
        except CommandError as exc:
            _fail(job, str(exc))
            return
        if not argv:
            _fail(job, f"{self.kind}.command is empty")
            return
        # An explicit working_dir wins; otherwise the checkout is the natural
        # place to run, so the agent's own file tools land on the right tree.
        job.cwd = self.config.working_dir or source_root or None

        # Fetch backend context (MR diff / Jira ticket) to pipe on stdin. Runs in
        # this worker thread; a failure here surfaces as a job error.
        stdin_text: str | None = None
        if stdin_provider is not None:
            self._add(job, "log", "fetching context…")
            stdin_text = self._fetch_context(
                job, lambda: stdin_provider(source_root, resolved.inputs.shown, scratch),
                deadline,
            )

        try:
            proc = subprocess.Popen(
                argv,
                cwd=job.cwd,
                stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",   # decode as UTF-8 regardless of OS locale
                errors="replace",
                bufsize=1,          # line-buffered, for live streaming
                env=self._child_env(),
                # Group leader on POSIX so a timeout can kill the whole tree
                # (see _kill_tree); Windows gets the tree via taskkill instead.
                start_new_session=(os.name != "nt"),
            )
        except FileNotFoundError:
            _fail(job, f"command not found: {argv[0]!r} (is it on PATH?)")
            return
        except OSError as exc:  # pragma: no cover - defensive
            _fail(job, f"failed to launch {self.kind}: {exc}")
            return

        # With start_new_session the child leads a new group whose id is its pid.
        # Captured now, while the pid is certainly still the child's.
        pgid = proc.pid if os.name != "nt" else None
        with _LIVE_LOCK:
            _LIVE[proc.pid] = (proc, pgid)

        if stdin_text is not None:
            # Write on a thread so a large bundle can't deadlock against stdout.
            threading.Thread(
                target=_feed_stdin, args=(proc, stdin_text), daemon=True
            ).start()

        result_parts: list[str] = []  # final answer from stream-json 'result'
        raw_parts: list[str] = []     # accumulated plain-text output
        stats: dict = {}              # facts the drains gleaned about the run
        capture = _open_capture(job)
        if capture is not None:
            self._add(job, "log", f"capturing this run's raw stream to {capture.name}")
        stderr_box: list[str] = []
        # Both pipes are drained on their own threads. EOF needs every write end
        # closed, and anything the child spawned that inherited a pipe can hold
        # it open past the child's death — so the worker waits for the CHILD,
        # which is the event the job actually depends on, and never blocks on a
        # read that a survivor could stall forever.
        drains = [
            threading.Thread(
                target=lambda: self._drain(
                    job, proc.stdout, stats,
                    lambda line: self._consume(
                        job, line, result_parts, raw_parts, stats, capture
                    ),
                ),
                daemon=True,
            ),
            threading.Thread(
                target=lambda: self._drain(job, proc.stderr, stats, stderr_box.append),
                daemon=True,
            ),
        ]
        for drain in drains:
            drain.start()

        # What is left of the job's budget after preparing — never less than a
        # second, so a command that only just made the deadline still gets to
        # report something rather than being killed on the starting line.
        #
        # The wait and the kill live on this one thread: a timer thread killing
        # by pid races the reap here, and could signal a pid the OS had already
        # handed to somebody else. Here the child is provably unreaped when the
        # kill goes out, so its group id is still its own.
        remaining = max(1.0, deadline - time.monotonic())
        wait_until = time.monotonic() + remaining
        timed_out = stopped = False
        # Waited in slices of a second so a stop from the panel is acted on at
        # once. The request only sets a flag; the kill still happens here.
        while True:
            left = wait_until - time.monotonic()
            # poll() first: a child that exited just as the stop arrived is a
            # finished run, not a stopped one.
            if job.stop_requested.is_set() and proc.poll() is None:
                stopped = True
            elif left <= 0:
                timed_out = True
            else:
                try:
                    proc.wait(timeout=min(left, 1.0))
                    break
                except subprocess.TimeoutExpired:
                    continue
            # Kills the tree: with the wait ceiling defaulted to 0 the CLI never
            # reaps its own background agents, so this is the only thing that
            # stops one from running (and spending) past the job.
            _kill_tree(proc, pgid)
            proc.wait()
            break

        if _join_all(drains, _DRAIN_GRACE):
            # The child is gone but our pipes are still held: it left something
            # behind. That survivor is both the reason a read can't finish and
            # a process running on radar's behalf with nothing supervising it,
            # so take the tree down and collect what the drains can still read.
            # Safe to signal by group here — a live member keeps the id from
            # being reused, and a live member is exactly what we just proved.
            log.warning("%s left a process holding its pipes; stopping them", self.kind)
            _kill_tree(proc, pgid)
            still_held = _join_all(drains, _DRAIN_GRACE)
            if still_held:
                # Best effort ran out: a descendant that re-parented or put
                # itself in a new session is beyond both kill paths. Say so in
                # the log the panel tails rather than in the result, because the
                # output collected so far is usually the whole of what the child
                # wrote — the survivor is holding the pipe, not still filling it.
                self._add(job, "log", (
                    "a process this run left behind is still holding its output "
                    "pipe; anything it writes from here on is not captured"
                ))
        with _LIVE_LOCK:
            _LIVE.pop(proc.pid, None)
        if capture is not None:
            with contextlib.suppress(OSError):
                capture.close()
        # The last request never got its `result` event to close it — this run
        # was killed, or stopped from the panel. Its entry is the most
        # interesting one on the chart, being the request the run died on, so
        # it is closed here instead. A no-op for a run that ended properly.
        self._close_sample(job, stats)

        job.returncode = proc.returncode

        stderr_text = "".join(stderr_box)

        # Blank line between results: a run can report more than one, and gluing
        # them together swallows the heading or list the next one opens with.
        output = ("\n\n".join(result_parts) if result_parts else "".join(raw_parts))[:_MAX_OUTPUT]

        if stopped:
            # Kept, like a timeout's: what a stuck run wrote is the evidence of
            # where it got stuck.
            job.output = output
            ran = int(time.monotonic() - job.started_mono)
            _fail(job, f"{self.kind} was stopped from the panel after {ran}s")
            return

        if timed_out:
            # Keep what the run did manage to say. A long review that ran out of
            # clock is more use half-written than replaced by the word "timeout".
            job.output = output
            detail = f"{self.kind} timed out after {self.config.timeout_seconds}s"
            if stats.get("async_agent"):
                detail += (
                    " while a background agent it launched was still working "
                    "(the whole process tree was stopped). Raise timeout_seconds "
                    "to outlast the work, or let radar's default env keep "
                    "subagents inline."
                )
            _fail(job, detail)
            return

        # A drain that raised took the rest of that pipe with it, so whatever
        # was captured is a fragment of the run. Report the fault instead of
        # publishing the fragment as the result.
        if stats.get("drain_error"):
            job.output = output
            _fail(job, f"could not read the {self.kind}'s output: {stats['drain_error']}")
            return

        if proc.returncode == 0 and output.strip():
            if _BG_SWEPT_RE.search(stderr_text):
                # The CLI killed background agents it was still waiting for and
                # exited with a deferral note instead of the findings. Radar's
                # default ceiling (0 = wait forever) makes this impossible, so
                # something overrides it. Keep the note visible under the error,
                # but never store it as the result.
                job.output = output
                _fail(job, (
                    "the CLI stopped waiting for a background agent the skill "
                    "launched and exited before it reported — the output below "
                    "is the run's deferral note, not the findings. Something "
                    "overrides radar's default CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0 "
                    "(wait forever): check this skill's env: and env_unset:, the "
                    "shell radar runs in, and .env."
                ))
                return
            # Publish output BEFORE flipping status so a reader that sees "done"
            # always sees the output too.
            job.output = output
            if on_success is not None:
                try:
                    on_success(job)
                except Exception as exc:  # noqa: BLE001 - report, don't crash
                    log.exception("%s result produced but not saved", self.kind)
                    job.persist_error = f"result was generated but could not be saved: {exc}"
            job.status = "done"
        else:
            detail = stderr_text.strip() or output or f"exited with code {proc.returncode}"
            _fail(job, detail.strip())

    def _fetch_context(self, job: CommandJob, fetch: Callable[[], object], deadline: float):
        """Fetch the skill's stdin bundle, trying again if the network blinks.

        These fetches are idempotent reads — a merge request's diff, a Jira
        ticket, a build log — and the failure that costs most is the cheapest
        to survive: a connection reset a minute into a pipeline, after half an
        hour of reviews, on the step that merges them.

        Bounded on both sides. At most `_CONTEXT_ATTEMPTS` tries, and never
        past the job's own deadline: a run with thirty seconds left does not
        spend them sleeping between attempts. The last failure is raised as
        itself, so the panel reports the actual fault rather than "retried".
        """
        for attempt in range(1, _CONTEXT_ATTEMPTS + 1):
            left = deadline - time.monotonic()
            try:
                return _with_deadline(fetch, left, "fetching context")
            except SkillContextError:
                raise            # a misconfiguration; trying again changes nothing
            except Exception as exc:  # noqa: BLE001 - transient until proven otherwise
                # Measured again, not reused from before the attempt: a fetch
                # that failed slowly has already spent the budget this decision
                # is about. Sleeping on the stale reading is how a run overruns
                # its deadline and then reports the timeout that followed
                # instead of the connection reset that started it.
                left = deadline - time.monotonic()
                wait = _CONTEXT_RETRY_WAIT_S * attempt
                if attempt >= _CONTEXT_ATTEMPTS or left - wait <= 1.0:
                    raise

                log.warning("%s: context fetch failed (%s); retrying", self.kind, exc)
                self._add(job, "log", (
                    f"fetching context failed ({_short(str(exc), 90)}); "
                    f"trying again in {wait:.0f}s "
                    f"[{attempt} of {_CONTEXT_ATTEMPTS - 1} retries]"
                ))
                time.sleep(wait)
        raise RuntimeError("unreachable")  # pragma: no cover

    def _consume(self, job, line, result_parts, raw_parts, stats, capture) -> None:
        """One line of the child's stdout: kept, if asked, then read."""
        if capture is not None:
            _capture(capture, job, line)
        self._ingest(job, line, result_parts, raw_parts, stats)

    def _drain(self, job: CommandJob, pipe, stats: dict, consume: Callable[[str], object]) -> None:
        """Read one of the child's pipes to EOF, handing each line to ``consume``.

        Runs on its own thread, so an exception here would otherwise vanish and
        take the rest of the pipe with it — leaving a fragment of the run to be
        published as if it were the whole. Anything unexpected is recorded for
        the worker to report; a torn-down pipe (the normal end of a killed run)
        is not an error.
        """
        try:
            for line in pipe:
                consume(line)
        except (OSError, ValueError):  # pipe torn down under us after a kill
            pass
        except Exception as exc:  # noqa: BLE001 - surfaced by the worker
            log.exception("%s output reader failed", self.kind)
            stats.setdefault("drain_error", f"{type(exc).__name__}: {exc}")
        finally:
            # Whoever read the pipe closes it. The worker cannot: closing takes
            # the buffer's lock, which this thread holds while blocked on a read
            # a survivor is keeping open — the worker would block behind it for
            # exactly as long as it was trying to avoid waiting.
            try:
                pipe.close()
            except (OSError, ValueError):  # pragma: no cover - already closed
                pass

    def _ingest(
        self,
        job: CommandJob,
        line: str,
        result_parts: list[str],
        raw_parts: list[str],
        stats: dict,
    ) -> None:
        """Handle one line of the child's stdout: parse Claude stream-json into
        progress + final result, or treat it as plain output."""
        line = line.rstrip("\r\n")
        if not line.strip():
            return
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            raw_parts.append(line + "\n")
            self._add(job, "log", _short(line))
            return
        if not isinstance(obj, dict):
            raw_parts.append(line + "\n")
            return

        if obj.get("type") == "rate_limit_event":
            # Where the account stands. A skill that is about to be throttled is
            # not a skill that is about to fail, but it is the reason the next
            # review waits — and radar is often the thing spending the budget.
            self._count_rate_limit(job, obj)
            return

        # A subagent dispatched to the background: the findings then depend on
        # that agent finishing, which is worth naming if the budget cuts it short.
        if _async_agent_launched(obj):
            stats["async_agent"] = True

        event_type = obj.get("type")
        if event_type in ("assistant", "user"):
            # The bytes of the conversation itself. A provider that reports no
            # token counts still sends the turns, and their size is what the
            # next request has to carry — so this stands in for the prompt when
            # nothing else does (see `_stats_charts`). Thinking events are left
            # out: they are the model's output, not the conversation, and on a
            # reasoning model they outnumber everything else fifty to one.
            stats["bytes"] = stats.get("bytes", 0) + len(line)
        if event_type == "assistant":
            self._count_request(job, obj, stats)
            # A subagent's turns arrive on this same stream, tagged with the tool
            # call that started it; marked, so the log says who is acting.
            mark = "↳ " if obj.get("parent_tool_use_id") else ""
            for block in _content_blocks(obj):
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "tool_use":
                    name = block.get("name")
                    name = name if isinstance(name, str) and name else "tool"
                    tool_input = block.get("input")
                    detail = _tool_detail(name, tool_input)
                    # What the run actually did, as a tally: forty Reads and one
                    # Bash is a different review from the other way round.
                    job.stats.tool_calls += 1
                    _bump(job.stats.tools, _short(name, 40))
                    # Waiting and working look identical in a scrolling log; this
                    # is what lets the panel say which one a run is doing.
                    if is_wait(name, tool_input):
                        job.waits_since_work += 1
                        job.waiting_on = detail
                    else:
                        job.last_work_mono = time.monotonic()
                        job.waits_since_work, job.waiting_on = 0, ""
                    self._add(job, "tool", mark + detail)
                elif block_type == "text" and str(block.get("text", "")).strip():
                    self._add(job, "text", mark + _short(block["text"]))
        elif event_type == "user":
            # Tool results going back to the model: from here until the next
            # assistant event, the run is waiting on the model and nothing else,
            # which is the wait `RunStats.avg_response_s` reports while the run
            # is still going. Nothing is logged — the tool call itself already
            # was, and its result is the agent's business, not the panel's.
            stats["turn_started"] = time.monotonic()
        elif event_type == "result":
            self._close_sample(job, stats)   # the last request has now answered
            self._count_result(job, obj)
            res = obj.get("result")
            if isinstance(res, str):
                result_parts.append(res)
            if obj.get("is_error"):
                self._add(job, "log", "run reported an error")
        elif event_type == "system":
            # Only the one that means a session actually started says so. A run
            # emits other system events as it goes (context compaction, and
            # whatever a later CLI adds), and logging them all as "session
            # started" reads like the run keeps restarting. An event with no
            # subtype keeps the old wording rather than vanishing: a CLI that
            # stops labelling its init should still show a session line.
            subtype = obj.get("subtype")
            session = obj.get("session_id")
            if subtype == "init" and isinstance(session, str) and not job.session_id:
                job.session_id = session[:100]  # the panel's `claude --resume` handle
            if subtype == "init":
                # What this session is: the model whose prices the token counts
                # are in, the CLI that ran it, and the permission mode and tool
                # surface it was given — the things that decide what the numbers
                # below could have been, and none of which radar can infer from
                # its own config, since a skill may override any of them.
                self._count_session(job, obj)
                stats["turn_started"] = time.monotonic()  # the first answer's clock
            if not isinstance(subtype, str) or not subtype.strip() or subtype == "init":
                self._add(job, "log", "session started")
            elif subtype == "task_started":
                description = _short(str(obj.get("description") or "a subagent"), 90)
                stats.setdefault("tasks", {})[str(obj.get("task_id"))] = description
                where = " in the background" if obj.get("is_backgrounded") else ""
                self._add(job, "log", f"subagent started{where}: {description}")
                # Counted here as well as from the result's own tally, so a run
                # that is stopped or times out still says how much of its work
                # it had handed to subagents.
                if not job.stats.subagents_billed:
                    job.stats.subagents_spawned += 1
                    _bump(job.stats.subagent_types, _text(obj, "subagent_type", 40) or "subagent")
                    job.stats.subagent_depth = max(
                        job.stats.subagent_depth, _positive_int(obj, "spawn_depth")
                    )
            elif subtype == "task_notification":
                description = stats.get("tasks", {}).get(str(obj.get("task_id")), "a subagent")
                status = _short(str(obj.get("status") or "ended"), 30)
                self._add(job, "log", f"subagent {status}: {description}")
            elif subtype == "thinking_tokens":
                # The CLI's own running estimate of what the model is thinking,
                # in tokens. `estimated_tokens` restarts with each turn, so it is
                # the delta that adds up over a run. Counted rather than logged:
                # a line per tick said "thinking tokens" and nothing more, and it
                # arrives often enough to push the tool calls off the panel.
                delta = _positive_int(obj, "estimated_tokens_delta")
                if delta and not job.stats.thinking_billed:
                    job.stats.thinking_tokens += delta
            elif subtype in ("task_progress", "task_updated"):
                # A running subagent's own tool calls already reach the log, marked;
                # these would add a line per tick that says nothing more.
                pass
            else:
                # Child-controlled text, so bounded like every other line here.
                self._add(job, "log", _short(subtype.replace("_", " "), 90))
        # other event types (tool results, partial deltas) are ignored in the log

    @staticmethod
    def _count_session(job: CommandJob, obj: dict) -> None:
        """What the session was given, from the event that opens the stream."""
        stats = job.stats
        if not stats.model:
            stats.model = _text(obj, "model")
        stats.cli_version = stats.cli_version or _text(obj, "claude_code_version", 20)
        stats.permission_mode = stats.permission_mode or _text(obj, "permissionMode", 20)
        stats.output_style = stats.output_style or _text(obj, "output_style", 30)
        for key, field_name in (("tools", "tools_offered"), ("mcp_servers", "mcp_servers")):
            value = obj.get(key)
            if isinstance(value, list):
                setattr(stats, field_name, len(value))

    @staticmethod
    def _count_rate_limit(job: CommandJob, obj: dict) -> None:
        """Where the account's rate limits stood when the run last heard.

        Utilisation per window (0..1), kept as the highest the run saw: what is
        left over is what the next review has to work with.
        """
        info = obj.get("rate_limit_info")
        if not isinstance(info, dict):
            return
        job.stats.rate_limit_status = _text(info, "status", 20)
        windows = info.get("unifiedWindows")
        if not isinstance(windows, dict):
            return
        for name, window in windows.items():
            if not isinstance(window, dict) or not isinstance(name, str):
                continue
            used = window.get("utilization")
            if isinstance(used, (int, float)) and not isinstance(used, bool):
                current = job.stats.rate_limits.get(name[:30], 0.0)
                job.stats.rate_limits[name[:30]] = max(current, float(used))

    @staticmethod
    def _close_sample(job: CommandJob, stats: dict) -> None:
        """Finish the open request's entry in the run's timeline.

        Closed when the next request starts (or when the run reports its
        result), never when it is opened: a request's tool calls and thinking
        happen after the model has answered it, so an entry written on the spot
        would credit them to the request before.
        """
        open_sample = stats.pop("sample", None)
        if open_sample is None:
            return
        open_sample.pop("request", None)   # bookkeeping, not part of the record
        open_sample["tools"] = max(0, job.stats.tool_calls - open_sample.pop("tools_at", 0))
        open_sample["thinking"] = max(
            0, job.stats.thinking_tokens - open_sample.pop("thinking_at", 0)
        )
        samples = job.stats.samples
        samples.append(open_sample)
        if len(samples) > _MAX_SAMPLES:
            # Halve the resolution rather than drop the beginning: what a long
            # run is asked about is its shape, and the shape starts at the start.
            job.stats.samples = samples[::2]

    def _count_request(self, job: CommandJob, obj: dict, stats: dict) -> None:
        """Count one request to the model, and how long it took to answer.

        Keyed on the message id, because a turn reaches us as one event per
        content block — the thinking, then the tool call it decided on — and all
        of them carry that one request's usage. Counting per event would count
        the same request twice, and its input tokens twice with it. An event with
        no id at all is counted on its own; a CLI that stops labelling messages
        would otherwise stop the counters dead.

        Only the input side is added up. A message's usage is emitted with its
        first block, before the turn has finished writing, so its output count is
        a fragment of the real one — those come from the result event instead.
        """
        message = obj.get("message")
        if not isinstance(message, dict):
            return
        message_id = message.get("id")
        if not (isinstance(message_id, str) and message_id):
            # A CLI that stops labelling messages would otherwise stop the
            # counters dead; an unlabelled event is counted on its own.
            message_id = f"?{job.stats.turns + 1}"
        seen = stats.setdefault("requests", {})
        counted = seen.get(message_id)
        first_event = counted is None
        if first_event:
            counted = {"input_tokens": 0, "cache_read_tokens": 0,
                       "cache_write_tokens": 0, "cache_write_5m_tokens": 0,
                       "cache_write_1h_tokens": 0, "reported": False}
            seen[message_id] = counted
        if not first_event:
            # A repeat of a request already counted — but not necessarily a
            # repeat of its usage. Fall through to the usage block below, which
            # only ever adds what this event carries beyond what the earlier
            # ones did, and skip everything that belongs to the request itself.
            self._count_usage(job, message, counted, stats)
            return
        job.stats.turns += 1
        # popped: the wait is over, and the blocks still to come for this same
        # request are not a new one to time.
        now = time.monotonic()
        started = stats.pop("turn_started", None)
        waited = max(0.0, now - started) if isinstance(started, float) else None
        if waited is not None:
            job.stats.latency_s += waited
            job.stats.latency_turns += 1
        self._close_sample(job, stats)
        stats["sample"] = {
            "n": job.stats.turns,
            "t": round(max(0.0, now - (job.started_mono or now)), 2),
            "wait": round(waited, 2) if waited is not None else None,
            "context": 0,
            # What the conversation weighed when this request went out.
            "bytes": stats.get("bytes", 0),
            "tools_at": job.stats.tool_calls,
            "thinking_at": job.stats.thinking_tokens,
            "request": counted,   # dropped when the sample is closed
        }
        self._count_usage(job, message, counted, stats)

    def _count_usage(
        self, job: CommandJob, message: dict, counted: dict, stats: dict | None = None
    ) -> None:
        """Add what this event says about its request's input, beyond what its
        earlier events already said.

        A turn arrives as one event per content block and every one of them
        carries that request's usage — but not always the same usage. On a model
        that thinks before it acts the first block is the thinking, and a
        provider may attach the token counts to a later block, or fill them in
        as the turn goes. Taking the first event's word for it recorded zero for
        a whole run against such a provider (and made it look as if the gateway
        reported nothing at all), so each field is tracked per request and only
        the increase is added.
        """
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return
        values = {
            "input_tokens": _positive_int(usage, "input_tokens"),
            "cache_read_tokens": _positive_int(usage, "cache_read_input_tokens"),
            "cache_write_tokens": _positive_int(usage, "cache_creation_input_tokens"),
        }
        creation = usage.get("cache_creation")
        if isinstance(creation, dict):
            values["cache_write_5m_tokens"] = _positive_int(
                creation, "ephemeral_5m_input_tokens"
            )
            values["cache_write_1h_tokens"] = _positive_int(
                creation, "ephemeral_1h_input_tokens"
            )
        if not any(values.values()):
            return   # an empty usage block says nothing about this request
        if not counted["reported"]:
            counted["reported"] = True
            job.stats.requests_with_usage += 1

        # Radar's own, and kept even once the run has published its totals: how
        # much context this one request carried. The run reports what it spent
        # in all, never how close any single request came to the window.
        carried = sum(
            values.get(name, 0) for name in
            ("input_tokens", "cache_read_tokens", "cache_write_tokens")
        )
        job.stats.peak_context_tokens = max(job.stats.peak_context_tokens, carried)
        sample = (stats or {}).get("sample")
        if isinstance(sample, dict) and sample.get("request") is counted:
            sample["context"] = carried
        if job.stats.billed:
            return
        for name, value in values.items():
            added = value - counted[name]
            if added > 0:
                counted[name] = value
                setattr(job.stats, name, getattr(job.stats, name) + added)

    @staticmethod
    def _count_result(job: CommandJob, obj: dict) -> None:
        """Take the run's own totals over radar's running count.

        A result event carries what the run was billed for — thinking, money and
        the per-model breakdown included, none of which anything before it
        reports exactly — so the first one to bring usage replaces everything
        counted until then. A run that reports several is adding a segment each
        time (radar keeps every result's answer for the same reason), so later
        ones add on.

        Thinking has a flag of its own: a CLI that reports usage without the
        thinking breakdown must not silently turn radar's live estimate into a
        billed zero, so the estimate stands until a billed number replaces it.
        """
        stats = job.stats
        usage = obj.get("usage")
        models = obj.get("modelUsage")
        models = models if isinstance(models, dict) else {}
        if (isinstance(usage, dict) or models) and not stats.billed:
            # The first result to bring numbers clears radar's running count;
            # a later one adds its own segment to what this one established.
            stats.billed = True
            stats.input_tokens = 0
            stats.cache_read_tokens = 0
            stats.cache_write_tokens = 0
            stats.cache_write_5m_tokens = 0
            stats.cache_write_1h_tokens = 0
            stats.output_tokens = 0
            stats.models = {}

        # Per model, because a run whose subagents answer on a cheaper one than
        # its main loop has two bills and one total hides which — and because
        # this is the only complete one. `usage` covers the main loop alone: a
        # run that delegated to a subagent reported 8.7k cache writes there
        # against the 22.5k it was actually billed for, which is the figure here.
        billed_by_model = RunStats()
        for name, use in models.items():
            if not isinstance(name, str) or not isinstance(use, dict):
                continue
            into = stats.models.setdefault(_short(name, 60), {})
            for key, source, field_name in (
                ("input", "inputTokens", "input_tokens"),
                ("output", "outputTokens", "output_tokens"),
                ("cache_read", "cacheReadInputTokens", "cache_read_tokens"),
                ("cache_write", "cacheCreationInputTokens", "cache_write_tokens"),
                ("thinking", "thinkingTokens", "thinking_tokens"),
            ):
                count = _positive_int(use, source)
                into[key] = into.get(key, 0) + count
                setattr(billed_by_model, field_name,
                        getattr(billed_by_model, field_name) + count)
            into["cost_usd"] = into.get("cost_usd", 0.0) + _positive_float(use, "costUSD")
            stats.context_window = max(
                stats.context_window, _positive_int(use, "contextWindow")
            )
            stats.max_output_tokens = max(
                stats.max_output_tokens, _positive_int(use, "maxOutputTokens")
            )

        if isinstance(usage, dict):
            # The per-model breakdown is the complete bill where it exists; this
            # is the fallback, and the only place the rest of these live.
            if not models:
                stats.input_tokens += _positive_int(usage, "input_tokens")
                stats.cache_read_tokens += _positive_int(usage, "cache_read_input_tokens")
                stats.cache_write_tokens += _positive_int(
                    usage, "cache_creation_input_tokens"
                )
                stats.output_tokens += _positive_int(usage, "output_tokens")
            details = usage.get("output_tokens_details")
            if not models and isinstance(details, dict) and "thinking_tokens" in details:
                _take_billed_thinking(stats, _positive_int(details, "thinking_tokens"))
            creation = usage.get("cache_creation")
            if isinstance(creation, dict):
                stats.cache_write_5m_tokens += _positive_int(
                    creation, "ephemeral_5m_input_tokens"
                )
                stats.cache_write_1h_tokens += _positive_int(
                    creation, "ephemeral_1h_input_tokens"
                )
            server = usage.get("server_tool_use")
            if isinstance(server, dict):
                # Server-side tools are billed per request, apart from tokens.
                stats.web_searches += _positive_int(server, "web_search_requests")
                stats.web_fetches += _positive_int(server, "web_fetch_requests")
            stats.service_tier = _text(usage, "service_tier", 20) or stats.service_tier
            stats.speed = _text(usage, "speed", 20) or stats.speed
            geo = _text(usage, "inference_geo", 20)
            if geo and geo != "not_available":
                stats.inference_geo = geo

        if models:
            for name in ("input_tokens", "cache_read_tokens", "cache_write_tokens",
                         "output_tokens"):
                setattr(stats, name, getattr(stats, name) + getattr(billed_by_model, name))
            _take_billed_thinking(stats, billed_by_model.thinking_tokens)

        stats.cost_usd += _positive_float(obj, "total_cost_usd")
        stats.api_ms += _positive_int(obj, "duration_api_ms")
        stats.wall_ms += _positive_int(obj, "duration_ms")
        stats.first_token_ms = max(stats.first_token_ms, _positive_int(obj, "ttft_ms"))
        stats.reported_turns += _positive_int(obj, "num_turns")
        stats.queued_turns += _positive_int(obj, "queued_turn_count")

        # How it ended, and whether anything stood in its way. A run launched
        # with `--permission-mode dontAsk` is never asked about a tool off the
        # allowlist — it is refused one and carries on writing its answer, so
        # the denials are the difference between a finding and a guess.
        for entry in obj.get("permission_denials") or ():
            name = _text(entry, "tool_name", 40) if isinstance(entry, dict) else ""
            _bump(stats.denials, name or "a tool")
        if obj.get("is_error"):
            stats.errors += 1
        stats.stop_reason = _text(obj, "stop_reason", 30) or stats.stop_reason
        outcome = _text(obj, "api_error_status", 40) or _text(obj, "terminal_reason", 30)
        stats.outcome = outcome or stats.outcome

        agents = obj.get("subagent_stats")
        if isinstance(agents, dict):
            # The run's own tally replaces what radar counted from the task
            # events: it knows about the ones that were refused before they ever
            # started, and radar only ever sees the ones that did.
            #
            # Only a result that actually brings the tally replaces it, though.
            # A CLI that reports usage without breaking subagents out is not a
            # run that started none, and clearing the count on its word would
            # wipe the only figure radar had while the log above still says a
            # subagent started — the mistake `_take_billed_thinking` exists to
            # avoid, in the one other place a zero means silence.
            if not stats.subagents_billed:
                stats.subagents_billed = True
                stats.subagents_spawned = 0
                stats.subagent_types = {}
            stats.subagents_spawned += _positive_int(agents, "spawned")
            stats.subagents_completed += _positive_int(agents, "completed")
            stats.subagents_failed += _positive_int(agents, "failed")
            stats.subagent_depth = max(stats.subagent_depth, _positive_int(agents, "max_depth"))
            for key, into in (("killed", "subagents_killed"), ("refused", "subagents_refused")):
                group = agents.get(key)
                if isinstance(group, dict):
                    setattr(stats, into, getattr(stats, into) + sum(
                        _positive_int(group, reason) for reason in group
                    ))
            by_type = agents.get("by_type")
            if isinstance(by_type, dict):
                for name in by_type:
                    if isinstance(name, str):
                        _bump(stats.subagent_types, _short(name, 40), _positive_int(by_type, name))

    def _child_env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if not _is_secret_env(k)}
        env["PYTHONIOENCODING"] = "utf-8"  # nudge Python skills to emit UTF-8
        # setdefault, not update: an operator who exported one of these before
        # starting radar has said what they want, and radar is filling a gap
        # rather than overruling them.
        for name, value in _DEFAULT_CHILD_ENV.items():
            env.setdefault(name, value)
        # The skill's own env has the last word — except over the denylist. The
        # config parser refuses those names too, but the promise that the child
        # never sees radar's credentials belongs to the function that builds the
        # child's environment, not to whoever happened to construct the config.
        for name, value in dict(getattr(self.config, "env", ()) or ()).items():
            if not _is_secret_env(name):
                env[name] = value
        for name in getattr(self.config, "env_unset", ()) or ():
            _unset_env(env, name)
        return env
