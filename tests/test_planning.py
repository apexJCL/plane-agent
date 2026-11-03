"""Plans, cycles, retroactive logs and the report, against FakePlane. Each test names the scenario it covers in
openspec/changes/add-plan-vs-actual/specs/work-planning/spec.md (or agent-coordination for dispatch and queue).

The fake's clock starts Tuesday 2026-10-06 12:00 UTC, 05:00 in the project's zone (America/Los_Angeles): week 2026-W41.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from plane_agent.client import PlaneClient, PlaneError
from plane_agent.planning import (
    PlanError,
    actuals,
    baseline,
    cycle_for_week,
    latest,
    local_day,
    logged,
    parse_estimate,
    parse_moment,
    plans,
    revisions,
    tail_statement,
    week_named,
    week_of,
)
from plane_agent.protocol import (
    EXIT_OWNERSHIP_LOST,
    EXIT_REFUSED,
    RETRO,
    ProtocolRefusal,
)
from plane_agent.report import _calibration, render, report

from .conftest import TZ, agent
from .test_protocol import comments_on, label_names, ref, started, state_name

ZONE = ZoneInfo(TZ)
T = "2026-10-09"  # Friday of W41


def refused(fn, *a, code=EXIT_REFUSED, **kw) -> str:
    with pytest.raises(ProtocolRefusal) as e:
        fn(*a, **kw)
    assert e.value.code == code
    return str(e.value)


def item_comments(fake, it):
    return [c for c in fake.comments if c["issue"] == it["id"]]


# -- statements (pure) ---------------------------------------------------------

SIG = "<p>agent=orchestrator · session=s</p>"
ST = "plan: rev=0 estimate=3h start=2026-10-06 target=2026-10-09"


def _c(body: str, minutes: int = 0) -> dict:
    return {"comment_html": body, "created_at": (datetime(2026, 10, 6, 12, tzinfo=UTC) + timedelta(minutes=minutes)).isoformat()}


def test_statement_counts_only_just_before_the_signature():
    good = _c(f"<p>Planned.</p><p>{ST}</p>{SIG}")
    assert [(p.estimate, p.by) for p in plans([good])] == [(3.0, "orchestrator")]
    assert tail_statement(good["comment_html"]) == (ST, "orchestrator")
    for bad in (
        f"<blockquote><p>{ST}</p></blockquote>{SIG}",  # quoted
        f"<p>{ST}</p><p>and then more</p>{SIG}",  # earlier in the body
        f"<p>{ST}</p>",  # unsigned
        f"<blockquote><p>{ST}</p>{SIG}</blockquote>",  # a whole quoted comment
    ):
        assert plans([_c(bad)]) == []


def test_statement_before_a_ruled_signature():
    # Comments since the signature rule carry <hr> before it; older ones don't, and both still count.
    for body in (f"<p>Planned.</p><p>{ST}</p><hr>{SIG}", f"<p>{ST}</p><hr />{SIG}"):
        assert tail_statement(body) == (ST, "orchestrator")
    assert plans([_c(f"<p>{ST}</p><hr><p>more</p><hr>{SIG}")]) == []


def test_baseline_is_the_first_real_rev0():
    def st(rev, est, posthoc=False):
        return f"<p>plan: rev={rev} estimate={est}h start=2026-10-06 target=2026-10-09{' posthoc=1' if posthoc else ''}</p>"

    ps = plans([_c(st(0, 1, posthoc=True) + SIG, 0), _c(st(0, 3) + SIG, 1), _c(st(1, 6) + SIG, 2),
                _c(st(0, 9) + SIG, 3)])  # fmt: skip
    assert baseline(ps).estimate == 3 and latest(ps).estimate == 6 and revisions(ps) == 1
    assert baseline(plans([_c(st(0, 1, posthoc=True) + SIG)])) is None  # post-hoc is never a baseline


@pytest.mark.parametrize("bad", ["0", "0h", "1.1", "-1", "x", "1.3h"])
def test_bad_estimates(bad):
    with pytest.raises(PlanError):
        parse_estimate(bad)


def test_good_estimates_moments_and_weeks():
    assert [parse_estimate(v) for v in ("3h", "2.5", "0.25h", None)] == [3, 2.5, 0.25, None]
    assert parse_moment("2026-10-20 14:00", ZONE, "x") == datetime(2026, 10, 20, 14, tzinfo=ZONE)
    assert parse_moment("2026-10-20T21:00Z", ZONE, "x") == datetime(2026, 10, 20, 14, tzinfo=ZONE)
    assert week_of(date(2026, 10, 20)) == ("2026-W43", date(2026, 10, 19), date(2026, 10, 25))
    assert week_named("2026-W43") == week_of(date(2026, 10, 25))
    assert week_of(date(2027, 1, 1))[0] == "2026-W53"
    # Cycle dates come back as UTC datetimes (checked live): W43 in Los Angeles
    assert local_day("2026-10-19T07:00:01Z", ZONE) == date(2026, 10, 19)
    assert local_day("2026-10-26T06:59:00Z", ZONE) == date(2026, 10, 25)
    cycles = [{"id": "late", "start_date": "2026-10-19T07:00:01Z", "end_date": "2026-10-26T06:59:00Z",
               "created_at": "2026-10-20T10:00:00Z"},
              {"id": "early", "start_date": "2026-10-19T07:00:01Z", "end_date": "2026-10-26T06:59:00Z",
               "created_at": "2026-10-20T09:00:00Z"},
              {"id": "other", "start_date": "2026-10-26T07:00:01Z", "end_date": "2026-11-02T07:59:00Z",
               "created_at": "2026-10-01T00:00:00Z"}]  # fmt: skip
    assert cycle_for_week(cycles, date(2026, 10, 19), date(2026, 10, 25), ZONE)["id"] == "early"


# -- actuals (pure) ------------------------------------------------------------

R, BL, IP, B, RV, D = "ready", "backlog", "in-progress", "blocked", "in-review", "done"
T0 = datetime(2026, 10, 6, 15, tzinfo=UTC)
AGENTS = {"L-api": "api", "L-docs": "docs"}


def _at(h: float) -> str:
    return (T0 + timedelta(hours=h)).isoformat()


def _st(h, old, new):
    return {"field": "state", "old_identifier": old, "new_identifier": new, "created_at": _at(h)}


def _label(h, lid="L-api"):
    return {"field": "labels", "new_identifier": lid, "created_at": _at(h)}


def _signed(h, slug="api", session="s", text="note"):
    return {"comment_html": f"<p>{text}</p><hr><p>agent={slug} · session={session} · where=repo@main</p>",
            "created_at": _at(h)}  # fmt: skip


def _actuals(activities, comments=(), now_h=500):
    return actuals(activities, list(comments), IP, AGENTS, T0 + timedelta(hours=now_h))


H = lambda h: timedelta(hours=h)
GRACE_H = 5 / 60


def test_actuals_blocked_stretch():
    acts = [_label(0), _st(0, R, IP), _st(2, IP, B), _st(26, B, IP), _st(27, IP, RV)]
    a = _actuals(acts, [_signed(2 + 1 / 3600), _signed(27 + 1 / 3600)])  # each move's comment lands just after it
    assert a.active_hours == 3 and a.elapsed_hours == 27 and a.end == T0 + timedelta(hours=27)


def test_actuals_parked_interval_ending_in_review_is_trimmed():
    acts = [_label(0), _st(0, R, IP), _st(144, IP, RV)]  # one hour of work, then six idle days
    a = _actuals(acts, [_signed(0.01, text="Claimed by agent:api."), _signed(1)])
    assert a.intervals == [(T0, T0 + H(1 + GRACE_H))] and a.parked
    assert not _actuals(acts, [_signed(143.99)]).parked


def test_actuals_holder_without_a_comment_counts_nothing_but_human_work_counts_fully():
    assert _actuals([_label(0), _st(0, R, IP), _st(5, IP, RV)]).never_started
    assert _actuals([_st(0, R, IP), _st(5, IP, RV)]).active_hours == 5  # no agent label: a human's work


def test_actuals_ignore_the_holder_slug_signed_from_another_session():
    acts = [_label(0), _st(0, R, IP), _st(144, IP, RV)]
    claim = _signed(0.01, session="worker-1", text="Claimed by agent:api.")
    moved = _signed(144 + 1 / 3600, session="orch-9", text="Ready for review.")  # the orchestrator under the slug
    a = _actuals(acts, [claim, _signed(2, session="worker-1"), moved])
    assert a.end == T0 + H(2 + GRACE_H)
    unclaimed = _actuals(acts, [_signed(2, session="worker-1"), moved])  # no claim: its first signed comment's session
    assert unclaimed.end == T0 + H(2 + GRACE_H)


def test_actuals_holder_from_a_deleted_label_or_the_handoff_comment():
    gone = {"field": "labels", "new_identifier": "L-deleted", "new_value": "agent:old-role", "created_at": _at(0)}
    acts = [gone, _st(0, R, IP), _st(10, IP, RV)]
    assert _actuals(acts, [_signed(1, "old-role")]).end == T0 + H(1 + GRACE_H)
    bare = [_st(0, R, IP), _st(10, IP, RV)]  # no label activity survives at all
    dispatched = _signed(1 / 3600, "orchestrator", text="Dispatched to agent:old-role.")
    assert _actuals(bare, [dispatched, _signed(3, "old-role")]).end == T0 + H(3 + GRACE_H)


def test_actuals_ignore_old_lost_ownership_comments():
    acts = [_label(0), _st(0, R, IP), _st(10, IP, R)]
    lost = _signed(9, text="agent:api lost ownership (state Ready); stopping without writing.")
    assert _actuals(acts, [_signed(1), lost]).end == T0 + H(1 + GRACE_H)


def test_actuals_rejected_and_redone():
    a = _actuals([_st(0, R, IP), _st(2, IP, RV), _st(3, RV, R), _st(5, R, IP), _st(6, IP, RV), _st(7, RV, D)])
    assert a.active_hours == 3 and a.end == T0 + timedelta(hours=6)  # the second move to In Review


def test_actuals_reclaim_with_and_without_comments():
    acts = [_label(0), _st(0, R, IP), _st(25, IP, R)]  # Scenario "Crashed session reclaimed"
    assert _actuals(acts, [_signed(1)]).active_hours == 1 + GRACE_H
    assert _actuals(acts, [_signed(1, "docs")]).intervals == []  # someone else's comment doesn't count
    assert _actuals(acts).never_started


def test_actuals_operator_pull_back_and_later_holder():
    acts = [_label(0), _st(0, R, IP), _st(3, IP, BL), _label(10, "L-docs"), _st(10, BL, IP), _st(12, IP, RV)]
    a = _actuals(acts, [_signed(2), _signed(11, "docs")])
    assert a.intervals == [(T0, T0 + H(2 + GRACE_H)), (T0 + H(10), T0 + H(11 + GRACE_H))]


def test_actuals_close_from_blocked_never_started_suspect_and_open():
    a = _actuals([_st(0, R, IP), _st(1, IP, B), _st(5, B, D)])
    assert a.active_hours == 1 and a.end == T0 + timedelta(hours=1)
    assert _actuals([_st(0, R, D)]).never_started
    assert _actuals([_st(0, R, IP), _st(10 / 3600, IP, D)]).suspect
    assert _actuals([_st(0, R, IP)], now_h=2).active_hours == 2  # still In Progress


def test_actuals_rejected_retro_item_adds_later_work():
    log = {"comment_html": "<p>x</p><p>actual: started=2026-10-05T14:00-07:00 ended=2026-10-05T16:30-07:00</p>"
                           "<p>agent=api · session=s</p>", "created_at": _at(0)}  # fmt: skip
    a = _actuals([_st(0, BL, RV), _st(1, RV, R), _label(2), _st(2, R, IP), _st(3, IP, RV)], [log, _signed(3)])
    assert a.retro and not a.suspect and a.active_hours == 3.5
    assert logged([log])[0] == datetime(2026, 10, 5, 14, tzinfo=ZONE)


# -- client and fake (task 2.1) ------------------------------------------------


def test_client_cycles_dates_and_completed_at(fake, orch):
    c, pid = orch.client, fake.pid
    w41 = c.create_cycle(pid, "2026-W41", "2026-10-05", "2026-10-11")
    assert (w41["start_date"], w41["end_date"]) == ("2026-10-05T07:00:01Z", "2026-10-12T06:59:00Z")
    w42 = c.create_cycle(pid, "2026-W42", "2026-10-12", "2026-10-18")
    assert [x["id"] for x in c.cycles(pid)] == [w41["id"], w42["id"]]
    it = fake.seed_item(pid, "A", "Ready")
    c.add_cycle_items(pid, w41["id"], [it["id"]])
    c.add_cycle_items(pid, w42["id"], [it["id"]])  # moves, doesn't duplicate
    assert c.cycle_items(pid, w41["id"]) == [] and [x["id"] for x in c.cycle_items(pid, w42["id"])] == [it["id"]]
    c.remove_cycle_item(pid, w42["id"], it["id"])
    moves = [(a["verb"], a["old_identifier"], a["new_identifier"]) for a in c.activities(pid, it["id"])
             if a["field"] == "cycles"]  # fmt: skip
    assert moves == [("created", None, w41["id"]), ("updated", w41["id"], w42["id"]), ("deleted", w42["id"], None)]
    ended = c.create_cycle(pid, "2026-W39", "2026-09-21", "2026-09-27")
    with pytest.raises(PlaneError) as e:
        c.add_cycle_items(pid, ended["id"], [it["id"]])
    assert e.value.status == 400 and "CYCLE_COMPLETED" in str(e.value)
    c.update_item(pid, it["id"], {"start_date": "2026-10-06", "target_date": T})
    assert {"start_date", "target_date"} <= {a["field"] for a in c.activities(pid, it["id"])}
    c.update_item(pid, it["id"], {"state": fake.state(pid, "Done")["id"]})
    assert fake.items[it["id"]]["completed_at"]
    c.update_item(pid, it["id"], {"state": fake.state(pid, "Ready")["id"]})
    assert fake.items[it["id"]]["completed_at"] is None
    with pytest.raises(PlaneError) as e:  # project settings need an Admin (task 1.3)
        c._req("PATCH", c._proj(pid, ""), json={"cycle_view": True})
    assert e.value.status == 403


# -- plan (task 3.2) -----------------------------------------------------------


def test_plan_sets_baseline_dates_and_cycle(fake, orch):
    it = fake.seed_item(fake.pid, "A", "Ready")
    plan, warning = orch.plan(ref(it), "3h", target=T)  # Scenario "Plan lands in a new week"
    assert warning is None and plan.statement() == f"plan: rev=0 estimate=3h start=2026-10-06 target={T}"
    assert (it["start_date"], it["target_date"]) == ("2026-10-06", T)
    assert tail_statement(comments_on(fake, it)[-1]) == (plan.statement(), "orchestrator")
    assert fake.members(fake.pid, "2026-W41") == [it]


def test_revision_keeps_omitted_values_and_moves_week(fake, orch):
    it = fake.seed_item(fake.pid, "A", "Ready")
    orch.plan(ref(it), "3h", target=T)
    plan, _ = orch.plan(ref(it), target="2026-10-14", reason="migration needs a backfill")
    assert plan.statement() == "plan: rev=1 estimate=3h start=2026-10-06 target=2026-10-14"
    assert "<p>Plan revision 1.</p><p>migration needs a backfill</p>" in comments_on(fake, it)[-1]
    # Scenario "Target moves to the next week"
    assert fake.members(fake.pid, "2026-W41") == [] and fake.members(fake.pid, "2026-W42") == [it]
    plan, _ = orch.plan(ref(it), "6h", reason="scope grew")  # Scenario "Scope grows mid-work"
    assert (plan.rev, plan.estimate, plan.target) == (2, 6, date(2026, 10, 14))
    it["target_date"] = "2026-10-15"  # the operator drags it on the timeline: the native date is the current plan
    plan, _ = orch.plan(ref(it), "7h", reason="more")
    assert plan.target == date(2026, 10, 15)
    ps = plans(item_comments(fake, it))
    assert [p.rev for p in ps] == [0, 1, 2, 3] and baseline(ps).estimate == 3


@pytest.mark.parametrize("kw", [
    {"estimate": "3h", "target": T, "start": "2026-10-10"},  # start after target
    {"estimate": "0", "target": T}, {"estimate": "1.1", "target": T},
    {"estimate": "3h"},  # no target
    {"target": T},  # no estimate on a non-epic
    {"estimate": "3h", "target": "2026-13-01"},
])  # fmt: skip
def test_invalid_plans_change_nothing(fake, orch, kw):
    it = fake.seed_item(fake.pid, "A", "Ready")  # Scenario "Invalid plan"
    refused(orch.plan, ref(it), **kw)
    assert comments_on(fake, it) == [] and it.get("target_date") is None and fake.cycles[fake.pid] == []


def test_plan_refusals(fake, orch):
    it = fake.seed_item(fake.pid, "A", "Ready")
    orch.plan(ref(it), "3h", target=T)
    n = len(comments_on(fake, it))
    assert "--reason" in refused(orch.plan, ref(it), "4h")  # Scenario "Revision without a reason"
    assert len(comments_on(fake, it)) == n
    done = fake.seed_item(fake.pid, "D", "Done")
    assert "closed" in refused(orch.plan, ref(done), "1h", target=T)


def test_held_item_plan_belongs_to_holder_and_orchestrator(fake, orch):
    it, w = started(fake, orch, "api")
    refused(agent(fake, "docs").plan, ref(it), "5h", reason="x", code=EXIT_OWNERSHIP_LOST)
    assert w.plan(ref(it), "5h", reason="bigger than thought")[0].rev == 1
    assert orch.plan(ref(it), "6h", reason="review found more")[0].rev == 2


def test_lone_agent_plans_then_dispatches_to_itself(fake, orch):
    it = fake.seed_item(fake.pid, "A", "Ready")
    solo = agent(fake, "solo")  # Scenario "Lone agent plans before dispatching to itself"
    solo.plan(ref(it), "1h", target=T)
    solo.dispatch(ref(it), "solo")
    solo.claim(ref(it))
    assert state_name(fake, it) == "In Progress" and "agent:solo" in label_names(fake, it)


def test_epic_plans_without_estimate_and_stays_out_of_cycles(fake, orch):
    epic = fake.seed_item(fake.pid, "Epic", "Backlog")
    fake.seed_item(fake.pid, "child", "Backlog", parent=epic["id"])
    plan, warning = orch.plan(ref(epic), target="2026-10-30")
    assert plan.statement() == "plan: rev=0 start=2026-10-06 target=2026-10-30" and warning is None
    assert epic["target_date"] == "2026-10-30" and fake.cycle_of == {}


def test_target_in_a_finished_week_warns(fake, orch):
    it = fake.seed_item(fake.pid, "A", "Ready")  # Scenario "Target in a finished week"
    _, warning = orch.plan(ref(it), "2h", start="2026-09-28", target="2026-09-30")
    assert "has ended" in warning and it["target_date"] == "2026-09-30" and fake.cycles[fake.pid] == []
    orch.plan(ref(it), target=T, reason="replanned")
    orch.client.create_cycle(fake.pid, "2026-W40", "2026-09-28", "2026-10-04")
    _, warning = orch.plan(ref(it), target="2026-10-02", reason="it was last week after all")
    assert "Plane refused" in warning and it["target_date"] == "2026-10-02"
    assert fake.members(fake.pid, "2026-W41") == [it]  # left where it was


def test_duplicate_week_cycles_earliest_wins(fake, orch):
    first = orch.client.create_cycle(fake.pid, "2026-W42", "2026-10-12", "2026-10-18")
    orch.client.create_cycle(fake.pid, "2026-W42", "2026-10-12", "2026-10-18")  # a racing agent's duplicate
    it = fake.seed_item(fake.pid, "A", "Ready")
    orch.plan(ref(it), "1h", target="2026-10-14")
    assert fake.cycle_of[it["id"]] == first["id"] and len(fake.cycles[fake.pid]) == 2


def test_default_start_and_week_use_the_project_zone(fake, orch):
    fake.clock = datetime(2026, 10, 12, 3, tzinfo=UTC)  # Monday in UTC, still Sunday evening in Los Angeles
    it = fake.seed_item(fake.pid, "A", "Ready")  # Scenario "CLI on a machine in another zone"
    plan, warning = orch.plan(ref(it), "1h", target="2026-10-11")
    assert plan.start == date(2026, 10, 11) and warning is None
    assert fake.members(fake.pid, "2026-W41") == [it]


# -- create, dispatch and queue (task 3.3) -------------------------------------


def test_create_with_plan(fake, orch):
    item = orch.create("HOME", "t", "feature", estimate="3h", target=T)  # Scenario "Planning at creation"
    stored = fake.items[item["id"]]
    assert (stored["start_date"], stored["target_date"]) == ("2026-10-06", T) and item["warning"] is None
    assert tail_statement(comments_on(fake, stored)[-1])[0] == f"plan: rev=0 estimate=3h start=2026-10-06 target={T}"
    assert fake.members(fake.pid, "2026-W41") == [stored]


def test_create_plan_refusals_and_dates_only(fake, orch):
    n = len(fake.items)
    assert "--target" in refused(orch.create, "HOME", "t", "feature", estimate="3h")
    refused(orch.create, "HOME", "t", "feature", estimate="3h", start="2026-10-10", target=T)
    refused(orch.create, "HOME", "t", "feature", estimate="0.1", target=T)
    assert len(fake.items) == n
    item = orch.create("HOME", "dated", "chore", target=T)  # dates without a plan
    stored = fake.items[item["id"]]
    assert stored["target_date"] == T and comments_on(fake, stored) == [] and fake.cycle_of == {}


def test_create_parent_takes_the_parent_out_of_its_cycle(fake, orch):
    parent = orch.create("HOME", "grows", "feature", estimate="2h", target=T)
    assert parent["id"] in fake.cycle_of
    orch.create("HOME", "part", "chore", parent=ref(parent))
    assert parent["id"] not in fake.cycle_of  # epics are never in cycles


def test_dispatch_refuses_unplanned_and_queue_lists_it_apart(fake, orch):
    loose = fake.seed_item(fake.pid, "loose", "Ready")
    planned = fake.seed_item(fake.pid, "planned", "Ready")
    orch.plan(ref(planned), "1h", target=T)
    ready, unplanned, _log = orch.queue("HOME", sweep=False)  # Scenario "Unplanned Ready item"
    assert [i["name"] for i in ready] == ["planned"] and [i["name"] for i in unplanned] == ["loose"]
    assert "has no plan" in refused(orch.dispatch, ref(loose), "api")  # Scenario "Unplanned item"
    assert state_name(fake, loose) == "Ready" and loose["assignees"] == []


# -- bootstrap (task 4.1) ------------------------------------------------------


def test_bootstrap_checks_zone_and_cycles(fake):
    pid = fake.add_project("UTCP", timezone="UTC", cycle_view=False)
    a = agent(fake, "orchestrator")
    msg = refused(a.bootstrap_project, "UTCP", TZ)
    assert "time zone is UTC" in msg and "cycles are off" in msg
    assert "project admin" in msg and f"Timezone {TZ}" in msg and "Cycles on" in msg
    assert RETRO in {lb["name"] for lb in fake.labels[pid]}  # the rest was still done
    fake.projects[pid].update(timezone=TZ, cycle_view=True)  # what the project admin does in the UI
    assert agent(fake, "orchestrator").bootstrap_project("UTCP", TZ) == []
    assert "PLANE_TZ" in refused(a.bootstrap_project, "HOME", "Mars/Olympus_Mons")


def _cli(fake, monkeypatch):
    from plane_agent import cli

    monkeypatch.setattr(cli.conf, "resolve_token", lambda _c, _f: "tok-claude")
    monkeypatch.setattr(cli, "PlaneClient", lambda *a, **k: PlaneClient(*a, **k, transport=fake.transport()))
    real = cli.Agent
    monkeypatch.setattr(cli, "Agent", lambda *a, **k: real(*a, **k, now=lambda: fake.clock))
    return lambda *argv: cli.main(["--agent", "orchestrator", "--session", "t", "--where", "x", *argv])


def test_cli_bootstrap_needs_plane_tz(fake, monkeypatch, capsys):
    run = _cli(fake, monkeypatch)
    monkeypatch.delenv("PLANE_TZ", raising=False)
    assert run("bootstrap-project", "HOME") == 2 and "PLANE_TZ" in capsys.readouterr().err


# -- log (task 4.2) ------------------------------------------------------------


def test_log_files_finished_work(fake, orch):
    item = agent(fake, "api").log("HOME", "fixed it", "bug", "2026-10-05T14:00", "2026-10-05T16:30",
                                  "Patched **x**.")  # fmt: skip  # Scenario "Filing finished work"
    stored = fake.items[item["id"]]
    assert state_name(fake, stored) == "Done" and item["warning"] is None
    assert label_names(fake, stored) == {"kind:bug", RETRO, "agent:api"}
    assert stored["assignees"] == [fake.uid("claude")]
    assert (stored["start_date"], stored["target_date"]) == ("2026-10-05", "2026-10-05")
    assert fake.members(fake.pid, "2026-W41") == [stored]
    cs = item_comments(fake, stored)
    assert logged(cs) == (datetime(2026, 10, 5, 14, tzinfo=ZONE), datetime(2026, 10, 5, 16, 30, tzinfo=ZONE))
    assert "<strong>x</strong>" in cs[0]["comment_html"] and "(2.5 h)" in cs[0]["comment_html"]
    assert plans(cs) == []


def test_retro_deploy_rejected_needs_a_real_plan(fake, orch):
    w = agent(fake, "deployer")
    item = w.log("HOME", "rolled out", "deploy", "2026-10-05T14:00", "2026-10-05T16:30", "done", estimate="2h")
    stored = fake.items[item["id"]]
    assert state_name(fake, stored) == "In Review" and item["state_name"] == "In Review"
    ps = plans(item_comments(fake, stored))
    assert [p.posthoc for p in ps] == [True] and baseline(ps) is None
    orch.reject(ref(stored), "health check fails")  # Scenario "Retro deploy item rejected"
    assert "has no plan" in refused(orch.dispatch, ref(stored), "deployer")
    orch.plan(ref(stored), "1h", target=T)
    orch.dispatch(ref(stored), "deployer")
    w.claim(ref(stored))
    fake.tick(hours=1)
    w.review(ref(stored), "fixed the health check")
    proj = orch.project("HOME")
    acts = orch.client.activities(fake.pid, stored["id"])
    a = actuals(acts, item_comments(fake, stored), proj.state_id("In Progress"),
                {lb["id"]: n.removeprefix("agent:") for n, lb in proj.labels.items() if n.startswith("agent:")},
                fake.clock)  # fmt: skip
    assert a.retro and round(a.active_hours, 1) == 3.5


@pytest.mark.parametrize("started_at,ended_at", [
    ("2026-10-05T16:00", "2026-10-05T14:00"),  # end before start
    ("2026-10-06T04:00", "2026-10-06T06:00"),  # ends in the future (it is 05:00)
    ("yesterday", "2026-10-05T14:00"),
])  # fmt: skip
def test_log_refuses_impossible_times(fake, orch, started_at, ended_at):
    n = len(fake.items)  # Scenario "Impossible times"
    refused(agent(fake, "api").log, "HOME", "t", "chore", started_at, ended_at, "s")
    assert len(fake.items) == n


def test_log_in_a_past_week_warns(fake, orch):
    item = agent(fake, "api").log("HOME", "old", "chore", "2026-09-22T10:00", "2026-09-22T11:00", "x")
    assert "has ended" in item["warning"] and state_name(fake, fake.items[item["id"]]) == "Done"
    assert fake.cycle_of == {}


# -- report (tasks 5.2, 5.3) ---------------------------------------------------


def _week(fake, orch):
    """W41: A (2 h) and E (1 h) planned Monday; A done Monday in 3 h; B (1 h) joins Wednesday; C logged
    Wednesday; D closed with no plan and no work; E cancelled Thursday; the operator drags A's target."""
    pid = fake.pid
    fake.clock = datetime(2026, 10, 5, 15, tzinfo=UTC)  # Monday 08:00 local
    a, e = fake.seed_item(pid, "A", "Ready"), fake.seed_item(pid, "E", "Ready")
    orch.plan(ref(a), "2h", target="2026-10-08")
    orch.plan(ref(e), "1h", target=T)
    orch.dispatch(ref(a), "api")
    w = agent(fake, "api")
    w.claim(ref(a))
    fake.tick(hours=3)
    w.review(ref(a), "ready")
    orch.accept(ref(a), "ok")
    fake.clock = datetime(2026, 10, 7, 15, tzinfo=UTC)  # Wednesday 08:00
    b = fake.seed_item(pid, "B", "Ready")
    orch.plan(ref(b), "1h", target=T)
    c = agent(fake, "docs").log("HOME", "C", "chore", "2026-10-07T06:00", "2026-10-07T07:30", "docs", estimate="1h")
    d = fake.seed_item(pid, "D", "Done", completed_at=fake.clock.isoformat())
    fake.clock = datetime(2026, 10, 8, 15, tzinfo=UTC)  # Thursday 08:00
    orch.cancel(ref(e), "not needed")
    a["target_date"] = T
    fake.clock = datetime(2026, 10, 8, 19, tzinfo=UTC)  # Thursday 12:00, when the report runs
    return a, b, fake.items[c["id"]], d, e


def test_report_rows_totals_and_burndown(fake, orch):
    a, _b, c, d, e = _week(fake, orch)
    rep = report(orch, "HOME", cycle="2026-W41", burndown=True)  # Scenario "Reconciling a finished week"
    rows = {r["ref"]: r for r in rep["rows"]}
    assert set(rows) == {ref(a), ref(c), ref(d)}  # B is open, E cancelled
    ra = rows[ref(a)]
    assert (ra["baseline_h"], ra["latest_h"], ra["active_h"], ra["x_baseline"]) == (2, 2, 3, 1.5)
    assert (ra["target"], ra["actual_end"], ra["slip_days"], ra["revisions"]) == (T, "2026-10-05", -4, 0)
    assert ra["flags"] == ["dates edited"]  # Scenario "Operator moves the dates"
    assert rows[ref(c)]["flags"] == ["retro", "post-hoc estimate", "no plan"] and rows[ref(c)]["active_h"] == 1.5
    assert rows[ref(d)]["flags"] == ["no plan", "never started"]  # Scenario "Old items without plans"
    assert rep["cancelled"] == [{"ref": ref(e), "name": "E", "active_h": 0}]
    assert rep["totals"] == {"items": 1, "baseline_h": 2, "active_h": 3, "median_x_baseline": 1.5,
                             "on_time_share": 1, "excluded": {"no plan": 2}}  # fmt: skip
    bd = rep["burndown"]  # Scenario "Burndown with scope change": B joins Wednesday
    assert bd["scope_h"] == 3
    assert [p["remaining_h"] for p in bd["days"]] == [1, 1, 2, 1, None, None, None]
    assert [p["ideal_h"] for p in bd["days"]] == [3, 2.5, 2, 1.5, 1, 0.5, 0]
    json.dumps(rep)
    text = "\n".join(render(rep))
    assert "HOME 2026-W41 (2026-10-05..2026-10-11, America/Los_Angeles)" in text
    assert "excluded: 2 no plan" in text and f"cancelled: {ref(e)} (0 h) E" in text
    assert "2026-10-07          2      2" in text


def test_report_shows_close_date_review_hours_and_calibration(fake, orch):
    a, *_ = _week(fake, orch)
    rep = report(orch, "HOME", cycle="2026-W41")
    ra = next(r for r in rep["rows"] if r["ref"] == ref(a))
    assert ra["closed"] == "2026-10-05" and ra["review_h"] == 0
    assert rep["calibration"] == {"rows": 1, "median_x": 1.5, "under_half": 0, "over_double": 0, "rescale": False}
    assert "calibration: median actual/baseline 1.5 over 1 rows" in "\n".join(render(rep))
    it = fake.seed_item(fake.pid, "slow review", "Ready")
    orch.plan(ref(it), "4h", target="2026-10-09")
    orch.dispatch(ref(it), "api")
    w = agent(fake, "api")
    w.claim(ref(it))
    fake.tick(minutes=30)
    w.review(ref(it), "ready")
    fake.tick(hours=30)
    orch.accept(ref(it))
    rep = report(orch, "HOME", cycle="2026-W41")
    row = next(r for r in rep["rows"] if r["ref"] == ref(it))
    assert row["closed"] == "2026-10-09" and row["actual_end"] == "2026-10-08" and round(row["review_h"]) == 30
    assert rep["calibration"]["median_x"] == 0.81 and not rep["calibration"]["rescale"]
    off = [{"x_baseline": x, "flags": []} for x in (0.1, 0.2, 0.3)]
    assert _calibration(off)["rescale"] and "rescale" not in str(render({**rep, "calibration": None}))
    assert "estimate the agent's wall-clock time" in "\n".join(render({**rep, "calibration": _calibration(off)}))


def test_report_skips_items_closed_after_the_span(fake, orch):
    fake.seed_item(fake.pid, "in W41", "Done", completed_at=fake.clock.isoformat())
    for n in range(5):
        fake.seed_item(fake.pid, f"later-{n}", "Done", completed_at="2026-10-20T12:00:00+00:00")
    a = agent(fake, "api")
    rep = report(a, "HOME", cycle="2026-W41")
    assert [r["name"] for r in rep["rows"]] == ["in W41"]
    assert rep["calls"] == 3 + 1 + 2  # loads, the list, one item's activities and comments: none of the later five


def test_report_max_calls_stops_with_partial_output(fake, orch):
    for n in range(10):
        fake.seed_item(fake.pid, f"done-{n}", "Done", completed_at=fake.clock.isoformat())
    rep = report(agent(fake, "api"), "HOME", cycle="2026-W41", max_calls=10, burndown=False)
    assert len(rep["rows"]) == 4 and "max-calls 10" in rep["partial"]  # the list, then 2 calls an item
    assert "PARTIAL: stopped at --max-calls 10" in "\n".join(render(rep))
    assert "partial" not in report(agent(fake, "api"), "HOME", cycle="2026-W41")


def test_report_date_span_and_refusals(fake, orch):
    _, _, c, d, _ = _week(fake, orch)
    rep = report(orch, "HOME", since="2026-10-07", until="2026-10-08")
    assert {r["ref"] for r in rep["rows"]} == {ref(c), ref(d)} and rep["cycle"] is None
    refused(report, orch, "HOME", since="2026-10-07", burndown=True)
    refused(report, orch, "HOME", cycle="W41")
    refused(report, orch, "HOME", until="2026-10-08")
    assert "no cycle" in refused(report, orch, "HOME", cycle="2026-W45", burndown=True)
    assert report(orch, "HOME")["cycle"] == "2026-W41"  # default: this week


def test_report_paces_long_reads(fake, orch):
    for n in range(80):
        fake.seed_item(fake.pid, f"done-{n}", "Done", completed_at=fake.clock.isoformat())
    slept, now = [], [0.0]

    def sleep(s):
        slept.append(s)
        now[0] += s

    a = agent(fake, "api", sleep=sleep, clock=lambda: now[0], rate_limit=300)
    rep = report(a, "HOME", cycle="2026-W41")
    assert len(rep["rows"]) == 80
    loads = 3  # projects, states, labels: before pacing starts
    assert rep["calls"] == loads + 1 + 2 * 80  # the item list, then activities and comments per closed item
    assert len(slept) == rep["calls"] - loads - 120 and all(abs(s - 60 / 250) < 1e-9 for s in slept)


def test_cli_plan_log_report_queue(fake, orch, monkeypatch, capsys):
    run = _cli(fake, monkeypatch)
    it = fake.seed_item(fake.pid, "A", "Ready")
    loose = fake.seed_item(fake.pid, "loose", "Ready")
    assert run("plan", ref(it), "--estimate", "2h", "--target", T) == 0
    assert "plan: rev=0 estimate=2h" in capsys.readouterr().out
    assert run("plan", ref(it), "--estimate", "3h") == 5
    assert run("log", "HOME", "t", "--kind", "chore", "--started", "2026-09-22T10:00", "--ended",
               "2026-09-22T11:00", "--summary", "s") == 0  # fmt: skip
    assert "warning:" in capsys.readouterr().err
    assert run("create", "HOME", "n", "--kind", "chore", "--estimate", "1h") == 5
    assert run("queue", "HOME") == 0
    out = capsys.readouterr().out
    assert out.index(ref(it)) < out.index("unplanned") < out.index(ref(loose))
    assert run("--json", "report", "HOME", "--cycle", "2026-W41", "--burndown") == 0
    rep = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert rep["cycle"] == "2026-W41" and rep["burndown"]["scope_h"] == 0  # planned Tuesday, after the scope line
    assert rep["burndown"]["days"][1]["remaining_h"] == 2
    assert run("report", "HOME") == 0 and "totals:" in capsys.readouterr().out


def test_cli_report_caps_calls_on_a_60_a_minute_budget(fake, orch, monkeypatch, capsys):
    from plane_agent import cli

    run = _cli(fake, monkeypatch)
    monkeypatch.setattr(cli, "PlaneClient", lambda *a, **k: PlaneClient(*a, **k, transport=fake.transport(),
                                                                         sleep=lambda s: None))  # fmt: skip
    for n in range(60):
        fake.seed_item(fake.pid, f"done-{n}", "Done", completed_at=fake.clock.isoformat())
    monkeypatch.setenv("PLANE_RATE_LIMIT", "60")
    assert run("--json", "report", "HOME", "--cycle", "2026-W41") == 0
    rep = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert "max-calls 100" in rep["partial"] and len(rep["rows"]) < 50  # the report's own reads, after the loads
    assert run("--json", "report", "HOME", "--cycle", "2026-W41", "--max-calls", "0") == 0
    rep = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert "partial" not in rep and len(rep["rows"]) == 60
