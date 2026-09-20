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

### Build and test health (Jenkins)

Above the board, a **CI strip** carries one dot per Jenkins job you name in
config — the builds and suites the team actually watches:

| Dot | Meaning |
|-----|---------|
| 🟢 **green** | the last build passed |
| 🟠 **amber** | `UNSTABLE` — it built, tests failed |
| 🔴 **red** | `FAILURE` |
| ⚪ **grey** | aborted, or never built |
| **spinner** | a build is running, ringed in the *last* result's colour — a build running over a red job still reads as red |
| **dashed grey + `!`** | radar could not reach Jenkins |

Click a dot and that job's latest build opens in Jenkins. The link is built from
the URL in your config plus the build number, never from the URL Jenkins reports
about itself — that one comes from its *Jenkins URL* setting and is routinely an
internal host your browser cannot reach.

#### Adding jobs

Name as many as you like: one entry, one dot, left to right in the order you
list them.

```yaml
jenkins:
  base_url: https://jenkins.example.com   # only needed by `path:` entries
  poll_interval_seconds: 60               # default 60, minimum 15
  jobs:
    # Either paste the job page straight out of your browser…
    - name: backend-ci
      url: https://jenkins.example.com/job/hub/job/backend/job/main/
    # …or give the job path under base_url (folders nested with /).
    - name: nightly-e2e
      path: hub/e2e/nightly
    # `name` is optional — this chip is labelled "api", the last path segment.
    - path: platform/api
```

Point each entry at a **job**, not at a folder or the top of a multibranch
project: those have no builds of their own, and `radar check` says so if you do.

Watch out for the one collision this shape allows — the `main` branch job of two
different multibranch projects, where both entries default to the name `main`.
radar refuses to load rather than draw two chips with the same label, so give at
least one of them an explicit `name:`.

There's no limit on how many you list. They are fetched concurrently (eight at a
time), so a pass costs about as long as the slowest job rather than the sum of
them all, and one job hanging cannot delay the rest of the strip.

#### Analysing what broke it

A chip whose last completed build did **not** pass carries a 🔎 button. Pressing
it gathers what Jenkins knows about the failure and hands it to a skill, the way
the review button hands over an MR diff:

* the commits Jenkins recorded for every build since the job last passed — sha,
  author, date, subject and the files each one touched;
* the broken build's console log, which is where a failure prints.

**Both arrive as files, with a short excerpt inline.** A build log runs to tens
of megabytes and a monorepo merge lists thousands of paths per commit; piping
either as prompt text buys one reply of *"prompt is too long"*. So radar writes
the log and the full change list into a scratch directory, names both paths in
the bundle, and inlines the last `jenkins.log_tail_lines` lines (default 120,
also capped by size so a single enormous line cannot fill the prompt) plus the
first 20 commits. A small failure is answerable from the excerpt alone; a real
one is answerable by grepping the file, which is what an agent is good at. The
files live exactly as long as the run that reads them.

The skill needs no Jenkins access of its own — radar fetches everything and
pipes the bundle on stdin. **Which skill runs is named in the `jenkins:` block**, next to the
pipelines it is about:

```yaml
jenkins:
  analysis:
    enabled: true
    skill: analyze-build    # names an entry in `skills:` below
  jobs:
    - name: CI
      path: hub/backend/main

skills:
  - name: analyze-build
    enabled: true
    command: claude -p "/analyze-build"
```

That name is the *only* thing that decides whether the button exists. A skill
does not become the analyser by being called something, or by declaring
anything — with ten skills on a board, which one a button runs should be a line
you can read rather than a property you have to know to go looking for.

A wiring that could not work is refused when the config loads, naming what is
wrong: an unknown skill, a skill left `enabled: false`, `analysis.enabled` with
no skill named, or no jobs to put a button on. Each of those used to render a
strip that looked completely normal and simply offered nothing.

The result streams into the same panel the review and QA buttons use, and is
saved against that build number: the chip then shows a ✓ that re-opens it, so
the next person reads the analysis instead of paying for it again. A new build
number is a new question, and the ✓ goes away.

A build running over a red one still offers the button — the breakage is the
news, and the build analysed is the last one that finished. A job that has never
built, or that radar cannot currently reach, has nothing to analyse and offers
nothing. Only the tail of the log is fetched, by byte offset, so a
hundred-megabyte log costs two small requests rather than a download.

#### A Jenkins with a private certificate

`CERTIFICATE_VERIFY_FAILED … self-signed certificate in certificate chain` means
the chain is signed by a root your machine does not trust — a TLS-inspecting
proxy, or an internal CA. The fix is to trust that root: point `SSL_CERT_FILE`
at it and radar copies it to the other TLS variables at startup, so GitLab, Jira
and Jenkins are all fixed by the one setting and every connection stays
verified. See [Behind a TLS-inspecting proxy](#2-behind-a-tls-inspecting-proxy-zscaler--co)
and the `tls.ca_bundle` line in `radar check`, which prints what each stack will
actually trust.

As a last resort, for a chain that cannot be trusted any other way:

```yaml
jenkins:
  verify_ssl: false
```

That turns verification off for **every** Jenkins call, so any host can present
any certificate for these jobs — including to a request carrying
`JENKINS_TOKEN`. It is off by default, `radar serve` warns at startup while it
is set, and `radar check` reports it as a warning for as long as it stays set.

A job radar cannot reach keeps its last known state, dimmed and flagged: a blip
should not repaint the board grey, and the strip's one-line summary counts it as
*unreachable* rather than as a pass, so a real outage never reads as "all green".

Jenkins is polled in the background (`jenkins.poll_interval_seconds`, default 60,
minimum 15) into an in-process cache, and every page render reads that cache — a
slow or hung Jenkins can never slow the board down. The strip refreshes itself
every 15s, independently of the board's 60s tick and of whichever view filter is
on, because builds start and finish faster than a minute.

radar only ever **reads** Jenkins: no triggering, re-running, or cancelling.
Anonymous by default; set `JENKINS_USER` / `JENKINS_TOKEN` if your Jenkins needs
a login. `radar check` reports every job — reachable, what it last did, and
whether the URL points at a folder or multibranch project rather than a branch
job (the commonest way to get this wrong).

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

- **Personal** — click any reviewer (a name chip on the board, an author name,
  or a pill) to see two lists (`/?view=<username>`):
  - **Authored by them** — MRs they opened, with every chip intact: who *they*
    are waiting on. An MR they opened is listed only here, so it never appears
    twice, and that includes an unassigned one (its assignment chip is theirs).
  - **Review requested from them** — MRs where they are a requested reviewer,
    narrowed to their own chip: who is waiting on *them*.
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
    timeout_grace_seconds: 300                    # …then ask, before stopping it
```

Every skill lives in the `skills:` list — that is the only place they are
declared. `review` and `qa` are ordinary entries whose **names** carry defaults
(see [Add your own skills](#add-your-own-skills-custom-board-buttons)).

`timeout_seconds` bounds the **whole job**, not just the command: preparing a
checkout and fetching the MR's context both talk to the network and are spent
from the same clock, so a job can never outlast its budget — a hung fetch fails
it rather than leaving the panel tailing a job that will never end.

##### Running out of time is a question, not a verdict

A run killed on the stroke of its deadline takes everything it did with it. Forty
minutes of review, three dollars of model time, and the only way back is to pay
for all of it again — because the process is gone and there is nothing left to
resume. Held for five more minutes instead, it usually finishes.

So that is what radar does. When the budget runs out the run is **not** killed:
it is held, the panel says so in amber, and the row offers **＋ 10 min**.

```
AI review   ⏳ out of time after 43m   still running — 4m to give it more time
                                      before it is stopped and its work is lost
                                      ＋ 10 min   ■ stop
```

Answer and it carries on. Answer again later and it carries on again — the
button has no limit, because a person clicking it is a person deciding. Say
nothing and after `timeout_grace_seconds` it ends exactly as it used to: stopped,
failed, and keeping whatever it had written, with the error saying it was held
and nobody answered.

The cost is honest and worth stating: **a held run is still running and still
spending.** That is the trade — five minutes of one model's time against losing
forty. `timeout_grace_seconds: 0` turns the hold off and the deadline is the
deadline again, exactly as it behaved before.

The same **＋ 10 min** sits on every running row from the start, not only once
time has run out, so a countdown getting short while a review is plainly
mid-thought is one click rather than a race. It is on a plain skill, on each step
of a pipeline, and on a build analysis — everywhere radar runs a skill — and a
pipeline gets one more, **＋ 10 min for every step**, because a stage of three
reviews that ran out together is one decision. Time given to a step lengthens the
pipeline's own countdown too, so the panel's clock keeps telling the truth about
a run someone deliberately extended.

Two things the hold deliberately does not cover. A **context fetch** that hangs
still fails on the job's own deadline — there is no work to save, only a socket
with nobody on it. And a pipeline has no hold of its own: `timeout_grace_seconds`
is refused on a `pipeline:` entry, because its steps own the clocks and so they
own what happens when one runs out.

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
  the modal shows **real-time progress** while the run is in flight, then
  renders the final result. radar reads the child's stdout line-by-line and
  streams it to the browser over SSE. Each tool use is logged with what it is
  actually doing (`Bash: git diff --stat`, `Grep: TODO in radar/`) rather than
  just the tool's name, an immediate repeat collapses into a count (`×3`) so a
  loop doesn't scroll the log away, and the panel counts down the time left of
  that skill's `timeout_seconds`. Commands that don't speak stream-json still
  work — their stdout lines become the progress log; they just aren't as
  granular. The countdown shows for those too.

##### What the run spent

The same events say what a run *costs*, and the panel shows it. The headline is
one line — the model that answered, requests made, everything it read, how much
of that the prompt cache served, output, the thinking inside that output, the
bill, and the average time one request took the model:

```
claude-opus-5[1m]  REQUESTS 3  IN 73.6k  CACHED 69%  OUT 411  THINKING 139  COST $0.209  AVG 2.5s
```

Two of those words mean something specific. A **request** is one exchange with
the model: the API is stateless, so every tool call sends the whole conversation
again and counts as another one — three requests here were *decide to run a
command*, *read its output and delegate*, *read that and write the answer*.
**In** is everything the model read on those requests, which is not the same as
what was billed at full price: `input_tokens` on its own counts only what was
neither read from nor written to the cache, and Claude Code caches nearly the
whole prompt, so that figure is 8 tokens beside a 73.6k prompt. The split is in
the tooltip and in the details below.

While a run works the figures sit under its step and move with it; when it ends
they move to the top of the panel, and a pipeline also gets a **total** across
its steps. Under the line, **AI stats** folds open with everything else the
run reported:

| | |
|---|---|
| **Tokens** | everything the model read · fresh input (the part that missed the cache) · cache read (with the hit rate) · cache written (split by the 5m and 1h TTLs, which are priced apart) · output · thinking · total · **biggest request**, against the context window — how close the run came to running out of room, which no total tells you |
| **Money** | what it was billed, and the same again per model — a run whose subagents answer on a cheaper model than its main loop has two bills |
| **Time** | wall clock · time waiting on the model and what share of the run that was (a review that spends four minutes of five running greps wants better context, not a faster model) · time to first token · average per request · queued turns |
| **What it did** | requests · tool calls, tallied by tool (`Read ×12, Bash ×9`) · web searches and fetches, which are billed per request rather than in tokens · subagents spawned, finished, failed, killed and **refused** (by depth, concurrency or budget), how deep they nested and of which type |
| **How it ended** | the run's own terminal reason and stop reason · errors it flagged · **denied tool calls**, by tool · where the account's rate limits stood (five-hour and seven-day utilisation) |
| **The run** | model and window · Claude Code version · permission mode · output style · how many tools and MCP servers it was given · service tier, speed and inference geography |

Two of those are worth the operator's attention beyond curiosity. **Denied tool
calls**: radar runs skills with `--permission-mode dontAsk`, so a tool off your
allowlist is refused without asking and the run writes its answer anyway — the
denials are the difference between a finding and a guess, and they get a pill on
the headline whenever there are any. **Biggest request**: a skill creeping
towards its window is one review away from truncating the diff it is meant to be
reading.

Where the numbers come from, because they are not all equally settled:

- **Input, cached, turns, tool calls and the context peak are counted as the run
  goes.** A request's input side is settled before the model starts answering,
  so those are exact from the first turn.
- **Output, thinking, money and the per-model breakdown arrive with the run's
  final `result` event**, which is where the CLI publishes what it was billed
  for. A message's usage is emitted with its first content block, long before
  the turn has finished writing, so its output count is a fragment (16 tokens
  against a real 479) and radar never adds those up. Thinking meanwhile shows
  the CLI's own live estimate, marked with a `~`, until the billed count
  replaces it. The per-model breakdown is preferred over the run's top-level
  totals: those cover its main loop, so a run that delegated to a subagent
  reports 8.7k cache writes there against the 22.5k it actually paid for.
- **The average** is the run's own `duration_api_ms` over its own turn count
  once it reports them — the model's time and nothing else. Until then it is
  radar's measure of the same wait: tool results going back, to the next answer
  starting.

A command that doesn't speak stream-json reports none of this, and its panel
shows no numbers rather than a row of zeros. A stored result (a QA plan, a build
analysis) keeps the numbers of the run that wrote it, so re-opening it later
answers what it cost as well as what it said.

##### The AI stats section, and the shape of a run

All of the above lives under **AI stats**, a collapsible section in the panel —
folded away by default, remembered per browser once you open it, and present
from the moment a run starts rather than only when it ends. It refreshes itself
every few seconds while the run works, so the numbers and the charts move
without the panel around them being redrawn (and without disturbing the live
progress log or the per-step figures above it, which are unchanged).

Totals cannot say that the last six requests each took a minute, or that the
context doubled halfway through and never came back down. Five charts do, all
sharing one x axis — seconds into the run — so a spike in one can be read
against the others:

| Chart | What it answers |
|---|---|
| **Response time per request** | is the model getting slower as the conversation grows? Drawn against the run's average |
| **Context carried per request** | is this skill filling its window? Drawn against the window when it gets close |
| **Tool calls per request** | a tall bar beside a long wait is an agent grinding through tools, not a slow model |
| **Thinking per request** | where the reasoning actually happened, from the run's live estimate |
| **Tokens read, cumulative** | every request re-sends the conversation, so this is the shape of the bill |

A pipeline draws one line per step on each chart, colour-coded with a legend, so
three parallel reviews can be compared on one axis. The charts are inline SVG
rendered server-side — no chart library, no client-side data fetch, and they
work with JavaScript off. A long run keeps its shape rather than its tail: past
400 requests the timeline halves its resolution instead of dropping the
beginning.

##### A stored result keeps what its run cost

Re-opening a saved answer — the **✓ Full review** button on the board — shows
the same figures the panel showed when the run finished, not just the answer:
the headline strip, the charts, and for a pipeline **the line per step**. Which
step, how long it took, and what it spent, because added together the steps
stop saying that the synthesis took two minutes and the QA plan twenty-four,
and that is the first thing anyone asks of a run that took half an hour.

What is not there is everything that only means something while a job is alive:
nothing to stop, nothing to retry, and no polling. The session id is still
shown — `claude --resume` either finds the conversation or says it cannot, and
the id is also how a run is matched to a captured stream.

The breakdown rides on the same `stats` column as the totals (a `RunStats` as
JSON; see `commands.py`), storing only the fields a step actually reported and
leaving its timeline to the shared `series` — about 400 bytes per step. The
column itself is bigger than that: nearly all of it is the timeline the charts
are drawn from, which measures ~15 KB for a run the size of a typical review
and up to ~160 KB for a four-step pipeline that reached the 400-sample cap on
every step. One row per merge request per skill, replaced rather than appended
to. A result stored before radar kept the breakdown shows its total exactly as
it always did, and is not back-filled: the per-step numbers were never measured
for those runs, and inventing them from the timeline would be a worse answer
than the honest absence.

##### When the numbers themselves are the mystery

Two things the panel says that are easy to misread, and one switch for when
reading is not enough.

**`IN 0` is not "this run read nothing".** It means no request came back with
token counts attached. Providers differ here — a gateway in front of a
third-party model may report usage on some request shapes and not others — so
whenever any request goes unreported, the details block says so outright:

```
usage reported   3 of 15 requests   the others came back with no token counts at all,
                                    so the totals here are only what the provider did
                                    report — not what the run actually read
```

Radar takes a request's counts from **whichever** of its events carries them,
not just the first. On a model that thinks before it acts, the turn opens with a
thinking block that may carry nothing and the counts arrive on the block after
it; reading only the first event recorded zero for entire runs against such a
provider, and made a reporting quirk look like a provider that reports nothing.

**Keep the evidence, then read it back.** Add `--capture` to the command you
already run, and every skill writes its raw event stream to `./radar-streams`,
one file per job, each line stamped with when radar saw it — the half no event
carries, and the half a latency question needs:

```bash
radar serve --capture                    # or --capture /somewhere/else
# one file per job: radar-streams/review-9f2c1ab4e7d1.jsonl
# {"t": 12.481, "event": {"type": "assistant", "message": {...}}}
```

It is off unless asked for — a forensic tool, not a log — and the panel prints
the exact command to read each run back, so the next step is a copy and a
paste. (`RADAR_CAPTURE_STREAM=<dir>` does the same thing for a radar started
some other way, and wins over the flag.)

Then, with no arguments, `radar diagnose-stream` reads back the newest run:

```
requests         14, of which 0 reported token counts
                 ⚠ no request reported any: the provider is not returning usage
served by        more than one model — this run was not answered by one backend:
                 deepseek-v4p1-flash@a: 2 requests, median 4.1s, max 6.6s
                 deepseek-v4p1-flash@b: 12 requests, median 29.5s, max 48.4s

  #  at      wait   context  think  first block  tool
  1   0:08      8.0s    31.6k    900  thinking     Bash
  …
  14  7:06     48.4s    31.1k    900  thinking     Bash

waits            min 8.0s · median 29.5s · max 48.4s
                 growing steadily: +3.16s per request (r=+1.00)
                 but they do NOT track the prompt size (r=-0.15) — the prompt
                 is not what is driving them
model went quiet 57s at 2:30, 41s at 1:12
result           completed · 14 turns · 9k out · $0.5100
```

Those last lines are the point: **growing waits that track the prompt size are
the skill accumulating context** (fix the skill), and **growing waits that
ignore it are the provider** (fix the provider, or stop running three of them at
once). `served by` is what shows a gateway routing concurrent sessions to
different upstreams — the model string is on every assistant event, so two of
them in one run is the diagnosis by itself.

`radar diagnose-stream --all` compares every run in the directory instead, which
is the shape of the question when a pipeline's steps behave differently from
each other:

```
run                       reqs  usage  median  max    trend      served by
------------------------  ----  -----  ------  -----  ---------  -----------------
review-15839d2d.jsonl     15    0/15   13.6s   57.1s  +3.1s/req  deepseek-flash@b
db-review-00229c06.jsonl  62    62/62  3.2s    9.4s   flat       deepseek-flash@a
qa-7c7e0562.jsonl         14    0/14   15.5s   55.2s  +3.4s/req  deepseek-flash@b
```

Either form takes no config and no network, so a capture from the machine that
saw the problem reads anywhere — including a plain `claude -p … > run.jsonl`
somebody produced by hand, which simply comes out with no waits.

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

**Transient failures are retried.** These fetches are idempotent reads, and the
one that costs most is the cheapest to survive — a connection reset a minute
into a pipeline, on the step that merges half an hour of reviews. The GitLab
session retries a reset or a 429/5xx three times with backoff, and radar retries
the whole bundle up to three times a few seconds apart, never past the job's own
deadline. A misconfiguration (an unset variable, a source root that is not a
directory) is refused immediately instead: it will be just as unset next time.
The panel says when a fetch is being retried, and reports the real fault if the
retries run out.

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

### Chain skills into a pipeline

One button can run several skills and hand their answers on. A pipeline is a
`skills:` entry with a `pipeline:` instead of a `command:`:

```yaml
skills:
  - name: full-review
    label: Full review
    icon: "🧭"
    enabled: true
    stores_result: true
    pipeline:
      - parallel: [review, db-review, qa]   # these three at the same time
      - skill: synthesize                   # then this, having read all three

  - name: synthesize
    label: Synthesis
    enabled: false        # no button of its own; it only runs inside the pipeline
    command: 'claude -p --permission-mode dontAsk --output-format stream-json --verbose "/review-synthesis"'
    timeout_seconds: 600
```

Stages run in order. A stage is one skill's name (`- review` or `- skill: review`)
or `- parallel: [...]`, whose skills run **at the same time, each as its own
process**. The pipeline's answer is the last stage's: a single step's output as
it is, or several under a heading each.

**Every step runs exactly as its own button would.** Its `context:`, `source:`,
`checkout: worktree`, `inputs:`, `env:` and `timeout_seconds` all apply — the QA
step still gets the Jira ticket, the DBA step still gets its worktree — which is
why a pipeline refuses those keys on itself: it has nothing to apply them to. A
step that `stores_result` still saves, so a QA plan made inside a pipeline shows
its ✓ badge like any other. A step needs no button of its own: leave it
`enabled: false` and it runs only inside the pipeline (`radar check` still
checks its command).

**What a later step is handed.** After its own stdin bundle, every step past the
first stage gets an `## Earlier steps` section: a `### <label>` per earlier step
with what it wrote, or `(failed)` with the error — plus, for a step that ran out
of time, whatever it had written by then.
Write the synthesizing skill to read that section — drop duplicate findings,
rank them, say where the reviewers disagree. The section introduces itself as
text written by other agents that quotes the MR, to be weighed as evidence
rather than obeyed.

**Failures.** A failed step does not stop the run: a synthesis of two reviews out
of three beats none, and it is told which one is missing. A stage in which
*every* step failed does stop it, since the next stage would have nothing to
work from; the panel shows each step's error.

When a stage does stop the run, the pipeline still answers with **everything
that finished** — a synthesis that could not start is no reason to lose the
three reviews it was going to merge, which on a slow model is half an hour of
work. The error names what survived and the output carries it.

**Running one step again.** Every step that ran carries a button on its row:
**↻ retry** on the one that failed, **↻ run again** on the one that didn't. It
runs that step and finishes the pipeline from there, keeping every step that
already finished *before* it — so a synthesis that died on a connection reset
costs one step to put right instead of the whole review. The resumed step is
handed the same `## Earlier steps` section the first run gave it, and a retry
that works clears the error rather than leaving it beside a good answer.

A step that *succeeded* is worth running again for two reasons, and neither is
a failure radar can detect. A review can finish cleanly and answer badly — a
zero exit code and a non-empty answer is a success by every measure available
here. And a synthesis of three reviews is a synthesis of something that no
longer exists the moment one of those reviews is re-run, so re-running it over
what is there *now* is the point of having steps at all. That case is one
click: nothing comes after the synthesis, so only the synthesis runs, over
whatever the reviews currently say — including a review that failed, which it
is still told about, because a synthesis that only hears from the steps that
worked cannot say what went unreviewed.

Every stage *after* the step runs again too, because its input is about to
change, so the button confirms first, names what that re-runs, and says when an
answer is about to be replaced. The pipeline's total keeps the earlier attempt's
tokens and money: it was paid for, and a bill that fell when you re-ran
something would be worth nothing. The attempt keeps its own row as well —
*SYNTH (earlier attempt)*, with what it spent — because money in a total with
no line to account for it is worse than no breakdown at all.

The buttons need the job radar started, which lives in memory for as long as
`serve` runs — but not only for as long as the panel stays open: re-opening a
saved answer from the board (**✓ Full review**) finds that job again and shows
the panel that produced it, rows and all. After a restart there is only the
saved row, which reads the same and offers nothing it cannot do; run the skill
again from the board instead.

**Budget.** `timeout_seconds` is worked out for you — the slowest step of each
stage, summed. Each step is stopped by its own timeout and nothing stops it
sooner, so that sum is the longest a run can take *if nobody gives a step more
time* (see [Running out of time is a
question](#running-out-of-time-is-a-question-not-a-verdict)), and it is what the
panel's countdown shows. Set `timeout_seconds` higher if you like; a lower figure is
refused.

**Progress.** The panel streams every step's live log, each line prefixed with
the step it came from (`[db-review] 🔧 Read: …`), plus a line as each stage
starts and each step ends.

**Keep the steps from talking to each other.** The steps of one stage run at the
same time in the same checkout, so each `claude -p` sees the others as busy Claude
sessions on the machine. A model that loses track of its own work can message one
of them and wait for an answer that never comes — a real `review` step did exactly
that, polling a sibling that had already finished until its timeout ran out. Give
every skill's command `--disallowedTools "ListAgents SendMessage"`; no board skill
needs either tool. (Clicking two board buttons at once has the same exposure; a
pipeline just guarantees it.)

**Seeing inside a run.** Above the live log the panel keeps a row per step (one
row for a plain skill), refreshed every few seconds: its state and how long it
has run, what it is doing right now, and its Claude session id — hover it for the
`claude --resume` line that opens the whole conversation, reasoning and all.
Subagents are named in the log as they start and end, and their own tool calls
are marked `↳`. A run that has done nothing but *wait* — list other sessions,
message one, poll a background task, `sleep` — for five minutes turns amber and
says what it is waiting on, rather than looking busy. **■ stop** ends that step
now and keeps whatever it wrote; the next stage is told it was stopped and the
pipeline carries on. **■ stop the whole pipeline** also skips every stage still to
come. The rows stay on the panel once a pipeline finishes, so a failed step says
why.

**Why radar runs the steps rather than an agent.** A skill *could* fan out to
subagents itself, but inside radar those run one after another (see [Headless
agents](#headless-agents-and-background-work)), because a background subagent
answers with a placeholder first. A pipeline gets real parallelism without that
trade, keeps the order in config rather than in a model's judgement, and shows
each step's progress.

Refused when the config loads, with the reason: a step naming no skill, a step
with no command, a pipeline as a step (they don't nest), a skill listed twice, a
pipeline with no stages, command-level keys on the pipeline itself, and the
Jenkins analysis skill as either a pipeline or a step (it analyses a build; a
pipeline runs for a merge request).

### Headless agents and background work

A skill that shells out to `claude -p` can hand its real work to a background
subagent. When it does, the first thing it says is a placeholder — *"I'll report
the findings when it completes"* — and the actual findings arrive only in a later
result event, if the run waits for them at all. radar takes the answer from those
result events, so the placeholder is at best glued to the front of your review and
at worst is the entire thing that gets stored.

So radar exports these to **every** skill it launches:

```
CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1     # run subagents inline
CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0     # if one backgrounds anyway, wait forever
```

Subagents then run inline and the run's last word is its actual work. The first
variable is a blanket switch inside the child: background shells and async
subagents are both unavailable there, **so a skill that fans work out now does it
serially** — raise that skill's `timeout_seconds` if it was already close to its
budget. The second is the backstop: a skill can always reach the background some
other way (opting out below, or shelling out to another `claude` itself), and
without it the CLI gives up on background agents after 10 minutes and exits with
only the placeholder. With the ceiling gone, radar's `timeout_seconds` is the
only clock — size it to the skill's real duration. When that clock does run
out, radar tries to take down the skill's **whole process tree**, not just the
`claude` it launched: with the ceiling gone nothing else ever reaps a background
agent, and one left behind would keep running (and spending API tokens) after
the job was failed. Radar does the same if a run exits leaving something that
still holds its output pipe, and sweeps anything still running when radar itself
exits. All of that is best effort — a descendant that re-parents or puts itself
in its own session is beyond reach on both platforms — so it is a strong default,
not a guarantee. The timeout error also says when a background agent was what ran
out of clock, so the fix — raise `timeout_seconds` — is named rather than guessed.
Both variables go only into that subprocess's environment — your own
interactive Claude Code sessions are untouched — and if you exported one
yourself before starting radar, your value stands.

To keep the fan-out instead, drop the inline default (the wait-forever backstop
already covers the rest):

```yaml
skills:
  - name: review
    env_unset: [CLAUDE_CODE_DISABLE_BACKGROUND_TASKS]
```

The placeholder text will then appear in the output ahead of the real result —
radar keeps every result a run reports, separated by a blank line. A run killed
by the timeout keeps whatever it had written by then, shown under the error
rather than thrown away. And whatever the skill's configuration, a run the CLI
itself cut short — it reports "Background tasks still running" on stderr when
an overridden ceiling elapses and it kills agents it was waiting for — is
failed, not stored: its output is a deferral note, not your review, and it
shows under the error together with which override to go looking for.

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
trust your organisation's root certificate — and radar reaches its backends
through two HTTP stacks that read **different** environment variables:

| Backend | Stack | Reads |
|---|---|---|
| GitLab | `python-gitlab` → `requests` | `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE` |
| Jira, Jenkins | `urllib` → `ssl` | `SSL_CERT_FILE`, `SSL_CERT_DIR` |

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
| `radar serve --capture [DIR]` | Same, but keep every run's raw event stream (default `./radar-streams`) for `diagnose-stream` to read back. Off unless asked for. |
| `radar recompute` | Re-derive every obligation from the event log under the current config. Run after changing SLA rules. |
| `radar validate` | Validate `config.yaml` and exit. |
| `radar diagnose-stream [PATH]` | Read back a captured run — the newest one by default, a file if named, or `--all` to compare a whole directory: one line per request with its wait, prompt size, thinking and tool, then whether the waits are growing, whether they track the prompt (the skill's fault) or ignore it (the provider's), which model actually served each request, and what went unreported. Takes no config and no network — a stream captured elsewhere reads fine. |
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
| `skills` | **Every** dashboard button, as a list. Each entry: `name` (url slug, unique), `label`, `button`, `icon`, `enabled`, `command`, `working_dir`, `timeout_seconds`, `timeout_grace_seconds`, `include_context`, `context`, `stores_result`, `source`, `inputs`, `checkout`, `remote`, `env`, `env_unset`. The names `review`, `qa` and `analyze` inherit defaults (see [Add your own skills](#add-your-own-skills-custom-board-buttons)); top-level `review:`/`qa:` blocks are refused. |
| `skills[].context` | Which backends radar fetches for the skill and pipes to it on stdin: `gitlab_diff`, `jira`, or a list. Only about merge-request skills — the CI strip's analyser is given the build's commits and log because [`jenkins.analysis.skill`](#analysing-what-broke-it) names it, and setting `context:` on that skill is refused rather than ignored. |
| `skills[].timeout_grace_seconds` | How long a run that has used up its `timeout_seconds` is **held** — still alive, still spending — for someone to give it more time before it is stopped. Default 300; `0` stops it on its deadline as radar used to. The panel's **＋ 10 min** grants time at any point while a run works, on a plain skill, on each step of a pipeline and on a build analysis. Refused on a `pipeline:` entry: its steps own the clocks. See [Running out of time is a question](#running-out-of-time-is-a-question-not-a-verdict). |
| `skills[].env` / `skills[].env_unset` | Extra environment for that skill's subprocess, and names it must not inherit. Values export as written; a valueless key is refused (use `env_unset`). radar's own credentials are stripped and refused in both, in any case spelling. radar exports `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` and `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0` to every skill unless you exported one yourself — the second removes the CLI's own 10-minute background-agent cutoff, leaving `timeout_seconds` as the only clock. See [Headless agents and background work](#headless-agents-and-background-work). |
| `jira` | `base_url` (builds the `PROJ-123` browse links on the board) and `project_keys` (optional filter so `UTF-8`-shaped tokens aren't matched). Not a credential — fetching a ticket uses `JIRA_BASE_URL`/`JIRA_EMAIL`/`JIRA_API_TOKEN` from the environment. |
| `teams` | Named GitLab-username groups; each becomes an *authored* / *to review* filter pill on the board. |
| `jenkins` | The jobs behind the [CI strip](#build-and-test-health-jenkins). `base_url` (optional, only for `path:` jobs), `poll_interval_seconds` (default 60, minimum 15), and `jobs`: each takes `url` (the job page as your browser shows it) **or** `path` (its job path under `base_url`), plus an optional `name` for the chip (defaults to the job's own last path segment). Omit the block and there is no strip. Not a credential — a Jenkins that refuses anonymous reads takes `JENKINS_USER`/`JENKINS_TOKEN` from the environment. |
| `jenkins.analysis` | Which skill the CI strip's 🔎 button runs: `enabled` and `skill` (the name of an entry in `skills:`). That name is the only thing that makes a skill the analyser — nothing about the skill itself does. Omit the block for no button; a wiring that cannot work (unknown skill, disabled skill, misspelled key) is refused when the config loads. See [Analysing what broke it](#analysing-what-broke-it). |
| `jenkins.log_tail_lines` | How many lines of the broken build's console log go *inline* in the bundle (default 120, the end of the log, also capped by size). The whole log and the whole change list are written to files the bundle names, so the skill can search them — see [Analysing what broke it](#analysing-what-broke-it). |
| `jenkins.verify_ssl` | Defaults to `true`. `false` stops verifying Jenkins certificates entirely, for a private chain that cannot be trusted any other way — see [A Jenkins with a private certificate](#a-jenkins-with-a-private-certificate). Trusting the CA via `SSL_CERT_FILE` is the fix that keeps verification on and covers GitLab and Jira too; `radar check` warns while this is set. |
| `gamification` | Consumed in Phase 3; carried verbatim for now. |

Secrets are **never** in this file — only `GITLAB_URL` / `GITLAB_TOKEN` (plus
`JIRA_*` for QA context, and `JENKINS_USER` / `JENKINS_TOKEN` for a Jenkins that
needs a login) in the environment.

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
- `poller.py` / `scheduler.py` — ingestion and the in-process loops.
- `jenkins.py` — the CI strip: a small Jenkins client, the state table behind
  the dots, and the background-refreshed cache every request renders from (no
  request path ever calls Jenkins).
- `commands.py` — launch one skill's command for a job, stream its progress,
  and measure it (`RunStats`: tokens in/cached/out/thinking, money, requests,
  tools, subagents, denials, and a per-request timeline).
- `charts.py` — the inline-SVG charts the panel draws that timeline with.
- `pipeline.py` — run several skills as one job: stages in order, the steps of
  a stage in parallel, each later step handed the earlier steps' answers.
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
(deploy on a trusted network), no AI/LLM features. radar reads Jenkins but never
writes to it — no triggering, re-running, or cancelling builds — and the CI strip
is team-level: per-MR pipeline status on each board row is a separate feature.
