"""Configuration loading and validation.

Parses config.yaml into typed, validated objects with actionable error
messages. Secrets are never read from here — GitLab credentials come from the
GITLAB_URL and GITLAB_TOKEN environment variables (see gitlab_client).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .business_time import WorkCalendar, parse_hhmm, parse_weekday
from .skillcontext import (
    Input,
    SkillContextError,
    SourceSpec,
    parse_inputs,
    parse_source,
)


class ConfigError(Exception):
    """Raised when config.yaml is missing, malformed, or invalid."""


# The credential variables radar reads for itself. Named here once: they are
# stripped from every skill's environment (see ``commands._ENV_DENYLIST``) and
# refused in a skill's own ``env:`` block, so neither path can hand a skill
# radar's GitLab PAT or Jira login.
SECRET_ENV_NAMES = frozenset(
    {"GITLAB_TOKEN", "GITLAB_URL", "JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"}
)


@dataclass(frozen=True)
class GitLabSettings:
    projects: list[str]
    poll_interval_minutes: int


@dataclass(frozen=True)
class CalendarConfig:
    calendar: WorkCalendar
    default_timezone: str
    reviewer_timezones: dict[str, str] = field(default_factory=dict)

    def tz_for(self, reviewer: str | None) -> ZoneInfo:
        """Timezone for a reviewer, falling back to the default."""
        name = self.reviewer_timezones.get(reviewer or "", self.default_timezone)
        return ZoneInfo(name)


@dataclass(frozen=True)
class SLAMatch:
    target_branch: str | None = None
    labels: tuple[str, ...] = ()

    @property
    def is_default(self) -> bool:
        return self.target_branch is None and not self.labels


@dataclass(frozen=True)
class SLARule:
    """Budgets for one class of merge request.

    ``assignment_business_hours`` is the budget for getting *any* reviewer onto
    an MR that has none. It is optional, and None means radar does not track
    unassigned MRs at all — the check is off until a config asks for it.
    """

    match: SLAMatch
    first_response_business_hours: float
    approval_business_hours: float
    assignment_business_hours: float | None = None


@dataclass(frozen=True)
class WaiveConfig:
    draft: bool = True
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillConfig:
    """One dashboard skill: an external command run for an MR, launched from a
    board button. The command is a template filled with MR context.

    ``name`` is the URL/id slug; ``button``/``label``/``icon`` drive the board
    button text, the panel heading, and the emoji. ``contexts`` names the backend
    fetches whose output is piped to the command on stdin when
    ``include_context`` is on — ``"gitlab_diff"`` (the MR diff) and/or ``"jira"``
    (the linked ticket(s)/epic) — so the skill needs no GitLab/Jira access of its
    own. ``stores_result`` persists the output so the board can re-open it later
    (used by the QA test plan).

    ``source`` and ``inputs`` are the skill's declared context bag (see
    ``skillcontext``): where this project's code is checked out, and any other
    input the skill needs. Both are resolved per job, because the source root
    varies by GitLab project. ``checkout: worktree`` gives each job its own
    detached worktree of that source at the MR's head commit (see ``worktree``);
    ``remote`` names the git remote to fetch the MR ref from.

    ``env`` is what this skill's subprocess gets on top of radar's own
    environment, and ``env_unset`` is what it must not inherit — including
    anything radar exports by default (``commands._DEFAULT_CHILD_ENV``).
    Credentials are refused in both: radar's own are stripped, and config files
    are not where secrets go.
    """

    name: str = ""
    label: str = ""  # panel heading / long name
    button: str = ""  # short board-button text
    icon: str = "▶"
    enabled: bool = False
    command: str = ""
    working_dir: str | None = None
    timeout_seconds: int = 600
    include_context: bool = False
    contexts: tuple[str, ...] = ()  # subset of {"gitlab_diff", "jira"}
    stores_result: bool = False
    source: SourceSpec | None = None
    inputs: tuple[Input, ...] = ()
    checkout: str = "none"  # "none" | "worktree"
    remote: str = "origin"
    env: tuple[tuple[str, str], ...] = ()  # NAME -> value, exported to the child
    env_unset: tuple[str, ...] = ()  # names the child must NOT inherit


# Backwards-compatible aliases (older names for the same shape).
CommandConfig = SkillConfig
ReviewConfig = SkillConfig


@dataclass(frozen=True)
class JiraConfig:
    """How to recognise and link the Jira issue(s) associated with an MR."""

    base_url: str | None = None  # e.g. https://yourco.atlassian.net (for browse links)
    project_keys: tuple[str, ...] = ()  # optional filter, e.g. ("PROJ", "BUG")


@dataclass(frozen=True)
class Team:
    """A named group of GitLab usernames, used for board filters."""

    name: str
    members: tuple[str, ...]

    @property
    def member_set(self) -> frozenset[str]:
        return frozenset(self.members)


@dataclass(frozen=True)
class Config:
    gitlab: GitLabSettings
    database_path: Path
    calendar: CalendarConfig
    slas: tuple[SLARule, ...]
    waive: WaiveConfig
    skills: tuple[SkillConfig, ...]
    jira: JiraConfig
    teams: tuple[Team, ...]
    gamification: dict  # consumed in Phase 3; carried verbatim for now

    def team_by_name(self, name: str) -> Team | None:
        for team in self.teams:
            if team.name == name:
                return team
        return None

    def skill_by_name(self, name: str) -> SkillConfig | None:
        """The skill declared under ``name``, or None if the config never names it.

        None means absent, and callers are expected to say so. An earlier version
        of this returned a disabled placeholder for ``review``/``qa`` so callers
        could dot into it unconditionally, which made a skill nobody configured
        indistinguishable from one deliberately turned off.
        """
        for skill in self.skills:
            if skill.name == name:
                return skill
        return None


# --- helpers ---------------------------------------------------------------


def _require(mapping: dict, key: str, ctx: str) -> object:
    if not isinstance(mapping, dict) or key not in mapping:
        raise ConfigError(f"{ctx}: missing required key '{key}'")
    return mapping[key]


def _validate_timezone(name: str, ctx: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"{ctx}: unknown timezone {name!r} ({exc})") from None
    return name


def _parse_calendar(raw: dict) -> CalendarConfig:
    ctx = "calendar"
    if not isinstance(raw, dict):
        raise ConfigError(f"{ctx}: expected a mapping")

    workday_names = _require(raw, "workdays", ctx)
    if not isinstance(workday_names, list) or not workday_names:
        raise ConfigError(f"{ctx}.workdays: expected a non-empty list of weekday names")
    try:
        workdays = frozenset(parse_weekday(str(d)) for d in workday_names)
    except ValueError as exc:
        raise ConfigError(f"{ctx}.workdays: {exc}") from None

    work_hours = _require(raw, "work_hours", ctx)
    if not isinstance(work_hours, dict):
        raise ConfigError(f"{ctx}.work_hours: expected a mapping with 'start' and 'end'")
    try:
        work_start = parse_hhmm(str(_require(work_hours, "start", f"{ctx}.work_hours")))
        work_end = parse_hhmm(str(_require(work_hours, "end", f"{ctx}.work_hours")))
    except ValueError as exc:
        raise ConfigError(f"{ctx}.work_hours: {exc}") from None

    try:
        calendar = WorkCalendar(workdays=workdays, work_start=work_start, work_end=work_end)
    except ValueError as exc:
        raise ConfigError(f"{ctx}: {exc}") from None

    default_tz = str(_require(raw, "default_timezone", ctx))
    _validate_timezone(default_tz, f"{ctx}.default_timezone")

    reviewer_tz_raw = raw.get("reviewer_timezones") or {}
    if not isinstance(reviewer_tz_raw, dict):
        raise ConfigError(f"{ctx}.reviewer_timezones: expected a mapping")
    reviewer_timezones: dict[str, str] = {}
    for user, tz_name in reviewer_tz_raw.items():
        tz_name = str(tz_name)
        _validate_timezone(tz_name, f"{ctx}.reviewer_timezones.{user}")
        reviewer_timezones[str(user)] = tz_name

    return CalendarConfig(
        calendar=calendar,
        default_timezone=default_tz,
        reviewer_timezones=reviewer_timezones,
    )


def _business_hours(value: object, ctx: str) -> float:
    """One SLA budget, in business hours.

    Booleans are refused rather than accepted as 1/0: YAML reads a bare `false`
    as a bool, and the obvious-looking way to switch a budget off would
    otherwise land as a zero-hour budget — the harshest setting there is, and
    silently. Off is expressed by omitting the key, which only the optional
    budgets allow.
    """
    if isinstance(value, bool):
        raise ConfigError(
            f"{ctx}: business-hours values must be numbers, not {str(value).lower()}. "
            "To switch a budget off, leave its key out entirely."
        )
    try:
        hours = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{ctx}: business-hours values must be numbers ({exc})") from None
    if hours < 0:
        raise ConfigError(f"{ctx}: business-hours values must be non-negative")
    return hours


def _parse_slas(raw: object) -> tuple[SLARule, ...]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError("slas: expected a non-empty list of rules")
    rules: list[SLARule] = []
    for i, entry in enumerate(raw):
        ctx = f"slas[{i}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{ctx}: expected a mapping")
        match_raw = entry.get("match", {})
        if not isinstance(match_raw, dict):
            raise ConfigError(f"{ctx}.match: expected a mapping (use {{}} for the default)")

        target_branch = match_raw.get("target_branch")
        if target_branch is not None:
            target_branch = str(target_branch)
        labels_raw = match_raw.get("labels", [])
        if not isinstance(labels_raw, list):
            raise ConfigError(f"{ctx}.match.labels: expected a list of strings")
        labels = tuple(str(x) for x in labels_raw)

        first = _business_hours(_require(entry, "first_response_business_hours", ctx), ctx)
        approval = _business_hours(_require(entry, "approval_business_hours", ctx), ctx)
        assignment_raw = entry.get("assignment_business_hours")
        assignment = None if assignment_raw is None else _business_hours(assignment_raw, ctx)

        rules.append(
            SLARule(
                match=SLAMatch(target_branch=target_branch, labels=labels),
                first_response_business_hours=first,
                approval_business_hours=approval,
                assignment_business_hours=assignment,
            )
        )

    if not any(r.match.is_default for r in rules):
        raise ConfigError(
            "slas: no default rule found; add a trailing rule with 'match: {}' "
            "so every obligation matches something"
        )
    if not rules[-1].match.is_default:
        raise ConfigError(
            "slas: the default rule (match: {}) must be last, since the first "
            "matching rule wins"
        )
    # All rules or none: the first matching rule wins outright, so a config that
    # sets the assignment budget on only some rules would quietly stop tracking
    # unassigned MRs whose branch or labels happen to match one of the others.
    missing = [i for i, r in enumerate(rules) if r.assignment_business_hours is None]
    if missing and len(missing) != len(rules):
        where = ", ".join(f"slas[{i}]" for i in missing)
        raise ConfigError(
            f"slas: assignment_business_hours is set on some rules but not on {where}. "
            "The first matching rule wins outright, so an MR matching one of those "
            "would not be checked for having no reviewers at all. Add the key to "
            "every rule, or to none of them to turn the check off."
        )
    return tuple(rules)


_VALID_CONTEXTS = {"gitlab_diff", "jira"}
_VALID_CHECKOUTS = {"none", "worktree"}

# A skill name is interpolated into dashboard routes and htmx URLs
# (/{name}/{project_id}/{mr_iid}), so it must be a URL-safe slug and must not
# shadow a fixed sub-path used within a skill's own route namespace.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_RESERVED_NAMES = frozenset({"status", "stream", "close", "stored"})

# Defaults carried by two well-known names. They are *not* skills in their own
# right — nothing exists until `skills:` declares it — but a skill that claims
# one of these names inherits the capability the generic machinery cannot infer
# from a command line: review pipes the MR diff to stdin; qa pipes the Jira
# ticket(s) and persists its output. Anything here can still be overridden
# per-skill; declaring `context:` or `stores_result:` explicitly always wins.
_BUILTIN_SKILLS: dict[str, dict] = {
    "review": {
        "label": "AI review", "button": "review", "icon": "🔍",
        "context": ["gitlab_diff"], "stores_result": False,
    },
    "qa": {
        "label": "QA test plan", "button": "QA plan", "icon": "🧪",
        "context": ["jira"], "stores_result": True,
    },
}


def _parse_contexts(raw: object, ctx: str) -> tuple[str, ...]:
    """Parse ``context:`` — one backend fetch or several.

    A scalar is still accepted (it is what every config in the wild says), so
    ``context: gitlab_diff`` and ``context: [gitlab_diff, jira]`` both work: a
    review skill can be given the diff *and* the linked ticket.
    """
    if raw is None:
        return ()
    values = raw if isinstance(raw, list) else [raw]
    out: list[str] = []
    for value in values:
        value = str(value)
        if value not in _VALID_CONTEXTS:
            allowed = ", ".join(sorted(_VALID_CONTEXTS))
            raise ConfigError(
                f"{ctx}.context: unknown value {value!r} (expected one of {allowed}, "
                "or omit it for a skill that needs no backend fetch)"
            )
        if value not in out:  # de-dup, keep order
            out.append(value)
    return tuple(out)


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _env_name(raw_name: object, ctx: str) -> str:
    """Validate one environment variable name for a skill.

    Credentials are matched without regard to case: Windows looks variables up
    case-insensitively, so a lowercase ``gitlab_token`` here would reach the
    child as the real thing and quietly undo the strip in ``commands``.
    """
    name = str(raw_name)
    if not _ENV_NAME_RE.match(name):
        raise ConfigError(
            f"{ctx}: {name!r} is not a usable variable name (letters, digits and "
            "underscore, not starting with a digit)"
        )
    if name.upper() in SECRET_ENV_NAMES:
        raise ConfigError(
            f"{ctx}: refusing to name {name}. radar keeps its own credentials out of "
            "every skill's environment, and this file is not where secrets go — a skill "
            "that needs its own credential should read it from the environment radar was "
            "started with, under a different name."
        )
    return name


def _parse_env(raw: object, ctx: str) -> tuple[tuple[str, str], ...]:
    """Parse a skill's ``env:`` mapping into ordered (name, value) pairs.

    Values are written the way a shell would read them, not the way Python
    repr's them: a YAML ``true`` exports as ``true``. Anything that is not text
    or a number is refused rather than str()'d, because ``['--fast']`` reaching
    a child as the literal characters ``['--fast']`` is a config mistake worth
    a message, not a value worth passing on.
    """
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ConfigError(f"{ctx}.env: expected a mapping of NAME: value")
    out: list[tuple[str, str]] = []
    for key, value in raw.items():
        name = _env_name(key, f"{ctx}.env")
        if value is None:
            # `FOO:` and `FOO: null` are the same thing to YAML, so neither can
            # mean "remove" without the other silently meaning it too.
            raise ConfigError(
                f"{ctx}.env.{name}: no value. A key with nothing after it is a "
                f"half-finished edit — give it one, or list {name} under 'env_unset:' "
                "to keep the skill from inheriting it."
            )
        if isinstance(value, bool):
            out.append((name, "true" if value else "false"))
        elif isinstance(value, (str, int, float)):
            out.append((name, str(value)))
        else:
            raise ConfigError(
                f"{ctx}.env.{name}: expected text or a number, got "
                f"{type(value).__name__}. An environment variable is a string; quote it "
                "if the literal text is what you meant."
            )
    return tuple(out)


def _parse_env_unset(raw: object, ctx: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"{ctx}.env_unset: expected a list of variable names")
    return tuple(_env_name(x, f"{ctx}.env_unset") for x in raw)


def _parse_skill(raw: object, name: str, ctx: str, base_dir: Path) -> SkillConfig:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{ctx}: expected a mapping")
    builtin = _BUILTIN_SKILLS.get(name, {})

    label = str(raw.get("label", builtin.get("label", name)))
    button = str(raw.get("button", builtin.get("button", label)))
    icon = str(raw.get("icon", builtin.get("icon", "▶")))

    enabled = bool(raw.get("enabled", False))
    command = str(raw.get("command", "")).strip()
    if enabled and not command:
        raise ConfigError(f"{ctx}.enabled is true but {ctx}.command is empty")

    working_dir = raw.get("working_dir")
    if working_dir is not None:
        # Expand ~ and $VARS so paths like "~/src/repo" work (Path/subprocess
        # don't expand them on their own); store the resolved absolute path.
        raw_dir = str(working_dir)
        working_dir = str(Path(os.path.expandvars(raw_dir)).expanduser())
        if not Path(working_dir).is_dir():
            raise ConfigError(f"{ctx}.working_dir does not exist: {raw_dir}")

    try:
        timeout = int(raw.get("timeout_seconds", 600))
    except (TypeError, ValueError):
        raise ConfigError(f"{ctx}.timeout_seconds: expected an integer") from None
    if timeout < 1:
        raise ConfigError(f"{ctx}.timeout_seconds: must be >= 1")

    contexts = _parse_contexts(raw.get("context", builtin.get("context")), ctx)

    include_context = bool(raw.get("include_context", False))
    if include_context and not contexts:
        raise ConfigError(
            f"{ctx}.include_context is true but no 'context' source is set, so radar "
            "wouldn't know what to fetch. Set context: gitlab_diff or jira, or drop "
            "include_context."
        )

    stores_result = bool(raw.get("stores_result", builtin.get("stores_result", False)))

    # The declared context bag: where the code is, and any other input the skill
    # needs. Shape errors are config errors; a value that is merely unset is a
    # deployment problem, reported by `radar check` and refused at the click.
    try:
        source = parse_source(raw.get("source"), ctx, base_dir)
        inputs = parse_inputs(raw.get("inputs"), ctx, base_dir)
    except SkillContextError as exc:
        raise ConfigError(str(exc)) from None

    checkout = str(raw.get("checkout", "none"))
    if checkout not in _VALID_CHECKOUTS:
        allowed = ", ".join(sorted(_VALID_CHECKOUTS))
        raise ConfigError(f"{ctx}.checkout: unknown value {checkout!r} (expected one of {allowed})")
    if checkout == "worktree" and source is None:
        raise ConfigError(
            f"{ctx}.checkout is 'worktree' but no 'source:' is set, so there is no "
            "repository to make a worktree of"
        )
    if checkout == "worktree" and working_dir is not None:
        # Both set is a contradiction radar cannot resolve quietly: it would
        # fetch and build the worktree, tell the skill (via {source_root} and
        # the stdin bundle) that the code is there, and then run it somewhere
        # else — so a skill whose tools follow the working directory would read
        # one tree while being told about another.
        raise ConfigError(
            f"{ctx}: 'working_dir' and 'checkout: worktree' contradict each other — the "
            "worktree is where the merge request's code is, but working_dir would run the "
            "command elsewhere. Drop one."
        )
    remote = str(raw.get("remote", "origin")).strip() or "origin"

    return SkillConfig(
        name=name, label=label, button=button, icon=icon, enabled=enabled,
        command=command, working_dir=working_dir, timeout_seconds=timeout,
        include_context=include_context, contexts=contexts, stores_result=stores_result,
        source=source, inputs=inputs, checkout=checkout, remote=remote,
        env=_parse_env(raw.get("env"), ctx),
        env_unset=_parse_env_unset(raw.get("env_unset"), ctx),
    )


# Where `review:` and `qa:` used to be configurable, back when they were the
# only two buttons. Kept only to be refused by name: silently ignoring a block
# would take a working button off a dashboard without saying so.
_LEGACY_SKILL_BLOCKS = ("review", "qa")


def _parse_skills(raw_top: dict, base_dir: Path) -> tuple[SkillConfig, ...]:
    """Build the skill list from ``skills:`` — the one place skills are declared.

    A skill exists because the config names it, and in the order the config
    names it. Nothing is contributed from anywhere else: two ways to declare the
    same skill meant one silently replaced the other, and a baseline nobody
    wrote meant ``config.skills`` did not match the file you were reading.
    """
    for name in _LEGACY_SKILL_BLOCKS:
        if name in raw_top:
            raise ConfigError(
                f"{name}: top-level '{name}:' blocks are no longer read — every skill "
                f"is declared in the 'skills:' list. Move this block there:\n\n"
                f"  skills:\n"
                f"    - name: {name}\n"
                f"      enabled: true\n"
                f"      command: ...\n\n"
                f"The fields are unchanged, and the name keeps what it means: review "
                f"defaults to context: gitlab_diff, qa to context: jira plus "
                f"stores_result: true (so the board can re-open its output). The two "
                f"context defaults only fetch anything when include_context is on."
            )

    skills_raw = raw_top.get("skills")
    if skills_raw is None:
        return ()
    if not isinstance(skills_raw, list):
        raise ConfigError("skills: expected a list of skill mappings")

    out: list[SkillConfig] = []
    seen: set[str] = set()
    for i, entry in enumerate(skills_raw):
        ctx = f"skills[{i}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{ctx}: expected a mapping")
        name = str(entry.get("name", "")).strip()
        if not name:
            raise ConfigError(f"{ctx}: missing 'name'")
        if not _NAME_RE.match(name):
            raise ConfigError(
                f"{ctx}: skill name {name!r} must be a slug — lowercase letters, "
                "digits, '-' or '_', starting with a letter or digit"
            )
        if name in _RESERVED_NAMES:
            raise ConfigError(f"{ctx}: skill name {name!r} is reserved")
        if name in seen:
            raise ConfigError(f"{ctx}: duplicate skill name {name!r}")
        seen.add(name)
        out.append(_parse_skill(entry, name, ctx, base_dir))

    return tuple(out)


def _parse_jira(raw: object) -> JiraConfig:
    if raw is None:
        return JiraConfig()
    if not isinstance(raw, dict):
        raise ConfigError("jira: expected a mapping")
    base_url = raw.get("base_url")
    if base_url is not None:
        base_url = str(base_url).strip()
    keys_raw = raw.get("project_keys", [])
    if not isinstance(keys_raw, list):
        raise ConfigError("jira.project_keys: expected a list of strings")
    return JiraConfig(base_url=base_url or None, project_keys=tuple(str(k) for k in keys_raw))


def _parse_teams(raw: object) -> tuple[Team, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("teams: expected a list of {name, members}")
    teams: list[Team] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        ctx = f"teams[{i}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{ctx}: expected a mapping with 'name' and 'members'")
        name = str(entry.get("name", "")).strip()
        if not name:
            raise ConfigError(f"{ctx}: missing 'name'")
        if name in seen:
            raise ConfigError(f"{ctx}: duplicate team name {name!r}")
        seen.add(name)
        members_raw = entry.get("members", [])
        if not isinstance(members_raw, list) or not members_raw:
            raise ConfigError(f"{ctx}.members: expected a non-empty list of usernames")
        members = tuple(dict.fromkeys(str(m) for m in members_raw))  # de-dup, keep order
        teams.append(Team(name=name, members=members))
    return tuple(teams)


def _parse_waive(raw: object) -> WaiveConfig:
    if raw is None:
        return WaiveConfig()
    if not isinstance(raw, dict):
        raise ConfigError("waive: expected a mapping")
    draft = bool(raw.get("draft", True))
    labels_raw = raw.get("labels", [])
    if not isinstance(labels_raw, list):
        raise ConfigError("waive.labels: expected a list of strings")
    return WaiveConfig(draft=draft, labels=tuple(str(x) for x in labels_raw))


def load_config(path: str | Path) -> Config:
    """Load and validate config.yaml, raising ConfigError with context."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"config file not found: {path}\n"
            "Copy config.example.yaml to config.yaml and edit it."
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")

    gitlab_raw = _require(raw, "gitlab", "config")
    if not isinstance(gitlab_raw, dict):
        raise ConfigError("gitlab: expected a mapping")
    projects_raw = _require(gitlab_raw, "projects", "gitlab")
    if not isinstance(projects_raw, list) or not projects_raw:
        raise ConfigError("gitlab.projects: expected a non-empty list of project paths or IDs")
    projects = [str(p) for p in projects_raw]
    poll_interval = gitlab_raw.get("poll_interval_minutes", 10)
    try:
        poll_interval = int(poll_interval)
    except (TypeError, ValueError):
        raise ConfigError("gitlab.poll_interval_minutes: expected an integer") from None
    if poll_interval < 1:
        raise ConfigError("gitlab.poll_interval_minutes: must be >= 1")

    db_raw = raw.get("database") or {}
    if not isinstance(db_raw, dict):
        raise ConfigError("database: expected a mapping")
    database_path = Path(str(db_raw.get("path", "radar.db")))

    calendar = _parse_calendar(_require(raw, "calendar", "config"))
    slas = _parse_slas(_require(raw, "slas", "config"))
    waive = _parse_waive(raw.get("waive"))
    # A declared `file:` input is written relative to the config that names it.
    skills = _parse_skills(raw, path.resolve().parent)
    jira = _parse_jira(raw.get("jira"))
    teams = _parse_teams(raw.get("teams"))
    gamification = raw.get("gamification") or {}
    if not isinstance(gamification, dict):
        raise ConfigError("gamification: expected a mapping")

    return Config(
        gitlab=GitLabSettings(projects=projects, poll_interval_minutes=poll_interval),
        database_path=database_path,
        calendar=calendar,
        slas=slas,
        waive=waive,
        skills=skills,
        jira=jira,
        teams=teams,
        gamification=gamification,
    )


def gitlab_credentials() -> tuple[str, str]:
    """Read GitLab URL and token from the environment.

    Returns (url, token). Raises ConfigError if either is missing. The token is
    never logged or persisted.
    """
    url = os.environ.get("GITLAB_URL", "").strip()
    token = os.environ.get("GITLAB_TOKEN", "").strip()
    missing = [name for name, val in (("GITLAB_URL", url), ("GITLAB_TOKEN", token)) if not val]
    if missing:
        raise ConfigError(
            "missing environment variable(s): "
            + ", ".join(missing)
            + "\nSet GITLAB_URL (e.g. https://gitlab.example.com) and GITLAB_TOKEN "
            "(a personal access token with the read_api scope)."
        )
    return url, token


def jira_credentials() -> tuple[str, str, str]:
    """Read Jira Cloud credentials from the environment for backend fetches.

    Returns (base_url, email, api_token). Raises ConfigError if any is missing.
    Jira Cloud REST uses HTTP Basic auth with the account email + an API token.
    Secrets are never logged or persisted.
    """
    base_url = os.environ.get("JIRA_BASE_URL", "").strip()
    email = os.environ.get("JIRA_EMAIL", "").strip()
    token = os.environ.get("JIRA_API_TOKEN", "").strip()
    missing = [
        name
        for name, val in (
            ("JIRA_BASE_URL", base_url),
            ("JIRA_EMAIL", email),
            ("JIRA_API_TOKEN", token),
        )
        if not val
    ]
    if missing:
        raise ConfigError(
            "missing environment variable(s): "
            + ", ".join(missing)
            + "\nSet JIRA_BASE_URL (e.g. https://yourco.atlassian.net), JIRA_EMAIL, and "
            "JIRA_API_TOKEN (create one at id.atlassian.com → Security → API tokens)."
        )
    return base_url, email, token
