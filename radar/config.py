"""Configuration loading and validation.

Parses config.yaml into typed, validated objects with actionable error
messages. Secrets are never read from here — GitLab credentials come from the
GITLAB_URL and GITLAB_TOKEN environment variables (see gitlab_client).
"""

from __future__ import annotations

import os
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
class Config:
    gitlab: GitLabSettings
    database_path: Path
    calendar: CalendarConfig
    slas: tuple[SLARule, ...]
    waive: WaiveConfig
    gamification: dict  # consumed in Phase 3; carried verbatim for now


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
    gamification = raw.get("gamification") or {}
    if not isinstance(gamification, dict):
        raise ConfigError("gamification: expected a mapping")

    return Config(
        gitlab=GitLabSettings(projects=projects, poll_interval_minutes=poll_interval),
        database_path=database_path,
        calendar=calendar,
        slas=slas,
        waive=waive,
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
