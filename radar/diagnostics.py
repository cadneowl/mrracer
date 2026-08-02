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
from .skillcontext import resolve_inputs, resolve_source
from .worktree import is_git_repo


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
        s.enabled and s.include_context and "jira" in s.contexts for s in config.skills
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
            fetched = ", ".join(skill.contexts) if skill.include_context else ""
            ctx = f" · fetches {fetched}" if fetched else ""
            out.append(Check(f"{name}.command", "ok", f"'{exe}' on PATH{ctx}"))
        else:
            out.append(Check(f"{name}.command", "warn", f"'{exe}' not found on PATH"))
    return out


def _check_skill_context(config: Config) -> list[Check]:
    """Report each enabled skill's declared context bag, resolved.

    The point is to fail here rather than at the click: an unset ``required``
    var or a source root that is not a directory is a deployment problem, and
    seeing it in ``radar check`` beats seeing an agent produce a confident
    review of a tree it never opened. Values are never printed — an ``env:``
    entry shows as its source.
    """
    out: list[Check] = []
    for skill in config.skills:
        if not skill.enabled or (skill.source is None and not skill.inputs):
            continue
        try:
            out.append(_skill_context_check(config, skill))
        except Exception as exc:  # noqa: BLE001 - report, never crash the run
            out.append(Check(f"{skill.name}.context", "fail", str(exc)))
    return out


def _skill_context_check(config: Config, skill) -> Check:
    """One skill's resolved bag, as a single check (see ``_check_skill_context``)."""
    name = f"{skill.name}.context"
    # Resolved per project, because that is how a job resolves it: a root that
    # serves project A and is missing for project B is not an ok.
    roots: dict[str, str] = {}
    rootless: list[str] = []
    unmatched: list[str] = []
    by_message: dict[str, list[str]] = {}
    for project in config.gitlab.projects:
        # Matched here by the name in `gitlab.projects`, which is all a check
        # has: a job matches on the numeric project id too, so a mapping keyed
        # by id against paths listed here resolves at run time even though it
        # cannot be resolved now. That is a caveat to report, not a failure.
        if skill.source is not None and skill.source.input_for(project, "") is None:
            unmatched.append(project)
            continue
        root, issues = resolve_source(skill.source, project, "")
        for issue in issues:
            by_message.setdefault(issue, []).append(project)
        if root:
            roots[project] = root
        elif skill.source is not None and not issues:
            rootless.append(project)

    # One declaration covering every project raises the same problem once per
    # project: say it once, and name the projects only when it is not all of them.
    problems = [
        msg if len(hit) == len(config.gitlab.projects) else f"{', '.join(hit)}: {msg}"
        for msg, hit in by_message.items()
    ]

    resolved = resolve_inputs(skill.inputs)
    problems += [f"input '{n}': {var} is not set" for n, var in resolved.missing]

    # `checkout: worktree` is git's to satisfy, and both ways it can fail are
    # invisible until someone presses the button.
    if skill.checkout == "worktree":
        if shutil.which("git") is None:
            problems.append("checkout: worktree needs git on PATH")
        else:
            problems += [
                f"{project}: {root} is not a git repository, so no worktree can be made"
                for project, root in roots.items()
                if not is_git_repo(root)
            ]

    distinct = set(roots.values())
    if not roots:
        detail = "no source root resolved"
    elif len(distinct) == 1:
        detail = next(iter(distinct))  # one checkout serves every project
    else:
        detail = "; ".join(f"{project} -> {root}" for project, root in roots.items())
    if skill.checkout == "worktree":
        detail += f" · worktree per job (fetching from '{skill.remote}')"
    if resolved.redacted:
        detail += " · inputs: " + ", ".join(f"{k}={v}" for k, v in resolved.redacted.items())

    if problems:
        return Check(name, "fail", "; ".join(problems))
    if unmatched:
        return Check(
            name,
            "warn",
            f"no 'source:' entry matches {', '.join(unmatched)} — a job on one of those "
            "will refuse to run unless the mapping is keyed by that project's numeric id, "
            f"or a 'default:' is added · {detail}",
        )
    if rootless:
        # Declared, matched, and optional-but-unset: legal (the skill runs
        # without a checkout) and worth saying out loud, because "I set that
        # variable" and "that variable is exported where radar runs" are
        # different claims.
        return Check(
            name,
            "warn",
            f"no checkout resolved for {', '.join(rootless)} — the declared variable is "
            f"not set, so the skill runs without one · {detail}",
        )
    return Check(name, "ok", detail)


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
    checks.extend(_check_skill_context(config))
    note = _check_note_parsing(config)
    if note is not None:
        checks.append(note)
    return checks
