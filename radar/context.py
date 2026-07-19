"""Backend context fetching: build the text radar pipes to a skill on stdin.

When a command has ``include_context: true``, radar fetches the relevant data
itself (the MR diff for review; the linked Jira ticket(s)/epic for qa) and pipes
a plain-text bundle to the command's stdin — so the skill needs no GitLab/Jira
access of its own, and the tokens stay in radar's process.
"""

from __future__ import annotations

from collections.abc import Callable

from .config import Config, gitlab_credentials


def build_review_input(source, project_id: int, mr_iid: int) -> str:
    """Fetch an MR's title/description/diff and format it for the review skill."""
    ctx = source.get_mr_context(project_id, mr_iid)
    parts = [f"# Merge request: {ctx.get('title', '')}".rstrip()]
    description = (ctx.get("description") or "").strip()
    if description:
        parts.append("## Description\n\n" + description)
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


def stdin_provider_for(
    kind: str, config: Config, project_id: int, mr_iid: int, keys: list[str]
) -> Callable[[], str] | None:
    """A thunk that produces the stdin bundle for a job, or None if this command
    doesn't fetch context. The fetch runs inside the worker thread (so a slow or
    failing fetch surfaces as a job error, not a slow button)."""
    if kind == "review" and config.review.include_context:
        def provider() -> str:
            from .gitlab_client import GitLabSource

            source = GitLabSource(*gitlab_credentials())
            return build_review_input(source, project_id, mr_iid)

        return provider

    if kind == "qa" and config.qa.include_context and keys:
        def provider() -> str:
            from .jira_client import JiraClient

            return build_qa_input(JiraClient.from_env(), keys)

        return provider

    return None
