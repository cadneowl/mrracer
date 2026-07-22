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


class ConfigError(Exception):
    """Raised when config.yaml is missing, malformed, or invalid."""


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
    match: SLAMatch
    first_response_business_hours: float
    approval_business_hours: float


@dataclass(frozen=True)
class WaiveConfig:
    draft: bool = True
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillConfig:
    """One dashboard skill: an external command run for an MR, launched from a
    board button. The command is a template filled with MR context.

    ``name`` is the URL/id slug; ``button``/``label``/``icon`` drive the board
    button text, the panel heading, and the emoji. ``context`` names an optional
    backend fetch whose output is piped to the command on stdin when
    ``include_context`` is on — ``"gitlab_diff"`` (the MR diff) or ``"jira"``
    (the linked ticket(s)/epic) — so the skill needs no GitLab/Jira access of its
    own. ``stores_result`` persists the output so the board can re-open it later
    (used by the QA test plan).
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
    context: str | None = None  # "gitlab_diff" | "jira" | None
    stores_result: bool = False


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
        for skill in self.skills:
            if skill.name == name:
                return skill
        return None

    @property
    def review(self) -> SkillConfig:
        """Back-compat accessor for the built-in review skill."""
        return self.skill_by_name("review") or SkillConfig(name="review")

    @property
    def qa(self) -> SkillConfig:
        """Back-compat accessor for the built-in qa skill."""
        return self.skill_by_name("qa") or SkillConfig(name="qa")


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

        try:
            first = float(_require(entry, "first_response_business_hours", ctx))
            approval = float(_require(entry, "approval_business_hours", ctx))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{ctx}: business-hours values must be numbers ({exc})") from None
        if first < 0 or approval < 0:
            raise ConfigError(f"{ctx}: business-hours values must be non-negative")

        rules.append(
            SLARule(
                match=SLAMatch(target_branch=target_branch, labels=labels),
                first_response_business_hours=first,
                approval_business_hours=approval,
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
    return tuple(rules)


_VALID_CONTEXTS = {"gitlab_diff", "jira"}

# A skill name is interpolated into dashboard routes and htmx URLs
# (/{name}/{project_id}/{mr_iid}), so it must be a URL-safe slug and must not
# shadow a fixed sub-path used within a skill's own route namespace.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_RESERVED_NAMES = frozenset({"status", "stream", "close", "stored"})

# The two built-in skills. They always exist (disabled unless configured), so
# the dashboard and `radar check` have a stable review + qa baseline, and each
# carries the special capability the generic skill machinery can't infer: review
# pipes the MR diff to stdin; qa pipes the Jira ticket(s) and persists the plan.
_BUILTIN_SKILLS: dict[str, dict] = {
    "review": {
        "label": "AI review", "button": "review", "icon": "🔍",
        "context": "gitlab_diff", "stores_result": False,
    },
    "qa": {
        "label": "QA test plan", "button": "QA plan", "icon": "🧪",
        "context": "jira", "stores_result": True,
    },
}


def _parse_skill(raw: object, name: str, ctx: str) -> SkillConfig:
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

    context = raw.get("context", builtin.get("context"))
    if context is not None:
        context = str(context)
        if context not in _VALID_CONTEXTS:
            allowed = ", ".join(sorted(_VALID_CONTEXTS))
            raise ConfigError(
                f"{ctx}.context: unknown value {context!r} (expected one of {allowed}, "
                "or omit it for a skill that needs no backend fetch)"
            )

    include_context = bool(raw.get("include_context", False))
    if include_context and context is None:
        raise ConfigError(
            f"{ctx}.include_context is true but no 'context' source is set, so radar "
            "wouldn't know what to fetch. Set context: gitlab_diff or jira, or drop "
            "include_context."
        )

    stores_result = bool(raw.get("stores_result", builtin.get("stores_result", False)))

    return SkillConfig(
        name=name, label=label, button=button, icon=icon, enabled=enabled,
        command=command, working_dir=working_dir, timeout_seconds=timeout,
        include_context=include_context, context=context, stores_result=stores_result,
    )


def _parse_skills(raw_top: dict) -> tuple[SkillConfig, ...]:
    """Build the ordered skill list: the built-in review + qa (always present,
    disabled unless configured), optionally overridden by legacy top-level
    ``review:``/``qa:`` blocks, plus any entries from a ``skills:`` list. A
    skills-list entry wins over a legacy block of the same name."""
    by_name: dict[str, SkillConfig] = {}
    order: list[str] = []

    # Built-in baseline (disabled defaults), then legacy top-level blocks.
    for name in ("review", "qa"):
        by_name[name] = _parse_skill({}, name, name)
        order.append(name)
    for name in ("review", "qa"):
        block = raw_top.get(name)
        if block is not None:
            by_name[name] = _parse_skill(block, name, name)

    skills_raw = raw_top.get("skills")
    if skills_raw is not None:
        if not isinstance(skills_raw, list):
            raise ConfigError("skills: expected a list of skill mappings")
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
            if name not in by_name:
                order.append(name)
            by_name[name] = _parse_skill(entry, name, ctx)

    return tuple(by_name[n] for n in order)


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
    skills = _parse_skills(raw)
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
