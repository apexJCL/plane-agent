# Plane Cloud: a manual checklist

plane-agent is developed against self-hosted Plane CE v1.4.2 and its tests fake that API. Nobody has run it against Plane Cloud yet. If you do, use a throwaway project and work through this list. Then open an issue that says which steps passed, which failed, and the exact error.

Plane Cloud allows 60 calls a minute per token. Keep the defaults (`rate_limit = 60`) and expect `report` to stop at 100 calls.

## Setup

1. `plane-agent init --url https://app.plane.so --workspace SLUG --project TEST --timezone Your/Zone`: the file should say `https://api.plane.so`, and stderr should print the untested-Cloud note.
2. Store the token (see the README) and run `plane-agent config`: the host should be `api.plane.so` and the token should be found.
3. `plane-agent --agent orchestrator bootstrap-project`: the states and labels should be created. It should also report the project's time zone and whether Cycles are on.
4. `plane-agent --agent orchestrator whoami`.

## One item, end to end

Export `PLANE_AGENT_SESSION=cloud-check` first.

1. `create "cloud check" --kind chore --estimate 1h --target <a date next week> --ready`, then `queue`: the item should be listed.
2. `dispatch REF --to tester`, then as `tester` run `context REF` and `claim REF`.
3. `comment REF "hello" --attach some.png`: the image should show in the comment in the web UI.
4. `block REF --wait "vendor reply" --until <tomorrow>`. Then `queue`, `stale`, and `ready REF` as the orchestrator: the item should come back as Ready.
5. Dispatch and claim again, `review REF --summary "done"`, and `accept REF` as the orchestrator.
6. `report --burndown`: one row with an actual, and a burndown series.
7. With a `notify_command` that writes stdin to a file, flag an item with `block REF --human "test"`. The event's `url` should open the item on `app.plane.so`.

## What to record

- Any 4xx or 5xx: the verb, the HTTP status and the route.
- Whether 429 responses carry `Retry-After` or `X-RateLimit-Reset`/`X-RateLimit-Remaining`.
- Whether the work-item attachments route (`work-items/<id>/attachments/`) behaves as on CE.
- Which of these exist in the project settings: Cycles and the time zone.
