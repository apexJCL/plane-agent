"""Plans, actuals and weeks (openspec/specs/work-planning).

Pure functions over Plane's comment and activity payloads; protocol.py does the I/O.

A plan or actual is a machine statement written as the plain paragraph immediately before a comment's signature:
    plan: rev=0 estimate=3h start=2026-10-21 target=2026-10-23
    plan: rev=0 withdrawn
    actual: started=2026-10-20T14:00:00-07:00 ended=2026-10-20T16:30:00-07:00
Anywhere else (quoted, earlier in the body, unsigned) it doesn't count.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo

# The statement paragraph, then the signature paragraph the CLI writes, at the very end of the comment.
_TAIL = re.compile(r"(?:^|</[a-z0-9]+>)<p>([^<]*)</p>(?:<hr[^>]*>)?<p>agent=([a-z0-9][a-z0-9-]*) · session=[^<]*</p>\s*$")
_PLAN = re.compile(
    r"plan: rev=(\d+)(?: estimate=(\d+(?:\.\d+)?)h)? start=(\d{4}-\d\d-\d\d) target=(\d{4}-\d\d-\d\d)( posthoc=1)?"
)
_HOLD = re.compile(r"hold: from=(Ready|In Progress|In Review)(?: until=(\d{4}-\d\d-\d\d))?")  # a block or wait
_WITHDRAWN = re.compile(r"plan: rev=(\d+) withdrawn")  # ends the current plan; the next one is a fresh baseline
_ACTUAL = re.compile(r"actual: started=(\S+) ended=(\S+)")


class PlanError(ValueError):
    """An invalid plan or log; the CLI refuses it with exit 5 before writing anything."""


@dataclass(frozen=True)
class Plan:
    rev: int
    estimate: float | None  # hours; None for an epic's dates-only plan
    start: date
    target: date
    posthoc: bool = False
    at: datetime | None = None  # comment time
    by: str = ""  # signing slug

    def statement(self) -> str:
        est = f" estimate={hours(self.estimate)}h" if self.estimate is not None else ""
        tail = " posthoc=1" if self.posthoc else ""
        return f"plan: rev={self.rev}{est} start={self.start.isoformat()} target={self.target.isoformat()}{tail}"


def hours(h: float) -> str:
    return f"{h:g}"


def actual_statement(started: datetime, ended: datetime) -> str:
    return f"actual: started={started.isoformat(timespec='minutes')} ended={ended.isoformat(timespec='minutes')}"


def tail_statement(comment_html: str) -> tuple[str, str] | None:
    """(statement text, signing slug) when the comment ends in a statement paragraph plus a signature."""
    m = _TAIL.search(comment_html or "")
    return (html.unescape(m[1]), m[2]) if m else None


def is_statement(text: str) -> bool:
    return any(r.fullmatch(text.strip()) for r in (_PLAN, _WITHDRAWN, _ACTUAL, _HOLD))


def hold_statement(origin: str, until: date | None = None) -> str:
    """Where a blocked item returns when it is released, and for a wait, its last day."""
    return f"hold: from={origin}" + (f" until={until.isoformat()}" if until else "")


def latest_hold(comments: list[dict]) -> tuple[str, date | None] | None:
    """(state the item returns to, the wait's last day or None) from the newest hold statement."""
    for c in reversed(_by_time(comments)):
        t = tail_statement(c.get("comment_html", ""))
        if t and (m := _HOLD.fullmatch(t[0])):
            return m[1], date.fromisoformat(m[2]) if m[2] else None
    return None


def withdrawal_statement(rev: int) -> str:
    return f"plan: rev={rev} withdrawn"


def _by_time(comments: list[dict]) -> list[dict]:
    return sorted(comments, key=lambda c: c["created_at"])


def plans(comments: list[dict]) -> list[Plan]:
    """The current plan's statements, oldest first: everything after the last withdrawal. A rev=0 after the first
    real one is ignored."""
    out: list[Plan] = []
    seen_rev0 = False
    for c in _by_time(comments):
        t = tail_statement(c.get("comment_html", ""))
        if t and _WITHDRAWN.fullmatch(t[0]):
            out = []
            continue
        if not t or not (m := _PLAN.fullmatch(t[0])):
            continue
        est = float(m[2]) if m[2] else None
        p = Plan(int(m[1]), est, date.fromisoformat(m[3]), date.fromisoformat(m[4]), bool(m[5]),
                 datetime.fromisoformat(c["created_at"]), t[1])  # fmt: skip
        if p.rev == 0 and not p.posthoc:
            if seen_rev0:
                continue
            seen_rev0 = True
        out.append(p)
    return out


def last_rev(comments: list[dict]) -> int | None:
    """The highest rev any plan or withdrawal statement used, withdrawn plans included; None if there was none."""
    revs = [int(m[1]) for c in comments if (t := tail_statement(c.get("comment_html", "")))
            and (m := _PLAN.fullmatch(t[0]) or _WITHDRAWN.fullmatch(t[0]))]  # fmt: skip
    return max(revs, default=None)


def baseline(ps: list[Plan]) -> Plan | None:
    """The current plan's first real (not post-hoc) statement, if it has an estimate: rev=0, or the first plan
    after a withdrawal. Later rev=0 statements are ignored."""
    first = next((p for p in ps if not p.posthoc), None)
    return first if first is not None and first.estimate is not None else None


def latest(ps: list[Plan]) -> Plan | None:
    real = [p for p in ps if not p.posthoc]
    return real[-1] if real else (ps[-1] if ps else None)


def latest_estimate(ps: list[Plan], until: datetime | None = None) -> float | None:
    for p in reversed(ps):
        if until is not None and p.at and p.at > until:
            continue
        if p.estimate is not None:
            return p.estimate
    return None


def revisions(ps: list[Plan]) -> int:
    base = baseline(ps)
    return sum(1 for p in ps if base and not p.posthoc and p.at and base.at and p.at > base.at)


def logged(comments: list[dict]) -> tuple[datetime, datetime] | None:
    """The first `actual:` statement, from a retroactive log."""
    for c in _by_time(comments):
        t = tail_statement(c.get("comment_html", ""))
        if t and (m := _ACTUAL.fullmatch(t[0])):
            return datetime.fromisoformat(m[1]), datetime.fromisoformat(m[2])
    return None


# -- validation ---------------------------------------------------------------


def parse_estimate(value: str | float | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        h = float(str(value).strip().removesuffix("h"))
    except ValueError:
        raise PlanError(f"estimate {value!r} must be hours, e.g. 3h or 2.5h") from None
    if h <= 0 or round(h * 4) != h * 4:
        raise PlanError(f"estimate {value!r} must be greater than 0 and a multiple of 0.25 h")
    return h


def parse_day(value: str | None, what: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise PlanError(f"{what} {value!r} must be a date like 2026-10-23") from None


def parse_moment(value: str, zone: tzinfo, what: str) -> datetime:
    """ISO date-time; without an offset it is read in the project's zone."""
    try:
        t = datetime.fromisoformat(value.strip().replace(" ", "T", 1))
    except ValueError:
        raise PlanError(f"{what} {value!r} must be a date and time like 2026-10-20T14:00") from None
    return t.replace(tzinfo=zone) if t.tzinfo is None else t


def check_dates(start: date, target: date) -> None:
    if start > target:
        raise PlanError(f"start {start} is after target {target}")


# -- weeks --------------------------------------------------------------------


def week_of(day: date) -> tuple[str, date, date]:
    """ISO week: (name like 2026-W43, Monday, Sunday)."""
    y, w, _ = day.isocalendar()
    monday = day - timedelta(days=day.weekday())
    return f"{y}-W{w:02d}", monday, monday + timedelta(days=6)


def week_named(name: str) -> tuple[str, date, date]:
    m = re.fullmatch(r"(\d{4})-W(\d{1,2})", name.strip())
    if not m:
        raise PlanError(f"cycle {name!r} must be an ISO week like 2026-W43")
    monday = date.fromisocalendar(int(m[1]), int(m[2]), 1)
    return week_of(monday)


def local_day(value: str, zone: tzinfo) -> date:
    """A Plane date or datetime as a day in the project's zone. Cycle dates come back as UTC datetimes."""
    if len(value) == 10:
        return date.fromisoformat(value)
    return datetime.fromisoformat(value).astimezone(zone).date()


def cycle_for_week(cycles: list[dict], monday: date, sunday: date, zone: tzinfo) -> dict | None:
    """The week's cycle, by its dates not its name; the earliest created wins when several cover it."""
    same = [c for c in cycles if c.get("start_date") and c.get("end_date")
            and local_day(c["start_date"], zone) == monday and local_day(c["end_date"], zone) == sunday]  # fmt: skip
    return min(same, key=lambda c: c["created_at"]) if same else None


# -- actuals ------------------------------------------------------------------


GRACE = timedelta(minutes=5)  # `_write` changes the state before it comments, so the closing comment lands after


@dataclass
class Actuals:
    intervals: list[tuple[datetime, datetime]] = field(default_factory=list)
    retro: bool = False
    trimmed: float = 0.0  # hours cut from agent-held intervals after the holder's last signed comment

    @property
    def start(self) -> datetime | None:
        return min((a for a, _ in self.intervals), default=None)

    @property
    def end(self) -> datetime | None:
        return max((b for _, b in self.intervals), default=None)

    @property
    def active_hours(self) -> float:
        return sum((b - a).total_seconds() for a, b in self.intervals) / 3600

    @property
    def elapsed_hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600 if self.intervals else 0.0

    @property
    def never_started(self) -> bool:
        return not self.intervals

    @property
    def parked(self) -> bool:
        """More than half of the In Progress time came after the holder went quiet."""
        return self.trimmed > 0 and self.trimmed > self.active_hours

    @property
    def suspect(self) -> bool:
        """Under a minute from start to end on an item not logged retroactively: filed after the fact."""
        return bool(self.intervals) and not self.retro and (self.end - self.start) < timedelta(minutes=1)


def _at(a: dict) -> datetime:
    return datetime.fromisoformat(a["created_at"])


def state_changes(activities: list[dict]) -> list[tuple[datetime, str | None, str | None]]:
    """(time, old state id, new state id), oldest first. States are matched by id: names change (Todo -> Ready)."""
    rows = [a for a in activities if a.get("field") == "state"]
    return sorted(((_at(a), a.get("old_identifier"), a.get("new_identifier")) for a in rows), key=lambda r: r[0])


def _label_adds(activities: list[dict], agent_labels: dict[str, str]) -> list[tuple[datetime, str]]:
    """(time, slug) for every agent: label added, by the label's id or, once the label is gone, by the name the
    activity recorded."""
    out = []
    for a in activities:
        if a.get("field") != "labels":
            continue
        slug = agent_labels.get(a.get("new_identifier") or "")
        if slug is None and str(a.get("new_value") or "").startswith("agent:"):
            slug = str(a["new_value"]).removeprefix("agent:")
        if slug:
            out.append((_at(a), slug))
    return sorted(out)


_HANDOFF = re.compile(r">(?:Dispatched to|Claimed by|Rework for) agent:([a-z0-9][a-z0-9-]*)")
_LOST = re.compile(r"^<p>agent:[a-z0-9-]+ lost ownership \(")  # written by CLIs before 0.3; never work


def actuals(
    activities: list[dict],
    comments: list[dict],
    in_progress: str,
    agent_labels: dict[str, str],
    now: datetime,
) -> Actuals:
    """Work intervals from the activity log. An In Progress interval held by an agent ends at the holder's last
    signed comment in it plus GRACE, or counts nothing if the holder never signed in it; an interval nobody held
    (human work) and the open interval of an item still In Progress count in full.

    The holder is the agent: label last added by the interval's start (matched by id, or by the name in the
    activity once the label is deleted), else the slug a dispatch, claim or rework comment names at the start. Its
    session is the one on its claim comment in the interval, else on its first signed comment there; a comment
    signed with the holder's slug from another session (someone moving a parked item for it) doesn't count."""
    out = Actuals()
    if log := logged(comments):
        out.intervals.append(log)
        out.retro = True
    adds = _label_adds(activities, agent_labels)
    signed = sorted((datetime.fromisoformat(c["created_at"]), sig, c.get("comment_html", "")) for c in comments
                    if (sig := signature(c.get("comment_html", ""))))  # fmt: skip
    opened: datetime | None = None
    for at, _old, new in state_changes(activities):
        if new == in_progress:
            opened = opened or at
            continue
        if opened is None:
            continue
        end = at
        window = [(t, sig, body) for t, sig, body in signed if opened <= t <= at + GRACE]
        holder = next((s for t, s in reversed(adds) if t <= opened + timedelta(seconds=5)), None)
        if holder is None:
            holder = next((m[1] for t, _sig, body in window if t <= opened + GRACE and (m := _HANDOFF.search(body))),
                          None)  # fmt: skip
        if holder is not None:
            mine = [(t, sig[1], body) for t, sig, body in window if sig[0] == holder and not _LOST.search(body)]
            claim = next((sess for _t, sess, body in mine if f">Claimed by agent:{holder}" in body), None)
            session = claim or (mine[0][1] if mine else None)
            last = max((t for t, sess, _b in mine if sess == session), default=None)
            end = min(at, last + GRACE) if last else opened
            out.trimmed += (at - max(end, opened)).total_seconds() / 3600
        if end > opened:
            out.intervals.append((opened, end))
        opened = None
    if opened is not None and now > opened:
        out.intervals.append((opened, now))  # still In Progress
    return out


_SIG = re.compile(r"(?:^|<p[^>]*>)agent=([a-z0-9][a-z0-9-]*) · session=([^<·]*?)(?: · [^<]*)?</p>\s*$")


def signature(comment_html: str) -> tuple[str, str] | None:
    """(slug, session) from the comment's signature paragraph, if it has one."""
    m = _SIG.search(comment_html or "")
    return (m[1], html.unescape(m[2]).strip()) if m else None


def signer(comment_html: str) -> str | None:
    """The slug in the comment's signature paragraph, if it has one."""
    sig = signature(comment_html)
    return sig[0] if sig else None


def closed_at(activities: list[dict], closed_states: set[str]) -> datetime | None:
    """When the item last entered a closed state, if it is still there."""
    changes = state_changes(activities)
    if not changes or changes[-1][2] not in closed_states:
        return None
    return changes[-1][0]


def membership(activities: list[dict], cycle_id: str) -> list[tuple[datetime, datetime | None]]:
    """Spans during which the item was in the cycle, from its `cycles` activities."""
    spans: list[tuple[datetime, datetime | None]] = []
    joined: datetime | None = None
    for a in sorted((a for a in activities if a.get("field") == "cycles"), key=_at):
        if a.get("new_identifier") == cycle_id and joined is None:
            joined = _at(a)
        elif joined is not None and a.get("old_identifier") == cycle_id:
            spans.append((joined, _at(a)))
            joined = None
    if joined is not None:
        spans.append((joined, None))
    return spans


def median(xs: list[float]) -> float | None:
    s = sorted(xs)
    if not s:
        return None
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2
