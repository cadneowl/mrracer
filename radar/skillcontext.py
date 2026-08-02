"""Declared skill inputs: where the source is checked out, plus whatever else a
skill needs beyond the merge request itself.

A skill that reviews code has to be told *where the code is*; one that checks a
migration also wants a schema; the next one wants an API spec. Rather than grow
a config key per idea, a skill declares an open-ended bag and radar resolves the
**value forms** without interpreting the keys:

    source: { env: HUB_REPO_ROOT, required: true }   # from the environment
    inputs:
      db_schema:    { file: ./references/schema.sql }  # file contents
      api_spec_url: https://internal/api/spec.json     # a literal

Resolution produces three views, and which consumer gets which is the point:

  - ``values``   — everything resolved, secrets included. Nothing sends this
    anywhere; it exists so a caller can ask whether a var actually resolved.
  - ``shown``    — what may be written into the stdin bundle. radar's child is
    an LLM agent, so the bundle *is* prompt text: an ``env:`` value is a token
    as often as it is a path, and it stays out by default (``secret: false`` to
    opt in). A skill that needs a credential already inherits it from radar's
    environment (see ``commands._ENV_DENYLIST`` for what is stripped) — it does
    not need it pasted into a prompt.
  - ``redacted`` — safe to print in ``radar check`` and in logs: an ``env:``
    entry shows as its source, never its contents.

``missing`` collects required vars that are unset rather than raising on the
first one, so ``radar check`` can report all of them at once — and a job refuses
to launch instead of failing halfway through an expensive agent run.

The source root is deliberately per-project: radar polls several GitLab projects
and one checkout cannot serve them all. A root that is *set but wrong* is
refused, because every tool the agent has would answer "no such file", which
reads exactly like a clean repository — it would review having opened nothing
and the run would look normal.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class SkillContextError(ValueError):
    """A declaration that cannot be used — a bad directive, or a run-time value
    that is required and missing (raised before a job launches)."""


# The keys a directive mapping may carry. A mapping carrying *any* of them is
# read as an attempted directive and validated strictly; one carrying none is a
# literal the skill wants verbatim (so `{a: 1}` still works). Being strict is
# the point: `{env: X, secrit: true}` read as a literal would forward the
# declaration instead of the variable, and a misspelled `required` would drop
# the preflight that exists to refuse an unset var.
_DIRECTIVE_KEYS = frozenset({"env", "file", "required", "secret"})

# `default` in a source mapping is the fallback root, not a project key.
_SOURCE_DEFAULT = "default"


@dataclass(frozen=True)
class Input:
    """One declared input, normalised at config load so run time only reads it.

    ``kind`` is "env" (read the variable), "file" (read the file's contents), or
    "literal" (use the value as written).
    """

    name: str
    kind: str
    env: str = ""
    path: Path | None = None
    literal: Any = None
    required: bool = False
    # Only meaningful for `env`, where it defaults to True: an environment value
    # is machine-local or secret until the author says otherwise.
    secret: bool = True

    @property
    def source_label(self) -> str:
        """How this input is described when its value must not be printed."""
        if self.kind == "env":
            return f"<env:{self.env}>"
        if self.kind == "file":
            return f"<file:{self.path}>"
        return str(self.literal)


@dataclass(frozen=True)
class SourceSpec:
    """Where a skill's source tree is, per project.

    ``by_project`` keys are matched against the MR's numeric project id *and*
    the project path taken from its web URL (``group/repo``), because a config
    author knows the path they typed under ``gitlab.projects`` and rarely knows
    the numeric id. ``default`` covers every project without its own entry.
    """

    default: Input | None = None
    by_project: tuple[tuple[str, Input], ...] = ()

    def input_for(self, project_id: object, project_path: str) -> Input | None:
        keys = {str(project_id), project_path} - {""}
        for key, decl in self.by_project:
            # A suffix match (on a path boundary) is what makes a GitLab served
            # under a sub-path work: an MR there has a web URL of
            # `host/gitlab/group/repo/-/…`, so the path is `gitlab/group/repo`
            # while the config sensibly says `group/repo`. Without this the key
            # matches nothing and the skill is handed no checkout at all.
            if key in keys or any(p.endswith("/" + key) for p in keys):
                return decl
        return self.default


@dataclass
class Resolved:
    """The resolved bag, split by who is allowed to see what (see module docs)."""

    values: dict[str, Any] = field(default_factory=dict)
    shown: dict[str, Any] = field(default_factory=dict)
    redacted: dict[str, str] = field(default_factory=dict)
    # `(name, env_var)` for each required `env:` that is unset — reported, not raised.
    missing: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class JobContext:
    """Everything a single job's skill declared, resolved for one merge request."""

    source_root: str | None = None
    inputs: Resolved = field(default_factory=Resolved)
    # Required declarations that did not resolve, and roots that are not
    # directories: collected together because both are reported the same way and
    # both stop a job before it spends anything.
    problems: list[str] = field(default_factory=list)

    def raise_for_problems(self) -> None:
        if self.problems:
            raise SkillContextError("; ".join(self.problems))


# --- parsing (config load) -------------------------------------------------


def _expand(raw: str) -> str:
    """Expand ~ and $VARS the way ``working_dir`` already does, so a declared
    path may be written the way it is written in a shell."""
    return str(Path(os.path.expandvars(raw)).expanduser())


def parse_input(name: str, decl: Any, ctx: str, base_dir: Path) -> Input:
    """Normalise one declaration. Raises SkillContextError on a bad directive.

    ``base_dir`` is the config file's directory, so ``{ file: ./x.sql }`` is
    written relative to the config that names it.
    """
    if decl is None:
        # `foo:` with nothing after it — a half-finished edit, not a value. Left
        # alone it reaches the skill as the four-letter string "None".
        raise SkillContextError(
            f"{ctx}: no value — give it a literal, a {{env: NAME}} or a {{file: ./path}}, "
            "or remove the key"
        )
    if not isinstance(decl, dict) or not (decl.keys() & _DIRECTIVE_KEYS):
        return Input(name=name, kind="literal", literal=decl)

    unknown = decl.keys() - _DIRECTIVE_KEYS
    if unknown:
        raise SkillContextError(
            f"{ctx}: unknown key(s) {', '.join(sorted(unknown))} — a directive takes "
            f"{', '.join(sorted(_DIRECTIVE_KEYS))}; for a literal mapping, use keys that "
            "are none of these"
        )
    if "env" in decl and "file" in decl:
        raise SkillContextError(f"{ctx}: give either 'env' or 'file', not both")
    required = bool(decl.get("required"))

    if "env" in decl:
        env = decl["env"]
        if not isinstance(env, str) or not env:
            raise SkillContextError(f"{ctx}: 'env' must name an environment variable")
        return Input(
            name=name,
            kind="env",
            env=env,
            required=required,
            secret=bool(decl.get("secret", True)),
        )

    if "file" in decl:
        rel = decl["file"]
        if not isinstance(rel, str) or not rel:
            raise SkillContextError(f"{ctx}: 'file' must be a path")
        path = (base_dir / _expand(rel)).resolve()
        if not path.is_file():
            raise SkillContextError(f"{ctx}: cannot read {rel!r}: no such file ({path})")
        return Input(name=name, kind="file", path=path, required=required)

    # Reachable for `{required: true}` alone — directive-shaped but naming no source.
    raise SkillContextError(
        f"{ctx}: a directive needs 'env' or 'file' to say where the value comes from"
    )


def parse_inputs(raw: Any, ctx: str, base_dir: Path) -> tuple[Input, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise SkillContextError(f"{ctx}.inputs: expected a mapping of name -> value")
    return tuple(
        parse_input(str(name), decl, f"{ctx}.inputs.{name}", base_dir)
        for name, decl in raw.items()
    )


def parse_source(raw: Any, ctx: str, base_dir: Path) -> SourceSpec | None:
    """Parse ``source:`` — one directive/literal, or a per-project mapping.

    A mapping carrying directive keys is one directive for every project; any
    other mapping is read as project -> directive, with an optional ``default``.
    """
    if raw is None:
        return None
    if isinstance(raw, dict) and not (raw.keys() & _DIRECTIVE_KEYS):
        default: Input | None = None
        by_project: list[tuple[str, Input]] = []
        for key, decl in raw.items():
            key = str(key)
            entry = parse_input("source", decl, f"{ctx}.source.{key}", base_dir)
            if key == _SOURCE_DEFAULT:
                default = entry
            else:
                by_project.append((key, entry))
        if default is None and not by_project:
            raise SkillContextError(f"{ctx}.source: empty mapping — remove it or name a project")
        return SourceSpec(default=default, by_project=tuple(by_project))
    return SourceSpec(default=parse_input("source", raw, f"{ctx}.source", base_dir))


# --- resolution (run time) -------------------------------------------------


def _value_of(decl: Input) -> tuple[Any, bool]:
    """One input's value, and whether it resolved at all."""
    if decl.kind == "env":
        value = os.environ.get(decl.env)
        return (value, value is not None and value != "")
    if decl.kind == "file":
        assert decl.path is not None
        try:
            return decl.path.read_text(encoding="utf-8"), True
        except OSError as exc:
            raise SkillContextError(f"input {decl.name!r}: cannot read {decl.path}: {exc}") from exc
    return decl.literal, True


def resolve_inputs(inputs: tuple[Input, ...]) -> Resolved:
    """Resolve every declared input into the three views (see module docstring)."""
    out = Resolved()
    for decl in inputs:
        value, present = _value_of(decl)
        if not present:
            if decl.required:
                out.missing.append((decl.name, decl.env))
            continue
        out.values[decl.name] = value
        out.redacted[decl.name] = decl.source_label if decl.kind != "literal" else str(value)
        if not (decl.kind == "env" and decl.secret):
            out.shown[decl.name] = value
    return out


def project_path_from_url(web_url: str) -> str:
    """``https://host/group/repo/-/merge_requests/7`` -> ``group/repo``.

    The snapshot carries the MR's web URL but not the project path, and a config
    author addresses a project the way they wrote it under ``gitlab.projects``.
    """
    if not web_url:
        return ""
    path = urlsplit(web_url).path.strip("/")
    return path.split("/-/", 1)[0] if "/-/" in path else ""


def resolve_source(
    spec: SourceSpec | None, project_id: object, web_url: str
) -> tuple[str | None, list[str]]:
    """The checkout for this MR's project, plus any problems with it."""
    if spec is None:
        return None, []
    decl = spec.input_for(project_id, project_path_from_url(web_url))
    if decl is None:
        # Declared, but nothing covers this project. Left silent, the skill runs
        # with no checkout at all — the same "reviewed what it never read"
        # outcome a wrong path produces, minus any hint that it happened.
        return None, [
            f"source: nothing declared for project {project_id}"
            + (f" ({project_path_from_url(web_url)})" if project_path_from_url(web_url) else "")
            + " — key the mapping by the project path or numeric id, or add a "
            "'default:' if this skill should run without a checkout"
        ]
    value, present = _value_of(decl)
    if not present:
        if decl.required:
            return None, [
                f"source: {decl.env} is not set, so the skill would be given no checkout"
            ]
        return None, []
    root = _expand(str(value).strip())
    if not str(value).strip():
        # `Path("")` is `.` — an empty declaration would silently resolve to
        # radar's own working directory and pass the is_dir() check below.
        return None, ["source: declared value is empty, so there is no checkout to point at"]
    if not Path(root).is_dir():
        # The quiet failure this exists to prevent: every read the agent tries
        # answers "no such file", which is indistinguishable from clean code, so
        # it reviews having opened nothing and the run looks entirely normal.
        return None, [
            f"source: {root!r} is not a directory — the skill would review with no access "
            "to the code and report on what it never read"
        ]
    return root, []


def job_context(skill: Any, project_id: object, web_url: str) -> JobContext:
    """Resolve a skill's declared context for one merge request.

    Never raises for a *missing* value: problems are collected so a caller can
    report them all at once (``raise_for_problems`` turns them into a refusal).
    """
    root, problems = resolve_source(getattr(skill, "source", None), project_id, web_url)
    inputs = resolve_inputs(getattr(skill, "inputs", ()))
    problems += [f"input {name!r}: {var} is not set" for name, var in inputs.missing]
    return JobContext(source_root=root, inputs=inputs, problems=problems)
