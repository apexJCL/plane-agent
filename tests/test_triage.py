"""Waits, holds and triage moves (block --wait, ready on Blocked, backlog, prioritize, plan --clear), and the
operator's attention list, against FakePlane."""

from __future__ import annotations

import pytest

from plane_agent.planning import baseline, plans
from plane_agent.protocol import EXIT_OWNERSHIP_LOST, EXIT_REFUSED, NEEDS_HUMAN, WAITING, ProtocolRefusal
from plane_agent.report import report

from .conftest import agent
from .test_planning import _cli
from .test_protocol import comments_on, label_names, planned, ref, started, state_name


def _refused(call, *args, code=EXIT_REFUSED, match=""):
    with pytest.raises(ProtocolRefusal) as e:
        call(*args)
    assert e.value.code == code and match in str(e.value), str(e.value)
    return str(e.value)


def _sweep(orch):
    return orch.queue("HOME", sweep=True)[2]


def _deploy(fake, orch, name="ship it"):
    it, w = started(fake, orch, "deployer", name=name, labels=[orch.project("HOME").label_id("kind:deploy")])
    w.review(ref(it), "ran `docker compose up -d`; health check 200")
    return it, w


# -- waits -------------------------------------------------------------------


def test_wait_is_a_known_state_until_its_last_day(fake, orch):
    it, w = started(fake, orch, "api")
    assert w.block_wait(ref(it), "the vendor's sandbox key").isoformat() == "2026-10-13"  # a week by default
    assert state_name(fake, it) == "Blocked" and {WAITING, "agent:api"} <= label_names(fake, it)
    assert NEEDS_HUMAN not in label_names(fake, it)
    assert "hold: from=In Progress until=2026-10-13" in comments_on(fake, it)[-1]
    fake.tick(days=7)  # 2026-10-13: still the wait's last day
    assert _sweep(orch) == []  # a known wait, not an anomaly
    fake.tick(days=1)
    assert _sweep(orch) == [f"{ref(it)}: wait expired -> Ready"]
    assert state_name(fake, it) == "Ready" and it["assignees"] == [] and WAITING not in label_names(fake, it)
    assert "the wait until 2026-10-13 has expired" in comments_on(fake, it)[-1]


def test_wait_creates_the_label_in_an_older_project(fake, orch):
    fake.labels[fake.pid] = [lb for lb in fake.labels[fake.pid] if lb["name"] != WAITING]
    it, _ = started(fake, orch, "api")
    w = agent(fake, "api")  # loads the project without the label
    w.block_wait(ref(it), "CI capacity", until="2026-10-07")
    assert WAITING in label_names(fake, it)


def test_wait_until_must_be_today_or_later(fake, orch):
    it, w = started(fake, orch, "api")
    _refused(w.block_wait, ref(it), "x", "2026-10-05", match="has passed")
    _refused(w.block_wait, ref(it), "x", "next week")
    _refused(w.block_wait, ref(it), " ", match="what the item waits for")
    assert state_name(fake, it) == "In Progress"


def test_wait_with_an_open_blocker_stays_until_both_clear(fake, orch):
    it, w = started(fake, orch, "api")
    b = fake.seed_item(fake.pid, "B", "Ready")
    w.block_on(ref(it), [ref(b)], "")
    orch.block_wait(ref(it), "and the footage", until="2026-10-06")
    fake.tick(days=2)
    assert _sweep(orch) == [] and state_name(fake, it) == "Blocked"
    b["state"] = fake.state(fake.pid, "Done")["id"]
    assert _sweep(orch) == [f"{ref(it)}: wait expired -> Ready"]


def test_orchestrator_holds_an_in_review_item_and_it_returns_to_review(fake, orch):
    it, w = started(fake, orch, "editor")
    w.review(ref(it), "cut is done")
    orch.block_wait(ref(it), "footage from the venue", until="2026-10-08")
    assert state_name(fake, it) == "Blocked" and "agent:editor" in label_names(fake, it)
    assert it["assignees"] == [orch.me["id"]]
    fake.tick(days=3)
    assert _sweep(orch) == [f"{ref(it)}: wait expired -> In Review"]
    assert state_name(fake, it) == "In Review" and it["assignees"] == [orch.me["id"]]
    b = fake.seed_item(fake.pid, "B", "Ready")
    orch.block_on(ref(it), [ref(b)], "needs the B colour pass")
    assert state_name(fake, it) == "Blocked"
    b["state"] = fake.state(fake.pid, "Done")["id"]
    assert _sweep(orch) == [f"{ref(it)}: unblocked -> In Review"]


def test_in_review_deploy_items_are_not_held(fake, orch):
    it, _ = _deploy(fake, orch)
    _refused(orch.block_wait, ref(it), "obsolete", match="waits on the operator")
    _refused(orch.block_on, ref(it), [ref(fake.seed_item(fake.pid, "B", "Ready"))], "")
    assert state_name(fake, it) == "In Review"


def test_worker_blocks_only_its_own_in_progress_item(fake, orch):
    it, w = started(fake, orch, "api")
    w.review(ref(it), "")
    _refused(w.block_wait, ref(it), "x", code=EXIT_OWNERSHIP_LOST)
    other, _ = started(fake, orch, "web", name="other")
    _refused(w.block_wait, ref(other), "x", code=EXIT_OWNERSHIP_LOST)
    _refused(orch.block_wait, ref(other), "x", code=EXIT_OWNERSHIP_LOST)  # only its holder blocks an In Progress item
    done = fake.seed_item(fake.pid, "closed", "Done")
    _refused(orch.block_wait, ref(done), "x", match="the orchestrator blocks")


def test_orchestrator_holds_a_ready_item(fake, orch):
    it = fake.seed_item(fake.pid, "later", "Ready")
    orch.block_wait(ref(it), "after the freeze", until="2026-10-07")
    assert state_name(fake, it) == "Blocked"
    fake.tick(days=2)
    assert _sweep(orch) == [f"{ref(it)}: wait expired -> Ready"]


def test_needs_human_item_that_waits_on_work_keeps_the_label(fake, orch):
    it, w = started(fake, orch, "api")
    w.block_human(ref(it), "Ship before or after HOME-9?")
    b = fake.seed_item(fake.pid, "HOME-9 stand-in", "Ready")
    orch.block_on(ref(it), [ref(b)], "the operator's answer depends on it")
    assert NEEDS_HUMAN in label_names(fake, it) and {"issue": it["id"], "related": b["id"], "type": "blocked_by"} in fake.relations
    _refused(orch.ready, ref(it), match="agents never do")
    b["state"] = fake.state(fake.pid, "Done")["id"]
    assert _sweep(orch) == [] and state_name(fake, it) == "Blocked"  # the question still holds it
    nh = orch.project("HOME").label_id(NEEDS_HUMAN)
    it["labels"].remove(nh)  # the operator, in the UI
    assert _sweep(orch) == [f"{ref(it)}: unblocked -> Ready"]


def test_relayed_answer_leaves_a_wait_or_hold_to_the_sweep(fake, orch):
    it, w = started(fake, orch, "api")
    w.block_human(ref(it), "Which region?")
    orch.block_wait(ref(it), "the region's quota", until="2026-10-08")
    assert w.answer(ref(it), "us-west") == "Blocked"  # the wait still holds it
    assert NEEDS_HUMAN not in label_names(fake, it) and WAITING in label_names(fake, it)
    fake.tick(days=3)
    assert _sweep(orch) == [f"{ref(it)}: wait expired -> Ready"]
    r, w2 = started(fake, orch, "editor", name="review")
    w2.review(ref(r), "")
    b = fake.seed_item(fake.pid, "B", "Ready")
    orch.block_on(ref(r), [ref(b)], "")  # held from In Review
    r["labels"].append(orch.project("HOME").label_id(NEEDS_HUMAN))
    b["state"] = fake.state(fake.pid, "Done")["id"]
    assert w2.answer(ref(r), "go ahead") == "In Review" and r["assignees"]


def test_blocked_without_relation_or_wait_is_still_reported(fake, orch):
    it = fake.seed_item(fake.pid, "odd", "Blocked")
    assert _sweep(orch) == [f"{ref(it)}: Blocked with no blocker relation, no wait and no {NEEDS_HUMAN}; left alone"]


def test_ready_ends_a_hold_early(fake, orch):
    it, w = started(fake, orch, "api")
    w.block_wait(ref(it), "the key")
    assert orch.ready(ref(it)) == "Ready" and WAITING not in label_names(fake, it) and it["assignees"] == []
    r, w2 = started(fake, orch, "editor", name="review")
    w2.review(ref(r), "")
    orch.block_wait(ref(r), "footage")
    assert orch.ready(ref(r), "footage arrived early") == "In Review"
    assert "Released from Blocked; now In Review." in comments_on(fake, r)[-1]
    b = fake.seed_item(fake.pid, "B", "Ready")
    orch.block_on(ref(r), [ref(b)], "")
    _refused(orch.ready, ref(r), match="open blockers")


# -- triage verbs ------------------------------------------------------------


def test_backlog_returns_an_unheld_ready_item(fake, orch):
    it = fake.seed_item(fake.pid, "not yet", "Ready")
    n = len(fake.comments)
    orch.backlog(ref(it), "depends on the Q1 hardware")
    assert state_name(fake, it) == "Backlog" and len(fake.comments) == n + 1
    assert "Returned to Backlog." in comments_on(fake, it)[-1] and "agent=orchestrator" in comments_on(fake, it)[-1]
    held, _ = started(fake, orch, "api", name="held")
    _refused(orch.backlog, ref(held), "x", match="nobody holds")
    _refused(orch.backlog, ref(fake.seed_item(fake.pid, "r", "Ready")), " ", match="--reason")
    _refused(agent(fake, "api").backlog, ref(it), "x", match="orchestrator move")


def test_prioritize(fake, orch):
    it = fake.seed_item(fake.pid, "a", "Backlog")
    orch.prioritize(ref(it), "high")
    assert it["priority"] == "high" and comments_on(fake, it) == []  # the activity is the record
    held, w = started(fake, orch, "api", name="held")
    w.prioritize(ref(held), "urgent", "blocks the release")
    assert held["priority"] == "urgent" and "Priority urgent." in comments_on(fake, held)[-1]
    _refused(w.prioritize, ref(it), "low", code=EXIT_OWNERSHIP_LOST)
    _refused(orch.prioritize, ref(it), "asap", match="priority must be one of")
    _refused(orch.prioritize, ref(fake.seed_item(fake.pid, "c", "Done")), "low")


def test_queue_orders_by_priority_then_target_then_age(fake, orch):
    late = fake.seed_item(fake.pid, "late", "Ready")
    fake.tick(minutes=1)
    soon = fake.seed_item(fake.pid, "soon", "Ready")
    fake.tick(minutes=1)
    hot = fake.seed_item(fake.pid, "hot", "Ready", priority="high")
    orch.plan(ref(late), "1h", target="2026-10-16")
    orch.plan(ref(soon), "1h", target="2026-10-09")
    orch.plan(ref(hot), "1h", target="2026-10-30")
    assert [i["name"] for i in orch.queue("HOME", sweep=False)[0]] == ["hot", "soon", "late"]


# -- plan withdrawal ---------------------------------------------------------


def test_plan_clear_withdraws_and_the_next_plan_is_a_fresh_baseline(fake, orch):
    it = fake.seed_item(fake.pid, "maybe", "Backlog")
    orch.plan(ref(it), "4h", target="2026-10-09")
    assert it["id"] in fake.cycle_of
    orch.withdraw_plan(ref(it), "no date we can stand behind")
    assert it["start_date"] is None and it["target_date"] is None and it["id"] not in fake.cycle_of
    assert "plan: rev=0 withdrawn" in comments_on(fake, it)[-1] and "Plan withdrawn." in comments_on(fake, it)[-1]
    cs = orch.client.comments(fake.pid, it["id"])
    assert plans(cs) == [] and baseline(plans(cs)) is None
    orch.ready(ref(it))
    _refused(orch.dispatch, ref(it), "api", match="has no plan")
    plan, _ = orch.plan(ref(it), "6h", target="2026-10-16")
    assert plan.rev == 1 and "plan: rev=1 estimate=6h" in comments_on(fake, it)[-1]
    assert "Planned again after a withdrawn plan" in comments_on(fake, it)[-1]
    assert baseline(plans(orch.client.comments(fake.pid, it["id"]))).estimate == 6
    assert "plan: rev=0 estimate=4h" in comments_on(fake, it)[0]  # history stays
    orch.dispatch(ref(it), "api")


def test_plan_clear_refusals(fake, orch):
    ready = fake.seed_item(fake.pid, "r", "Ready")
    planned(orch, ready)
    _refused(orch.withdraw_plan, ref(ready), "x", match="only a Backlog item")
    bare = fake.seed_item(fake.pid, "b", "Backlog")
    _refused(orch.withdraw_plan, ref(bare), "x", match="no plan to withdraw")
    _refused(orch.withdraw_plan, ref(bare), "", match="--reason")
    _refused(agent(fake, "api").withdraw_plan, ref(bare), "x", match="orchestrator move")
    orch.backlog(ref(ready), "later")
    orch.withdraw_plan(ref(ready), "later")
    _refused(orch.withdraw_plan, ref(ready), "again", match="no plan to withdraw")


def test_report_does_not_count_a_withdrawn_plan(fake, orch):
    it = fake.seed_item(fake.pid, "dropped plan", "Backlog")
    orch.plan(ref(it), "4h", target="2026-10-09")
    orch.withdraw_plan(ref(it), "re-scoped")
    it["state"] = fake.state(fake.pid, "Done")["id"]
    it["completed_at"] = fake.clock.isoformat()
    row = next(r for r in report(orch, "HOME", cycle="2026-W41")["rows"] if r["ref"] == ref(it))
    assert row["baseline_h"] is None and "no plan" in row["flags"]


# -- what waits on the operator ----------------------------------------------


def test_attention_and_expired_waits(fake, orch):
    dep, _ = _deploy(fake, orch)
    q, w = started(fake, orch, "api", name="question")
    w.block_human(ref(q), "Which port?")
    old, w2 = started(fake, orch, "web", name="slow review")
    w2.review(ref(old), "")
    late = fake.seed_item(fake.pid, "late", "Ready")
    orch.plan(ref(late), "1h", target="2026-10-07")
    wt, w3 = started(fake, orch, "ops", name="waiting")
    w3.block_wait(ref(wt), "the ISP", until="2026-10-08")
    fake.tick(days=4)
    att = orch.attention("HOME")
    names = {k: [i["name"] for i in v] for k, v in att.items()}
    assert names["deploy_review"] == ["ship it"] and names["needs_human"] == ["question"]
    assert names["review_old"] == ["slow review"] and "late" in names["overdue"]
    assert [(i["name"], str(d)) for i, d in orch.expired_waits("HOME")] == [("waiting", "2026-10-08")]
    assert ref(dep)


def test_cli_triage_verbs(fake, orch, monkeypatch, capsys):
    run = _cli(fake, monkeypatch)
    it = fake.seed_item(fake.pid, "a", "Ready")
    assert run("block", ref(it), "--on", ref(it), "--until", "2026-10-09") == 2
    assert run("block", ref(it), "--wait", "the key", "--until", "2026-10-09") == 0
    assert "waiting_until=2026-10-09" in capsys.readouterr().out
    assert run("ready", ref(it)) == 0 and state_name(fake, it) == "Ready"
    assert run("prioritize", ref(it), "high") == 0 and it["priority"] == "high"
    assert run("backlog", ref(it), "--reason", "later") == 0 and state_name(fake, it) == "Backlog"
    assert run("plan", ref(it), "--estimate", "2h", "--target", "2026-10-09") == 0
    assert run("plan", ref(it), "--clear", "--estimate", "2h", "--reason", "x") == 2
    assert run("plan", ref(it), "--clear", "--reason", "re-scoped") == 0 and it["target_date"] is None
    capsys.readouterr()
    _deploy(fake, orch)
    assert run("queue", "HOME") == 0
    assert "deploy items In Review, waiting on the operator: HOME-" in capsys.readouterr().err
    assert run("stale", "HOME") == 0
    assert "waiting on the operator" in capsys.readouterr().err


def test_cli_worker_may_not_withdraw_or_backlog(fake, orch, monkeypatch, capsys):
    from plane_agent import cli

    _cli(fake, monkeypatch)
    it = fake.seed_item(fake.pid, "a", "Ready")
    argv = ["--agent", "api", "--session", "t", "--where", "x"]
    assert cli.main([*argv, "backlog", ref(it), "--reason", "x"]) == 5
    assert cli.main([*argv, "plan", ref(it), "--clear", "--reason", "x"]) == 5
    assert "orchestrator move" in capsys.readouterr().err
