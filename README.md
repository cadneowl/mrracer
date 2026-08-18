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
| **assignment** | from the moment an MR was opened with **no reviewers at all** (see [MRs with no reviewers](#mrs-with-no-reviewers)) | anyone being added as a reviewer |

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

**↻ refresh now** (top right) polls GitLab immediately rather than waiting for
the next `poll_interval_minutes` tick — for when someone asks you to review an
MR that radar has not seen yet. The button waits for the pass to finish and
answers with the board built from it, so the MR is there when it returns. It
appears only when `serve` has a poller (i.e. GitLab credentials are set); the
60s auto-refresh alone only re-renders what is already in SQLite.

### MRs with no reviewers

An MR nobody was asked to review has no review obligation, so nothing above has
anything to say about it — and it is exactly the MR most likely to be forgotten.
Give the SLA rules an `assignment_business_hours` budget and each one appears on
the board carrying a single hollow **NO REVIEWERS** chip:

```yaml
slas:
  - match: {} # every rule needs the key, or none of them
    first_response_business_hours: 16
    approval_business_hours: 24
    assignment_business_hours: 4 # half a business day to find a reviewer
```

The chip is colored by the same green / amber / red buckets. Its clock runs in
business hours from the point the MR last became something someone could have
been asked to review — when it was opened, when it was **marked ready**, or when
its **last reviewer was removed**, whichever is latest. So a week spent as a
draft is not billed the moment the draft flag clears, and an MR left orphaned is
not billed for the days somebody was on it.

The chip carries no name and does not link anywhere, because nobody was given
the job; the row's **Author** column says who still owes it, and the MR counts
toward their pill in the VIEW bar and their personal view (an MR of yours that
needs a reviewer *is* waiting on you).

Three cases deliberately produce no chip at all: a **draft** (shown blue and
waived under the usual `waive:` rules, since it is not expected to have
reviewers yet), an MR that has **already been approved** (it is waiting to
merge, not waiting for someone to look at it, even if the approver was later
removed), and any MR once it is merged or closed. The chip also never reaches
the *team · to review* filter or the coach view, both of which are about reviews
people were actually asked for.

Omit `assignment_business_hours` everywhere and none of this happens — the check
is off and unassigned MRs stay off the board, as before.

### Read the discussion without leaving the board

A chip tells you a reviewer is waiting; it does not tell you what *for*. Expand
the row and radar shows the conversation inline:

* **💬 n** next to the MR title — every thread on the MR (unresolved first,
  then resolved, then plain comments). The number is the unresolved count.
* **💬 n** attached to a reviewer's chip — just the threads *that person*
  opened and nobody has resolved. This is the usual case: a paused chip means
  they commented and are waiting on the author, and this is the comment.

Each thread shows who opened it, the file and line, every reply, and a **reply
on GitLab** deep-link to that exact note. Comment bodies are rendered as
markdown and sanitized the same way skill output is.

Threads are cached by the poller from the discussions it already fetches, so
this costs no extra GitLab API calls. They follow GitLab: resolve a thread there
and it stops counting here on the next poll. An expanded row survives the 60s
auto-refresh and reloads with it, so a thread resolved mid-read updates in place.

> After upgrading, run **`radar poll-once --full`** once. Normal polling skips
> MRs that haven't changed since the last pass — which is exactly the quiet,
> stalled MR whose threads you most want to read.

### Coach view (manager-only)

Per the "no surveillance" principle, individual breach detail lives *only* on a
separate **`/coach`** page (unlinked except a subtle board-footer link — no auth
yet, manager-only by obscurity). It shows, per reviewer: current **open
breaches** (which MRs, hours over), **SLA compliance %**, **median / p90
first-response** time, **open load**, and a **chronic** flag for high recurring
breach rates. Everything is derived live from the event log (open breaches from
open MRs, compliance from resolved obligations); waived obligations are excluded.

### Filters (personal & team)

The **VIEW** bar filters the board. The chosen filter is remembered in a
`radar_view` cookie, so the board returns to it on your next visit and across
the 60s auto-refresh; **All MRs** clears it. There is **no login** — the board
holds no private data, so the cookie stores a display preference, not identity.

- **Personal** — click any reviewer (a name chip on the board or a pill) to see
  *the MRs waiting on them* (`/?view=<username>`).
- **Team** — define teams in config (`teams:`), and each gets two pills:
  - **`<team> · authored`** — MRs **opened by** a team member.
  - **`<team> · to review`** — MRs where a team member is a **requested
    reviewer** (obligations narrowed to that team's members).

  ```yaml
  teams:
    - name: backend
      members: [dan, maya, ophira]
  ```

### Launch an AI code review from the board

Each MR row can show a **🔍 review** button that runs a command you configure
and shows its stdout as a rendered markdown review, in a modal over the board.
It's tool-agnostic — point it at whatever review skill you've prepared:

```yaml
skills:
  - name: review
    enabled: true
    command: 'claude -p "/code-review {web_url}"' # e.g. a Claude Code skill, headless
    working_dir: /path/to/checkout                # optional; where to run it
    timeout_seconds: 600                          # budget for the whole job
```

Every skill lives in the `skills:` list — that is the only place they are
declared. `review` and `qa` are ordinary entries whose **names** carry defaults
(see [Add your own skills](#add-your-own-skills-custom-board-buttons)).

`timeout_seconds` bounds the **whole job**, not just the command: preparing a
checkout and fetching the MR's context both talk to the network and are spent
from the same clock, so a job can never outlast its budget — a hung fetch fails
it rather than leaving the panel tailing a job that will never end.

Placeholders filled from the MR: `{web_url}`, `{mr_iid}`, `{project_id}`,
`{source_branch}`, `{target_branch}`, `{title}`, `{author}`, `{head_sha}`, plus
`{source_root}` if the skill declares a
[`source:`](#tell-a-skill-where-the-code-is-source-and-inputs).
The command runs
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

#### Running headless (no prompts) + live progress

radar runs the command **non-interactively** (no TTY), so it must not stop to
ask for tool permissions. Run the skill with permissions pre-resolved:

- Pre-approve a **narrow, read-only allowlist** in `~/.claude/settings.json`
  under `permissions.allow`:
  ```json
  {
    "permissions": {
      "allow": [
        "Read", "Grep", "Glob",
        "WebFetch(domain:gitlab.yourco.com)",   // code review -> GitLab
        "mcp__atlassian__*",                     // QA -> Jira via an Atlassian MCP
        "WebFetch(domain:*.atlassian.net)"       // QA -> Jira via WebFetch (alt)
      ]
    }
  }
  ```
  MCP tools use `mcp__<server>__<tool>`; the `__*` wildcard approves a whole
  server (a bare `mcp__atlassian` is rejected). Match `atlassian` to your Jira
  MCP server's real name. Add `Bash(git *)` only if a skill reviews a local
  checkout; avoid blanket `Bash` and any write tools. Don't use `--bare` (it
  skips loading MCP servers). Then
  `--permission-mode dontAsk` **enforces** that allowlist without prompting —
  anything not on the list is **auto-denied (fails closed)**, so the run never
  blocks and never silently gains capabilities. Avoid `--permission-mode
  bypassPermissions` (it allows everything — fails open); only consider it
  inside a locked-down sandbox with no internal-network access and no secrets in
  the environment.
- `--output-format stream-json --verbose` — makes Claude emit live events, so
  the modal shows **real-time progress** (a log of tool uses and drafting) while
  the run is in flight, then renders the final result. radar reads the child's
  stdout line-by-line and streams it to the browser over SSE. Commands that
  don't speak stream-json still work — their stdout lines become the progress
  log; they just aren't as granular.

Authentication comes from your normal Claude Code setup (`~/.claude/settings.json`
gateway/token, or `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` in the
environment that launches `radar serve`). Don't use `--bare` — it skips loading
`settings.json`. radar strips `GITLAB_TOKEN`/`GITLAB_URL` from the child env, so
a skill that needs GitLab/Jira access must have its own credentials (e.g. a
GitLab/Atlassian MCP).

### Launch a QA test plan from the board (shift-left)

To involve QA on every MR, radar can generate a **manual QA test plan** (not unit
tests) from the MR's linked Jira ticket(s), on demand. It works exactly like the
review button — radar just launches your command:

```yaml
jira:
  base_url: https://yourco.atlassian.net   # for the issue links
  project_keys: [PROJ, BUG]                # optional filter

skills:
  - name: qa
    enabled: true
    command: 'claude -p "/qa-testplan {jira_keys}"'
    working_dir: /path/to/checkout
    timeout_seconds: 900
```

radar recognises Jira keys (`PROJ-123`) in each MR's **branch, title, and
description**, shows them as links on the board, and passes them to the command
via `{jira_keys}` (space-separated) / `{jira_keys_csv}`. Setting `project_keys`
is recommended — without it, tokens like `UTF-8` or `SHA-256` match the key
pattern and would show as phantom links. Your `/qa-testplan`
**skill** reads the ticket(s) itself and — since it already has Jira access — can
write the plan back to Jira (a comment, or Xray/Zephyr test cases if you have
them). radar keeps a copy: the generated plan is saved and shown on the board
with a **✓ plan** badge that re-opens it, no re-run needed. **radar needs no Jira
credentials** — the skill owns all Jira access.

> radar provides the *infrastructure* (extraction, launch, storage, display);
> the `/qa-testplan` skill is yours to write, like the review skill. Output is
> sanitized and rendered the same way as reviews.

### Let radar fetch the context (`include_context`)

By default the skill fetches its own data (the MR from GitLab, the ticket from
Jira). On a **private/self-hosted GitLab** that fails — the skill has no
credentials, and a WebFetch of a private MR just returns a login page. Set
`include_context: true` and **radar fetches the data from its backend** (it
already holds the tokens) and pipes it to the skill on **stdin**:

```yaml
skills:
  - name: review
    include_context: true  # radar fetches the MR title/description/diff -> stdin
  - name: qa
    include_context: true  # radar fetches the Jira ticket(s) + epic children -> stdin
```

- **Review** uses `GITLAB_URL` / `GITLAB_TOKEN` (the same env the poller uses).
- **QA** uses `JIRA_BASE_URL` / `JIRA_EMAIL` / `JIRA_API_TOKEN` (Jira Cloud REST,
  basic auth). An **epic** also pulls its child issues.

With this on, the skill needs **no GitLab/Jira access of its own** — write it to
read the context from stdin (you can drop `{web_url}` / `{jira_keys}` from the
command). The tokens stay inside radar's process; they're still stripped from
the child's environment. The fetch runs inside the job (you'll see a "fetching
context…" line), and a fetch failure surfaces as a clear job error.

`context:` takes a **list**, so one skill can be given both:

```yaml
skills:
  - name: review
    enabled: true
    include_context: true
    context: [gitlab_diff, jira]  # the diff *and* the ticket that motivated it
```

### Tell a skill where the code is (`source:` and `inputs:`)

A review skill that can only see a diff is reviewing through a keyhole: it can't
check whether a changed function has other callers, or whether the pattern it's
flagging is used everywhere else in the repo. Point it at the checkout:

```yaml
skills:
  - name: dba
    enabled: true
    command: 'claude -p --permission-mode dontAsk "/dba"'
    source: { env: HUB_REPO_ROOT }        # where this project is checked out
    inputs:                               # anything else the skill needs
      db_schema:    { file: ./references/schema.sql }
      api_spec_url: https://internal/api/spec.json
```

The skill then gets the path three ways: as the `{source_root}` placeholder, as
a **`## Source`** section in its stdin bundle, and as the process's **working
directory** — so a Claude Code skill's `Read`/`Grep`/`Glob` land on the right
tree with no extra wiring. An explicit `working_dir` still wins if you set one.

**Value forms.** Each entry is one of three things:

| form | meaning | shown to the skill? |
|---|---|---|
| `a literal` (string, list, map) | the value as written | yes |
| `{ file: ./path }` | the file's contents, relative to `config.yaml` | yes |
| `{ env: NAME }` | that environment variable | **no** — see below |
| `{ env: NAME, secret: false }` | that environment variable | yes |

An `env:` value is treated as a **credential or a machine-local path** and is
resolved but *not* written into the stdin bundle — that bundle is prompt text for
an LLM agent, and a token does not belong in a prompt (or in the transcript it
leaves behind). A skill that needs a credential already inherits it from the
environment that launched `radar serve`. Add `secret: false` for things that are
genuinely just settings (a base URL, a cluster name). Add `required: true` to
anything the skill cannot work without.

**One skill, several projects.** radar polls several GitLab projects, and one
checkout can't serve them all, so `source:` also takes a mapping keyed by project
path or numeric id:

```yaml
    source:
      group/hub-backend: { env: HUB_REPO_ROOT }
      group/hub-web:     ~/src/hub-web
      "42":              /srv/checkouts/legacy
      default:           { env: FALLBACK_REPO }
```

Each job resolves the root for **its own** MR's project, so two projects reviewed
by one skill run in their own checkouts. Keys match the project path from the
MR's URL (a GitLab served under a sub-path still matches, on a path boundary) or
the numeric project id. An MR whose project matches **no** entry refuses to run —
add `default:` if the skill should run without a checkout on the rest.

**Nothing is resolved quietly.** A `required` input that is unset, or a source
root that is set but is not a directory, **refuses the job before the command
launches** — the panel shows why. That failure mode is the reason it's strict: an
agent pointed at a path that doesn't exist gets "no such file" from every read,
which is indistinguishable from a clean repository, so it would review having
opened nothing and the run would look entirely normal. `radar check` reports the
same thing ahead of time, per project, with `env:` values shown as `<env:NAME>`
rather than their contents.

### Give each job the MR's own code (`checkout: worktree`)

A `source:` on its own hands the skill whatever the checkout is sitting on —
usually the default branch, and never reliably the MR under review. Two jobs
started from the board share that one working tree, so they'd read each other's
checkout. Turn on per-job worktrees:

```yaml
skills:
  - name: dba
    enabled: true
    command: 'claude -p --permission-mode dontAsk "/dba"'
    source: { env: HUB_REPO_ROOT }
    checkout: worktree     # default: none
    remote: origin         # which git remote to fetch the MR ref from
```

radar creates a **detached `git worktree`** at the merge request's head commit,
runs the job in it, and removes it when the job ends. `{source_root}` and the
`## Source` section then name that worktree, not the shared clone. `working_dir`
and `checkout: worktree` are rejected together — they contradict each other, and
silently honouring one would tell the skill about a tree it isn't running in.

The commit is the one **just fetched**, not the SHA from radar's last poll: the
diff a skill is handed comes from GitLab live, so pinning the tree to a snapshot
up to `poll_interval_minutes` old would show it a diff of code it can't see.
`{head_sha}` is the fallback if the fetch fails. Each job fetches into a ref of
its own, so concurrent jobs can't be served each other's commit.

The commit comes from GitLab's `refs/merge-requests/<iid>/head`, which every
GitLab server publishes and which resolves **even for MRs from forks** — where
the source branch doesn't exist on the target repo at all. The fetch uses your
normal git credentials for that remote; radar's own GitLab token is not involved.

Your working copy is not touched: a worktree adds no branch, moves no `HEAD`, and
leaves nothing behind once removed. Concurrent jobs get independent trees.

If the fetch or the worktree fails, **the job fails** — it does not fall back to
the current branch, because a review of the default branch presented as a review
of the MR is a confidently wrong answer about other code. `radar check` verifies
git is on `PATH` and that each resolved source is really a repository.

Independently of the checkout mode, `{head_sha}` is available as a placeholder
(recorded on each poll), and with `include_context` the bundle carries a
`## Commits` section with the MR's `base`/`head`/`start` SHAs — so a skill can
pin its own comparison.

### Add your own skills (custom board buttons)

Every skill is an entry in the `skills:` list, and each one becomes a button on
every MR row. `review` and `qa` are entries like any other — nothing exists
until the list names it. Add as many of your own as you like:

```yaml
skills:
  - name: dba            # url/id slug; must be unique
    label: DBA review    # panel heading / long name
    button: DBA          # short button text (optional; defaults to label)
    icon: "🗄"           # emoji shown on the button (optional)
    enabled: true
    command: 'claude -p "/dba {web_url}"'
    working_dir: /path/to/checkout
    timeout_seconds: 600
```

Every entry takes the **same fields** and gets the same placeholders, safety
guards, streaming, and sanitized-markdown output. These capabilities are opt-in
via extra fields:

- `context: gitlab_diff` / `context: jira` / `context: [gitlab_diff, jira]` — pair
  with `include_context: true` to have radar fetch those backends and pipe them to
  the skill on stdin (see above). Omit `context` for a skill that needs no fetch.
- `stores_result: true` — persist the output and show a **✓** badge that re-opens
  it (this is what `qa` uses for saved test plans).
- `source:` / `inputs:` — the skill's declared context bag: where the code is
  checked out, plus any other input it needs (see above). Every skill declares its
  own, so several skills on the same board can point at different trees.
- `checkout: worktree` (+ optional `remote:`) — give each job its own worktree at
  the MR's head commit instead of sharing the configured checkout (see above).

**Two names come with defaults.** `review` and `qa` aren't special *skills* —
they're names that carry the one capability a command line can't advertise:

| `name:` | inherits |
|---|---|
| `review` | `context: gitlab_diff`, the 🔍 icon |
| `qa` | `context: jira`, `stores_result: true`, the 🧪 icon |

They are defaults, not magic — write `context:` or `stores_result:` yourself and
yours wins. Any other name starts with no capabilities at all, so a skill that
needs the diff says `context: gitlab_diff` outright.

Skills are declared **only** here. Older versions also accepted top-level
`review:` and `qa:` blocks; those are now refused with a message pointing at the
list, because two ways to declare one skill meant a `skills:` entry could
silently replace a block of the same name, and a `review` button could appear on
the board without appearing in `skills:`. Moving a block is mechanical — indent
it under `skills:` and give it `- name: review`.

Every enabled skill also appears in `radar check`, so you can confirm its command
is on `PATH` before clicking it.

### Headless agents and background work

A skill that shells out to `claude -p` can hand its real work to a background
subagent. When it does, the first thing it says is a placeholder — *"I'll report
the findings when it completes"* — and the actual findings arrive only in a later
result event, if the run waits for them at all. radar takes the answer from those
result events, so the placeholder is at best glued to the front of your review and
at worst is the entire thing that gets stored.

So radar exports this to **every** skill it launches:

```
CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1
```

Subagents then run inline and the run's last word is its actual work. The variable
goes only into that subprocess's environment — your own interactive Claude Code
sessions are untouched — and if you exported it yourself before starting radar,
your value stands. Inside the child it is a blanket switch: background shells and
async subagents are both unavailable there, **so a skill that fans work out now
does it serially**. Raise that skill's `timeout_seconds` if it was already close
to its budget.

To keep the fan-out instead, drop the default and tell the CLI to wait
indefinitely rather than giving up on background work partway:

```yaml
skills:
  - name: review
    env:
      CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS: "0"
    env_unset: [CLAUDE_CODE_DISABLE_BACKGROUND_TASKS]
```

radar's own budget then becomes the binding limit, and the placeholder text will
appear in the output ahead of the real result — radar keeps every result a run
reports, separated by a blank line. A run killed by the timeout keeps whatever it
had written by then, shown under the error rather than thrown away.

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

Or keep them in a **`.env` file beside `config.yaml`**, which radar reads at
startup (`radar -c /etc/radar/config.yaml` reads `/etc/radar/.env`, falling back
to `./.env`):

```
GITLAB_URL=https://gitlab.example.com
GITLAB_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx
JIRA_BASE_URL=https://yourco.atlassian.net
JIRA_EMAIL=you@yourco.com
JIRA_API_TOKEN=...
REQUESTS_CA_BUNDLE=/path/to/corp-root.pem
```

Add it to `.gitignore`. An exported variable wins over the file — except an
exported *empty* one, which is treated as an open slot rather than an answer, so
a `VAR=` left in a shell profile can't quietly suppress the file's value.
`radar check` prints an `env.dotenv` line naming which variables came from the
file and which the shell overrode (names only, never values).

### 2. Behind a TLS-inspecting proxy (Zscaler & co.)

Skip this unless HTTPS is intercepted on your network. If it is, every call must
trust your organisation's root certificate — and radar reaches its two backends
through two HTTP stacks that read **different** environment variables:

| Backend | Stack | Reads |
|---|---|---|
| GitLab | `python-gitlab` → `requests` | `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE` |
| Jira | `urllib` → `ssl` | `SSL_CERT_FILE`, `SSL_CERT_DIR` |

Neither looks at the other's. A proxy installer typically exports only
`REQUESTS_CA_BUNDLE`, which is why the board can poll GitLab perfectly while the
QA button dies on a certificate error (or the reverse).

**Set whichever one you like — radar copies it to the others at startup**, so
both stacks and any skill it launches agree:

```bash
export REQUESTS_CA_BUNDLE=/path/to/corp-root.pem   # any one of the four
```

A `.env` beside `config.yaml` works just as well, and is read *before* the copy
happens, so a bundle named only there still reaches both stacks.

A variable you set yourself is never overwritten, so pointing the two stacks at
different bundles deliberately still works. A path that doesn't exist is *not*
propagated — Python ignores a missing bundle and silently falls back to the
system store, so radar logs a warning rather than spreading a typo. `radar check`
prints a `tls.ca_bundle` line ahead of the GitLab and Jira checks reporting what
each stack will actually trust.

### 3. Configure

```bash
cp config.example.yaml config.yaml
# edit config.yaml — at minimum, set gitlab.projects and your calendar
uv run radar validate            # sanity-check the file
```

### 4. Poll and serve

```bash
uv run radar poll-once           # fetch once, print obligation counts
uv run radar serve               # dashboard at http://127.0.0.1:8000 + background poller
```

Open <http://127.0.0.1:8000>. `serve` polls GitLab every
`poll_interval_minutes` in-process, and the board's **↻ refresh now** button
runs that same pass on demand; killing and restarting loses nothing (the event
log is on disk in SQLite).

---

## CLI

| Command | What it does |
|---------|--------------|
| `radar poll-once` | One polling pass, then exit (also refreshes the derived snapshot). |
| `radar poll-once --full` | Same, but ignores the last-polled watermark and re-fetches **every open MR**. Safe any time (events dedup, caches are replaced); run it once after upgrading to backfill discussion threads. |
| `radar serve [--host H] [--port P]` | Run the dashboard and the background poller. |
| `radar recompute` | Re-derive every obligation from the event log under the current config. Run after changing SLA rules. |
| `radar validate` | Validate `config.yaml` and exit. |
| `radar check` | Diagnostics: validate config + the DB, and check GitLab/Jira connectivity (auth, token scope, project reachability) and that the review/QA commands are on PATH. Prints ✅/⚠️/❌ per check; exits non-zero on any failure. Also flags if review-request times came from created-date backfill (inflated breaches) rather than system notes. |

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
| `slas[].assignment_business_hours` | Optional budget for getting **any** reviewer onto an MR that has none — the [NO REVIEWERS](#mrs-with-no-reviewers) chip. Omitted everywhere, the check is off. Set it on **every** rule or none: first match wins outright, so a partial config would silently skip MRs matching the rules that lack it (radar refuses to load one). |
| `waive` | Obligations are waived (excluded, shown blue) when `draft: true` and the MR is **currently** a draft, or the MR carries any `labels` listed here. (Only the current draft state waives; historical draft periods are not subtracted from the clock.) |
| `skills` | **Every** dashboard button, as a list. Each entry: `name` (url slug, unique), `label`, `button`, `icon`, `enabled`, `command`, `working_dir`, `timeout_seconds`, `include_context`, `context`, `stores_result`, `source`, `inputs`, `checkout`, `remote`, `env`, `env_unset`. The names `review` and `qa` inherit defaults (see [Add your own skills](#add-your-own-skills-custom-board-buttons)); top-level `review:`/`qa:` blocks are refused. |
| `skills[].env` / `skills[].env_unset` | Extra environment for that skill's subprocess, and names it must not inherit. Values export as written; a valueless key is refused (use `env_unset`). radar's own credentials are stripped and refused in both, in any case spelling. radar exports `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` to every skill unless you exported it yourself — see [Headless agents and background work](#headless-agents-and-background-work). |
| `jira` | `base_url` (builds the `PROJ-123` browse links on the board) and `project_keys` (optional filter so `UTF-8`-shaped tokens aren't matched). Not a credential — fetching a ticket uses `JIRA_BASE_URL`/`JIRA_EMAIL`/`JIRA_API_TOKEN` from the environment. |
| `teams` | Named GitLab-username groups; each becomes an *authored* / *to review* filter pill on the board. |
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
- `threads.py` — the human comments from those same discussions, cached (not
  derived: `resolved` is mutable state only GitLab knows).
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
