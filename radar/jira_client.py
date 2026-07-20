"""Minimal Jira Cloud REST client for backend context fetches.

Used by the QA flow to download the linked ticket(s)/epic and hand the content
to the test-plan skill, so the skill needs no Jira access of its own. Uses the
stdlib (no extra dependency) and REST API v2 (description comes back as a
readable string rather than v3's Atlassian Document Format JSON).

Auth is HTTP Basic with the account email + an API token, from the
JIRA_BASE_URL / JIRA_EMAIL / JIRA_API_TOKEN environment variables.
"""

from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
from collections.abc import Callable

from .config import jira_credentials

_ISSUE_FIELDS = "summary,description,issuetype,status,labels,parent"


class JiraError(Exception):
    """A Jira request failed."""


class JiraClient:
    def __init__(
        self, base_url: str, email: str, token: str,
        getter: Callable[[str], dict] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        cred = base64.b64encode(f"{email}:{token}".encode()).decode()
        self._auth_header = f"Basic {cred}"
        self._getter = getter or self._http_get  # injectable for tests

    @classmethod
    def from_env(cls) -> JiraClient:
        base_url, email, token = jira_credentials()
        return cls(base_url, email, token)

    def _http_get(self, path: str) -> dict:
        req = urllib.request.Request(
            self.base_url + path,
            headers={"Authorization": self._auth_header, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed base_url
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise JiraError(f"Jira {exc.code} for {path}") from None
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise JiraError(f"Jira request failed for {path}: {exc}") from None

    def myself(self) -> dict:
        """The authenticated account — used to validate credentials."""
        return self._getter("/rest/api/2/myself")

    def get_issue(self, key: str) -> dict:
        return self._getter(f"/rest/api/2/issue/{urllib.parse.quote(key)}?fields={_ISSUE_FIELDS}")

    def search(self, jql: str, max_results: int = 50) -> dict:
        query = urllib.parse.urlencode(
            {"jql": jql, "maxResults": max_results, "fields": _ISSUE_FIELDS}
        )
        return self._getter(f"/rest/api/2/search?{query}")

    def epic_children(self, key: str) -> list[dict]:
        """Best-effort: children of an epic (next-gen `parent`, classic `Epic Link`)."""
        jql = f'parent = {key} OR "Epic Link" = {key}'
        try:
            return self.search(jql).get("issues", []) or []
        except JiraError:
            return []
