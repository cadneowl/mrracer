"""CA-bundle propagation: one configured trust store, both HTTP stacks.

radar reaches GitLab through requests and Jira through urllib, and the two read
disjoint environment variables. Behind a TLS-inspecting proxy that asymmetry is
what makes the board poll happily while the QA button fails a handshake.
"""

from __future__ import annotations

import ssl
from pathlib import Path

import pytest

from radar.tls import ca_trust_state, sync_ca_bundle

REQUESTS_VARS = ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
URLLIB_VARS = ("SSL_CERT_FILE", "SSL_CERT_DIR")
ALL_VARS = (*REQUESTS_VARS, *URLLIB_VARS)


@pytest.fixture
def ca(tmp_path):
    pem = tmp_path / "corp-root.pem"
    pem.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    return pem


def _env(**kwargs) -> dict[str, str]:
    return dict(kwargs)


# --- propagation -----------------------------------------------------------


def test_no_bundle_configured_changes_nothing():
    env = _env(PATH="/usr/bin")
    assert sync_ca_bundle(env) is None
    assert env == {"PATH": "/usr/bin"}  # the system trust store is left alone


@pytest.mark.parametrize("source", ["REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "CURL_CA_BUNDLE"])
def test_a_bundle_set_for_one_stack_reaches_both(ca, source):
    """The actual bug: a proxy installer exports one variable, and whichever
    backend reads a different one fails with a certificate error."""
    env = _env(**{source: str(ca)})
    result = sync_ca_bundle(env)

    assert result is not None and result.problem is None
    assert result.source == source
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        assert env[var] == str(ca), f"{var} was left unset, so one stack still fails"
    assert source not in result.applied  # it reports what it added, not what it read


def test_an_existing_value_is_never_overwritten(ca, tmp_path):
    other = tmp_path / "other.pem"
    other.write_text("x", encoding="utf-8")
    env = _env(SSL_CERT_FILE=str(ca), REQUESTS_CA_BUNDLE=str(other))

    sync_ca_bundle(env)

    # Two different bundles is a deliberate act, not a mistake to reconcile.
    assert env["SSL_CERT_FILE"] == str(ca)
    assert env["REQUESTS_CA_BUNDLE"] == str(other)
    assert env["CURL_CA_BUNDLE"] == str(ca)  # only the genuinely unset one is filled


def test_a_directory_bundle_goes_to_the_variables_that_accept_one(tmp_path):
    cadir = tmp_path / "certs"
    cadir.mkdir()
    env = _env(SSL_CERT_DIR=str(cadir))

    sync_ca_bundle(env)

    assert env["REQUESTS_CA_BUNDLE"] == str(cadir)  # requests takes a dir
    assert "CURL_CA_BUNDLE" not in env  # file-only; a dir there would be wrong
    assert "SSL_CERT_FILE" not in env


def test_a_missing_path_is_refused_rather_than_propagated(tmp_path):
    """Python drops a non-existent SSL_CERT_FILE and silently falls back to the
    system store, so copying a typo around would hide the cause of the failure
    in three variables instead of one."""
    ghost = tmp_path / "not-here.pem"
    env = _env(REQUESTS_CA_BUNDLE=str(ghost))

    result = sync_ca_bundle(env)

    assert result is not None and result.problem is not None
    assert "does not exist" in result.problem and str(ghost) in result.problem
    assert "SSL_CERT_FILE" not in env and "CURL_CA_BUNDLE" not in env


def test_a_relative_path_is_anchored_before_it_is_inherited(tmp_path, monkeypatch):
    """A skill runs with its own working directory — the checkout, or a per-job
    worktree — so a relative bundle inherited from radar resolves to nothing
    there. That child is the one calling an LLM API through the same proxy."""
    ca = tmp_path / "ca.pem"
    ca.write_text("x", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    env = _env(REQUESTS_CA_BUNDLE="./ca.pem")

    result = sync_ca_bundle(env)

    assert result.problem is None
    for var in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "CURL_CA_BUNDLE"):
        assert Path(env[var]).is_absolute(), f"{var} would break in a child's cwd"
        assert Path(env[var]).is_file()


def test_a_quoted_value_is_not_reported_as_missing(ca):
    # cmd.exe's `set VAR="C:\certs\ca.pem"` keeps the quotes in the value.
    env = _env(REQUESTS_CA_BUNDLE=f'"{ca}"')
    result = sync_ca_bundle(env)
    assert result.problem is None
    assert env["SSL_CERT_FILE"] == str(ca)


def test_every_variable_is_cleaned_not_just_the_one_read_from(ca):
    """A setup script that quotes both exports would otherwise leave the
    unread one broken — and the report, which normalises before comparing,
    would call the pair healthy while requests raised on every call."""
    env = _env(SSL_CERT_FILE=f'"{ca}"', REQUESTS_CA_BUNDLE=f'"{ca}"')

    sync_ca_bundle(env)

    assert env["SSL_CERT_FILE"] == str(ca)
    assert env["REQUESTS_CA_BUNDLE"] == str(ca)  # cleaned despite not being the source


def test_blank_is_treated_as_unset(ca):
    env = _env(SSL_CERT_FILE="   ", REQUESTS_CA_BUNDLE=str(ca))
    result = sync_ca_bundle(env)
    assert result.source == "REQUESTS_CA_BUNDLE"
    assert env["SSL_CERT_FILE"] == str(ca)  # whitespace didn't count as configured


def test_running_twice_is_a_no_op(ca):
    env = _env(SSL_CERT_FILE=str(ca))
    first = sync_ca_bundle(env)
    snapshot = dict(env)
    second = sync_ca_bundle(env)
    assert env == snapshot
    assert second.applied == ()  # nothing left to add
    assert first.applied  # the first call did the work


# --- the mechanism actually works ------------------------------------------


def test_ssl_module_picks_up_the_exported_file(ca, monkeypatch):
    """Not a restatement of the code: this asserts that setting the variable at
    startup is early enough. OpenSSL reads SSL_CERT_FILE when a context is
    built, not when ssl is imported, which is the whole reason a main()-time
    export works at all."""
    for var in ALL_VARS:
        monkeypatch.delenv(var, raising=False)
    assert ssl.get_default_verify_paths().cafile != str(ca)

    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca))  # the variable ssl ignores
    assert ssl.get_default_verify_paths().cafile != str(ca)

    sync_ca_bundle()  # mutates the real environment; monkeypatch undoes it

    assert ssl.get_default_verify_paths().cafile == str(ca)


def test_requests_picks_up_the_exported_bundle(ca, monkeypatch):
    requests = pytest.importorskip("requests")
    for var in ALL_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", str(ca))  # the variable requests ignores

    session = requests.Session()
    before = session.merge_environment_settings("https://x/", {}, None, True, None)["verify"]
    assert before is True, "requests read SSL_CERT_FILE after all; this test is stale"

    sync_ca_bundle()

    after = session.merge_environment_settings("https://x/", {}, None, True, None)["verify"]
    assert after == str(ca)  # GitLab calls now trust the same bundle Jira does


# --- reporting -------------------------------------------------------------


def test_trust_state_is_ok_when_both_stacks_agree(ca, monkeypatch):
    for var in ALL_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca))
    sync_ca_bundle()
    status, detail = ca_trust_state()
    assert status == "ok" and str(ca) in detail


def test_trust_state_skips_when_nothing_is_configured(monkeypatch):
    for var in ALL_VARS:
        monkeypatch.delenv(var, raising=False)
    status, detail = ca_trust_state()
    assert status == "skip" and "system trust store" in detail


def test_trust_state_names_a_split_brain(ca, tmp_path, monkeypatch):
    other = tmp_path / "other.pem"
    other.write_text("x", encoding="utf-8")
    for var in ALL_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca))
    monkeypatch.setenv("SSL_CERT_FILE", str(other))

    status, detail = ca_trust_state()

    assert status == "warn"
    assert str(ca) in detail and str(other) in detail  # says which side trusts what


def test_a_missing_requests_bundle_fails_rather_than_warns(tmp_path, monkeypatch):
    """The two stacks fail differently and the report must not flatten that:
    requests raises OSError and makes no call at all, so a missing bundle on
    that side means GitLab polling is dead, not degraded."""
    for var in ALL_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "ghost.pem"))
    status, detail = ca_trust_state()
    assert status == "fail" and "every GitLab call fails" in detail


def test_a_missing_ssl_bundle_only_warns(tmp_path, monkeypatch):
    # urllib ignores it and falls back to the system store, which may be fine.
    for var in ALL_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "ghost.pem"))
    status, detail = ca_trust_state()
    assert status == "warn" and "system trust store" in detail


def test_a_broken_bundle_on_one_side_is_not_masked_by_a_good_one(ca, tmp_path, monkeypatch):
    """`sync` will not overwrite the operator's own REQUESTS_CA_BUNDLE, so a
    working SSL_CERT_FILE next to a broken one leaves GitLab hard-failing — the
    report has to say so rather than reporting the healthy half."""
    for var in ALL_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "ghost.pem"))
    monkeypatch.setenv("SSL_CERT_FILE", str(ca))

    sync_ca_bundle()

    assert ca_trust_state()[0] == "fail"


def test_diagnostics_reports_tls_before_the_calls_that_need_it(tmp_path, monkeypatch):
    from radar.diagnostics import _check_tls

    for var in ALL_VARS:
        monkeypatch.delenv(var, raising=False)
    check = _check_tls()
    assert check.name == "tls.ca_bundle" and check.status == "skip"
