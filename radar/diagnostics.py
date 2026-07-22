"""radar check — validate config, the DB, and external connectivity.

Each check returns a Check(status) and never raises: a failing check reports
``fail`` instead of crashing the whole run, so ``radar check`` always prints a
full report. Status is ok / warn / fail / skip.
"""

from __future__ import annotations

import os
import shlex
import shutil
from dataclasses import dataclass

from .config import Config, ConfigError, gitlab_credentials, jira_credentials
from .db import Database


@dataclass
class Check:
    name: str
    status: str  # ok / warn / fail / skip
    detail: str


def _first_token(command: str) -> str | None:
    try:
        tokens = shlex.split(command, posix=(os.name != "nt"))
    except ValueError:
        return None
    if not tokens:
        return None
    token = tokens[0]
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        token = token[1:-1]
    return token


def _check_database(config: Config) -> Check:
    try:
        with Database(str(config.database_path)) as db:
            events = db.event_count()
            snaps = db.all_snapshots()
            open_count = sum(1 for s in snaps if s.get("state") == "opened")
            plans = db.conn.execute("SELECT count(*) FROM test_plans").fetchone()[0]
        return Check(
            "database",
            "ok",
            f"{config.database_path}: {events} events, {len(snaps)} MRs "
            f"({open_count} open), {plans} test plans",
        )
    except Exception as exc:  # noqa: BLE001 - report, never crash the run
        return Check("database", "fail", f"{config.database_path}: {exc}")


def _check_gitlab(config: Config) -> list[Check]:
    try:
        url, token = gitlab_credentials()
    except ConfigError as exc:
        return [Check("gitlab.env", "fail", exc.args[0].splitlines()[0])]
    out = [Check("gitlab.env", "ok", f"GITLAB_URL={url}")]

    try:
        import gitlab

        gl = gitlab.Gitlab(url, private_token=token)
        gl.auth()
        out.append(Check("gitlab.auth", "ok", f"authenticated as {gl.user.username}"))
    except Exception as exc:  # noqa: BLE001
        out.append(Check("gitlab.auth", "fail", str(exc).splitlines()[0]))
        return out  # no point checking projects without auth

    try:
        scopes = gl.http_get("/personal_access_tokens/self").get("scopes", [])
        if {"read_api", "api"} & set(scopes):
            out.append(Check("gitlab.scope", "ok", f"scopes: {', '.join(scopes)}"))
        else:
            joined = ", ".join(scopes)
            out.append(Check("gitlab.scope", "warn", f"read_api missing; scopes: {joined}"))
    except Exception as exc:  # noqa: BLE001
        out.append(Check("gitlab.scope", "warn", f"could not read token scopes ({exc})"))

    for project in config.gitlab.projects:
        try:
            proj = gl.projects.get(project)
            out.append(Check(f"gitlab.project[{project}]", "ok", f"reachable (id {proj.id})"))
        except Exception as exc:  # noqa: BLE001
            out.append(Check(f"gitlab.project[{project}]", "fail", str(exc).splitlines()[0]))
    return out


def _check_jira(config: Config) -> Check:
    needed = any(
        s.enabled and s.include_context and s.context == "jira" for s in config.skills
    )
    try:
        base, email, token = jira_credentials()
    except ConfigError as exc:
        if needed:
            return Check("jira.env", "fail", exc.args[0].splitlines()[0])
        return Check("jira.env", "skip", "not set (no Jira-context skill is enabled)")

    try:
        from .jira_client import JiraClient

        me = JiraClient(base, email, token).myself()
        who = me.get("displayName") or me.get("emailAddress") or "?"
        return Check("jira.auth", "ok", f"authenticated as {who}")
    except Exception as exc:  # noqa: BLE001
        return Check("jira.auth", "fail", str(exc).splitlines()[0])


def _check_commands(config: Config) -> list[Check]:
    out = []
    for skill in config.skills:
        name = skill.name
        if not skill.enabled:
            out.append(Check(f"{name}.command", "skip", "disabled"))
            continue
        exe = _first_token(skill.command)
        if exe and shutil.which(exe):
            ctx = " · include_context (radar fetches)" if skill.include_context else ""
            out.append(Check(f"{name}.command", "ok", f"'{exe}' on PATH{ctx}"))
        else:
            out.append(Check(f"{name}.command", "warn", f"'{exe}' not found on PATH"))
    return out


def _check_note_parsing(config: Config) -> Check | None:
    try:
        with Database(str(config.database_path)) as db:
            rows = db.conn.execute(
                "SELECT COALESCE(json_extract(payload, '$.source'), 'note') AS s, count(*) "
                "FROM events WHERE event_type='review_requested' GROUP BY s"
            ).fetchall()
    except Exception:  # noqa: BLE001
        return None
    counts = {r[0]: r[1] for r in rows}
    total = sum(counts.values())
    if total == 0:
        return Check("note_parsing", "skip", "no review requests recorded yet")
    backfill = counts.get("reviewer_snapshot", 0)
    from_notes = total - backfill
    if backfill > from_notes:
        return Check(
            "note_parsing",
            "warn",
            f"{backfill}/{total} review requests came from created-date backfill, not "
            "system notes — breach counts may be inflated; check radar/notes.py patterns",
        )
    return Check("note_parsing", "ok", f"{from_notes}/{total} review requests from system notes")


def run_checks(config: Config) -> list[Check]:
    """Run every diagnostic and return the results (order = report order)."""
    checks: list[Check] = [
        Check(
            "config",
            "ok",
            f"{len(config.gitlab.projects)} project(s), {len(config.slas)} SLA rule(s), "
            f"{len(config.teams)} team(s), default tz {config.calendar.default_timezone}",
        ),
        _check_database(config),
    ]
    checks.extend(_check_gitlab(config))
    checks.append(_check_jira(config))
    checks.extend(_check_commands(config))
    note = _check_note_parsing(config)
    if note is not None:
        checks.append(note)
    return checks
