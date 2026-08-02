"""Reading a dotenv file at startup.

Every secret and machine-specific path radar needs arrives as an environment
variable, and operators keep them in a file beside config.yaml. radar not
reading it failed silently: an unset variable is indistinguishable from one
that was never needed, so a CA bundle sat unread on disk while Jira raised a
certificate error naming no setting at all.
"""

from __future__ import annotations

import pytest

from radar.dotenv import candidates, load_dotenv, parse

FILENAME = ".env"


def _write(directory, text):
    path = directory / FILENAME
    path.write_text(text, encoding="utf-8")
    return path


# --- parsing ---------------------------------------------------------------


def test_parses_the_ordinary_shapes():
    parsed, malformed = parse(
        "\n".join(
            [
                "# a comment",
                "",
                "PLAIN=value",
                "  SPACED = padded  ",
                "export EXPORTED=from-a-sourceable-file",
                "QUOTED='single'",
                'DQUOTED="double"',
                "EMPTY=",
            ]
        )
    )
    assert malformed == 0
    assert parsed == {
        "PLAIN": "value",
        "SPACED": "padded",
        "EXPORTED": "from-a-sourceable-file",
        "QUOTED": "single",
        "DQUOTED": "double",
        "EMPTY": "",
    }


def test_a_hash_inside_a_value_is_kept():
    """Stripping inline comments would quietly truncate a token — the failure
    then looks like bad credentials rather than bad parsing."""
    parsed, _ = parse("TOKEN=abc#def\nPATH_=/opt/certs/ca#1.pem")
    assert parsed["TOKEN"] == "abc#def"
    assert parsed["PATH_"] == "/opt/certs/ca#1.pem"


def test_unusable_lines_are_counted_not_guessed_at():
    parsed, malformed = parse("GOOD=1\nnot-an-assignment\nHAS SPACE=2\n=novalue\n")
    assert parsed == {"GOOD": "1"}
    assert malformed == 3


# --- loading ---------------------------------------------------------------


def test_values_reach_the_environment(tmp_path):
    _write(tmp_path, "GITLAB_TOKEN=glpat-xyz\nREQUESTS_CA_BUNDLE=/opt/corp/ca.pem\n")
    env: dict[str, str] = {}

    result = load_dotenv(tmp_path / "config.yaml", env)

    assert env["GITLAB_TOKEN"] == "glpat-xyz"
    assert env["REQUESTS_CA_BUNDLE"] == "/opt/corp/ca.pem"
    assert set(result.applied) == {"GITLAB_TOKEN", "REQUESTS_CA_BUNDLE"}


def test_an_exported_value_wins_over_the_file(tmp_path):
    _write(tmp_path, "GITLAB_TOKEN=from-file\n")
    env = {"GITLAB_TOKEN": "from-shell"}

    result = load_dotenv(tmp_path / "config.yaml", env)

    assert env["GITLAB_TOKEN"] == "from-shell"  # the more specific instruction
    assert result.overridden == ("GITLAB_TOKEN",)
    assert result.applied == ()


def test_an_exported_empty_value_does_not_win(tmp_path):
    """The exact shape that made this bite: a profile exports `VAR=` to leave a
    slot open, a no-override loader treats that as an answer, and the file line
    that would have fixed it is skipped."""
    _write(tmp_path, "REQUESTS_CA_BUNDLE=/opt/corp/ca.pem\n")
    env = {"REQUESTS_CA_BUNDLE": ""}

    load_dotenv(tmp_path / "config.yaml", env)

    assert env["REQUESTS_CA_BUNDLE"] == "/opt/corp/ca.pem"


def test_it_is_read_from_beside_the_config_that_was_named(tmp_path):
    # `radar -c /etc/radar/config.yaml` must read /etc/radar/.env, not ./.env
    elsewhere = tmp_path / "etc"
    elsewhere.mkdir()
    _write(elsewhere, "JIRA_EMAIL=ops@example.com\n")
    env: dict[str, str] = {}

    load_dotenv(elsewhere / "config.yaml", env)

    assert env["JIRA_EMAIL"] == "ops@example.com"


def test_the_config_directory_is_searched_before_the_cwd(tmp_path, monkeypatch):
    beside = tmp_path / "etc"
    beside.mkdir()
    cwd = tmp_path / "work"
    cwd.mkdir()
    _write(beside, "WHO=config-dir\n")
    _write(cwd, "WHO=cwd\nONLY_IN_CWD=yes\n")
    monkeypatch.chdir(cwd)
    env: dict[str, str] = {}

    result = load_dotenv(beside / "config.yaml", env)

    assert env["WHO"] == "config-dir"  # first file to set a name keeps it
    assert env["ONLY_IN_CWD"] == "yes"  # but the second still contributes
    assert len(result.files) == 2


def test_loading_twice_still_credits_the_file(tmp_path):
    """`radar check` re-reads the file to report on it, after startup already
    applied it. Comparing names alone would make every variable look like the
    shell had overridden it — accusing the file of being ignored at exactly the
    moment it is working."""
    _write(tmp_path, "REQUESTS_CA_BUNDLE=/opt/corp/ca.pem\n")
    env: dict[str, str] = {}

    first = load_dotenv(tmp_path / "config.yaml", env)
    second = load_dotenv(tmp_path / "config.yaml", env)

    assert first.applied == ("REQUESTS_CA_BUNDLE",)
    assert second.applied == ("REQUESTS_CA_BUNDLE",)
    assert second.overridden == ()


def test_a_missing_file_changes_nothing(tmp_path):
    env = {"KEEP": "me"}
    result = load_dotenv(tmp_path / "config.yaml", env)
    assert env == {"KEEP": "me"}
    assert result.files == () and result.applied == ()


def test_an_unreadable_file_is_reported_not_raised(tmp_path, monkeypatch):
    _write(tmp_path, "A=1\n")

    def boom(*_args, **_kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr("pathlib.Path.read_text", boom)
    env: dict[str, str] = {}

    result = load_dotenv(tmp_path / "config.yaml", env)  # must not raise

    assert env == {}
    assert result.problems and "permission denied" in result.problems[0]


def test_candidates_do_not_repeat_when_config_sits_in_the_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert len(candidates(tmp_path / "config.yaml")) == 1


# --- the reported failure, end to end --------------------------------------


def test_a_ca_bundle_in_the_file_reaches_both_http_stacks(tmp_path, monkeypatch):
    """The bug as reported: the CA path lived in the file, so nothing was in the
    environment for the TLS sync to propagate and Jira failed its handshake.
    Loading the file first is what makes the sync have something to work with —
    and it fixes GitLab and the skills radar launches, not just Jira."""
    from radar.tls import ca_trust_state, sync_ca_bundle

    ca = tmp_path / "corp-root.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    _write(tmp_path, f"REQUESTS_CA_BUNDLE={ca}\n")

    for var in ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        monkeypatch.delenv(var, raising=False)

    assert sync_ca_bundle() is None  # nothing in the environment to propagate
    assert ca_trust_state()[0] == "skip"

    load_dotenv(tmp_path / "config.yaml")
    sync_ca_bundle()

    status, detail = ca_trust_state()
    assert status == "ok" and str(ca) in detail

    import os

    assert os.environ["SSL_CERT_FILE"] == str(ca)  # urllib (Jira) now trusts it
    assert os.environ["REQUESTS_CA_BUNDLE"] == str(ca)  # requests (GitLab) too


def test_diagnostics_reports_what_the_file_contributed(tmp_path):
    from radar.diagnostics import _check_dotenv

    _write(tmp_path, "REQUESTS_CA_BUNDLE=/opt/corp/ca.pem\n")
    check = _check_dotenv(tmp_path / "config.yaml")
    assert check.name == "env.dotenv" and check.status == "ok"
    assert "REQUESTS_CA_BUNDLE" in check.detail


def test_diagnostics_says_where_it_looked_when_there_is_no_file(tmp_path):
    from radar.diagnostics import _check_dotenv

    check = _check_dotenv(tmp_path / "config.yaml")
    assert check.status == "skip" and "looked in" in check.detail


def test_diagnostics_never_prints_a_value(tmp_path):
    from radar.diagnostics import _check_dotenv

    _write(tmp_path, "GITLAB_TOKEN=glpat-SUPERSECRET\n")
    check = _check_dotenv(tmp_path / "config.yaml")
    assert "GITLAB_TOKEN" in check.detail  # the name is useful
    assert "SUPERSECRET" not in check.detail  # the value never is


@pytest.mark.parametrize("name", ["GITLAB_TOKEN", "JIRA_API_TOKEN"])
def test_a_loaded_secret_is_still_kept_from_the_skill_env(tmp_path, monkeypatch, name):
    """Loading credentials from a file must not smuggle them past the denylist
    that keeps radar's own tokens out of the child process."""
    from radar.commands import _ENV_DENYLIST

    monkeypatch.delenv(name, raising=False)
    _write(tmp_path, f"{name}=secret-value\n")
    load_dotenv(tmp_path / "config.yaml")

    assert name in _ENV_DENYLIST
