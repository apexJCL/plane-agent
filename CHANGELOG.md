# Changelog

## 0.4.0 (2026-11-05)

Added:
- `search [--open | --state NAME] [--kind K] [--label NAME] [--project KEY] [--limit N] -- TERM…` finds items by ref, title and description, ignoring case and accents. Every term must match, and each argument is one term. It reads only, needs no session, and costs 3 API calls plus one per 100 items. `--json` gives one object per hit, then `{"total": N}`.

## 0.3.0 (2026-11-03)

Removes the planning layer. Coordination (dispatch, claim, block, review, the human gates, `stale`) is unchanged.

Removed:
- `plan: rev=…` statements, hour estimates, and the rule that every dispatchable item needs a plan.
- Weekly cycles: creating them, placing items in them, and the cycle, Cycles-setting and project time zone checks in `bootstrap-project`.
- `report` (actuals, burndown, calibration, `--max-calls`) and the request pacing it used. 429 handling stays.
- `log` (retro and post-hoc entries) and the `retro` label.
- `stale --reclaim`. `stale` only lists now; take an item back with `reclaim REF --reason TEXT`.

Changed:
- Dates are Plane's native start and target dates: `create --target DATE [--start DATE]`, and `plan REF --target DATE [--start DATE] [--clear] [--reason TEXT]`. `plan` keeps the ownership rule (a held item: its holder or the orchestrator, else exit 4) and comments only when `--reason` is given.
- The dispatch comment reads `Dispatched to agent:SLUG. Target DATE.`
- `timezone` / `PLANE_TZ` only decides "today" (overdue items, `--until` waits) and defaults to the local zone.
- Added `--version`.

Compatibility:
- `--estimate` on `create` and `plan` is accepted and ignored, with a one-line deprecation notice on stderr. `plan REF --estimate X` alone does nothing and exits 0.
- `report`, `log` and `stale --reclaim` exit 2 with a message saying they were removed.
- `context` still reads old trails with `plan:` and `actual:` lines; they are plain comments now.

## 0.2.0 (2026-11-03)

First standalone release: the `tracker` skill and the `plane-agent` CLI as one Claude Code plugin for Plane Cloud or self-hosted Plane. It covers the agent-coordination protocol (shared agent account, `agent:<slug>` labels, signed comments, seven states, blockers, `needs-human`, review and deploy gates, stale work, `notify_command`), configuration by `.plane-agent.toml` and per-host tokens, attachments, and plans with estimates, weekly cycles and `report`.
