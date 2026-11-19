"""`related_sources:` — the other repositories a skill must take into account.

Driven through real git and a real command, like the worktree tests: the claims
are about which tree a run actually read, and what it was told about it.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from radar.config import ConfigError
from radar.db import Database
from radar.diagnostics import _check_related_sources
from radar.web.app import create_app
from tests.test_worktree import PY, _config, _git, _run, _seed

# Prints the file `ui.txt` under the directory passed as --ui=..., then STDIN.
READ_UI = (
    f'{PY} -c "import sys,pathlib; root=sys.argv[1].split(chr(61),1)[1]; '
    "print(pathlib.Path(root, chr(117)+chr(105)+chr(46)+chr(116)+chr(120)+chr(116)).read_text()); "
    'print(sys.stdin.read())" --ui={source_ui}'
)


@pytest.fixture
def ui_repo(tmp_path):
    """A UI repository whose clone sits on `dev` while `main` says otherwise —
    so a run can only see "UI-MAIN" if it was pinned to main."""
    upstream = tmp_path / "ui-upstream"
    upstream.mkdir()
    _git("init", "-b", "main", cwd=upstream)
    _git("config", "user.email", "t@example.com", cwd=upstream)
    _git("config", "user.name", "t", cwd=upstream)
    (upstream / "ui.txt").write_text("UI-MAIN", encoding="utf-8")
    _git("add", ".", cwd=upstream)
    _git("commit", "-m", "main", cwd=upstream)

    clone = tmp_path / "ui"
    _git("clone", str(upstream), str(clone), cwd=tmp_path)
    _git("config", "user.email", "t@example.com", cwd=clone)
    _git("config", "user.name", "t", cwd=clone)
    _git("checkout", "-b", "dev", cwd=clone)
    (clone / "ui.txt").write_text("UI-DEV", encoding="utf-8")
    _git("commit", "-am", "dev", cwd=clone)
    return clone


def _skill(command: str, related: str) -> str:
    return (
        "skills:\n"
        "  - name: dba\n"
        "    enabled: true\n"
        f"    command: '{command}'\n"
        "    timeout_seconds: 60\n"
        "    related_sources:\n" + related
    )


def _serve(cfg, tmp_path) -> TestClient:
    db_path = tmp_path / "r.db"
    db = Database(db_path)
    _seed(db)  # MR !7, target_branch main
    db.close()
    return TestClient(create_app(cfg, str(db_path)))


# --- config ---------------------------------------------------------------


def test_a_related_source_takes_a_path_or_a_mapping(tmp_path, ui_repo):
    cfg = _config(
        tmp_path,
        _skill(
            "echo {source_ui} {source_common}",
            f"      ui: {{path: {ui_repo}, about: calls our APIs, ref: '{{target_branch}}'}}\n"
            f"      common: {tmp_path}\n",
        ),
    )
    ui, common = cfg.skill_by_name("dba").related_sources
    assert (ui.name, ui.path, ui.about, ui.ref, ui.remote) == (
        "ui", str(ui_repo), "calls our APIs", "{target_branch}", "origin"
    )
    assert (common.name, common.path, common.ref) == ("common", str(tmp_path), "")


@pytest.mark.parametrize(
    ("related", "match"),
    [
        ("      my-ui: /tmp\n", "lowercase letters"),
        ("      root: /tmp\n", r"\{source_root\} is already a placeholder"),
        ("      branch: /tmp\n", r"\{source_branch\} is already a placeholder"),
        ("      ui: {about: x}\n", "missing 'path'"),
        ("      ui: /no/such/dir/anywhere\n", "does not exist"),
        ("      ui: {path: /tmp, branch: main}\n", "unknown key"),
        ("      ui: {path: /tmp, ref: '{title}'}\n", "cannot name a branch"),
    ],
)
def test_a_related_source_that_cannot_work_is_refused(tmp_path, related, match):
    with pytest.raises(ConfigError, match=match):
        _config(tmp_path, _skill("echo hi", related))


def test_a_misspelt_placeholder_is_refused_not_passed_through(tmp_path):
    """`--add-dir {source_iu}` would grant a directory literally named that, and
    the run would read the UI as absent without anyone being told."""
    with pytest.raises(ConfigError, match=r"\{source_iu\} but no related source"):
        _config(tmp_path, _skill("claude --add-dir {source_iu}", "      ui: /tmp\n"))


def test_the_built_in_source_placeholders_are_not_taken_for_related_ones(tmp_path):
    """{source_branch} is the MR's branch, not a related source nobody declared."""
    cfg = _config(
        tmp_path, _skill("tool --branch {source_branch} {source_root}", "      ui: /tmp\n")
    )
    assert cfg.skill_by_name("dba").command.endswith("{source_root}")


def test_a_relative_path_is_read_from_the_configs_directory(tmp_path, monkeypatch):
    (tmp_path / "ui").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # radar started from somewhere else entirely
    cfg = _config(tmp_path, _skill("echo {source_ui}", "      ui: ui\n"))
    assert cfg.skill_by_name("dba").related_sources[0].path == str(tmp_path / "ui")


def test_a_pipeline_cannot_carry_related_sources(tmp_path):
    with pytest.raises(ConfigError, match="related_sources would do nothing here"):
        _config(
            tmp_path,
            "skills:\n"
            "  - name: a\n    enabled: true\n    command: echo a\n"
            "  - name: both\n    enabled: true\n    pipeline: [a]\n"
            "    related_sources: {ui: /tmp}\n",
        )


# --- a run ----------------------------------------------------------------


def test_an_unpinned_source_reaches_the_command_and_the_bundle(tmp_path, ui_repo):
    cfg = _config(
        tmp_path,
        _skill(READ_UI, f"      ui: {{path: {ui_repo}, about: Calls this repo's REST APIs.}}\n"),
    )
    html = _run(_serve(cfg, tmp_path), "dba")

    assert "review-output" in html, html
    assert "UI-DEV" in html  # the directory as it is checked out
    assert "Related sources" in html
    assert str(ui_repo) in html and "Calls this repo" in html


def test_a_pinned_source_is_read_at_the_target_branch_then_removed(tmp_path, ui_repo):
    """The clone sits on `dev`; the MR targets `main`. Only a run that really
    checked out main can print UI-MAIN — and the operator's checkout must be
    exactly where it was afterwards."""
    cfg = _config(
        tmp_path,
        _skill(READ_UI, f"      ui: {{path: {ui_repo}, ref: '{{target_branch}}'}}\n"),
    )
    html = _run(_serve(cfg, tmp_path), "dba")

    assert "review-output" in html, html
    assert "UI-MAIN" in html and "UI-DEV" not in html
    assert "checked out at <code>main</code>" in html or "checked out at `main`" in html
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=ui_repo) == "dev"

    deadline = time.time() + 15
    while "radar-ref-" in _git("worktree", "list", cwd=ui_repo):
        assert time.time() < deadline, "the pinned worktree was not removed"
        time.sleep(0.05)


def test_a_pinned_subdirectory_stays_that_subdirectory(tmp_path, ui_repo):
    """A worktree is of the whole repository; a source that names a folder in it
    must be handed that folder of the pinned tree, not the repository's root."""
    upstream = tmp_path / "ui-upstream"
    (upstream / "web").mkdir()
    (upstream / "web" / "ui.txt").write_text("WEB-MAIN", encoding="utf-8")
    _git("add", ".", cwd=upstream)
    _git("commit", "-m", "web", cwd=upstream)
    (ui_repo / "web").mkdir()  # in the clone, but not yet fetched
    cfg = _config(
        tmp_path,
        _skill(READ_UI, f"      ui: {{path: {ui_repo / 'web'}, ref: '{{target_branch}}'}}\n"),
    )
    html = _run(_serve(cfg, tmp_path), "dba")
    assert "review-output" in html, html
    assert "WEB-MAIN" in html and "UI-MAIN" not in html


def test_a_branch_the_related_repo_lacks_fails_the_job(tmp_path, ui_repo):
    """Reading some other branch instead would answer a question nobody asked."""
    cfg = _config(
        tmp_path,
        _skill(READ_UI, f"      ui: {{path: {ui_repo}, ref: release/9.9}}\n"),
    )
    html = _run(_serve(cfg, tmp_path), "dba")
    assert "review-error" in html
    assert "release/9.9" in html


# --- radar check ------------------------------------------------------------


def test_radar_check_reports_related_sources(tmp_path, ui_repo):
    plain = tmp_path / "plain"
    plain.mkdir()
    cfg = _config(
        tmp_path,
        _skill(
            "echo hi",
            f"      ui: {{path: {ui_repo}, ref: main}}\n"
            f"      docs: {{path: {plain}, ref: main}}\n",
        ),
    )
    (check,) = _check_related_sources(cfg)
    assert check.status == "fail"
    assert "docs:" in check.detail and "not a git repository" in check.detail
    assert "ui:" not in check.detail  # the git repo with an origin is fine

    ok = _config(tmp_path, _skill("echo hi", f"      ui: {{path: {ui_repo}, ref: main}}\n"))
    (check,) = _check_related_sources(ok)
    assert check.status == "ok"
    assert f"ui -> {ui_repo} at main" in check.detail


def test_radar_check_covers_a_disabled_skill_that_runs_as_a_pipeline_step(tmp_path, ui_repo):
    """It runs whenever its pipeline does, so its sources must be checked too."""
    plain = tmp_path / "plain"
    plain.mkdir()
    cfg = _config(
        tmp_path,
        "skills:\n"
        "  - name: dba\n    enabled: false\n    command: echo hi\n"
        f"    related_sources: {{docs: {{path: {plain}, ref: main}}}}\n"
        "  - name: full\n    enabled: true\n    pipeline: [dba]\n",
    )
    (check,) = _check_related_sources(cfg)
    assert check.name == "dba.related" and check.status == "fail"
