# plane-agent

Coordinate an orchestrator and its AI subagents through a [Plane](https://plane.so) project — Plane Cloud or self-hosted.

Long-running agent work loses its thread: sessions end, context is compacted, two agents pick up the same task, a question for the human gets buried in a transcript. `plane-agent` makes a Plane project the durable coordination channel instead:

- **One shared agent account, many agents.** Agents differ by *slug* (`api-implementation`, `docs-writer`); an `agent:<slug>` label says who holds an item, and every comment is signed `agent=<slug> · session=… · where=repo@branch`.
- **A strict protocol** over seven states (Backlog, Ready, In Progress, Blocked, In Review, Done, Cancelled): the orchestrator dispatches, a worker claims, checks it still holds the item before risky steps, blocks on other items or on the human (`needs-human`), and hands off for review. Writes are refused (exit 4) the moment an agent no longer holds its item.
- **A trail to resume from.** `context PROJ-12` returns the description, blockers, attachments and signed comments, so a fresh session picks up where the last one stopped.
- **Dates, not plans.** Items carry Plane's native start and target dates (`create --target`, `plan REF --target`); the queue orders by priority, then target date, and flags what is overdue.
- **Human gates.** Deploy items and `needs-human` items only close by the human's hand; `stale` lists items whose holder went quiet, and `reclaim REF` takes one back.

It ships as a **Claude Code plugin** (the `tracker` skill that teaches agents the protocol) plus the **`plane-agent` CLI** (Python, any agent runtime that can run a shell command can use it).

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
# the CLI
uv tool install git+https://github.com/apexJCL/plane-agent

# the skill, in Claude Code
/plugin marketplace add apexJCL/plane-agent
/plugin install plane-agent@plane-agent
```

Not using Claude Code? The protocol the skill teaches is plain markdown in [`skills/tracker/SKILL.md`](skills/tracker/SKILL.md); give it to your agent as instructions.

## Set up your instance

1. **Create the agent account.** Add a dedicated user (for example `claude`) to the workspace with the **Member** role, so agents can't change project settings. Sharing your own account with the agents works, but then the protocol can't tell an item you hold from one an agent holds.
2. **Create an API token** while signed in as that account. Plane lists API tokens in the account's or the workspace's settings, depending on the version; the token acts as the account that created it.
3. **Point a repository at the instance.** From the repository the agents work in:

   ```sh
   # Plane Cloud (untested, see Compatibility; an app.plane.so URL is mapped to api.plane.so)
   plane-agent init --url https://api.plane.so --workspace acme --project WEB

   # self-hosted (set --rate-limit if you changed API_KEY_RATE_LIMIT from its default of 60/minute)
   plane-agent init --url https://plane.example.com --workspace acme --project WEB
   ```

   This writes `.plane-agent.toml` at the repository root. It holds no secrets; commit it. Add `--user` to write your user-level defaults (`~/.config/plane-agent/config.toml`) instead.
4. **Store the token** in the system keyring under service `plane-agent`, account = the instance host:

   ```sh
   security add-generic-password -U -s plane-agent -a api.plane.so -w      # macOS (prompts for it)
   secret-tool store --label='plane-agent api.plane.so' service plane-agent account api.plane.so   # Linux
   ```

   Or see [Tokens](#tokens) for files, password managers and CI.
5. **Shape the project** to the protocol (states, `kind:*` and `needs-human` labels), then check:

   ```sh
   plane-agent --agent orchestrator bootstrap-project
   plane-agent --agent orchestrator whoami
   ```

## Use it

```sh
export PLANE_AGENT=orchestrator
plane-agent create "Add rate-limit headers" --kind feature --target 2026-11-06 --ready
plane-agent queue                                     # dispatchable items, in order
plane-agent dispatch WEB-12 --to api-implementation   # then start the subagent with that slug and WEB-12

# in the subagent: every write needs a session id, so give each subagent its own
export PLANE_AGENT=api-implementation PLANE_AGENT_SESSION=$RUN_ID
plane-agent context WEB-12 && plane-agent claim WEB-12
plane-agent comment WEB-12 "Middleware in; tests next"
plane-agent block WEB-12 --human "Should 429s count against the quota?"   # then stop
plane-agent review WEB-12 --summary - <<'EOF'
Shipped in `be25f24`; `pytest` 312 passed.
EOF

# back in the orchestrator
plane-agent accept WEB-12
```

Other moves: `block --wait TEXT --until DATE` (waiting on time or an outside party), `ready` (release a Blocked item), `backlog`, `prioritize`, `plan REF --target DATE` (move dates; `--clear` drops them), `rework` (send an item In Review back to its holder), `review --for SLUG` (hand over for an agent that went silent), and `stale` (lists stale holds, expired waits, items long In Review), `reclaim REF` (take an item back). `queue` and `stale` end with a one-line stderr footer: deploy items waiting on the operator, needs-human items, and the count past their target date.

`plane-agent --help` and `plane-agent VERB --help` list every verb; the skill documents the full protocol, exit codes and formatting rules.

## Configuration

Settings, highest precedence first: environment → nearest `.plane-agent.toml` from the current directory up → user file → defaults.

| Setting | Environment | Default | |
|---|---|---|---|
| `base_url` | `PLANE_BASE_URL` | `https://api.plane.so` | instance URL |
| `workspace` | `PLANE_WORKSPACE_SLUG` | — | from the workspace URL, `https://<host>/<slug>/…` |
| `project` | `PLANE_PROJECT` | — | default project key for `queue`, `stale`, `create`, `bootstrap-project` |
| `timezone` | `PLANE_TZ` | this machine's zone | IANA zone that decides "today" for overdue items and `--until` waits |
| `rate_limit` | `PLANE_RATE_LIMIT` | `60` | API calls a minute the instance allows the token |

Per-agent: `PLANE_AGENT` (slug), `PLANE_AGENT_SESSION` (run id in signatures; every write needs one, or `--session`), `PLANE_OPENSPEC_CHANGE` (optional: links an [OpenSpec](https://github.com/Fission-AI/OpenSpec) change in signatures and descriptions).

Two settings live in your **user** file only; a repository's `.plane-agent.toml` can't set them:

| Setting | Default | |
|---|---|---|
| `require_session` | `true` | refuse writes without `PLANE_AGENT_SESSION` or `--session`; `false` lets them through unsigned by a session |
| `notify_command` | — | a command run on events that need the operator (below); run without a shell |

`plane-agent config` prints what resolved and where each value came from.

### Notifications

With `notify_command` set, the CLI runs it after three events: an item flagged `needs-human`, a `kind:deploy` item entering In Review, and a wait expiring (found by `queue`). The command is split like a shell would (`shlex`) but runs without one, gets one JSON object on stdin and has 10 seconds. If it fails or times out the CLI only warns; the tracker write stands.

```json
{"event": "needs_human", "ref": "WEB-12", "title": "Add rate-limit headers", "url": "https://plane.example.com/acme/browse/WEB-12/",
 "agent": "api-implementation", "session": "run-41", "state": "Blocked", "text": "Should 429s count against the quota?"}
```

`event` is `needs_human`, `deploy_review` or `wait_expired`. A Telegram example, with the bot token in a file:

```toml
# ~/.config/plane-agent/config.toml
notify_command = "/home/me/.local/bin/plane-notify"   # a full path: no shell expands ~
```

```sh
#!/bin/sh
# ~/.local/bin/plane-notify: post the event to a Telegram chat
msg=$(jq -r '"\(.event): \(.ref) \(.title)\n\(.text)\n\(.url)"')
curl -fsS --max-time 8 "https://api.telegram.org/bot$(cat ~/.secrets/telegram.token)/sendMessage" \
  --data-urlencode chat_id=123456789 --data-urlencode text="$msg" >/dev/null
```

### Tokens

A token is always tied to the instance it belongs to. A repository's `.plane-agent.toml` can choose an instance but can't hold a token, name a token file or command, or send your token to another host. The CLI looks, in order, for:

1. `--token-file FILE` on the command line;
2. `PLANE_API_KEY`, used only when `PLANE_BASE_URL` is also set in the environment (handy for CI);
3. a `[hosts."<host>"]` entry in your **user** file:

   ```toml
   # ~/.config/plane-agent/config.toml
   workspace = "acme"            # defaults for repositories without a project file

   [hosts."plane.example.com"]
   rate_limit = 300              # this server's API_KEY_RATE_LIMIT
   keychain_account = "claude"   # keyring account, instead of the host name
   # token_file = "~/.secrets/plane.token"
   # token_command = "op read op://dev/plane/token"   # its stdout; run without a shell
   ```

4. the system keyring: service `plane-agent`, account = `keychain_account` or the host (macOS Keychain via `security`, Linux Secret Service via `secret-tool`).

### Rate limit

Every agent shares the agent account's token, so they share one budget. On a 429 the CLI waits as the server asks (`Retry-After`, else `X-RateLimit-Reset`) and gives up after about 3 minutes. At Plane Cloud's 60 a minute, a batch of `create --blocked-by` can take a while; that's expected.

## Compatibility

- **Self-hosted Plane CE v1.4.2**: developed and used against it daily; the test suite fakes its API.
- **Plane Cloud: untested.** The CLI uses the same `/api/v1` routes per Plane's API reference (work items, comments, relations, `work-items/<id>/attachments/`), with its 60/minute limit as the default, but no release has been run against it; `init` and `config` say so. If you try it, [docs/cloud-checklist.md](docs/cloud-checklist.md) lists what to check, and please open an issue with what you find.

Upgrading from 0.2? See [CHANGELOG.md](CHANGELOG.md): 0.3.0 removed the planning layer.

## Development

```sh
uv run pytest
uvx ruff check src tests
```

The tests run against an in-memory fake of the Plane API (`tests/fakeplane.py`) and never read your settings files or keyring.

## License

MIT
