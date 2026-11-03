---
name: tracker
description: Coordinate work through a Plane tracker (Plane Cloud or self-hosted) via the plane-agent CLI. Use whenever an orchestrator or subagent needs to queue, dispatch, block, hand off or finish tickets, resume a ticket from its trail, file follow-ups, plan work, or report progress durably. Covers the agent-coordination protocol: one shared agent account, agent:<slug> labels, signed comments, states, blockers, needs-human, stale work and deploy hand-off.
---

# Plane tracker for agents

A Plane project is the durable, big-picture coordination channel between the operator (the human), an orchestrator and its subagents: who holds what, what blocks what, what's waiting on the operator, and a signed trail any later session can resume from. Fine-grained plans and task lists can stay in the repository (an OpenSpec change, a design doc); tickets link to them.

**Follow the protocol exactly; don't improvise states or bypass the CLI for writes.** Repository instructions (`CLAUDE.md`, `AGENTS.md`) may add local rules, such as what counts as a deploy and who may run it; follow them too.

## One account, many agents

Every agent uses the same tracker account, a dedicated bot user (for example `claude`) whose token the CLI reads. What tells agents apart:
- **Agent slug:** your role in the project, such as `api-implementation` or `docs-writer`. It's lowercase letters, digits and dashes, and `orchestrator` is reserved. Labels are per project, so don't prefix the project name.
- **Label `agent:<slug>`:** marks which agent holds an item. The operator filters the board by it.
- **Signed comments:** the CLI appends `agent=<slug> · session=… · where=repo@branch` (plus `openspec=<change>` when set) to every comment you write.
- **Never sign as another slug.** Relaying for an agent that finished or went silent is the orchestrator's `review --for`, signed as `orchestrator`. If you sign with a slug that another session claimed the item under, the CLI warns.
- **Pick role slugs, not task slugs:** name the kind of work, such as `api-implementation`, `docs-writer`, `test-runner` or `value-review`, and reuse a free role for the next item. A slug that already holds an item is refused, and the refusal lists the free roles. Past 20 `agent:` labels the CLI warns when it makes another.

The assignee is the agent account when an agent holds the item, or a human when a human does.

## Running the CLI

```sh
plane-agent config                              # where the instance, project and token come from
export PLANE_AGENT=api-implementation           # your slug
export PLANE_AGENT_SESSION=<your session or run id>   # required for every write
export PLANE_OPENSPEC_CHANGE=<change-name>      # optional: the OpenSpec change the work belongs to
plane-agent whoami
```

- **Session:** every command that may write needs a session id. That's every verb except `context`, `check`, `report`, `whoami`, `config`, `init`, a worker's `queue` and `stale` without `--reclaim`; the orchestrator's `queue` sweeps, so it writes. Such a command needs `PLANE_AGENT_SESSION` or `--session`; without one it exits 2 and says so. **Orchestrators: start every subagent with its own session id** as well as its slug. The operator can turn the requirement off in their user file (`require_session = false`); a repository can't.
- **Not installed?** `uv tool install git+https://github.com/apexJCL/plane-agent` puts `plane-agent` on the PATH; `uvx --from git+https://github.com/apexJCL/plane-agent plane-agent …` runs it without installing.
- **Settings:** the instance URL, workspace, default project, time zone and rate limit come from the nearest `.plane-agent.toml` (committed in the repository), the user's `~/.config/plane-agent/config.toml`, or `PLANE_*` variables. `plane-agent config` shows what resolved and from where. If nothing is configured, **ask the operator**; don't guess an instance. Setup is theirs: `plane-agent init --url URL --workspace SLUG --project KEY --timezone Area/City`.
- **Token:** never ask for it in chat, print it or write it to a file. The CLI reads it from the system keyring (service `plane-agent`, account = the instance host), the user file's `[hosts."<host>"]` entry, `--token-file`, or `PLANE_API_KEY` together with `PLANE_BASE_URL`. If it's missing, the error names the command the operator runs to store it.
- **Project key:** commands below use `PROJ` (refs look like `PROJ-12`). Verbs that take a project (`queue`, `report`, `stale`, `create`, `log`, `bootstrap-project`) default to the configured `project`, so `plane-agent queue` works on its own.
- **`where=`:** detected from the current git checkout, as `<directory name>@<branch>`; a `--where` path is cut to its last part.
- **Global flags go before the verb:** `--json` (machine-readable output), `--agent`, `--session`, `--openspec`, `--where` and `--token-file`, e.g. `plane-agent --json queue PROJ`. After the verb they're a usage error.
- **New project:** if a command says the project has no such state or label, run `plane-agent bootstrap-project PROJ` once (it needs `timezone` set). It shapes the project's states and labels to the protocol, and is safe to re-run. It also checks that cycles are on and that the project's time zone matches. If either is wrong it exits 5 and says what a project admin changes in the Plane UI; ask the operator (the agent account is a Member and doesn't change project settings).

**Exit codes:**

| Code | Meaning | What to do |
|---|---|---|
| 0 | ok | — |
| 3 | not dispatchable | pick another item |
| 4 | **you don't hold the item** | stop working on it now |
| 5 | refused by the protocol | read the message |
| 2 | usage or configuration error: no agent slug, no workspace or project, no token, bad or misplaced flags, no time zone for bootstrap, stdin misuse | read the message; fix the flag, or ask the operator to configure the instance |
| 1 | API or network error (a write with `--attach` removes its uploads first) | see the message |

### One shared rate limit

All agents share the agent account's token, so they share **one budget of API calls a minute**: 60 on Plane Cloud and on self-hosted Plane by default, or whatever `rate_limit` says (`plane-agent config`). On a 429 the CLI waits as long as the server asks and retries; it gives up and exits 1 after about 3 minutes of waiting. Spend the budget carefully:
- **Don't poll.** `queue` costs one call per Ready item. Run it when you need the next item, not in a loop. If the queue is empty, stop and say so; don't spin.
- **Filing many items:** each `create --blocked-by` costs about 5 calls, plus a walk of the blocker graph. At 60 a minute, file more than about 5 items in batches spread over a few minutes.
- Don't wrap the CLI in your own retry loop. It already waits out 429s.

## States

| State | Meaning |
|---|---|
| Backlog | captured, not ready |
| **Ready** | dispatchable |
| **In Progress** | held by exactly one agent (or the operator) |
| **Blocked** | waiting on other work or a human |
| **In Review** | awaiting a verdict |
| **Done** | finished |
| **Cancelled** | abandoned |

Labels: exactly one `kind:{feature,bug,chore,spike,deploy}` per item, plus `needs-human` when waiting on the operator, `waiting` while a Blocked item waits on time or the outside world (`block --wait`), plus the `agent:<slug>` label set by dispatch.

Every Blocked item should say why: a blocker relation, `waiting`, or `needs-human`. The sweep reports a Blocked item with none of them and leaves it alone.

## Orchestrator loop (`PLANE_AGENT=orchestrator`)

1. **`plane-agent queue PROJ`:** lists dispatchable items in order (priority, then the earliest target, then oldest), with their targets. Ready items without target dates are listed apart under `unplanned`; plan them before dispatching. The queue judges by the item's dates, but dispatch needs a plan comment, so an item whose dates were set some other way (the UI, `create --target` alone) is listed yet refused with exit 5: run `plane-agent plan` on it. It also **sweeps** Blocked items whose blockers are all closed, and waits past their `--until` day, back to where they were held from (see **Holds**). It ends with a one-line footer on stderr: deploy items In Review waiting on the operator, `needs-human` items, and how many open items are past their target date. Mention those to the operator; don't act on them yourself.
2. **`plane-agent dispatch PROJ-12 --to api-implementation`:** the item becomes In Progress, assigned to the agent account, labelled `agent:api-implementation`, *before* the subagent starts.
   - Then start the subagent with `PLANE_AGENT=api-implementation` and the ref.
   - A slug that already holds an In Progress item is refused (exit 5); the message lists the free roles, so reuse one.
   - The dispatch comment carries the plan (`Plan: 3 h by 2026-10-23.`).
   - An item with no plan is refused (exit 5). Run `plane-agent plan` first (see **Plans**).
3. **`plane-agent stale PROJ --hours 24 [--reclaim]`:** finds items whose agent hasn't written a signed comment for 24 h. Your own nudges don't reset the clock. `--reclaim` returns them to Ready, unassigned; it needs `--hours` of at least 24. An item with two `agent:` labels is reported for the operator, never reclaimed. `stale` also lists waits past their `--until` day and items In Review for over 72 h, and ends with the same footer as `queue`.
   - **One item, earlier:** `plane-agent reclaim PROJ-12 --reason "why"` takes one agent-held In Progress item back to Ready. It's refused while the holder was active in the last 24 h; `--force` overrides that, with the reason on record. Use it when you know the subagent is gone (its session ended or crashed), not to hurry one.
   - **A subagent finished but didn't hand over** (its session ended after the work was pushed): `plane-agent review PROJ-12 --for api-implementation --reason "its session ended after the last push" [--summary "..."]`. It moves the item to In Review, signed as you, and needs 24 h of silence from that slug. It refuses `kind:deploy` and `needs-human` items. Never run `review` or `done` with the subagent's slug instead.
4. **Give review verdicts:** when a worker has moved an item to In Review, check the work, then:
   - `plane-agent accept PROJ-12 [--summary "..."]`: moves it to Done, which releases the items it blocked. Add a summary only if what you verified adds something;
   - `plane-agent reject PROJ-12 --reason "what's wrong"`: returns it to Ready, unassigned, for another dispatch;
   - `plane-agent rework PROJ-12 "what's left"`: returns it to In Progress for the **same** holder, with your notes in the comment, when the holder should finish it. Start that slug again on the item. It refuses `kind:deploy` and `needs-human` items, and a holder that has taken other work meanwhile (reject instead).

   `accept` refuses `kind:deploy` and `needs-human` items (exit 5); those wait in In Review for the operator. The review verdicts (`accept`, `reject`, `rework`) are for agent work: don't give one on a `kind:deploy` item.
   - **Epics** (parent items, never dispatched): once every sub-item is Done or Cancelled, `plane-agent close-epic PROJ-3 --summary "what the epic delivered"` moves the epic to Done and lists its sub-items in a signed comment. It refuses while any sub-item is open, and names them; it also refuses epics held In Progress or labelled `kind:deploy` or `needs-human`.
5. **Triage the backlog:**
   - `plane-agent ready PROJ-12 [--reason "..."]`: moves a Backlog item to Ready, unassigned, so it can be dispatched. If it has open blockers it goes to Blocked instead, and the sweep returns it to Ready once they close. On a **Blocked** item whose blockers are closed it ends the hold early (a wait the outside world answered, say), returning it to where it was held from. It refuses `needs-human` items and epics (items with sub-items). No comment is written without `--reason`.
   - `plane-agent backlog PROJ-12 --reason "why"`: moves a Ready item nobody holds back to Backlog.
   - `plane-agent prioritize PROJ-12 high [--reason "..."]`: sets the priority (`urgent`, `high`, `medium`, `low`, `none`), which orders the queue. The orchestrator may set it on any open item, a worker only on the item it holds. No comment is written without `--reason`. It's a verb of its own, not a `plan` option, because a priority isn't a plan revision: it needs no reason and starts no new rev.
   - **Plans you can't stand behind:** don't invent dates to get an item planned. `plane-agent plan PROJ-12 --clear --reason "why"` withdraws a Backlog item's plan (its dates and cycle) with a signed `Plan withdrawn.` comment. The next plan is a fresh baseline at the next rev, and `report` doesn't count the withdrawn one. Overdue items with nobody on them are the usual case: withdraw, or move the target with `plan --reason`.
   - `plane-agent cancel PROJ-12 --reason "why it's dropped"`: abandons an item nobody holds. It refuses items already Done or Cancelled, In Progress items (reclaim first), `kind:deploy` and `needs-human` items, epics with open sub-items, and items that open work is blocked by, since a Cancelled blocker would release that work. Cancel or re-plan the dependents first.
6. **File work:** `plane-agent create PROJ "title" --kind chore --estimate 3h --target 2026-10-23 [--start D] [--ready] [--parent PROJ-3] [--blocked-by PROJ-4 ...] [--priority high] [--description "..."]`. Plan when you file, so the item can be dispatched (see **Plans**); Only `--estimate` and `--target` together write a plan; `--estimate` without `--target` is refused, and `--target` alone just sets the dates. Filing a sub-item with `--parent` takes the parent out of its cycle: epics are planned by dates only. With `PLANE_OPENSPEC_CHANGE` set, the change is linked in the description. Descriptions and comments are **markdown** (see **Formatting**).

`queue --sweep`, `stale --reclaim`, `reclaim`, `accept`, `reject`, `rework`, `review --for`, `close-epic`, `ready`, `backlog`, `plan --clear`, `cancel` and `comment --force` (nudging an item you don't hold) only work with `PLANE_AGENT=orchestrator`. Items still labelled `needs-human` can't be dispatched until the question is answered (see below).

### Holds: the orchestrator's `block`

The orchestrator may `block` an item no agent is working on, with `--on` or `--wait`:
- a **Ready** item nobody holds: it returns to Ready when released;
- an **In Review** item (not `kind:deploy`, which waits on the operator by design): the holder and its `agent:` label stay, and it returns to In Review when released;
- a **Blocked** item, including one with `needs-human`: this adds a blocker or a wait beside the question. The label stays. Once the operator removes it, the sweep treats the item as an ordinary wait or blocker.

The CLI records where the item was held from in the block comment, so the sweep, `ready` and `answer` return it there.

**Working alone, without an orchestrator?** The item must already be Ready (file it with `--ready`; promoting a Backlog item is an orchestrator move). Plan it if it has no plan, dispatch it to yourself (`plane-agent dispatch PROJ-12 --to <your slug>`), then `claim`. Agents never take items any other way.

## Plans, actuals and the report

Every item gets a plan before it's dispatched: an hour estimate plus start and target dates, in the project's time zone. The CLI writes it as a signed comment whose last paragraph before the signature is a statement such as `plan: rev=0 estimate=3h start=2026-10-21 target=2026-10-23`. It also sets the item's native dates, so the item shows on the Timeline and Calendar, and puts it in its target week's cycle (`2026-W43`, created when missing). Actual times come from the state changes; don't write them yourself. The first plan is the **baseline**, which the report compares against. Never fake a baseline after the work is done.

- **Estimates are agent wall-clock hours:** how long the agent will hold the item In Progress, start to hand-over, not how long a person would take. Most agent items run under a few hours. The report's `calibration` line gives the median actual/baseline ratio. When it says estimates are off by more than 2x, scale the next estimates by that ratio.
- **First plan:** `plane-agent plan PROJ-12 --estimate 3h --target 2026-10-23 [--start D]`. The start defaults to today. Estimates are > 0 in steps of 0.25 h. Epics take `--target` (and `--start`) only, and stay out of cycles.
- **Revise:** `plane-agent plan PROJ-12 [--estimate 5h] [--target D] [--start D] --reason "why"`. Omitted values are kept, and a moved target moves the item to that week's cycle. Revise when scope changes, rather than letting the item silently overrun. Any slug may plan an item nobody holds; once it's held, only the holder or the orchestrator may (otherwise exit 4).
- **Work done without a ticket:** don't create, claim and close it after the fact. Log it with the real times: `plane-agent log PROJ "title" --kind chore --started "2026-10-20 14:00" --ended "2026-10-20 16:30" --summary "what changed" [--estimate 2h] [--parent PROJ-3]`. Times without an offset are in the project's zone. The item is labelled `retro` and `agent:<slug>` and goes straight to Done (`kind:deploy` goes to In Review for the operator). `--estimate` here is recorded as post-hoc and never counts as a baseline.
- **Report:** `plane-agent [--json] report PROJ [--cycle 2026-W43 | --since D [--until D]] [--burndown] [--max-calls N]`. It covers this week by default. It lists each item that closed in the span with its baseline and latest estimates, active hours, ratios, slip, close date and hours In Review (`review_h`), and lists cancelled items apart. A `calibration` line gives the median actual/baseline ratio. The `parked` flag marks an item that sat In Progress mostly after its holder went quiet. Totals leave out items flagged `no plan`, `never started` or `suspect`, and say how many. `--burndown` adds the cycle's remaining hours per day against the ideal line; it needs a cycle (this week's or `--cycle`), not `--since`. Reading is open to any slug, but it costs about 2 calls per item; it paces itself under the shared limit. On a 60-a-minute instance it stops after 100 calls by default and marks the output `partial`; narrow the span, or pass `--max-calls 0` when you can afford the wait.
- **Warnings:** if a target week has already ended, Plane won't take the item into its cycle. The CLI prints a warning on stderr and still exits 0, with the dates set.
- **Refused (exit 5), with nothing written:** an invalid estimate or date, start after target, no target, a revision or `--clear` without `--reason`, `--clear` on an item that isn't in Backlog or has no plan, planning a closed item, a log ending before it starts or in the future, and any text body (comment, `--summary`, `--reason`, `--human`) whose last paragraph is itself a `plan:`, `actual:` or `hold:` statement. One exception: `create --blocked-by` that would form a cycle is refused after the item is created, so the item exists without that relation; fix it or cancel it.

## Formatting

Every text body is markdown: comment text, `--summary`, `--reason`, `--answer`, `--human` and `--description`. Use it so the operator can scan a comment or a description:
- **Enumerations** (findings, changed files, options) go in a bullet list, one item per line, not a comma-run sentence.
- **Flows and steps** (a repro, a deploy sequence, what happens in order) go in a numbered list.
- **Commands, paths, flags, hostnames and values** go in `code`; command output or a config excerpt goes in a fenced code block.
- **Comparisons** (before/after, expected/actual, plan vs. options) go in a table.
- **A long comment** gets a short **bold** lead-in or `###` heading per part. Keep it to the point: one sentence needs no structure.
- **Loose ends** found along the way are filed as items (`plane-agent create … --blocked-by` or a follow-up), and the comment links them. They don't go in an "Open:" paragraph.

A hand-off that packs commits, rules, checks, timings and loose ends into flat bullets is hard to read. Lay it out instead:

```markdown
**Commits** on `feat/rate-limit-headers`: `be25f24` (middleware), `663ca56` (tests)

### Behaviour
- `RateLimiter` reads the limit per API key from `settings.RATE_LIMITS`
- 429 responses carry `Retry-After` and `X-RateLimit-Reset`

### Verified
- `pytest`: 312 passed; `ruff` clean
- staging: 61st request in a minute answers 429 with `Retry-After: 12`

### Latency (p50 ms, `/api/items`)
| path | before | after |
|---|---|---|
| cache hit | 4.2 | 4.4 |
| cache miss | 38 | 39 |

Follow-ups: PROJ-61 (per-route limits), PROJ-62 (document the headers)
```

**Descriptions** (`create --description`) follow the same rules, and are what the agent who claims the item works from. Start with one or two sentences on the goal, then give each part its own section. Leave out a section that has nothing to say. The CLI adds the OpenSpec line itself.

```markdown
Signed-in users can export their data as a ZIP from the account page (data-export, group 3).

### Scope
- export job in `jobs/export.py`, archive in object storage, link valid for 24 h
- button and status on `app/account/page.tsx`
- update `openspec/changes/data-export`, which still says the export is emailed

### Out of scope
- exports for organization admins

### Done when
- [ ] a user downloads a ZIP with their profile and items
- [ ] tests and lint pass
```

The CLI renders it into the HTML Plane's editor shows:
- **Supported:** headings, **bold**, *italic*, ~~strike~~, `code`, fenced code blocks, links, quotes, bullet and numbered lists, tables, and checklists (`- [ ] item` / `- [x] item`, which become Plane to-do lists when every item in the list is one).
- **Raw HTML** is escaped and shown as text, never rendered.
- **Line breaks:** a single newline is a line break and a blank line starts a new paragraph. `"\n"` inside double quotes is a literal backslash-n, not a newline.
- **Long or multi-line bodies:** pass `-` and feed the text on stdin, so backticks, `$` and quotes reach Plane untouched:

  ```sh
  plane-agent done PROJ-12 --summary - <<'EOF'
  Shipped:

  - [x] migration
  - `make test` passes
  EOF
  ```

  Only one argument per command can be `-`; two, or an empty stdin, exit 2.
- The signature and the `plan:`/`actual:` statements are written by the CLI as plain paragraphs, with a horizontal rule above the signature; don't write them yourself.

## Screenshots and attachments

Post a screenshot when it shows what prose can't: a fixed or broken page, a dashboard, a rendered chart, a simulator screen. Attach a log or report when it's evidence the reviewer needs. Don't attach what fits in a comment.

- **Usage:** `--attach FILE`, repeatable, on `comment` (also `--force`), `block` (`--on` or `--human`), `review` and `done`. Paths are relative to the current directory; the file's name without directories is what shows.

  ```sh
  plane-agent done PROJ-12 --summary "Header fixed on mobile" --attach header.png
  plane-agent comment PROJ-12 "Failing run" --attach build.log
  ```
- **Shown inline:** PNG, JPEG, GIF and WebP appear after the text, above the signature, at most 720 px wide. Everything else is listed in an `Attached:` line. All of them appear in the item's Attachments panel.
- **Accepted:** `png jpg jpeg gif webp pdf txt log yaml yml diff patch md json csv zip gz tgz tar mp4 mov webm`, up to 25 MiB each. SVG and HTML are refused, because the tracker would serve them from its own origin.
- **Making one:**
  - Browser pages: headless Chrome (`chrome --headless --hide-scrollbars --window-size=1280,800 --screenshot=out.png URL`; on macOS the binary is `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"`), or Playwright cropped to the element that matters.
  - iOS simulator: `xcrun simctl io booted screenshot out.png`.
  - Terminal output: paste it as a fenced code block instead of a picture of it.
  - **Never** a full-desktop capture. Keep the files in a scratch directory, not the repo.
- **No secrets on screen:** check every screenshot before posting it. No tokens, passwords, credential or Keychain prompts, `env` output, or config with secret values. Deleting an attachment in the UI doesn't erase it from storage or backups, so a leaked secret must be rotated: tell the operator.
- **Cost:** 2 API calls per file, on top of the command's own. The file itself goes to storage outside the shared budget.
- **Failures:** a bad file (missing, empty, a directory, too big, a refused type, an image whose content doesn't match its extension, two files with the same name) exits 5 before anything is sent. Uploads happen before the item changes. If an upload, the item change or the comment fails, the CLI removes what it uploaded and exits 1, naming any file it couldn't remove; rerun the command once the cause is fixed.
- **Reading back:** `plane-agent context` lists the item's attachments (name, type, size) and shows an embedded image as `[image: name]`, or `[image: removed]` once it's deleted. `context` doesn't fetch the image, so say in the text what the screenshot shows.

## Agent loop

1. **Start:**
   - `plane-agent context PROJ-12`: read the description, blockers, attachments and earlier comments, and the linked OpenSpec change. Add `--by <slug>` for one agent's notes only.
   - `plane-agent claim PROJ-12`: confirms the item is dispatched to your slug and posts a one-line claim comment. Exit 4 means it isn't yours, so don't touch it.
2. **While working:** `plane-agent check PROJ-12` before any long-running or irreversible step (every write re-checks anyway). It writes nothing. Exit 4 means the operator or orchestrator took it back: **stop and don't write**.
3. **Progress notes:** `plane-agent comment PROJ-12 "..."`. These are the trail the next session resumes from, so write what you'd want to read.
4. **Stuck on other work:** `plane-agent block PROJ-12 --on PROJ-15 --reason "..."`. Create the prerequisite first if it doesn't exist. Cycles are refused and escalated with `needs-human`.
   - **Waiting on time or the outside world** (a CI queue, a vendor, a scheduled window): `plane-agent block PROJ-12 --wait "what you wait for" [--until 2026-11-09]`. The item gets the `waiting` label, not `needs-human`; `--until` is the last day of the wait and defaults to a week from today. When it passes, the sweep returns the item to Ready for another dispatch, and `stale` lists it. Don't use `--human` for a wait nobody has to answer.
5. **Stuck on the operator** (a decision, a permission, a credential): `plane-agent block PROJ-12 --human "exact question"`. Then stop; don't wait in a loop.
   - If the operator answers **in your session** and approves closing the question, relay it: `plane-agent answer PROJ-12 --answer "<their answer, quoted>"`. This removes `needs-human`, puts the answer in a signed comment, and returns the item to Ready. It leaves the item Blocked if other blockers are open or a wait is on; an item the orchestrator held from In Review goes back to In Review. Then wait for it to be dispatched again; don't resume it on your own.
   - **Only on the operator's explicit answer and approval.** Agents never remove `needs-human` on their own: not by `answer` without the operator's word, not by `ready`, and not in the UI. Never clear it because you think the question is settled, and never answer one on the operator's behalf. Any agent may relay an answer the operator gave it, including for an item another agent raised.
6. **Finish:**
   - normal work: `plane-agent done PROJ-12`;
   - needs a verdict: `plane-agent review PROJ-12`. The orchestrator then accepts or rejects it; `kind:deploy` items wait for the operator.

   `--summary` is optional: the item, its plan and its sub-items already say what the work was. Write one only where it's needed, and the CLI refuses an empty one there (exit 5):
   - `kind:bug` (`done` or `review`): what was wrong and how it was fixed;
   - `kind:deploy` (`review`): the commands run and how they were verified;
   - `close-epic`: what the epic delivered.

   Elsewhere a summary is for what the operator can't see otherwise: commit or PR refs, a screenshot. Don't recap the work or list "next steps": file follow-ups as items (`plane-agent create …`) and link them.

Never leave an item In Progress you aren't actively working on. The `agent:` label stays after you leave, as attribution.

## Deploy items (`kind:deploy`) end with the operator

Anything that changes a live system (a server, a production database, DNS, a release) ends in `review`, never `done`. Run only the deploy steps the repository's instructions let agents run. **Outward-facing or destructive steps** (public exposure, DNS or network changes, deleting volumes, data or backups, restoring over production) and anything needing elevated privileges need the operator first: `plane-agent block --human` with the exact command. Put the **commands you ran and their verification** in the summary. The CLI refuses `done` on `kind:deploy` and `needs-human` items; only the operator moves those to Done.

A deploy item In Review is the operator's: no agent verb accepts, rejects, reworks, holds or cancels it. If one became obsolete (superseded, or the change was dropped), say so in a comment (`comment --force` for the orchestrator) and leave it; the operator cancels it in the UI. The `queue` and `stale` footers list deploy items In Review until then.

**Notifications:** if the operator set a `notify_command`, the CLI pushes an event to them when an item gets `needs-human`, a deploy item enters In Review, or a wait expires. You don't have to do anything for it, and a failed push only warns.

## The operator's moves are authoritative

If the operator changes an item's state, assignee or `agent:` label, accept it. Don't revert it, and don't argue in comments. When the operator answers a `needs-human` item in the UI, they remove the label and move the item to Ready; or they answer in an agent's session and that agent relays it with `plane-agent answer`. It then goes back into the queue with the answer in its comments.
