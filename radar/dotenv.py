"""Read a ``.env`` file into the environment at startup.

Everything radar needs from outside its config file arrives as an environment
variable — ``GITLAB_TOKEN``, the Jira credentials, the CA bundle behind a
TLS-inspecting proxy — because none of them belong in a file that gets
committed. Operators keep them in a ``.env`` next to ``config.yaml`` and expect
them to be picked up, which is the convention every tool in this space follows.

radar did not follow it, and the failure was silent in the worst way: a variable
that is merely absent looks exactly like one that was never needed. Jira raised
a certificate error naming no setting, and the ``.env`` line that would have
fixed it sat unread on disk.

A value already exported wins — the shell is the more specific instruction. An
exported *empty* value does not: `VAR=` is how a shell profile leaves a slot
open, and treating it as a deliberate choice is what makes a ``.env`` file look
like it is being ignored.

Values are never logged. This file routinely holds a token.
"""

from __future__ import annotations

import logging
import os
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("radar.dotenv")

_FILENAME = ".env"


@dataclass
class DotEnv:
    """What was read, and what it changed. Names only — never values."""

    files: tuple[Path, ...] = ()
    applied: tuple[str, ...] = ()  # names whose live value is the file's
    overridden: tuple[str, ...] = ()  # names where the environment says something else
    malformed: int = 0
    problems: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.files:
            return "no .env file"
        where = ", ".join(str(p) for p in self.files)
        return (
            f"{where}: {len(self.applied)} set, "
            f"{len(self.overridden)} already in the environment"
        )


def parse(text: str) -> tuple[dict[str, str], int]:
    """Parse ``.env`` text into a mapping, plus a count of unusable lines.

    Deliberately small: ``KEY=value``, an optional ``export`` prefix, one layer
    of matched quotes removed. No interpolation and no inline-comment stripping —
    a ``#`` is legal inside a path or a token, and guessing which one is a
    comment is how a credential silently becomes a truncated credential.
    """
    out: dict[str, str] = {}
    malformed = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[len("export") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or any(c.isspace() for c in key):
            malformed += 1
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out, malformed


def candidates(config_path: str | Path | None) -> tuple[Path, ...]:
    """Where to look: beside the config file radar was actually given, then the
    working directory. The config path is the one an operator names explicitly,
    so ``radar -c /etc/radar/config.yaml`` reads ``/etc/radar/.env``."""
    seen: list[Path] = []
    roots: list[Path] = []
    if config_path:
        roots.append(Path(config_path).expanduser().resolve().parent)
    roots.append(Path.cwd())
    for root in roots:
        path = root / _FILENAME
        if path not in seen:
            seen.append(path)
    return tuple(seen)


def load_dotenv(
    config_path: str | Path | None = None,
    env: MutableMapping[str, str] | None = None,
) -> DotEnv:
    """Fill unset variables from ``.env``. Never raises: a missing or unreadable
    file leaves the environment exactly as it was."""
    env = os.environ if env is None else env
    result = DotEnv()
    files: list[Path] = []
    applied: list[str] = []
    overridden: list[str] = []

    for path in candidates(config_path):
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            result.problems.append(f"{path} could not be read: {exc}")
            continue
        files.append(path)
        parsed, malformed = parse(text)
        result.malformed += malformed
        for key, value in parsed.items():
            existing = (env.get(key) or "").strip()
            # An exported empty string is an open slot, not an answer.
            if existing and existing != value:
                if key not in overridden:
                    overridden.append(key)
                continue
            # Either unset, or already holding exactly what the file says — which
            # is what a second call sees after the first one applied it. Both
            # mean the live value is the file's, and reporting that as "the
            # shell overrode it" would accuse the file of being ignored at the
            # very moment it is working.
            if not existing:
                env[key] = value
            if key not in applied:
                applied.append(key)

    result.files = tuple(files)
    result.applied = tuple(applied)
    result.overridden = tuple(overridden)
    return result
