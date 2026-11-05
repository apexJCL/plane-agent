---
name: tracker
description: Coordinate work through a Plane tracker (Plane Cloud or self-hosted) via the plane-agent CLI. Use whenever an orchestrator or subagent needs to queue, dispatch, block, hand off or finish tickets, resume a ticket from its trail, file follow-ups, or report progress durably. Covers the agent-coordination protocol: one shared agent account, agent:<slug> labels, signed comments, states, blockers, needs-human, stale work and deploy hand-off.
---

# Plane tracker for agents

A Plane project is the durable coordination channel between the operator (the human), an orchestrator and its subagents: who holds what, what blocks what, what waits on the operator, and a signed trail a later session resumes from. Detailed plans stay in the repository (an OpenSpec change, a design doc); tickets link to them. **Follow the protocol exactly; never bypass the CLI for writes.** Repository instructions (`CLAUDE.md`, `AGENTS.md`) may add local rules; follow them too.

## Identity and setup

- **One account, many agents.** Every agent uses the same bot account. Your **slug** (`api-implementation`, `docs-writer`; `orchestrator` is reserved) is a role, not a task: reuse a free role. The `agent:<slug>` label marks who holds an item; the CLI signs each comment `agent=<slug> · session=… · where=repo@branch` (+ `openspec=<change>`). **Never sign as another slug.**
- **Environment:** `PLANE_AGENT=<slug>`, `PLANE_AGENT_SESSION=<run id>` (every write needs one; **orchestrators: give each subagent its own**), optional `PLANE_OPENSPEC_CHANGE`. Global flags (`--json`, `--agent`, `--session`, `--where`, `--token-file`) go **before** the verb.
- **Settings** come from `.plane-agent.toml`, the user file or `PLANE_*`; `plane-agent config` shows them. Nothing configured: **ask the operator**. Never ask for, print or store the token. Not installed: `uv tool install git+https://github.com/apexJCL/plane-agent`.
- **New project:** `plane-agent --agent orchestrator bootstrap-project PROJ` shapes states and labels; it's safe to re-run. Verbs taking a project default to the configured one.
- **Rate limit:** all agents share one budget (60 calls a minute by default). The CLI waits out 429s; don't add retry loops and **don't poll** `queue`. If the queue is empty, stop and say so.

| Exit | Meaning | Do |
|---|---|---|
| 0 | ok | — |
| 2 | usage or configuration error (also a removed verb) | read the message |
| 3 | not dispatchable | pick another item |
| 4 | **you don't hold the item** | stop now; write nothing more |
| 5 | refused by the protocol | read the message |
| 1 | API or network error | see the message |

## States and labels

Backlog → **Ready** (dispatchable) → **In Progress** (one holder) → **In Review** → Done; also **Blocked** and Cancelled. Each item has exactly one `kind:{feature,bug,chore,spike,deploy}`, plus `needs-human` when the operator must answer and `waiting` while it waits on time or the outside world. A Blocked item says why: a blocker relation, `waiting` or `needs-human`.

**Dates** are Plane's native start and target dates. They're optional and drive the queue order and the overdue count. Set them with `create --target D [--start D]` or `plan REF --target D [--start D] | --clear [--reason "why"]`. `plan` writes a comment only with `--reason`; a held item's dates belong to its holder or the orchestrator.

## Orchestrator (`PLANE_AGENT=orchestrator`)

1. `plane-agent queue` lists dispatchable items by priority, then target date, then age. It also **sweeps**: Blocked items whose blockers closed, or whose `--until` day passed, return to where they were held from. Its stderr footer lists deploy items In Review, `needs-human` items and the overdue count; tell the operator, don't act on them.
2. `plane-agent dispatch PROJ-12 --to <slug>` sets In Progress, the agent account and `agent:<slug>` *before* you start the subagent with that slug, the ref and a session id. A slug already holding an item is refused, and the refusal lists the free roles.
3. `plane-agent stale [--hours 24]` lists items whose holder wrote nothing signed for that long, expired waits, and items In Review over 72 h. It changes nothing. To take one back: `reclaim PROJ-12 --reason "why"` (refused if the holder was active in the last 24 h; `--force` overrides that). If a subagent finished but never handed over: `review PROJ-12 --for <slug> --reason "why"`.
4. **Verdicts** on In Review items:
   - `accept [--summary]` moves to Done and releases dependents;
   - `reject --reason` returns to Ready, unassigned;
   - `rework PROJ-12 "what's left"` returns to the same holder;
   - `close-epic PROJ-3 --summary` once every sub-item is closed.
   None of them works on `kind:deploy` or `needs-human` items.
5. **Triage:**
   - `ready` moves Backlog to Ready, or ends a hold early;
   - `backlog --reason` moves a Ready item back to Backlog;
   - `prioritize PROJ-12 high` sets the priority;
   - `cancel --reason` drops an item nobody holds, but not if it blocks open work;
   - `block PROJ-12 --on X | --wait TEXT` holds an item nobody is working on.
6. **File work:** check for a duplicate first with `plane-agent search --open "topic"`. Then `plane-agent create PROJ "title" --kind chore [--ready] [--target D] [--parent PROJ-3] [--blocked-by PROJ-4 …] [--priority high] [--description -]`.

Orchestrator-only: `queue --sweep`, `reclaim`, `accept`, `reject`, `rework`, `review --for`, `close-epic`, `ready`, `backlog`, `cancel`, `comment --force`. **Working alone?** File the item `--ready`, then dispatch it to yourself and claim it.

## Searching

`plane-agent search [--open | --state S] [--kind K] [--label L] [--limit N] [--project P] -- TERM…` finds items by ref, title and description, not comments; **any agent can use it, with no session, and it writes nothing**. Each argument is one term and all must match; quote a phrase. Case and accents are ignored. A ref matches only whole (`PROJ-12` or `12`). Without a filter it includes Done and Cancelled items, where earlier work and decisions sit. Run it before `create`, and when a task touches a topic that may already have a trail, then read a hit with `context`. No results is exit 0, with a note on stderr.

## Agent loop

1. `plane-agent context PROJ-12` (`--by <slug>` for one agent's notes), then `plane-agent claim PROJ-12`. If you get exit 4, it isn't yours.
2. `plane-agent check PROJ-12` before any long or irreversible step. Exit 4 means it was taken back: **stop**.
3. `plane-agent comment PROJ-12 "…"`: progress notes are the trail the next session resumes from.
4. **Blocked:**
   - by other work: `block PROJ-12 --on PROJ-15 --reason "…"` (create the prerequisite first);
   - by time or an outside party: `block --wait "what" [--until D]` (a week by default);
   - by the operator: `block --human "exact question"`, then **stop**.
   Relay an answer with `answer PROJ-12 --answer "<their words>"`, but only when the operator answered in your session and approved. **Never clear `needs-human` on your own.**
5. **Finish:** `done PROJ-12`, or `review PROJ-12` when it needs a verdict. `--summary` is required only for `kind:bug` (what was wrong and the fix), `kind:deploy` review (the commands run and how you verified them) and `close-epic`. Otherwise add one only for refs or screenshots. File follow-ups as items; don't list "next steps".

Never leave an item In Progress you aren't working on.

## Formatting and attachments

Every text body is markdown. Use lists for enumerations and steps, `code` for commands, paths and values, fenced blocks for output, tables for comparisons, and `###` headings in long comments. Pass `-` to read a long body from stdin (`--summary - <<'EOF'`); only one `-` per command. The CLI writes the signature; a body can't end in a `hold:` line.

`--attach FILE` (repeatable) works on `comment`, `block`, `review` and `done`. Images show inline. Allowed: common image, text, log, PDF and archive types up to 25 MiB; SVG and HTML are refused. **Check every screenshot for secrets** before posting it. Never post a full-desktop capture. If an attachment leaks a secret, tell the operator so it is rotated.

## Deploy items and the operator

Anything that changes a live system is `kind:deploy`, and it ends in `review`, never `done`. Run only the steps the repository allows. Before any **outward-facing, destructive or privileged step**, run `block --human` with the exact command. In the summary, list the commands you ran and how you verified them. A deploy item In Review is the operator's: no agent verb moves it. If it's obsolete, comment and leave it.

If the operator set a `notify_command`, the CLI notifies them when an item gets `needs-human`, a deploy item enters review, or a wait expires. **The operator's moves are authoritative:** never revert their changes to a state, assignee or label.
