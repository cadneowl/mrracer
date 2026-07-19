# radar

**R**eview **A**irtime & **D**eadline **A**ccountability **R**adar — a self-hosted
dashboard that tracks open GitLab merge requests waiting for code review and
enforces configurable, business-hours review SLAs.

radar is **event-sourced**: a poller writes an append-only log of facts pulled
from GitLab, and every SLA state and statistic is *derived* by replaying that
log. Change your SLA definitions in config and re-derive history with one
command.

> **Status: Phase 1** — poller, event store, and the live SLA board. History,
> statistics, gamification, and nudges are later phases (see [Roadmap](#roadmap)).

---

## What it tracks

The unit of tracking is a **review obligation**: `(project, mr_iid, reviewer,
round)`. One MR can carry several obligations with independent clocks, and a new
review request after an approval opens a fresh round.

Each obligation runs through two phases against two budgets:

| Phase | Clock runs… | Resolved by |
|-------|-------------|-------------|
| **first response** | from the review request until the reviewer's first qualifying response | a diff thread, `changes_requested`, or an approval |
| **approval** | until approval — but **pauses while the ball is in the author's court** (reviewer asked for changes / opened a thread and the author hasn't pushed or replied since) | an approval |

The board shows a single chip per obligation, tracking **whichever clock is
currently live** (most-urgent, auto-switching), colored by how much of its
budget is consumed:

| Chip | Meaning |
|------|---------|
| 🟢 **green** (IN_SLA) | clock running, under 75% of budget |
| 🟠 **amber** (AT_RISK) | clock running, ≥ 75% of budget |
| 🔴 **red** (BREACHED) | clock running, over budget |
| ⚪ **grey** (PENDING) | paused (author's court) or resolved-awaiting |
| 🔵 **blue** (WAIVED) | draft, waive-label, reviewer removed, or MR closed |

Rows are sorted **most-overdue first**, and the board auto-refreshes every 60s
via htmx. Breach counts are surfaced at **team level only** — there is
deliberately no per-person breach list on the main board.

### Personal view (click-a-person)

Click any reviewer — a name chip on the board, or a pill in the **VIEW** bar —
to filter down to *the MRs waiting on them* (`/?view=<username>`). The choice is
remembered in a `radar_view` cookie, so the board returns to that personal view
on your next visit and across the 60s auto-refresh; **← back to team board**
clears it. There is **no login**: the board holds no private data, so the cookie
just stores a display preference, not an identity.

### Launch an AI code review from the board

Each MR row can show a **🔍 review** button that runs a command you configure
and shows its stdout as a rendered markdown review, in a modal over the board.
It's tool-agnostic — point it at whatever review skill you've prepared:

```yaml
review:
  enabled: true
  command: 'claude -p "/code-review {web_url}"'   # e.g. a Claude Code skill, headless
  working_dir: /path/to/checkout                  # optional; where to run it
  timeout_seconds: 600
```

Placeholders filled from the MR: `{web_url}`, `{mr_iid}`, `{project_id}`,
`{source_branch}`, `{target_branch}`, `{title}`, `{author}`. The command runs
**locally on the same machine as `radar serve`**, with `shell=False`, and the
template is tokenized *before* substitution — so an MR field can never inject
shell metacharacters or extra arguments. As a further guard, if a substituted
value would make a token *start with* `-` (flag smuggling via an
attacker-chosen title/branch), radar refuses to run; embed placeholders after a
fixed prefix (`--url={web_url}`) if you need dash-leading values. Reviews run as
background jobs; the modal polls until done. The result is shown in the
dashboard only (nothing is written back to GitLab).

The command's stdout is treated as **untrusted** — it can quote MR content
authored by others — so the rendered markdown is HTML-sanitized against a strict
allowlist (via `nh3`) before display: no `<script>`, event handlers, or
`javascript:` URLs survive, while headings, code blocks, tables, and links do.

### Launch a QA test plan from the board (shift-left)

To involve QA on every MR, radar can generate a **manual QA test plan** (not unit
tests) from the MR's linked Jira ticket(s), on demand. It works exactly like the
review button — radar just launches your command:

```yaml
jira:
  base_url: https://yourco.atlassian.net   # for the issue links
  project_keys: [PROJ, BUG]                # optional filter

qa:
  enabled: true
  command: 'claude -p "/qa-testplan {jira_keys}"'
  working_dir: /path/to/checkout
  timeout_seconds: 900
```

radar recognises Jira keys (`PROJ-123`) in each MR's **branch, title, and
description**, shows them as links on the board, and passes them to the command
via `{jira_keys}` (space-separated) / `{jira_keys_csv}`. Your `/qa-testplan`
**skill** reads the ticket(s) itself and — since it already has Jira access — can
write the plan back to Jira (a comment, or Xray/Zephyr test cases if you have
them). radar keeps a copy: the generated plan is saved and shown on the board
with a **✓ plan** badge that re-opens it, no re-run needed. **radar needs no Jira
credentials** — the skill owns all Jira access.

> radar provides the *infrastructure* (extraction, launch, storage, display);
> the `/qa-testplan` skill is yours to write, like the review skill. Output is
> sanitized and rendered the same way as reviews.

### Business-hours math

SLA budgets are in **business hours**. Weekends and off-hours never burn budget.
The work calendar (workdays, hours) plus a per-reviewer timezone map define each
reviewer's clock, so a reviewer in `Asia/Jerusalem` and one in
`America/New_York` are measured against their own working day. DST transitions
are handled correctly (all math is done on UTC instants). This logic lives in
[`radar/business_time.py`](radar/business_time.py) and has exhaustive unit tests.

### Where review-request times come from

GitLab has no reliable "review requested at" field, so radar treats **system
notes as the source of truth** (`requested review from @user`,
`requested changes`, `approved this merge request`, `added N commits`, …). This
gives true timestamps *and* full historical backfill for MRs that predate
radar. The `/reviewers` snapshot is used only to reconcile current reviewers
that lack a request note. All note parsing is centralized in
[`radar/notes.py`](radar/notes.py) — exact wording varies by GitLab version, so
adjust the patterns there if needed.

---

## Setup

Requires **Python 3.11+** and [`uv`](https://docs.astral.sh/uv/).

```bash
uv venv
uv pip install -e ".[dev]"      # drop [dev] for a runtime-only install
```

### 1. Create a GitLab token

Create a **personal access token** with the **`read_api`** scope:

1. In GitLab: **User Settings → Access Tokens** (or a group/project token).
2. Name it (e.g. `radar`), select the **`read_api`** scope, set an expiry.
3. Copy the token — you won't see it again.

radar reads credentials **only from the environment** and never writes them to
disk or logs:

```bash
export GITLAB_URL=https://gitlab.example.com
export GITLAB_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx
```

On Windows PowerShell:

```powershell
$env:GITLAB_URL = "https://gitlab.example.com"
$env:GITLAB_TOKEN = "glpat-xxxxxxxxxxxxxxxxxxxx"
```

### 2. Configure

```bash
cp config.example.yaml config.yaml
# edit config.yaml — at minimum, set gitlab.projects and your calendar
uv run radar validate            # sanity-check the file
```

### 3. Poll and serve

```bash
uv run radar poll-once           # fetch once, print obligation counts
uv run radar serve               # dashboard at http://127.0.0.1:8000 + background poller
```

Open <http://127.0.0.1:8000>. `serve` polls GitLab every
`poll_interval_minutes` in-process; killing and restarting loses nothing (the
event log is on disk in SQLite).

---

## CLI

| Command | What it does |
|---------|--------------|
| `radar poll-once` | One polling pass, then exit (also refreshes the derived snapshot). |
| `radar serve [--host H] [--port P]` | Run the dashboard and the background poller. |
| `radar recompute` | Re-derive every obligation from the event log under the current config. Run after changing SLA rules. |
| `radar validate` | Validate `config.yaml` and exit. |

Global flags: `-c/--config PATH` (default `config.yaml`), `-v/--verbose`.

Run modules directly with `python -m radar <command>` if you prefer.

---

## Configuration reference

See [`config.example.yaml`](config.example.yaml) for a fully-commented file.

| Key | Meaning |
|-----|---------|
| `gitlab.projects` | List of project paths (`group/name`) or numeric IDs to monitor. |
| `gitlab.poll_interval_minutes` | How often `serve` polls (default 10). |
| `database.path` | SQLite file location (default `radar.db`). |
| `calendar.workdays` | Working weekdays, e.g. `[mon, tue, wed, thu, fri]`. |
| `calendar.work_hours` | `{start: "09:00", end: "18:00"}` — the daily work window. |
| `calendar.default_timezone` | Timezone for reviewers not in the map. |
| `calendar.reviewer_timezones` | Per-reviewer timezone overrides. |
| `slas` | Ordered rules; **first match wins**. Each has a `match` (optional `target_branch` glob and/or required `labels`) and `first_response_business_hours` / `approval_business_hours`. The last rule must be the default `match: {}`. |
| `waive` | Obligations are waived (excluded, shown blue) when `draft: true` and the MR is a draft, or the MR carries any `labels` listed here. |
| `gamification` | Consumed in Phase 3; carried verbatim for now. |

Secrets are **never** in this file — only `GITLAB_URL` / `GITLAB_TOKEN` in the
environment.

---

## Development

```bash
uv run pytest            # run the test suite
uv run ruff check .      # lint
```

Tests never hit a real GitLab: the poller is driven by a `FixtureSource` that
serves recorded-shape JSON. The business-hours module is tested exhaustively
(window clipping, weekends, week boundaries, timezone conversion, DST
spring-forward/fall-back, and the deadline inverse).

### Architecture

```
GitLab REST ─▶ gitlab_client ─▶ poller ─▶ [ events ]  (append-only, idempotent)
                                              │
                                              ▼
                          derive  ◀── config (SLAs, calendar, waivers)
                                              │
                        ┌─────────────────────┼─────────────────────┐
                        ▼                                            ▼
                  service.build_dashboard                    service.recompute
                        │                                            │
                        ▼                                            ▼
                  web (FastAPI + Jinja + htmx)             obligations snapshot table
```

- `business_time.py` — pure business-hours math (no I/O).
- `config.py` — validated config loading; credentials from env only.
- `events.py` / `notes.py` — event model and GitLab note/discussion parsing.
- `db.py` — hand-written SQLite repository (no ORM).
- `derive.py` — replay events → obligation states.
- `poller.py` / `scheduler.py` — ingestion and the in-process loop.
- `service.py` / `web/` — read-side dashboard and recompute.

> **Design note (extension):** clock fairness needs to know when the author
> pushed. GitLab emits an `added N commits` system note, which radar records as
> a `commits_pushed` event (not in the original canonical list, but required for
> the approval-clock pause).

---

## Roadmap

- **Phase 1 (this release)** — poller, event store, live SLA board.
- **Phase 2** — weekly breach-rate trend, aging histogram, per-developer stats, a manager-only `/coach` view, reviewer load balance.
- **Phase 3** — config-driven points engine, leaderboard, badges, guardrails.
- **Phase 4** — optional batched Slack/Teams nudges when obligations enter AT_RISK.

## Non-goals

No reviewer auto-assignment, no GitLab webhooks (polling only), no auth layer
(deploy on a trusted network), no AI/LLM features.
