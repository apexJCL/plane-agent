"""Roles and review moves: rework, review --for, free roles in the busy-slug refusal, the agent-label warning, the
target date in the dispatch comment, and the session-mismatch warning, against FakePlane."""

from __future__ import annotations

from plane_agent.protocol import EXIT_REFUSED, NEEDS_HUMAN

from .conftest import agent
from .conftest import cli_runner as _cli
from .test_protocol import comments_on, label_names, ref, started, state_name
from .test_triage import _deploy, _refused


def test_rework_returns_the_item_to_its_holder(fake, orch):
    it, w = started(fake, orch, "api")
    w.review(ref(it), "done, I think")
    fake.tick(hours=30)
    assert orch.rework(ref(it), "- handle the 429 path\n- add a test") == "api"
    assert state_name(fake, it) == "In Progress" and it["assignees"] == [orch.me["id"]]
    assert label_names(fake, it) >= {"agent:api"}
    assert "Rework for agent:api:" in comments_on(fake, it)[-1]
    w.check(ref(it))  # the holder owns it again
    assert orch.stale("HOME", 24) == []  # the rework comment restarts its stale clock
    w.note(ref(it), "on it")


def test_rework_refusals(fake, orch):
    dep, _ = _deploy(fake, orch)
    _refused(orch.rework, ref(dep), "more", match="the operator decides")
    it, w = started(fake, orch, "api", name="a")
    w.review(ref(it), "")
    started(fake, orch, "api", name="b")  # api took other work meanwhile
    _refused(orch.rework, ref(it), "more", match="already holds")
    _refused(orch.rework, ref(it), " ", match="notes")
    _refused(agent(fake, "api").rework, ref(it), "more", match="orchestrator move")
    assert state_name(fake, it) == "In Review"


def test_review_for_a_silent_agent(fake, orch):
    it, w = started(fake, orch, "api")
    w.note(ref(it), "tests pass")
    _refused(orch.review_for, ref(it), "api", "the agent finished", match="let it hand over itself")
    _refused(orch.review_for, ref(it), "web", "x", match="not agent:web")
    fake.tick(hours=25)
    _refused(orch.review_for, ref(it), "api", "", match="--reason")
    orch.review_for(ref(it), "api", "the session ended after its last push", "Shipped in `abc123`.")
    assert state_name(fake, it) == "In Review"
    last = comments_on(fake, it)[-1]
    assert "handed over for agent:api" in last and "agent=orchestrator" in last and "agent=api" not in last


def test_review_for_refuses_operator_items(fake, orch):
    it, _w = started(fake, orch, "api", labels=[orch.project("HOME").label_id("kind:deploy")])
    fake.tick(hours=25)
    _refused(orch.review_for, ref(it), "api", "x", match="kind:deploy or needs-human")
    q, _w2 = started(fake, orch, "web", name="q")
    q["labels"].append(orch.project("HOME").label_id(NEEDS_HUMAN))
    fake.tick(hours=25)
    _refused(orch.review_for, ref(q), "web", "x", match="kind:deploy or needs-human")


def test_busy_slug_refusal_lists_free_roles(fake, orch):
    started(fake, orch, "api", name="a")
    done, w = started(fake, orch, "docs", name="d")
    w.review(ref(done), "")
    it = fake.seed_item(fake.pid, "next", "Ready")
    msg = _refused(orch.dispatch, ref(it), "api", code=EXIT_REFUSED)
    assert "free roles: docs" in msg and "orchestrator" not in msg.split("free roles:")[1]


def test_many_agent_labels_warn_on_a_new_one(fake, orch):
    for n in range(21):
        orch._agent_label_id(orch.project("HOME"), f"task-{n}")
    assert len(orch.notices) == 1 and "21 agent labels" in orch.notices[0] and "reuse a free one" in orch.notices[0]
    orch._agent_label_id(orch.project("HOME"), "task-3")  # an existing label: no new warning
    assert len(orch.notices) == 1


def test_dispatch_comment_carries_the_target(fake, orch):
    it = fake.seed_item(fake.pid, "a", "Ready")
    orch.plan(ref(it), target="2026-10-09")
    orch.dispatch(ref(it), "api")
    assert "Dispatched to agent:api. Target 2026-10-09." in comments_on(fake, it)[-1]
    bare = fake.seed_item(fake.pid, "b", "Ready")
    orch.dispatch(ref(bare), "web")
    assert "Dispatched to agent:web.</p>" in comments_on(fake, bare)[-1]


def test_signing_from_another_session_warns(fake, orch):
    it, _w = started(fake, orch, "api")
    relay = agent(fake, "api")
    relay.session = "orchestrator-relay"
    relay.review(ref(it), "")
    assert any("claimed HOME-" in n and "session s-api" in n and "never sign as another" in n for n in relay.notices)
    it2, w2 = started(fake, orch, "web", name="b")
    w2.review(ref(it2), "")
    assert w2.notices == []


def test_cli_rework_and_review_for(fake, orch, monkeypatch, capsys):
    run = _cli(fake, monkeypatch)
    it, _w = started(fake, orch, "api")
    fake.tick(hours=25)
    assert run("review", ref(it), "--reason", "x") == 2
    assert run("review", ref(it), "--for", "api", "--reason", "session ended") == 0
    assert "holder_last_active=" in capsys.readouterr().out
    assert run("rework", ref(it), "one more test") == 0 and state_name(fake, it) == "In Progress"
    from plane_agent import cli

    assert cli.main(["--agent", "api", "--session", "t", "--where", "x", "review", ref(it), "--for", "api",
                     "--reason", "x"]) == 5  # fmt: skip
