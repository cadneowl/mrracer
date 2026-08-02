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

from .config import Config, gitlab_credentials


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

    def provider(source_root: str = "", inputs: dict | None = None) -> str:
        parts = [section() for section in fetchers]
        if source_root:
            parts.append(build_source_section(source_root, worktree=worktree))
        if inputs:
            parts.append(build_inputs_section(inputs))
        return "\n\n".join(p for p in parts if p)

    return provider
