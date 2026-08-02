"""The declared context bag: where a skill's source is, and what else it gets.

Covers the three value forms, the per-project source root, the refusals that
must happen *before* a job launches, and — the case radar actually runs — several
skills with different declarations working side by side.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from radar.config import ConfigError, load_config
from radar.db import Database
from radar.diagnostics import _check_skill_context
from radar.events import EventType as ET
from radar.skillcontext import (
    SkillContextError,
    job_context,
    parse_input,
    parse_source,
    project_path_from_url,
    resolve_inputs,
    resolve_source,
)
from radar.web.app import create_app
from tests.conftest import ev, ny

PY = f'"{sys.executable}"'

# Prints cwd, then each argv value, then whatever arrived on stdin — enough to
# assert what a skill was actually given, end to end.
ECHO_ALL = (
    f'{PY} -c "import sys,os; sys.stdout.write(os.getcwd()+chr(10)+'
    f'chr(10).join(sys.argv[1:])+chr(10)+sys.stdin.read())"'
)

# Writes a marker file named by its first argument, and prints something (a job
# with empty output is an error, however cleanly it exited).
TOUCH = (
    f'{PY} -c "import sys,pathlib; pathlib.Path(sys.argv[1]).write_text(chr(120)); '
    f'print(chr(111)+chr(107))"'
)

_BASE = """
gitlab: {{projects: [{projects}]}}
calendar:
  workdays: [mon, tue, wed, thu, fri]
  work_hours: {{start: "09:00", end: "18:00"}}
  default_timezone: America/New_York
slas:
  - match: {{}}
    first_response_business_hours: 16
    approval_business_hours: 24
waive: {{draft: true}}
{extra}
"""


def _config(tmp_path, extra, projects="g/p"):
    path = tmp_path / "config.yaml"
    path.write_text(_BASE.format(extra=extra, projects=projects), encoding="utf-8")
    return load_config(path)


def _seed(db, project_id=1, mr_iid=7, path="g/p"):
    db.upsert_mr_snapshot(
        project_id=project_id, mr_iid=mr_iid, title="Add widget", author="aviva",
        web_url=f"https://gitlab.example.com/{path}/-/merge_requests/{mr_iid}",
        source_branch="f", target_branch="main", description="", labels=[], draft=False,
        state="opened", reviewers=["dan"], created_at="2026-03-02T09:00:00Z",
        updated_at="2026-03-02T09:00:00Z",
    )
    db.insert_events(
        [ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan", mr_iid=mr_iid)]
    )


def _start(client, kind, project_id=1, mr_iid=7):
    """Press a skill's button. Returns (job_id, panel_html); job_id is None when
    the job was refused before launching."""
    start = client.post(f"/{kind}/{project_id}/{mr_iid}")
    assert start.status_code == 200
    match = re.search(r'data-job-id="([0-9a-f]+)"', start.text)
    return (match.group(1) if match else None), start.text


def _await_panel(client, kind, job_id, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        html = client.get(f"/{kind}/status/{job_id}").text
        if "review-output" in html or "review-error" in html:
            return html
        time.sleep(0.05)
    raise AssertionError(f"{kind} job did not finish")


def _run(client, kind, project_id=1, mr_iid=7, timeout=20.0):
    """Press a skill's button and wait for the panel to settle."""
    job_id, html = _start(client, kind, project_id, mr_iid)
    if job_id is None:  # refused before launching — the panel already shows why
        return html
    return _await_panel(client, kind, job_id, timeout)


# --- value forms -----------------------------------------------------------


def test_literal_env_and_file_forms(tmp_path, monkeypatch):
    schema = tmp_path / "schema.sql"
    schema.write_text("CREATE TABLE t (id int);", encoding="utf-8")
    monkeypatch.setenv("SPEC_TOKEN", "s3cret")

    inputs = (
        parse_input("api", "https://internal/spec.json", "x", tmp_path),
        parse_input("db_schema", {"file": "./schema.sql"}, "x", tmp_path),
        parse_input("token", {"env": "SPEC_TOKEN"}, "x", tmp_path),
    )
    resolved = resolve_inputs(inputs)

    assert resolved.values["api"] == "https://internal/spec.json"
    assert "CREATE TABLE" in resolved.values["db_schema"]
    assert resolved.values["token"] == "s3cret"
    # An env value is a credential until the author says otherwise: resolved,
    # but never written into the prompt bundle.
    assert "token" not in resolved.shown
    assert resolved.shown["api"] == "https://internal/spec.json"
    assert "CREATE TABLE" in resolved.shown["db_schema"]
    assert resolved.redacted["token"] == "<env:SPEC_TOKEN>"
    assert "s3cret" not in str(resolved.redacted)


def test_env_can_opt_into_being_shown(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_URL", "https://internal/spec.json")
    resolved = resolve_inputs(
        (parse_input("spec", {"env": "SPEC_URL", "secret": False}, "x", tmp_path),)
    )
    assert resolved.shown["spec"] == "https://internal/spec.json"
    assert resolved.redacted["spec"] == "<env:SPEC_URL>"


def test_required_env_is_collected_not_raised(tmp_path, monkeypatch):
    monkeypatch.delenv("NOPE_VAR", raising=False)
    resolved = resolve_inputs(
        (parse_input("thing", {"env": "NOPE_VAR", "required": True}, "x", tmp_path),)
    )
    assert resolved.missing == [("thing", "NOPE_VAR")]
    assert resolved.values == {}


@pytest.mark.parametrize(
    ("decl", "match"),
    [
        ({"env": "X", "secrit": True}, "unknown key"),
        ({"env": "X", "file": "./y"}, "not both"),
        ({"required": True}, "needs 'env' or 'file'"),
        ({"env": ""}, "must name an environment variable"),
    ],
)
def test_bad_directives_are_rejected(tmp_path, decl, match):
    with pytest.raises(SkillContextError, match=match):
        parse_input("thing", decl, "skills[0].inputs.thing", tmp_path)


def test_a_key_with_no_value_is_rejected(tmp_path):
    # `inputs:\n  foo:` parses as None and would reach the skill as "None".
    with pytest.raises(ConfigError, match="no value"):
        _config(
            tmp_path,
            "skills:\n  - name: dba\n    command: 'x'\n    inputs:\n      foo:\n",
        )


def test_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="no such file"):
        _config(
            tmp_path,
            "skills:\n  - name: dba\n    command: 'x'\n"
            "    inputs:\n      schema: {file: ./nope.sql}\n",
        )


# --- source root -----------------------------------------------------------


def test_project_path_from_url():
    url = "https://gitlab.example.com/group/sub/repo/-/merge_requests/7"
    assert project_path_from_url(url) == "group/sub/repo"
    assert project_path_from_url("") == ""
    assert project_path_from_url("x") == ""


def test_source_per_project_by_path_and_id(tmp_path, monkeypatch):
    one, two, fallback = (tmp_path / n for n in ("one", "two", "fallback"))
    for d in (one, two, fallback):
        d.mkdir()
    monkeypatch.setenv("HUB_REPO", str(one))

    spec = parse_source(
        {"g/p": {"env": "HUB_REPO"}, "42": str(two), "default": str(fallback)},
        "skills[0]",
        tmp_path,
    )
    # Matched by the path in the MR's web URL...
    root, problems = resolve_source(spec, 1, "https://gl/g/p/-/merge_requests/7")
    assert root == str(one) and problems == []
    # ...or by the numeric project id, whichever the config author used.
    root, _ = resolve_source(spec, 42, "https://gl/other/repo/-/merge_requests/1")
    assert root == str(two)
    # Anything else falls back.
    root, _ = resolve_source(spec, 99, "https://gl/z/z/-/merge_requests/1")
    assert root == str(fallback)


def test_source_that_is_not_a_directory_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_REPO", str(tmp_path / "gone"))
    spec = parse_source({"env": "HUB_REPO"}, "skills[0]", tmp_path)
    root, problems = resolve_source(spec, 1, "")
    assert root is None
    assert "not a directory" in problems[0] and "never read" in problems[0]


def test_required_source_that_is_unset_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("HUB_REPO", raising=False)
    spec = parse_source({"env": "HUB_REPO", "required": True}, "skills[0]", tmp_path)
    root, problems = resolve_source(spec, 1, "")
    assert root is None and "HUB_REPO is not set" in problems[0]
    # Not required: no root, no complaint — the skill just works without one.
    spec = parse_source({"env": "HUB_REPO"}, "skills[0]", tmp_path)
    assert resolve_source(spec, 1, "") == (None, [])


def test_empty_source_mapping_is_rejected(tmp_path):
    with pytest.raises(SkillContextError, match="empty mapping"):
        parse_source({}, "skills[0]", tmp_path)


# --- what the skill actually receives --------------------------------------


def test_source_root_reaches_argv_cwd_and_bundle(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HUB_REPO", str(repo))

    cfg = _config(
        tmp_path,
        "skills:\n"
        "  - name: dba\n"
        "    enabled: true\n"
        f"    command: '{ECHO_ALL} --root={{source_root}}'\n"
        "    timeout_seconds: 30\n"
        "    source: {env: HUB_REPO}\n"
        "    inputs:\n"
        "      api: https://internal/spec.json\n",
    )
    db_path = tmp_path / "r.db"
    db = Database(db_path)
    _seed(db)
    db.close()

    html = _run(TestClient(create_app(cfg, str(db_path))), "dba")
    assert "review-output" in html
    assert f"--root={repo}" in html          # substituted into argv
    assert str(repo) in html                 # and the child ran in the checkout
    assert "<h2>Source</h2>" in html         # named in the stdin bundle (rendered)
    assert "https://internal/spec.json" in html  # declared input attached


def test_explicit_working_dir_wins_over_source(tmp_path, monkeypatch):
    repo, elsewhere = tmp_path / "repo", tmp_path / "elsewhere"
    repo.mkdir()
    elsewhere.mkdir()
    monkeypatch.setenv("HUB_REPO", str(repo))

    cfg = _config(
        tmp_path,
        "skills:\n"
        "  - name: dba\n"
        "    enabled: true\n"
        f"    command: '{ECHO_ALL}'\n"
        "    timeout_seconds: 30\n"
        f"    working_dir: {elsewhere}\n"
        "    source: {env: HUB_REPO}\n",
    )
    db_path = tmp_path / "r.db"
    db = Database(db_path)
    _seed(db)
    db.close()

    html = _run(TestClient(create_app(cfg, str(db_path))), "dba")
    assert str(elsewhere) in html
    assert "<h2>Source</h2>" in html  # the checkout is still named, just not run in


def test_secret_input_never_reaches_the_skill_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILL_TOKEN", "TOP-SECRET-VALUE")
    cfg = _config(
        tmp_path,
        "skills:\n"
        "  - name: dba\n"
        "    enabled: true\n"
        f"    command: '{ECHO_ALL}'\n"
        "    timeout_seconds: 30\n"
        "    inputs:\n"
        "      token: {env: SKILL_TOKEN}\n"
        "      note: visible-literal\n",
    )
    db_path = tmp_path / "r.db"
    db = Database(db_path)
    _seed(db)
    db.close()

    html = _run(TestClient(create_app(cfg, str(db_path))), "dba")
    assert "visible-literal" in html
    assert "TOP-SECRET-VALUE" not in html  # inherited via env, never pasted into the prompt


def test_job_is_refused_before_launching_when_required_input_is_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("MISSING_VAR", raising=False)
    refused, allowed = tmp_path / "refused.txt", tmp_path / "allowed.txt"
    # Both skills run the same command; only the declarations differ. The second
    # is the control: it proves the command really does leave a marker, so the
    # first one's missing marker means "never launched" rather than "never worked".
    cfg = _config(
        tmp_path,
        "skills:\n"
        "  - name: dba\n"
        "    enabled: true\n"
        f"    command: '{TOUCH} {refused}'\n"
        "    timeout_seconds: 30\n"
        "    inputs:\n"
        "      token: {env: MISSING_VAR, required: true}\n"
        "  - name: arch\n"
        "    enabled: true\n"
        f"    command: '{TOUCH} {allowed}'\n"
        "    timeout_seconds: 30\n",
    )
    db_path = tmp_path / "r.db"
    db = Database(db_path)
    _seed(db)
    db.close()
    client = TestClient(create_app(cfg, str(db_path)))

    html = _run(client, "dba")
    assert "review-error" in html
    assert "MISSING_VAR is not set" in html

    assert "review-output" in _run(client, "arch")
    assert allowed.exists()   # the control ran and left its marker...
    assert not refused.exists()  # ...so the refused job truly never launched


def test_job_is_refused_when_source_root_is_wrong(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_REPO", str(tmp_path / "does-not-exist"))
    cfg = _config(
        tmp_path,
        "skills:\n"
        "  - name: dba\n"
        "    enabled: true\n"
        f"    command: '{ECHO_ALL}'\n"
        "    timeout_seconds: 30\n"
        "    source: {env: HUB_REPO}\n",
    )
    db_path = tmp_path / "r.db"
    db = Database(db_path)
    _seed(db)
    db.close()

    html = _run(TestClient(create_app(cfg, str(db_path))), "dba")
    assert "review-error" in html
    assert "not a directory" in html


# --- several skills, several projects --------------------------------------


def test_two_skills_get_their_own_source_and_inputs(tmp_path, monkeypatch):
    """The case radar runs: one board, several skills in flight at once, each
    declaring its own source tree and inputs."""
    dba_repo, arch_repo = tmp_path / "dba-repo", tmp_path / "arch-repo"
    dba_repo.mkdir()
    arch_repo.mkdir()
    monkeypatch.setenv("DBA_REPO", str(dba_repo))
    monkeypatch.setenv("ARCH_REPO", str(arch_repo))
    schema = tmp_path / "schema.sql"
    schema.write_text("CREATE TABLE widgets (id int);", encoding="utf-8")

    cfg = _config(
        tmp_path,
        "skills:\n"
        "  - name: dba\n"
        "    enabled: true\n"
        f"    command: '{ECHO_ALL} --root={{source_root}}'\n"
        "    timeout_seconds: 30\n"
        "    source: {env: DBA_REPO}\n"
        "    inputs:\n"
        "      db_schema: {file: ./schema.sql}\n"
        "  - name: arch\n"
        "    enabled: true\n"
        f"    command: '{ECHO_ALL} --root={{source_root}}'\n"
        "    timeout_seconds: 30\n"
        "    source: {env: ARCH_REPO}\n"
        "    inputs:\n"
        "      spec: https://internal/arch.json\n",
    )
    db_path = tmp_path / "r.db"
    db = Database(db_path)
    _seed(db)
    db.close()
    client = TestClient(create_app(cfg, str(db_path)))

    # Both launched before either is awaited: the two run concurrently, as they
    # do when someone presses both buttons on a row.
    dba_job, _ = _start(client, "dba")
    arch_job, _ = _start(client, "arch")
    dba_html = _await_panel(client, "dba", dba_job)
    arch_html = _await_panel(client, "arch", arch_job)

    assert f"--root={dba_repo}" in dba_html
    assert "CREATE TABLE widgets" in dba_html
    assert str(arch_repo) not in dba_html          # no bleed from the other skill
    assert "https://internal/arch.json" not in dba_html

    assert f"--root={arch_repo}" in arch_html
    assert "https://internal/arch.json" in arch_html
    assert str(dba_repo) not in arch_html
    assert "CREATE TABLE widgets" not in arch_html


def test_one_skill_two_projects_two_checkouts(tmp_path, monkeypatch):
    """A skill is configured once but reviews MRs from several projects."""
    repo_a, repo_b = tmp_path / "repo-a", tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    monkeypatch.setenv("REPO_A", str(repo_a))

    cfg = _config(
        tmp_path,
        "skills:\n"
        "  - name: dba\n"
        "    enabled: true\n"
        f"    command: '{ECHO_ALL} --root={{source_root}}'\n"
        "    timeout_seconds: 30\n"
        "    source:\n"
        "      g/a: {env: REPO_A}\n"
        f"      g/b: {repo_b}\n",
        projects="g/a, g/b",
    )
    db_path = tmp_path / "r.db"
    db = Database(db_path)
    _seed(db, project_id=1, mr_iid=7, path="g/a")
    _seed(db, project_id=2, mr_iid=9, path="g/b")
    db.close()
    client = TestClient(create_app(cfg, str(db_path)))

    a_html = _run(client, "dba", project_id=1, mr_iid=7)
    b_html = _run(client, "dba", project_id=2, mr_iid=9)
    assert f"--root={repo_a}" in a_html and str(repo_b) not in a_html
    assert f"--root={repo_b}" in b_html and str(repo_a) not in b_html


def test_context_list_composes_diff_and_jira(tmp_path, monkeypatch):
    """A review skill can be given the diff *and* the ticket in one bundle."""
    import radar.context as ctxmod

    cfg = _config(
        tmp_path,
        "jira: {base_url: 'https://x.atlassian.net', project_keys: [PROJ]}\n"
        "skills:\n"
        "  - name: review\n"
        "    enabled: true\n"
        f"    command: '{ECHO_ALL}'\n"
        "    timeout_seconds: 30\n"
        "    include_context: true\n"
        "    context: [gitlab_diff, jira]\n",
    )
    assert cfg.skill_by_name("review").contexts == ("gitlab_diff", "jira")

    monkeypatch.setattr(
        ctxmod, "build_review_input", lambda *a, **k: "# Merge request: X\n\nDIFF-MARKER"
    )
    monkeypatch.setattr(ctxmod, "build_qa_input", lambda *a, **k: "# Jira\n\nTICKET-MARKER")
    monkeypatch.setattr(ctxmod, "gitlab_credentials", lambda: ("https://gl", "tok"))
    monkeypatch.setattr("radar.gitlab_client.GitLabSource", lambda *a, **k: object())
    monkeypatch.setattr("radar.jira_client.JiraClient.from_env", staticmethod(lambda: object()))

    db_path = tmp_path / "r.db"
    db = Database(db_path)
    db.upsert_mr_snapshot(
        project_id=1, mr_iid=7, title="PROJ-42 add widget", author="aviva",
        web_url="https://gitlab.example.com/g/p/-/merge_requests/7",
        source_branch="f", target_branch="main", description="", labels=[], draft=False,
        state="opened", reviewers=["dan"], created_at="2026-03-02T09:00:00Z",
        updated_at="2026-03-02T09:00:00Z",
    )
    db.insert_events([ev(ET.REVIEW_REQUESTED, ny(2026, 3, 2, 9), reviewer="dan", mr_iid=7)])
    db.close()

    html = _run(TestClient(create_app(cfg, str(db_path))), "review")
    assert "DIFF-MARKER" in html and "TICKET-MARKER" in html


def test_scalar_context_still_parses(tmp_path):
    cfg = _config(
        tmp_path,
        "skills:\n  - name: review\n    enabled: true\n    command: 'x'\n"
        "    include_context: true\n    context: gitlab_diff\n",
    )
    assert cfg.skill_by_name("review").contexts == ("gitlab_diff",)


def test_the_bundle_uses_the_inputs_it_was_handed_not_a_fresh_read(tmp_path):
    """Inputs are resolved once, when the job is admitted.

    Re-resolving in the provider re-read every `file:` from disk — wasteful for
    a large schema, and able to disagree with the values the job was accepted
    on if anything changed in between.
    """
    from radar.context import stdin_provider_for

    schema = tmp_path / "schema.sql"
    schema.write_text("ORIGINAL-SCHEMA", encoding="utf-8")
    cfg = _config(
        tmp_path,
        "skills:\n  - name: dba\n    enabled: true\n    command: 'x'\n"
        "    inputs:\n      db_schema: {file: ./schema.sql}\n",
    )
    provider = stdin_provider_for("dba", cfg, 1, 7, [])
    schema.write_text("CHANGED-AFTER-THE-JOB-STARTED", encoding="utf-8")

    out = provider("", {"db_schema": "ORIGINAL-SCHEMA"})
    assert "ORIGINAL-SCHEMA" in out
    assert "CHANGED-AFTER-THE-JOB-STARTED" not in out


def test_skill_with_no_declarations_sends_no_stdin(tmp_path):
    from radar.context import stdin_provider_for

    cfg = _config(
        tmp_path, "skills:\n  - name: dba\n    enabled: true\n    command: 'x'\n"
    )
    assert stdin_provider_for("dba", cfg, 1, 7, []) is None


# --- diagnostics -----------------------------------------------------------


def test_radar_check_reports_the_bag(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HUB_REPO", str(repo))
    monkeypatch.setenv("SKILL_TOKEN", "s3cret")

    cfg = _config(
        tmp_path,
        "skills:\n"
        "  - name: dba\n"
        "    enabled: true\n"
        "    command: 'x'\n"
        "    source: {env: HUB_REPO}\n"
        "    inputs:\n"
        "      token: {env: SKILL_TOKEN}\n",
    )
    check = {c.name: c for c in _check_skill_context(cfg)}["dba.context"]
    assert check.status == "ok"
    assert str(repo) in check.detail
    assert "<env:SKILL_TOKEN>" in check.detail and "s3cret" not in check.detail


def test_radar_check_fails_on_a_bad_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_REPO", str(tmp_path / "gone"))
    cfg = _config(
        tmp_path,
        "skills:\n  - name: dba\n    enabled: true\n    command: 'x'\n"
        "    source: {env: HUB_REPO}\n",
    )
    check = {c.name: c for c in _check_skill_context(cfg)}["dba.context"]
    assert check.status == "fail" and "not a directory" in check.detail


def test_radar_check_names_the_project_only_when_some_are_fine(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("REPO_A", str(repo))
    monkeypatch.setenv("REPO_B", str(tmp_path / "gone"))
    cfg = _config(
        tmp_path,
        "skills:\n  - name: dba\n    enabled: true\n    command: 'x'\n"
        "    source:\n      g/a: {env: REPO_A}\n      g/b: {env: REPO_B}\n",
        projects="g/a, g/b",
    )
    check = {c.name: c for c in _check_skill_context(cfg)}["dba.context"]
    assert check.status == "fail"
    assert check.detail.startswith("g/b:")  # the one that is broken, named

    # One declaration serving every project states the problem once, unprefixed.
    cfg = _config(
        tmp_path,
        "skills:\n  - name: dba\n    enabled: true\n    command: 'x'\n"
        "    source: {env: REPO_B}\n",
        projects="g/a, g/b",
    )
    check = {c.name: c for c in _check_skill_context(cfg)}["dba.context"]
    assert check.detail.count("not a directory") == 1
    assert not check.detail.startswith("g/")


def test_radar_check_warns_when_a_project_is_not_covered(tmp_path, monkeypatch):
    # A mapping that covers g/a but not g/b. A warn, not a fail: a check matches
    # only on the names in gitlab.projects, while a job also matches the numeric
    # id — so a mapping keyed by id would resolve at run time. The message says
    # what a job on g/b will actually do.
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("REPO_A", str(repo))
    cfg = _config(
        tmp_path,
        "skills:\n  - name: dba\n    enabled: true\n    command: 'x'\n"
        "    source:\n      g/a: {env: REPO_A}\n",
        projects="g/a, g/b",
    )
    check = {c.name: c for c in _check_skill_context(cfg)}["dba.context"]
    assert check.status == "warn"
    assert "g/b" in check.detail and "refuse to run" in check.detail
    assert str(repo) in check.detail  # and still reports what did resolve


def test_radar_check_warns_when_an_optional_source_is_unset(tmp_path, monkeypatch):
    # Declared for every project, but the variable is not set: legal — the skill
    # just runs without a checkout — and worth saying out loud.
    monkeypatch.delenv("MAYBE_REPO", raising=False)
    cfg = _config(
        tmp_path,
        "skills:\n  - name: dba\n    enabled: true\n    command: 'x'\n"
        "    source: {env: MAYBE_REPO}\n",
    )
    check = {c.name: c for c in _check_skill_context(cfg)}["dba.context"]
    assert check.status == "warn" and "not set" in check.detail


def test_skills_without_declarations_are_not_reported(tmp_path):
    cfg = _config(tmp_path, "skills:\n  - name: dba\n    enabled: true\n    command: 'x'\n")
    assert _check_skill_context(cfg) == []


# --- regressions -----------------------------------------------------------


def test_a_null_snapshot_column_substitutes_empty_not_the_word_none():
    """`str(None)` is "None", which a skill would take for a real ref or URL."""
    from radar.commands import build_argv

    ctx = {"head_sha": None, "author": None, "web_url": "http://x", "source_root": ""}
    argv = build_argv("tool --sha={head_sha} --author={author} --url={web_url}", ctx)
    assert argv == ["tool", "--sha=", "--author=", "--url=http://x"]


def test_an_empty_source_is_refused_rather_than_becoming_the_cwd():
    # Path("") is "." — an empty declaration would silently resolve to radar's
    # own directory and pass the is_dir() check.
    spec = parse_source("", "skills[0]", Path("."))
    root, problems = resolve_source(spec, 1, "")
    assert root is None and "empty" in problems[0]


def test_a_project_with_no_mapping_entry_is_refused_not_ignored():
    spec = parse_source({"g/a": "/somewhere"}, "skills[0]", Path("."))
    root, problems = resolve_source(spec, 42, "https://gl/g/b/-/merge_requests/1")
    assert root is None
    assert "nothing declared for project 42" in problems[0]
    assert "default:" in problems[0]  # and says how to opt out


def test_source_matches_a_gitlab_served_under_a_subpath(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    spec = parse_source({"group/repo": str(repo)}, "skills[0]", tmp_path)
    # `https://host/gitlab/group/repo/-/…` → path `gitlab/group/repo`, which an
    # exact match would miss even though the config is the obvious one.
    root, problems = resolve_source(spec, 1, "https://host/gitlab/group/repo/-/merge_requests/7")
    assert root == str(repo) and problems == []


def test_suffix_matching_respects_path_boundaries(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    spec = parse_source({"group/repo": str(repo)}, "skills[0]", tmp_path)
    # `othergroup/repo` must NOT match `group/repo`.
    root, problems = resolve_source(spec, 1, "https://host/othergroup/repo/-/merge_requests/7")
    assert root is None and problems  # refused, not silently mismatched


# --- resolution helper -----------------------------------------------------


def test_job_context_collects_every_problem_at_once(tmp_path, monkeypatch):
    monkeypatch.delenv("A_VAR", raising=False)
    monkeypatch.setenv("HUB_REPO", str(tmp_path / "gone"))
    cfg = _config(
        tmp_path,
        "skills:\n  - name: dba\n    enabled: true\n    command: 'x'\n"
        "    source: {env: HUB_REPO}\n"
        "    inputs:\n      a: {env: A_VAR, required: true}\n",
    )
    resolved = job_context(cfg.skill_by_name("dba"), 1, "")
    assert len(resolved.problems) == 2  # both, not just the first
    with pytest.raises(SkillContextError, match="A_VAR"):
        resolved.raise_for_problems()
