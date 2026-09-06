"""Backend context fetching: build the text radar pipes to a skill on stdin.

When a command has ``include_context: true``, radar fetches the relevant data
itself (the MR diff for review; the linked Jira ticket(s)/epic for qa) and pipes
a plain-text bundle to the command's stdin — so the skill needs no GitLab/Jira
access of its own, and the tokens stay in radar's process.

The bundle is *composed*, not chosen: a skill declaring
``context: [gitlab_diff, jira]`` gets the diff and the ticket in one document,
which is what a review of a change that implements a ticket actually needs. The
skill's declared context bag (``source:`` / ``inputs:``) is appended the same
way — the checkout path so the skill can open the code, then any other declared
input that is safe to show. Secrets are not written here: this bundle is prompt
text for an LLM agent, and an ``env:`` value stays out of it by default (see
``skillcontext``).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .config import Config, gitlab_credentials

# How much of the change list goes inline. The rest is in the file beside it:
# an agent needs enough to see the shape of what changed, not every path.
_INLINE_COMMITS = 20
_INLINE_FILES = 5


def _commit_line(commit, file_limit: int | None) -> str:
    """One commit, with its files capped (or not, for the file on disk)."""
    head = f"- `{commit.short_sha}` {commit.author} {commit.when}".rstrip()
    body = f"\n  {commit.message}" if commit.message else ""
    if not commit.files:
        return head + body
    shown = commit.files if file_limit is None else commit.files[:file_limit]
    more = len(commit.files) - len(shown)
    files = f"\n  files: {', '.join(shown)}" + (f" (+{more} more)" if more else "")
    return head + body + files


def build_review_input(source, project_id: int, mr_iid: int) -> str:
    """Fetch an MR's title/description/refs/diff and format it for the skill."""
    ctx = source.get_mr_context(project_id, mr_iid)
    parts = [f"# Merge request: {ctx.get('title', '')}".rstrip()]
    description = (ctx.get("description") or "").strip()
    if description:
        parts.append("## Description\n\n" + description)
    # The commits the diff was computed against, so a skill with a checkout can
    # read that exact snapshot instead of whatever the branch has moved on to.
    refs = [
        f"- {label}: `{ctx[key]}`"
        for label, key in (("base", "base_sha"), ("head", "head_sha"), ("start", "start_sha"))
        if ctx.get(key)
    ]
    if refs:
        parts.append("## Commits\n\n" + "\n".join(refs))
    diff = ctx.get("diff") or ""
    parts.append("## Diff\n\n```diff\n" + diff + "\n```")
    return "\n\n".join(parts)


def build_jenkins_input(
    client, job, status, log_lines: int, number: int, dest_dir: str | None = None
) -> str:
    """Fetch what Jenkins knows about a broken build and format it for the skill.

    Two pieces of evidence, in the order a person would want them: what changed
    since the build last passed, and the end of the log where it failed. Both
    come from Jenkins alone, so an analysis needs no GitLab or git access.

    ``number`` is passed in rather than read off ``status``: when a build is
    running over a broken one, the chip's own number is the *running* build and
    the one being explained is the last that finished. Reading it here would
    describe one build while every other part of the job named the other.
    """
    from .jenkins import RUNNING, commit_range, fetch_builds, fetch_log_tail, slug

    # For a job building over a failure, the state to report is what the build
    # being analysed did, not what the job is doing now.
    result = (status.previous or "unknown") if status.state == RUNNING else status.state
    parts = [
        f"# Jenkins build failure: {job.name} #{number}\n\n"
        f"Result: {result.upper()}\n"
        f"Build: {job.url}/{number}/"
    ]

    last_good, commits = commit_range(fetch_builds(client, job), number)
    if commits:
        since = f"the last successful build (#{last_good})" if last_good else "the builds on record"
        total_files = sum(len(c.files) for c in commits)
        listed = [_commit_line(c, _INLINE_FILES) for c in commits[:_INLINE_COMMITS]]

        # Capped, because this section has no natural size: one merge commit in
        # a monorepo lists thousands of paths, and twenty-five builds of them
        # ran to megabytes — a prompt too long to send, spent on file names.
        note = ""
        if len(commits) > _INLINE_COMMITS or total_files > _INLINE_FILES * _INLINE_COMMITS:
            note = (
                f"\n\n{min(len(commits), _INLINE_COMMITS)} of {len(commits)} commits shown, "
                f"{total_files} files touched in all."
            )
        if dest_dir:
            full = Path(dest_dir) / f"{slug(job.name)}-{number}-commits.txt"
            full.write_text(
                "\n".join(_commit_line(c, None) for c in commits),
                encoding="utf-8",
                # A commit message can carry a lone surrogate (json.loads makes
                # one out of a \udXXX escape). Without this the write raises and
                # the whole analysis is lost over one mangled character.
                errors="replace",
            )
            note += f"\n\nEvery commit and file: {full}"
        if not last_good:
            note += (
                "\n\nNo successful build is within the window radar looked at, so these are "
                "the changes it can see rather than the full range since it last passed."
            )
        parts.append(f"## Commits since {since}\n\n" + "\n".join(listed) + note)
    else:
        # Said out loud: an empty list is evidence too — it points at the
        # environment rather than at the change, and a skill told nothing would
        # have to guess which of the two it is.
        parts.append(
            "## Commits\n\nJenkins recorded no source changes for this build"
            + (f" since the last successful one (#{last_good})." if last_good else ".")
            + " The failure may be environmental (an agent, a dependency, a flaky test)"
            " rather than something in the code."
        )

    log = fetch_log_tail(client, job, number, lines=log_lines, dest_dir=dest_dir)
    parts.append(_log_section(log))
    return "\n\n".join(parts)


def _log_section(log) -> str:
    """The console log: where the file is, what part of it this is, and an excerpt.

    ``has_end`` governs every sentence here. Radar can end up holding the
    *beginning* of a log — a Jenkins that reports no size and ignores a byte
    range leaves nothing else to read — and the opening of a build is where it
    says what it is about to do, not why it failed. Describing that as the tail,
    or as the whole log, is how an agent comes to reason confidently about the
    wrong part of a run.
    """
    held = f"{log.slab_lines:,} lines, {log.slab_bytes / 1_000_000:.1f} MB"
    if not log.has_end:
        size = f"the FIRST {held}; the log is longer and its end could not be fetched"
        hint = (
            "the end of this log could not be fetched, so the failure itself is NOT below "
            "and is not at the end of the file either"
        )
        where = f"First {log.lines} lines"
    else:
        size = (
            f"the last {held} of {log.total_bytes / 1_000_000:.1f} MB"
            if log.total_bytes > log.slab_bytes
            else held
        )
        hint = "the first error is usually well above the end"
        where = f"Last {log.lines} lines"

    if log.path:
        return (
            f"## Console log\n\nThe log is at `{log.path}` ({size}). Open or search it for "
            f"anything the excerpt below does not cover — {hint}.\n\n{where}:\n\n"
            f"```\n{log.text}\n```"
        )

    # No file: the excerpt is all there is, so the heading carries the caveat.
    if not log.has_end:
        scope = f"first {log.lines} lines — {hint}"
    elif log.truncated:
        scope = f"last {log.lines} lines"
    else:
        scope = f"all {log.lines} lines"
    return f"## Console log ({scope})\n\n```\n{log.text}\n```"


def _format_issue(key: str, issue: dict, child: bool = False) -> str:
    fields = issue.get("fields", {}) or {}
    summary = fields.get("summary", "")
    itype = (fields.get("issuetype") or {}).get("name", "")
    status = (fields.get("status") or {}).get("name", "")
    labels = ", ".join(fields.get("labels", []) or [])
    description = (fields.get("description") or "").strip()
    heading = f"### Child {key} — {summary}" if child else f"## {key} — {summary}"
    meta = f"Type: {itype} · Status: {status}" + (f" · Labels: {labels}" if labels else "")
    body = f"\n\n{description}" if description else ""
    return f"{heading}\n{meta}{body}"


def build_qa_input(client, keys: list[str]) -> str:
    """Fetch each Jira ticket (and an epic's children) for the test-plan skill."""
    parts = ["# Jira context for QA test-plan generation"]
    for key in keys:
        issue = client.get_issue(key)
        parts.append(_format_issue(key, issue))
        itype = ((issue.get("fields") or {}).get("issuetype") or {}).get("name", "")
        if itype.lower() == "epic":
            for child in client.epic_children(key):
                parts.append(_format_issue(child.get("key", "?"), child, child=True))
    return "\n\n".join(parts)


def build_source_section(root: str, worktree: bool = False) -> str:
    """Tell the skill where this project's code is.

    Stated as a fact about the machine rather than an instruction: what the
    skill does with a checkout is the skill's business, but it cannot open a
    tree nobody named. Whether the tree is *this merge request's* code or the
    checkout's current branch is the difference between reading the change and
    reading whatever was there before it, so it is said explicitly.
    """
    what = (
        "checked out at this merge request's head commit, in a worktree created "
        "for this run"
        if worktree
        else "checked out"
    )
    return (
        f"## Source\n\nThis merge request's project is {what} at `{root}` (the working "
        "directory this command was started in, unless the skill was given an explicit "
        "one). Read it directly for anything the diff alone does not show."
    )


def build_inputs_section(shown: dict) -> str:
    """The skill's own declared inputs, each under its declared name."""
    parts = ["## Declared inputs"]
    for name, value in shown.items():
        text = str(value).strip()
        # A file's contents are a document, not a setting: fenced so a schema or
        # a spec cannot be mistaken for the instructions around it.
        body = f"```\n{text}\n```" if "\n" in text else f"`{text}`"
        parts.append(f"### {name}\n\n{body}")
    return "\n\n".join(parts)


def jenkins_stdin_provider_for(
    kind: str,
    config: Config,
    client,
    job,
    status,
    number: int,
) -> Callable[[str, dict], str] | None:
    """The stdin bundle for a build analysis.

    A sibling of ``stdin_provider_for`` rather than a branch inside it: that
    one's signature is (project_id, mr_iid, jira keys), which says nothing about
    a build, and threading both subjects through one function would make each
    caller read the other's parameters.
    """
    skill = config.skill_by_name(kind)
    if skill is None:
        return None

    def provider(
        source_root: str = "", inputs: dict | None = None, scratch: str = ""
    ) -> str:
        # Always the evidence: being named in `jenkins.analysis.skill` is what
        # gets it, so there is no second switch to forget. (`include_context`
        # gates the merge-request fetches, where a skill may sensibly want none;
        # an analysis with no commits and no log has nothing to work from, and
        # the loader refuses that setting on this skill rather than ignoring it.)
        parts = [
            build_jenkins_input(
                client, job, status, config.jenkins.log_tail_lines, number, scratch
            )
        ]
        if source_root:
            parts.append(build_source_section(source_root))
        if inputs:
            parts.append(build_inputs_section(inputs))
        return "\n\n".join(p for p in parts if p)

    return provider


def stdin_provider_for(
    kind: str,
    config: Config,
    project_id: int,
    mr_iid: int,
    keys: list[str],
) -> Callable[[str, dict], str] | None:
    """Build the stdin bundle for a job, or None if this skill declared nothing
    to send.

    The runner supplies both things it alone knows: the checkout the job will
    actually use — the per-job worktree under ``checkout: worktree``, which does
    not exist until the worker makes it — and the skill's inputs, resolved once
    when the job was admitted. Resolving them a second time here would re-read
    every ``file:`` from disk and could disagree with the values the job was
    accepted on.

    The fetch runs inside the worker thread (so a slow or failing fetch surfaces
    as a job error, not a slow button). Which backends are fetched is driven by
    the skill's ``context`` capability, not its name.
    """
    skill = config.skill_by_name(kind)
    if skill is None:
        return None

    fetchers: list[Callable[[], str]] = []
    if skill.include_context:
        if "gitlab_diff" in skill.contexts:

            def gitlab_section() -> str:
                from .gitlab_client import GitLabSource

                source = GitLabSource(*gitlab_credentials())
                return build_review_input(source, project_id, mr_iid)

            fetchers.append(gitlab_section)

        if "jira" in skill.contexts and keys:

            def jira_section() -> str:
                from .jira_client import JiraClient

                return build_qa_input(JiraClient.from_env(), keys)

            fetchers.append(jira_section)

    has_declared = skill.source is not None or bool(skill.inputs)
    if not fetchers and not has_declared:
        return None
    worktree = getattr(skill, "checkout", "none") == "worktree"

    def provider(
        source_root: str = "", inputs: dict | None = None, scratch: str = ""
    ) -> str:
        parts = [section() for section in fetchers]
        if source_root:
            parts.append(build_source_section(source_root, worktree=worktree))
        if inputs:
            parts.append(build_inputs_section(inputs))
        return "\n\n".join(p for p in parts if p)

    return provider
