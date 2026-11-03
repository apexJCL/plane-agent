"""Native dates (0.3.0): the `plan` date setter, `create --target/--start`, "today" in a zone, queue order and the
overdue footer, the --estimate deprecation, the removed verbs, and old plan/actual trails, against FakePlane."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest

from plane_agent.protocol import EXIT_OWNERSHIP_LOST, EXIT_REFUSED, ProtocolRefusal

from .conftest import agent
from .conftest import cli_runner as _cli
from .test_protocol import claimable, comments_on, ref, started


def test_plan_sets_and_clears_native_dates_without_a_comment(fake, orch):
    it = fake.seed_item(fake.pid, "a", "Ready")
    assert orch.plan(ref(it), target="2026-10-09") == {"target_date": "2026-10-09"}
    assert orch.plan(ref(it), start="2026-10-07") == {"start_date": "2026-10-07"}
    assert (it["start_date"], it["target_date"]) == ("2026-10-07", "2026-10-09")
    orch.plan(ref(it), clear=True)
    assert (it["start_date"], it["target_date"]) == (None, None)
    assert comments_on(fake, it) == []  # no --reason, no comment


def test_plan_comments_only_with_a_reason(fake, orch):
    it = fake.seed_item(fake.pid, "a", "Ready")
    orch.plan(ref(it), target="2026-10-09", start="2026-10-07", reason="The API work slipped a day.")
    body = comments_on(fake, it)[-1]
    assert "Dates: start 2026-10-07, target 2026-10-09." in body and "slipped a day" in body and "agent=orchestrator" in body
    orch.plan(ref(it), clear=True, reason="Parked.")
    assert comments_on(fake, it)[-1].startswith("<p>Dates cleared.</p>")


def test_plan_refusals(fake, orch):
    it = fake.seed_item(fake.pid, "a", "Ready", target_date="2026-10-09")
    for kw, match in (({}, "needs --target"), ({"clear": True, "target": "2026-10-09"}, "takes no"),
                      ({"start": "2026-10-12"}, "after target"), ({"target": "10/09"}, "YYYY-MM-DD|date")):  # fmt: skip
        with pytest.raises(ProtocolRefusal, match=match) as e:
            orch.plan(ref(it), **kw)
        assert e.value.code == EXIT_REFUSED
    done = fake.seed_item(fake.pid, "d", "Done")
    with pytest.raises(ProtocolRefusal, match="closed item") as e:
        orch.plan(ref(done), target="2026-10-09")
    assert e.value.code == EXIT_REFUSED and it["target_date"] == "2026-10-09"


def test_a_held_item_is_dated_by_its_holder_or_the_orchestrator(fake, orch):
    it, w = started(fake, orch, "api")
    w.plan(ref(it), target="2026-10-09")
    orch.plan(ref(it), target="2026-10-10")
    with pytest.raises(ProtocolRefusal) as e:
        agent(fake, "web").plan(ref(it), target="2026-10-20")
    assert e.value.code == EXIT_OWNERSHIP_LOST and it["target_date"] == "2026-10-10"
    free = fake.seed_item(fake.pid, "free", "Ready")
    agent(fake, "web").plan(ref(free), target="2026-10-20")  # nobody holds it: anyone may date it
    assert free["target_date"] == "2026-10-20"


def test_create_sets_native_dates(fake, orch):
    item = orch.create("HOME", "dated", "chore", start="2026-10-07", target="2026-10-09")
    stored = fake.items[item["id"]]
    assert (stored["start_date"], stored["target_date"]) == ("2026-10-07", "2026-10-09")
    with pytest.raises(ProtocolRefusal, match="after target"):
        orch.create("HOME", "backwards", "chore", start="2026-10-10", target="2026-10-09")


def test_today_follows_the_zone_or_the_local_default(fake):
    fake.clock = datetime(2026, 10, 7, 3, 0, tzinfo=UTC)  # 20:00 on the 6th in Tijuana
    assert agent(fake, "api", tz="America/Tijuana").today() == date(2026, 10, 6)
    assert agent(fake, "api", tz="UTC").today() == date(2026, 10, 7)
    assert agent(fake, "api", tz=None).today() == fake.clock.astimezone().date()
    with pytest.raises(ProtocolRefusal):
        agent(fake, "api", tz="Mars/Olympus")


def test_queue_orders_by_priority_then_target_then_creation(fake, orch):
    late = fake.seed_item(fake.pid, "late", "Ready", target_date="2026-10-20")
    undated = fake.seed_item(fake.pid, "undated", "Ready")
    soon = fake.seed_item(fake.pid, "soon", "Ready", target_date="2026-10-08")
    urgent = fake.seed_item(fake.pid, "urgent", "Ready", priority="urgent")
    assert [i["name"] for i in claimable(orch)] == [urgent["name"], soon["name"], late["name"], undated["name"]]


def test_queue_footer_counts_overdue_items(fake, monkeypatch, capsys):
    run = _cli(fake, monkeypatch)
    assert run("bootstrap-project", "HOME") == 0
    fake.seed_item(fake.pid, "slipped", "Ready", target_date="2026-10-01")
    fake.seed_item(fake.pid, "fine", "Ready", target_date="2026-10-30")
    assert run("queue", "HOME") == 0
    assert "1 open item(s) past their target date" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ("create", "HOME", "x", "--kind", "chore", "--estimate", "3h"),
    ("plan", "HOME-1", "--target", "2026-10-09", "--estimate", "3h"),
])  # fmt: skip
def test_estimate_is_ignored_with_a_notice(fake, monkeypatch, capsys, argv):
    run = _cli(fake, monkeypatch)
    run("bootstrap-project", "HOME")
    fake.seed_item(fake.pid, "a", "Ready")
    capsys.readouterr()
    assert run(*argv) == 0
    err = capsys.readouterr().err
    assert f"--estimate is deprecated and ignored since 0.3.0 (estimates were removed); drop it from {argv[0]}" in err
    assert all("estimate" not in str(v) for it in fake.items.values() for v in it.values())


def test_estimate_only_plan_is_a_no_op(fake, monkeypatch, capsys):
    run = _cli(fake, monkeypatch)
    run("bootstrap-project", "HOME")
    it = fake.seed_item(fake.pid, "a", "Ready")
    capsys.readouterr()
    assert run("--json", "plan", ref(it), "--estimate", "2h") == 0
    out = capsys.readouterr()
    assert '"unchanged": true' in out.out and "deprecated" in out.err and comments_on(fake, it) == []
    assert run("plan", ref(it)) == 2 and "needs --target" in capsys.readouterr().err


@pytest.mark.parametrize(("argv", "says"), [
    (("report", "HOME", "--max-calls", "50"), "report was removed in 0.3.0"),
    (("log", "HOME", "retro", "--kind", "chore"), "log was removed in 0.3.0"),
    (("stale", "HOME", "--reclaim"), "stale --reclaim was removed in 0.3.0"),
])  # fmt: skip
def test_removed_verbs_exit_2_and_say_so(fake, monkeypatch, capsys, argv, says):
    run = _cli(fake, monkeypatch, slug="api")
    calls = len(fake.requests)
    assert run(*argv) == 2
    err = capsys.readouterr().err
    assert says in err and len(fake.requests) == calls  # refused before any API call
    if argv[0] == "stale":
        assert "reclaim REF" in err


def test_context_reads_old_plan_and_actual_trails(fake, orch):
    it, w = started(fake, orch, "api")
    sig = "<hr><p>agent={} · session=s-{} · where=repo@main</p>"
    old = ["<p>plan: rev=0 estimate=3h start=2026-10-06 target=2026-10-09</p>" + sig.format("orchestrator", "orchestrator"),
           "<p>Shipped.</p><p>actual: started=2026-10-06T09:00-07:00 ended=2026-10-06T11:00-07:00</p>"
           + sig.format("api", "api")]  # fmt: skip
    for html in old:
        fake.comments.append({"id": str(uuid.uuid4()), "issue": it["id"], "actor": fake.uid("claude"),
                              "comment_html": html, "created_at": fake.clock.isoformat()})  # fmt: skip
        fake.tick()
    texts = [c["text"] for c in orch.context(ref(it))["comments"]]
    assert any("plan: rev=0" in t for t in texts) and any("actual: started=" in t for t in texts)
    assert any("actual:" in c["text"] for c in orch.context(ref(it), agent="api")["comments"])
    w.check(ref(it))  # an old statement in the trail doesn't disturb the claim
    w.done(ref(it), "Wrapped up.")
