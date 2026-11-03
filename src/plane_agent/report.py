"""Plan-vs-actual report (openspec/specs/work-planning, "Plan-vs-actual report").

Read-only and open to any slug. One pass over the project's items, then activities and comments for each item that
closed in the span (and, with --burndown, for every item in the cycle not already read): 2 calls per item, paced by
the client. `max_calls` stops the item reads early; the report then says it is partial.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from .planning import (
    PlanError,
    actuals,
    baseline,
    closed_at,
    cycle_for_week,
    hours,
    latest,
    latest_estimate,
    local_day,
    median,
    membership,
    parse_day,
    plans,
    revisions,
    state_changes,
    week_named,
    week_of,
)
from .protocol import (
    AGENT_PREFIX,
    CLOSED_GROUPS,
    EXIT_REFUSED,
    RETRO,
    Agent,
    Project,
    ProtocolRefusal,
)


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 2)


def _day_end(day: date, proj: Project) -> datetime:
    return datetime.combine(day + timedelta(days=1), time(0), proj.zone)


class _Item:
    """An item's activities and comments, fetched once."""

    def __init__(self, agent: Agent, proj: Project, raw: dict):
        self.raw = raw
        self.activities = agent.client.activities(proj.id, raw["id"])
        self.comments = agent.client.comments(proj.id, raw["id"])
        self.plans = plans(self.comments)


def report(
    agent: Agent, identifier: str, cycle: str | None = None, since: str | None = None, until: str | None = None,
    burndown: bool = False, max_calls: int | None = None,
) -> dict:  # fmt: skip
    proj = agent.project(identifier)
    first_call = agent.client.calls
    agent.client.pace()
    try:
        if since:
            lo, hi = parse_day(since, "--since"), parse_day(until, "--until") or agent.today(proj)
            name = None
            if burndown:
                raise PlanError("--burndown needs a cycle, not a --since span")
        else:
            if until:
                raise PlanError("--until goes with --since")
            name, lo, hi = week_named(cycle) if cycle else week_of(agent.today(proj))
        if lo > hi:
            raise PlanError(f"the span starts {lo}, after it ends {hi}")
    except PlanError as e:
        raise ProtocolRefusal(str(e), EXIT_REFUSED) from None

    raw_items = list(agent.client.iter_items(proj.id))
    parents = {i["parent"] for i in raw_items if i.get("parent")}
    by_id = {s["id"]: s for s in proj.states.values()}
    group = lambda sid: by_id.get(sid, {}).get("group")
    in_progress, in_review = proj.state_id("In Progress"), proj.state_id("In Review")
    closed_ids = {sid for sid in by_id if group(sid) in CLOSED_GROUPS}
    agent_labels = {lb["id"]: n.removeprefix(AGENT_PREFIX) for n, lb in proj.labels.items()
                    if n.startswith(AGENT_PREFIX)}  # fmt: skip
    retro_id = proj.labels.get(RETRO, {}).get("id")
    now = agent.now()
    fetched: dict[str, _Item] = {}

    class _Budget(Exception):
        pass

    def item(raw: dict) -> _Item:
        if raw["id"] not in fetched:
            if max_calls is not None and agent.client.calls - first_call + 2 > max_calls:
                raise _Budget
            fetched[raw["id"]] = _Item(agent, proj, raw)
        return fetched[raw["id"]]

    def in_span(value: str | None) -> bool:
        return bool(value) and lo <= local_day(value, proj.zone) <= hi

    rows, cancelled = [], []
    partial = None
    try:
        for raw in raw_items:
            g = group(raw["state"])
            if raw["id"] in parents or g not in CLOSED_GROUPS:
                continue
            # completed_at is stable after later patches; a cancel only shows in updated_at and the activity log.
            # Neither is before the close. completed_at is the close itself, so a Done item stamped more than a day
            # after the span (the day absorbs a stamp a moment past midnight) closed after it; updated_at moves with
            # any later edit, so a cancelled item gets no upper bound.
            hint = raw.get("completed_at") if g == "completed" else raw.get("updated_at")
            if not hint or local_day(hint, proj.zone) < lo:
                continue
            if g == "completed" and local_day(hint, proj.zone) > hi + timedelta(days=1):
                continue
            it = item(raw)
            closed = closed_at(it.activities, closed_ids)
            if not in_span(closed.isoformat() if closed else hint):
                continue
            act = actuals(it.activities, it.comments, in_progress, agent_labels, now)
            if g == "cancelled":
                cancelled.append({"ref": proj.ref(raw), "name": raw["name"], "active_h": _r(act.active_hours)})
                continue
            rows.append(_row(proj, raw, it, act, retro_id, closed, in_review))
    except _Budget:
        partial = f"stopped at --max-calls {max_calls}: rows and totals cover only the items read so far"

    out: dict = {"project": proj.identifier, "zone": proj.tz, "cycle": name, "from": lo.isoformat(),
                 "to": hi.isoformat(), "rows": rows, "cancelled": cancelled, "totals": _totals(rows),
                 "calibration": _calibration(rows)}  # fmt: skip
    if burndown and partial is None:
        try:
            out["burndown"] = _burndown(agent, proj, lo, hi, raw_items, parents, closed_ids, item, fetched, now)
        except _Budget:
            partial = f"stopped at --max-calls {max_calls} during the burndown, which is left out"
    if partial:
        out["partial"] = partial
    out["calls"] = agent.client.calls
    return out


def _review_hours(it: _Item, in_review: str, closed: datetime | None) -> float | None:
    """Hours the item spent In Review before it closed, over every review round."""
    total, opened, seen = 0.0, None, False
    for at, _old, new in state_changes(it.activities):
        if closed is not None and at > closed:
            break
        if new == in_review:
            opened, seen = opened or at, True
        elif opened is not None:
            total += (at - opened).total_seconds() / 3600
            opened = None
    return total if seen else None


def _calibration(rows: list[dict]) -> dict | None:
    """How actual agent time compares with the baselines: the median ratio and the share far off either way."""
    xs = [r["x_baseline"] for r in rows if r["x_baseline"] is not None and not ({"suspect", "retro"} & set(r["flags"]))]
    if not xs:
        return None
    mid = median(xs)
    return {"rows": len(xs), "median_x": _r(mid), "under_half": _r(sum(1 for x in xs if x < 0.5) / len(xs)),
            "over_double": _r(sum(1 for x in xs if x > 2) / len(xs)), "rescale": not 0.5 <= mid <= 2}  # fmt: skip


def _row(proj: Project, raw: dict, it: _Item, act, retro_id: str | None, closed: datetime | None,
         in_review: str) -> dict:  # fmt: skip
    real = [p for p in it.plans if not p.posthoc]
    base, cur = baseline(it.plans), latest(real)
    target = raw.get("target_date") or (cur.target.isoformat() if cur else None)
    end = act.end.astimezone(proj.zone).date() if act.end else None
    flags = []
    if (retro_id and retro_id in (raw.get("labels") or [])) or act.retro:
        flags.append("retro")
    if any(p.posthoc for p in it.plans):
        flags.append("post-hoc estimate")
    if base is None:
        flags.append("no plan")
    if act.never_started:
        flags.append("never started")
    if cur and (raw.get("start_date"), raw.get("target_date")) != (cur.start.isoformat(), cur.target.isoformat()):
        flags.append("dates edited")
    if act.suspect:
        flags.append("suspect")
    if act.parked:
        flags.append("parked")
    latest_est = latest_estimate(real)
    return {
        "ref": proj.ref(raw), "name": raw["name"],
        "baseline_h": base.estimate if base else None, "latest_h": latest_est,
        "active_h": _r(act.active_hours), "elapsed_h": _r(act.elapsed_hours),
        "x_baseline": _r(act.active_hours / base.estimate) if base and not act.never_started else None,
        "x_latest": _r(act.active_hours / latest_est) if latest_est and not act.never_started else None,
        "target": target, "actual_end": end.isoformat() if end else None,
        "closed": closed.astimezone(proj.zone).date().isoformat() if closed else None,
        "review_h": _r(_review_hours(it, in_review, closed)),
        "slip_days": (end - date.fromisoformat(target)).days if end and target else None,
        "revisions": revisions(it.plans), "flags": flags,
    }  # fmt: skip


def _totals(rows: list[dict]) -> dict:
    excluded = {"no plan": 0, "never started": 0, "suspect": 0}
    counted = []
    for r in rows:
        why = next((f for f in excluded if f in r["flags"]), None)
        if why:
            excluded[why] += 1
        else:
            counted.append(r)
    on_time = [r for r in counted if r["slip_days"] is not None]
    return {
        "items": len(counted),
        "baseline_h": _r(sum(r["baseline_h"] for r in counted)),
        "active_h": _r(sum(r["active_h"] for r in counted)),
        "median_x_baseline": _r(median([r["x_baseline"] for r in counted])),
        "on_time_share": _r(sum(1 for r in on_time if r["slip_days"] <= 0) / len(on_time)) if on_time else None,
        "excluded": {k: v for k, v in excluded.items() if v},
    }


def _burndown(agent, proj, lo, hi, raw_items, parents, closed_ids, item, fetched, now) -> dict:
    """Remaining latest-estimate hours of the cycle's items at the end of each day, against the ideal line."""
    cycle = cycle_for_week(agent.client.cycles(proj.id), lo, hi, proj.zone)
    if cycle is None:
        raise ProtocolRefusal(f"{proj.identifier} has no cycle for {lo}..{hi}", EXIT_REFUSED)
    members = {i["id"]: i for i in agent.client.cycle_items(proj.id, cycle["id"])}
    # Items that joined and left the cycle are no longer listed; those closed in the span were already read.
    candidates = [i for i in raw_items if i["id"] not in parents and (i["id"] in members or i["id"] in fetched)]
    tracks = []
    for raw in candidates:
        it = item(raw)
        spans = membership(it.activities, cycle["id"])
        if spans:
            tracks.append((it, spans))

    def remaining(at: datetime, include_closed: bool = False) -> float:
        total = 0.0
        for it, spans in tracks:
            if not any(a <= at and (b is None or at < b) for a, b in spans):
                continue
            changes = [c for c in state_changes(it.activities) if c[0] <= at]
            if not include_closed and changes and changes[-1][2] in closed_ids:
                continue
            total += latest_estimate([p for p in it.plans if not p.posthoc], until=at) or 0.0
        return total

    days = [lo + timedelta(days=n) for n in range((hi - lo).days + 1)]
    scope = remaining(_day_end(lo, proj), include_closed=True)
    last = len(days) - 1
    points = []
    for n, d in enumerate(days):
        at = _day_end(d, proj)
        points.append({"day": d.isoformat(), "remaining_h": _r(remaining(min(at, now))) if d <= agent.today(proj)
                       else None, "ideal_h": _r(scope * (last - n) / last) if last else 0.0})  # fmt: skip
    return {"cycle": cycle["name"], "scope_h": _r(scope), "days": points}


def render(rep: dict) -> list[str]:
    """The report as text lines."""
    fmt = lambda x: "-" if x is None else (hours(x) if isinstance(x, float | int) else str(x))
    span = rep["cycle"] or f"{rep['from']}..{rep['to']}"
    out = [f"{rep['project']} {span} ({rep['from']}..{rep['to']}, {rep['zone']})"]
    cols = ["ref", "base", "latest", "active", "elapsed", "x base", "x latest", "target", "end", "slip", "closed",
            "review", "rev", "flags", "name"]  # fmt: skip
    table = [cols] + [[r["ref"], fmt(r["baseline_h"]), fmt(r["latest_h"]), fmt(r["active_h"]), fmt(r["elapsed_h"]),
                       fmt(r["x_baseline"]), fmt(r["x_latest"]), fmt(r["target"]), fmt(r["actual_end"]),
                       fmt(r["slip_days"]), fmt(r["closed"]), fmt(r["review_h"]), str(r["revisions"]),
                       ", ".join(r["flags"]) or "-", r["name"]]
                      for r in rep["rows"]]  # fmt: skip
    widths = [max(len(row[i]) for row in table) for i in range(len(cols) - 1)]
    out += ["  ".join(c.ljust(w) for c, w in zip(row[:-1], widths, strict=True)) + "  " + row[-1] for row in table]
    if not rep["rows"]:
        out.append("(nothing reached Done in this span)")
    t = rep["totals"]
    excl = ", ".join(f"{v} {k}" for k, v in t["excluded"].items()) or "none"
    share = "-" if t["on_time_share"] is None else f"{t['on_time_share']:.0%}"
    out.append(f"totals: {t['items']} items, baseline {fmt(t['baseline_h'])} h, active {fmt(t['active_h'])} h, "
               f"median x baseline {fmt(t['median_x_baseline'])}, on time {share}; excluded: {excl}")  # fmt: skip
    if c := rep.get("calibration"):
        hint = ("; estimates are off by more than 2x: estimate the agent's wall-clock time" if c["rescale"] else "")
        out.append(f"calibration: median actual/baseline {fmt(c['median_x'])} over {c['rows']} rows, "
                   f"{c['under_half']:.0%} under 0.5x, {c['over_double']:.0%} over 2x{hint}")  # fmt: skip
    if rep["cancelled"]:
        out.append("cancelled: " + ", ".join(f"{c['ref']} ({fmt(c['active_h'])} h) {c['name']}" for c in rep["cancelled"]))
    if bd := rep.get("burndown"):
        out.append(f"burndown {bd['cycle']} (scope {fmt(bd['scope_h'])} h): day  remaining  ideal")
        out += [f"  {p['day']}  {fmt(p['remaining_h']):>9}  {fmt(p['ideal_h']):>5}" for p in bd["days"]]
    if rep.get("partial"):
        out.append(f"PARTIAL: {rep['partial']}")
    return out
