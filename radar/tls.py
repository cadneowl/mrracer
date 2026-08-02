"""Trusting the organisation's CA, whichever variable happens to name it.

Behind a TLS-inspecting proxy (Zscaler, Netskope, a corporate MITM appliance)
every HTTPS call must trust the proxy's root certificate instead of the public
roots. There is no one environment variable for that, and radar talks to its two
backends through two HTTP stacks that read *disjoint* sets:

    GitLab   python-gitlab -> requests   reads REQUESTS_CA_BUNDLE, CURL_CA_BUNDLE
    Jira     urllib.request -> ssl       reads SSL_CERT_FILE, SSL_CERT_DIR

Neither stack looks at the other's variables. A proxy installer sets whichever
one it favours, so exactly one half of radar works and the other half fails with
a certificate error that names none of this — the board polls fine and the QA
button reports a handshake failure, or the reverse.

``sync_ca_bundle`` copies whatever the operator *did* set across to the ones they
didn't, once, at startup. Skills inherit the environment, so a ``claude -p`` child
behind the same proxy gets it too.

Two things are deliberate. An already-set variable is never overwritten: if the
operator pointed two stacks at two different bundles, they meant it, and
``ca_trust_state`` reports the disagreement rather than resolving it. And a path
that does not exist is refused rather than exported — Python drops a
non-existent ``SSL_CERT_FILE`` silently and falls back to the system store, so
propagating a typo would turn one honest failure into an unexplained one.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path

# Variables naming a single PEM bundle, and the one naming an OpenSSL hash dir.
# Order is the search order: the first one set decides what everything trusts.
_FILE_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
_DIR_VARS = ("SSL_CERT_DIR",)
_SEARCH = (*_FILE_VARS, *_DIR_VARS)

# requests accepts a directory for REQUESTS_CA_BUNDLE (urllib3 takes it as
# ca_cert_dir); CURL_CA_BUNDLE is file-only, so a directory is not copied there.
_DIR_TARGETS = ("SSL_CERT_DIR", "REQUESTS_CA_BUNDLE")


def _unquote(value: str) -> str:
    """Drop one layer of matched surrounding quotes.

    ``set VAR="C:\\certs\\ca.pem"`` in cmd.exe keeps the quotes in the value, and
    the resulting path is reported as missing — technically true, and useless.
    No filesystem in play permits a quote at both ends of a real name.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1].strip()
    return value


@dataclass(frozen=True)
class CaBundle:
    """What the trust store was set from, and where it was propagated to."""

    source: str  # the variable the operator set
    path: str
    applied: tuple[str, ...] = ()  # variables this filled in from it
    problem: str | None = None  # set when `path` is unusable; nothing was applied

    def summary(self) -> str:
        if self.problem:
            return self.problem
        where = ", ".join(self.applied) if self.applied else "already set everywhere"
        return f"{self.source}={self.path} -> {where}"


def sync_ca_bundle(env: MutableMapping[str, str] | None = None) -> CaBundle | None:
    """Give every HTTP stack the CA bundle the operator configured for one.

    Returns None when no bundle variable is set at all — the ordinary case off a
    proxied network, where the system trust store is correct and untouched.
    """
    env = os.environ if env is None else env

    # Normalise every bundle variable in place first. Quoting and relativeness
    # are the same file wearing a different name, and cleaning only the one this
    # happens to read from would leave a second variable in a shape its stack
    # rejects — while `ca_trust_state`, which normalises in order to compare,
    # called the pair healthy. A relative path is anchored to radar's working
    # directory here, once: skills run in the checkout (or a per-job worktree),
    # and that child is the one calling an LLM API through the same proxy.
    for var in _SEARCH:
        raw = (env.get(var) or "").strip()
        if not raw:
            continue
        clean = _unquote(raw)
        if not os.path.isabs(clean):
            clean = os.path.abspath(clean)
        if clean != env[var]:
            env[var] = clean

    source = next((var for var in _SEARCH if (env.get(var) or "").strip()), None)
    if source is None:
        return None
    path = env[source]

    is_dir = Path(path).is_dir()
    if not is_dir and not Path(path).is_file():
        return CaBundle(
            source=source,
            path=path,
            problem=(
                f"{source}={path} does not exist, so it was not passed on to the other "
                "TLS variables. Python ignores a missing bundle and falls back to the "
                "system trust store, so HTTPS will fail with a certificate error that "
                "does not mention this setting."
            ),
        )

    targets = _DIR_TARGETS if is_dir else _FILE_VARS
    applied = tuple(var for var in targets if var != source and not (env.get(var) or "").strip())
    for var in applied:
        env[var] = path
    return CaBundle(source=source, path=path, applied=applied)


def ca_trust_state(env: MutableMapping[str, str] | None = None) -> tuple[str, str]:
    """(status, detail) describing what each stack will actually trust.

    Read-only, and reported per stack rather than per variable, because the
    question an operator has after a certificate error is "which bundle did the
    call that failed use?" — not which variable is exported.
    """
    env = os.environ if env is None else env

    def value(*names: str) -> str:
        return next((_unquote(env[n].strip()) for n in names if (env.get(n) or "").strip()), "")

    gitlab = value("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
    jira = value("SSL_CERT_FILE", "SSL_CERT_DIR")

    if not gitlab and not jira:
        return "skip", "no CA bundle set; using the system trust store"

    # The two stacks disagree about what a missing bundle means, so the report
    # does too. requests refuses to make the call at all; urllib ignores the
    # setting and quietly uses the system store, which may well work.
    if gitlab and not Path(gitlab).exists():
        return "fail", (
            f"{gitlab} does not exist — requests refuses to run without the bundle it "
            "was pointed at, so every GitLab call fails"
        )
    if jira and not Path(jira).exists():
        return "warn", (
            f"{jira} does not exist — it is ignored, so Jira falls back to the system "
            "trust store and will fail if that store lacks your proxy's CA"
        )
    if not gitlab or not jira:
        blind = "GitLab (requests)" if not gitlab else "Jira (urllib)"
        return "warn", f"{blind} has no CA bundle set while the other does"
    if gitlab != jira:
        return "warn", f"GitLab trusts {gitlab}, Jira trusts {jira}"
    return "ok", f"{gitlab} (GitLab + Jira)"
