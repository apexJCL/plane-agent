"""Protocol tests against FakePlane (shapes from Plane CE v1.4.2). Each test names the spec scenario it covers
in openspec/changes/deploy-project-tracker/specs/agent-coordination/spec.md.

Every agent uses the one `claude` account (token tok-claude); agents differ only by slug. `operator` is the human.
"""

from __future__ import annotations

import httpx
import pytest

from plane_agent.cli import VERBS, build_parser
from plane_agent.client import PlaneClient, PlaneError
from plane_agent.protocol import (
    EXIT_NOT_CLAIMABLE,
    EXIT_OWNERSHIP_LOST,
    EXIT_REFUSED,
    NEEDS_HUMAN,
    STATES,
    ProtocolRefusal,
    signed_by,
)

from .conftest import TZ, agent


def ref(it):
    return f"HOME-{it['sequence_id']}"


TARGET = "2026-10-16"  # the fake's clock starts Tuesday 2026-10-06; this is the Friday after


def planned(orch, it, estimate="2h", target=TARGET):
    """The item's ref, after giving it a baseline plan if it has none (dispatch refuses unplanned items)."""
    if not any("plan: rev=" in c["comment_html"] for c in orch.client.comments(it["project"], it["id"])):
        orch.plan(ref(it), estimate, target=target)
    return ref(it)


def claimable(orch, sweep=False):
    """Every claimable item, planned first; `queue` lists unplanned ones apart."""
    ready, unplanned, _log = orch.queue("HOME", sweep=sweep)
    return ready + unplanned


def state_name(fake, it):
    return next(s["name"] for s in fake.states[fake.pid] if s["id"] == it["state"])


def label_names(fake, it):
    return {lb["name"] for lb in fake.labels[fake.pid] if lb["id"] in it["labels"]}


def comments_on(fake, it):
    return [c["comment_html"] for c in fake.comments if c["issue"] == it["id"]]


def started(fake, orch, slug, name="A", **kw):
    """A Ready item dispatched to `slug` and claimed by it; returns (item, worker)."""
    it = fake.seed_item(fake.pid, name, "Ready", **kw)
    orch.dispatch(planned(orch, it), slug)
    w = agent(fake, slug)
    w.claim(ref(it))
    return it, w


# -- bootstrap ---------------------------------------------------------------


def test_bootstrap_shapes_states_and_is_idempotent(fake):
    todo_id = fake.state(fake.pid, "Todo")["id"]
    a = agent(fake, "orchestrator")
    changes = a.bootstrap_project("HOME", TZ)
    assert "renamed state Todo -> Ready" in changes
    ordered = sorted(fake.states[fake.pid], key=lambda s: s["sequence"])
    assert [(s["name"], s["group"]) for s in ordered] == [(n, g) for n, g, _ in STATES]
    assert fake.state(fake.pid, "Ready")["id"] == todo_id  # renamed, not duplicated
    assert fake.state(fake.pid, "Backlog")["default"] is True
    assert {lb["name"] for lb in fake.labels[fake.pid]} >= {"kind:deploy", NEEDS_HUMAN}
    # Scenario "New project bootstrap": a re-run changes nothing.
    assert agent(fake, "orchestrator").bootstrap_project("HOME", TZ) == []


def test_bad_slug_refused(fake):
    with pytest.raises(ProtocolRefusal) as e:
        agent(fake, "Meow Feeder_API")
    assert e.value.code == EXIT_REFUSED


# -- queue -------------------------------------------------------------------


def test_blocked_item_is_skipped_then_dispatchable_when_blocker_resolves(fake, orch):
    a = fake.seed_item(fake.pid, "A", "In Progress")
    b = fake.seed_item(fake.pid, "B", "Ready")
    fake.relate(b, a)
    assert [i["name"] for i in claimable(orch, sweep=False)] == []  # Scenario: Blocked item is skipped
    a["state"] = fake.state(fake.pid, "Done")["id"]
    assert [i["name"] for i in claimable(orch, sweep=False)] == ["B"]  # Scenario: Blocker resolves


def test_queue_order_priority_then_age(fake, orch):
    fake.seed_item(fake.pid, "old-low", "Ready", priority="low")
    fake.seed_item(fake.pid, "new-urgent", "Ready", priority="urgent")
    fake.seed_item(fake.pid, "old-none", "Ready")
    fake.seed_item(fake.pid, "assigned", "Ready", assignees=[fake.uid("operator")])
    assert [i["name"] for i in claimable(orch, sweep=False)] == ["new-urgent", "old-low", "old-none"]


def test_queue_pages_through_many_items(fake, orch):
    for n in range(250):
        fake.seed_item(fake.pid, f"done-{n}", "Done")
    fake.seed_item(fake.pid, "late-ready", "Ready")
    assert [i["name"] for i in claimable(orch, sweep=False)] == ["late-ready"]


def test_sweep_returns_resolved_blocked_items_to_ready(fake, orch):
    a = fake.seed_item(fake.pid, "A", "Done")
    b = fake.seed_item(fake.pid, "B", "Blocked", assignees=[fake.uid("claude")])
    fake.relate(b, a)
    human = fake.seed_item(fake.pid, "H", "Blocked", labels=[orch.project("HOME").label_id(NEEDS_HUMAN)])
    agent(fake, "api-implementation").queue("HOME", sweep=False)  # workers don't sweep
    assert state_name(fake, b) == "Blocked"
    # Scenario "Prerequisite finished": the orchestrator's queue read returns B to Ready, unassigned, with a comment.
    items = claimable(orch, sweep=True)
    assert state_name(fake, b) == "Ready" and b["assignees"] == []
    assert [i["name"] for i in items] == ["B"]
    assert any("Unblocked" in c for c in comments_on(fake, b))
    assert state_name(fake, human) == "Blocked"  # needs-human waits for the operator


# -- dispatch / claim ----------------------------------------------------------


def test_dispatch_labels_item_before_worker_starts(fake, orch):
    it = fake.seed_item(fake.pid, "A", "Ready", labels=[orch.project("HOME").label_id("kind:feature")])
    orch.dispatch(planned(orch, it), "api-implementation")
    # Scenario "Orchestrator dispatch": In Progress, this account assigned, agent label set, before the worker runs.
    assert it["assignees"] == [fake.uid("claude")] and state_name(fake, it) == "In Progress"
    assert label_names(fake, it) == {"kind:feature", "agent:api-implementation"}
    assert "Dispatched to agent:api-implementation." in comments_on(fake, it)[-1]


def test_claim_posts_signed_comment(fake, orch):
    it, _w = started(fake, orch, "api-implementation")
    c = comments_on(fake, it)[-1]
    assert "Claimed by agent:api-implementation" in c
    assert "agent=api-implementation" in c and "session=s-api-implementation" in c and "repo@main" in c


def test_claim_refused_when_dispatched_to_someone_else(fake, orch):
    it = fake.seed_item(fake.pid, "A", "Ready")
    orch.dispatch(planned(orch, it), "api-implementation")
    with pytest.raises(ProtocolRefusal) as e:  # same account, different slug
        agent(fake, "docs-writer").claim(ref(it))
    assert e.value.code == EXIT_OWNERSHIP_LOST
    assert not any("Claimed by agent:docs-writer" in c for c in comments_on(fake, it))


def test_dispatch_refuses_non_ready_and_busy_slug(fake, orch):
    backlog = fake.seed_item(fake.pid, "A", "Backlog")
    with pytest.raises(ProtocolRefusal) as e:
        orch.dispatch(planned(orch, backlog), "api-implementation")
    assert e.value.code == EXIT_NOT_CLAIMABLE
    started(fake, orch, "api-implementation", name="first")
    second = fake.seed_item(fake.pid, "second", "Ready")
    with pytest.raises(ProtocolRefusal) as e:  # Scenario "Slug already in use"
        orch.dispatch(planned(orch, second), "api-implementation")
    assert e.value.code == EXIT_REFUSED and state_name(fake, second) == "Ready"


def test_redispatch_replaces_agent_label(fake, orch):
    it, w = started(fake, orch, "api-implementation")
    w.review(ref(it), "please look")
    it["state"] = fake.state(fake.pid, "Ready")["id"]  # operator sends it back
    it["assignees"] = []
    orch.dispatch(planned(orch, it), "api-fixes")
    assert {n for n in label_names(fake, it) if n.startswith("agent:")} == {"agent:api-fixes"}


# -- ownership-checked writes -------------------------------------------------


def test_write_aborted_after_operator_pulls_item_back(fake, orch):
    it, w = started(fake, orch, "api-implementation")
    it["state"] = fake.state(fake.pid, "Backlog")["id"]  # Scenario "Operator pulls an item back"
    with pytest.raises(ProtocolRefusal) as e:
        w.done(ref(it), "finished")
    assert e.value.code == EXIT_OWNERSHIP_LOST
    assert state_name(fake, it) == "Backlog"  # nothing reverted
    assert "not held by agent:api-implementation" in str(e.value)


def test_refused_writes_and_checks_leave_no_comment(fake, orch):
    it, w = started(fake, orch, "api")
    w.review(ref(it), "ready")
    before = len(comments_on(fake, it))
    for call in (lambda: w.check(ref(it)), lambda: w.note(ref(it), "more"), lambda: w.done(ref(it))):
        with pytest.raises(ProtocolRefusal) as e:
            call()
        assert e.value.code == EXIT_OWNERSHIP_LOST
    assert len(comments_on(fake, it)) == before  # exit 4 and stderr only; the item's trail is untouched


def test_check_never_writes(fake, orch):
    it, w = started(fake, orch, "api")
    before = len(fake.requests)
    assert w.check(ref(it)) == "In Progress"
    assert all(m == "GET" for m, _ in fake.requests[before:])


def test_write_aborted_after_operator_takes_item(fake, orch):
    it, w = started(fake, orch, "api-implementation")
    it["assignees"] = [fake.uid("operator")]
    with pytest.raises(ProtocolRefusal) as e:
        w.check(ref(it))
    assert e.value.code == EXIT_OWNERSHIP_LOST


def test_reclaimed_under_live_worker_cannot_finish(fake, orch):
    it, w = started(fake, orch, "api-implementation")
    fake.tick(hours=25)
    orch.stale("HOME", 24, reclaim=True)
    with pytest.raises(ProtocolRefusal) as e:  # Scenario "Reclaimed under a live worker"
        w.done(ref(it), "finished late")
    assert e.value.code == EXIT_OWNERSHIP_LOST and state_name(fake, it) == "Ready"


def test_stale_reclaim_has_a_24h_floor(fake, orch):
    it, _w = started(fake, orch, "api")
    fake.tick(hours=3)
    with pytest.raises(ProtocolRefusal) as e:
        orch.stale("HOME", 2, reclaim=True)
    assert e.value.code == EXIT_REFUSED and "reclaim REF" in str(e.value)
    assert [f[1] for f in orch.stale("HOME", 2, reclaim=False)] == ["api"]  # listing early is fine
    assert state_name(fake, it) == "In Progress"


def test_reclaim_verb_refuses_an_active_holder_unless_forced(fake, orch):
    it, w = started(fake, orch, "api")
    fake.tick(hours=3)
    with pytest.raises(ProtocolRefusal) as e:
        orch.reclaim(ref(it), "the operator wants it back")
    assert e.value.code == EXIT_REFUSED and "--force" in str(e.value)
    assert orch.reclaim(ref(it), "the operator wants it back", force=True) == "api"
    assert state_name(fake, it) == "Ready" and it["assignees"] == []
    assert "Reclaimed from agent:api" in comments_on(fake, it)[-1] and "the operator wants" in comments_on(fake, it)[-1]
    with pytest.raises(ProtocolRefusal) as e:
        w.done(ref(it))
    assert e.value.code == EXIT_OWNERSHIP_LOST


def test_reclaim_verb_after_the_floor_and_its_gates(fake, orch):
    it, _w = started(fake, orch, "api")
    with pytest.raises(ProtocolRefusal) as e:
        agent(fake, "api").reclaim(ref(it), "mine")
    assert e.value.code == EXIT_REFUSED
    with pytest.raises(ProtocolRefusal):
        orch.reclaim(ref(it), "")
    fake.tick(hours=25)
    assert orch.reclaim(ref(it), "silent for a day") == "api"
    with pytest.raises(ProtocolRefusal) as e:  # Ready now: nothing to reclaim
        orch.reclaim(ref(it), "again")
    assert e.value.code == EXIT_REFUSED


def test_done_refused_for_deploy_items(fake, orch):
    it, w = started(fake, orch, "deployer", labels=[orch.project("HOME").label_id("kind:deploy")])
    with pytest.raises(ProtocolRefusal) as e:  # Scenario "Deploy item"
        w.done(ref(it), "deployed")
    assert e.value.code == EXIT_REFUSED
    w.review(ref(it), "Operator: run ./plane-compose up -d")
    assert state_name(fake, it) == "In Review"
    assert "agent:deployer" in label_names(fake, it)  # attribution stays after the agent leaves


def reviewed(fake, orch, slug="api", **kw):
    it, w = started(fake, orch, slug, **kw)
    w.review(ref(it), "ready")
    return it


def test_orchestrator_accepts_reviewed_item(fake, orch):
    a = reviewed(fake, orch)
    b = fake.seed_item(fake.pid, "B", "Ready")
    fake.relate(b, a)
    orch.accept(ref(a), "review passed")  # Scenario "Orchestrator accepts reviewed work"
    assert state_name(fake, a) == "Done" and "Accepted." in comments_on(fake, a)[-1]
    assert "agent=orchestrator" in comments_on(fake, a)[-1]
    assert [ref(i) for i in claimable(orch, sweep=False)] == [ref(b)]


def test_orchestrator_rejects_reviewed_item(fake, orch):
    it = reviewed(fake, orch)
    orch.reject(ref(it), "tests fail")  # Scenario "Orchestrator rejects reviewed work"
    assert state_name(fake, it) == "Ready" and it["assignees"] == []
    assert [ref(i) for i in claimable(orch, sweep=False)] == [ref(it)]
    orch.dispatch(planned(orch, it), "api")
    assert label_names(fake, it) >= {"agent:api"}


@pytest.mark.parametrize("label", ["kind:deploy", NEEDS_HUMAN])
def test_accept_refuses_operator_items(fake, orch, label):
    it = reviewed(fake, orch)
    it["labels"].append(orch.project("HOME").label_id(label))  # dispatch refuses needs-human, so add it after
    with pytest.raises(ProtocolRefusal) as e:  # Scenario "Deploy item awaiting sign-off"
        orch.accept(ref(it), "looks fine")
    assert e.value.code == EXIT_REFUSED and state_name(fake, it) == "In Review"


def test_verdicts_need_orchestrator_and_in_review(fake, orch):
    it, w = started(fake, orch, "api")
    with pytest.raises(ProtocolRefusal):
        orch.accept(ref(it), "not reviewed yet")  # In Progress, not In Review
    w.review(ref(it), "ready")
    for move in (w.accept, w.reject):
        with pytest.raises(ProtocolRefusal) as e:
            move(ref(it), "self-verdict")
        assert e.value.code == EXIT_REFUSED
    assert state_name(fake, it) == "In Review"


def test_close_epic_when_children_closed(fake, orch):
    epic = fake.seed_item(fake.pid, "Epic", "Backlog")
    a = fake.seed_item(fake.pid, "a", "Done", parent=epic["id"])
    b = fake.seed_item(fake.pid, "b", "Cancelled", parent=epic["id"])
    refs = orch.close_epic(ref(epic), "all shipped")  # Scenario "Every sub-item is closed"
    assert refs == [f"{ref(a)} (Done)", f"{ref(b)} (Cancelled)"]
    assert state_name(fake, epic) == "Done"
    assert ref(a) in comments_on(fake, epic)[-1] and "agent=orchestrator" in comments_on(fake, epic)[-1]


def test_close_epic_refuses_open_children(fake, orch):
    epic = fake.seed_item(fake.pid, "Epic", "Backlog")
    fake.seed_item(fake.pid, "a", "Done", parent=epic["id"])
    b = fake.seed_item(fake.pid, "b", "In Review", parent=epic["id"])
    with pytest.raises(ProtocolRefusal) as e:  # Scenario "A sub-item is still open"
        orch.close_epic(ref(epic), "early")
    assert e.value.code == EXIT_REFUSED and f"{ref(b)} (In Review)" in str(e.value)
    assert state_name(fake, epic) == "Backlog" and comments_on(fake, epic) == []


def test_close_epic_refusals(fake, orch):
    lone = fake.seed_item(fake.pid, "no children", "Backlog")
    deploy = fake.seed_item(fake.pid, "deploy epic", "Backlog", labels=[orch.project("HOME").label_id("kind:deploy")])
    fake.seed_item(fake.pid, "child", "Done", parent=deploy["id"])
    for it in (lone, deploy):
        with pytest.raises(ProtocolRefusal) as e:
            orch.close_epic(ref(it), "x")
        assert e.value.code == EXIT_REFUSED and state_name(fake, it) == "Backlog"
    with pytest.raises(ProtocolRefusal) as e:
        agent(fake, "api").close_epic(ref(deploy), "x")
    assert "orchestrator move" in str(e.value)


def test_ready_promotes_backlog_item_unassigned(fake, orch):
    it = fake.seed_item(fake.pid, "filed", "Backlog", assignees=[orch.me["id"]])  # project default assignee
    assert orch.ready(ref(it), "specified now") == "Ready"  # Scenario "Promoting a Backlog item"
    assert state_name(fake, it) == "Ready" and it["assignees"] == []
    assert "Promoted from Backlog" in comments_on(fake, it)[-1] and "agent=orchestrator" in comments_on(fake, it)[-1]
    assert [i["id"] for i in claimable(orch, sweep=False)] == [it["id"]]


def test_ready_without_reason_writes_no_comment(fake, orch):
    it = fake.seed_item(fake.pid, "filed", "Backlog")
    assert orch.ready(ref(it)) == "Ready"
    assert comments_on(fake, it) == []  # the activity log records the move


def test_ready_with_open_blocker_goes_to_blocked_then_sweeps(fake, orch):
    blocker = fake.seed_item(fake.pid, "first", "Ready")
    it = fake.seed_item(fake.pid, "second", "Backlog")
    fake.relate(it, blocker)
    assert orch.ready(ref(it)) == "Blocked"  # Scenario "Promoting an item with open blockers"
    assert claimable(orch, sweep=True) == [blocker]
    blocker["state"] = fake.state(fake.pid, "Done")["id"]
    orch.queue("HOME", sweep=True)
    assert state_name(fake, it) == "Ready"


def test_ready_refusals(fake, orch):
    nh = orch.project("HOME").label_id(NEEDS_HUMAN)
    ready = fake.seed_item(fake.pid, "already", "Ready")
    asked = fake.seed_item(fake.pid, "question", "Backlog", labels=[nh])
    epic = fake.seed_item(fake.pid, "epic", "Backlog")
    fake.seed_item(fake.pid, "child", "Backlog", parent=epic["id"])
    for it, before in ((ready, "Ready"), (asked, "Backlog"), (epic, "Backlog")):
        with pytest.raises(ProtocolRefusal) as e:
            orch.ready(ref(it))
        assert e.value.code == EXIT_REFUSED and state_name(fake, it) == before
    lone = fake.seed_item(fake.pid, "lone", "Backlog")
    with pytest.raises(ProtocolRefusal) as e:
        agent(fake, "api").ready(ref(lone))
    assert "orchestrator move" in str(e.value) and state_name(fake, lone) == "Backlog"


def test_cancel_abandons_unheld_item(fake, orch):
    it = fake.seed_item(fake.pid, "dropped", "Ready")
    orch.cancel(ref(it), "superseded by HOME-9")
    assert state_name(fake, it) == "Cancelled"
    assert "superseded by HOME-9" in comments_on(fake, it)[-1] and "agent=orchestrator" in comments_on(fake, it)[-1]


def test_cancel_refuses_prerequisite_of_open_work(fake, orch):
    blocker = fake.seed_item(fake.pid, "prereq", "Backlog")
    dep = fake.seed_item(fake.pid, "dependent", "Blocked")
    fake.relate(dep, blocker)
    with pytest.raises(ProtocolRefusal) as e:  # Scenario "Cancelling a prerequisite"
        orch.cancel(ref(blocker), "not needed")
    assert e.value.code == EXIT_REFUSED and ref(dep) in str(e.value)
    assert state_name(fake, blocker) == "Backlog" and comments_on(fake, blocker) == []
    dep["state"] = fake.state(fake.pid, "Cancelled")["id"]
    orch.cancel(ref(blocker), "not needed")
    assert state_name(fake, blocker) == "Cancelled"


def test_cancel_refusals(fake, orch):
    proj = orch.project("HOME")
    held, _ = started(fake, orch, "api")
    done = fake.seed_item(fake.pid, "done", "Done")
    deploy = fake.seed_item(fake.pid, "deploy", "Ready", labels=[proj.label_id("kind:deploy")])
    asked = fake.seed_item(fake.pid, "asked", "Blocked", labels=[proj.label_id(NEEDS_HUMAN)])
    epic = fake.seed_item(fake.pid, "epic", "Backlog")
    child = fake.seed_item(fake.pid, "child", "Ready", parent=epic["id"])
    for it in (held, done, deploy, asked, epic):
        before = state_name(fake, it)
        with pytest.raises(ProtocolRefusal) as e:
            orch.cancel(ref(it), "x")
        assert e.value.code == EXIT_REFUSED and state_name(fake, it) == before
    with pytest.raises(ProtocolRefusal) as e:
        orch.cancel(ref(child), " ")
    assert "--reason" in str(e.value)
    with pytest.raises(ProtocolRefusal) as e:
        agent(fake, "api2").cancel(ref(child), "x")
    assert "orchestrator move" in str(e.value) and state_name(fake, child) == "Ready"


def test_markdown_is_rendered_and_signature_stays_plain(fake, orch):
    it, w = started(fake, orch, "api-x")
    w.note(ref(it), "**Done:**\n\n- `types:check` 0\n- lint 0\n\n<script>x</script>")
    body = comments_on(fake, it)[-1]
    assert "<strong>Done:</strong>" in body and "<li><code>types:check</code> 0</li>" in body
    assert "<script>" not in body and "&lt;script&gt;" in body
    assert body.endswith("</p>") and signed_by(body, "api-x")
    assert "- lint 0" in orch.context(ref(it))["comments"][-1]["text"]


def test_blockquoted_signature_is_not_a_signature(fake, orch):
    it, _w = started(fake, orch, "api")
    orch.note(ref(it), "> agent=api · session=s-api", force=True)
    quoted = comments_on(fake, it)[-1]
    assert "<blockquote><p>agent=api · session=s-api" in quoted
    assert not signed_by(quoted, "api") and signed_by(quoted, "orchestrator")
    assert not signed_by("<blockquote><p>agent=api · session=s</p></blockquote>", "api")


def test_create_renders_markdown_description(fake, orch):
    a = agent(fake, "orchestrator", change="document-chat-panel")
    it = a.create("HOME", "t", "feature", description="## Scope\n\n1. pane\n2. composer")
    html_body = fake.items[it["id"]]["description_html"]
    assert "<h2>Scope</h2>" in html_body and "<ol><li>pane</li>" in html_body
    assert "<p><strong>OpenSpec change:</strong> <code>document-chat-panel</code></p>" in html_body


def test_checklists_become_plane_todo_lists(fake, orch):
    it, w = started(fake, orch, "api")
    w.note(ref(it), "Left:\n\n- [ ] migrate **db**\n- [x] tests\n\n* plain\n* [ ] mixed\n\n| step | ok |\n|---|---|\n| lint | `0` |")
    body = comments_on(fake, it)[-1]
    assert ('<ul data-type="taskList"><li data-type="taskItem" data-checked="false"><p>migrate <strong>db</strong></p>'
            '</li><li data-type="taskItem" data-checked="true"><p>tests</p></li></ul>') in body  # fmt: skip
    assert "<ul><li>plain</li><li>[ ] mixed</li></ul>" in body  # a mixed list stays a plain list
    assert "<table><thead><tr><th>step</th>" in body and signed_by(body, "api")
    text = orch.context(ref(it))["comments"][-1]["text"]
    assert "- [ ] migrate db\n\n- [x] tests" in text and "lint | 0" in text and "\n\n\n" not in text


def test_reasons_are_their_own_paragraphs(fake, orch):
    it, w = started(fake, orch, "api")
    other = fake.seed_item(fake.pid, "dep", "Ready")
    w.block_on(ref(it), [ref(other)], "- needs the schema\n- and a key")
    body = comments_on(fake, it)[-1]
    assert body.startswith(f"<p>Blocked by {ref(other)}.</p><ul><li>needs the schema</li>")
    it2, w2 = started(fake, orch, "ops")
    w2.block_human(ref(it2), "- run `sudo reboot`?\n- or wait")
    assert comments_on(fake, it2)[-1].startswith("<p>Needs a human:</p><ul><li>run <code>sudo reboot</code>?</li>")


def test_agent_relays_operator_answer(fake, orch):
    it, w = started(fake, orch, "api")
    w.block_human(ref(it), "Which port?")
    assert w.answer(ref(it), "Use **8091**.") == "Ready"  # Scenario "Agent relays the operator's answer"
    assert NEEDS_HUMAN not in label_names(fake, it) and state_name(fake, it) == "Ready" and it["assignees"] == []
    last = comments_on(fake, it)[-1]
    assert "<strong>8091</strong>" in last and "relayed by agent:api" in last and signed_by(last, "api")
    assert [ref(i) for i in claimable(orch, sweep=False)] == [ref(it)]


def test_answer_keeps_item_blocked_on_open_work(fake, orch):
    blocker = fake.seed_item(fake.pid, "prereq", "Ready")
    it, w = started(fake, orch, "api")
    fake.relate(it, blocker)
    w.block_human(ref(it), "OK to wait?")
    assert w.answer(ref(it), "yes") == "Blocked"
    assert state_name(fake, it) == "Blocked" and NEEDS_HUMAN not in label_names(fake, it)


def test_answer_refuses_items_without_needs_human(fake, orch):
    it, w = started(fake, orch, "api")
    for text in ("yes", "  "):
        with pytest.raises(ProtocolRefusal) as e:
            w.answer(ref(it), text)
        assert e.value.code == EXIT_REFUSED
    assert state_name(fake, it) == "In Progress"


# -- blockers ----------------------------------------------------------------


def test_block_human_labels_and_blocks(fake, orch):
    it, w = started(fake, orch, "api-implementation")
    w.block_human(ref(it), "Which port?")  # Scenario "Waiting on the operator"
    assert state_name(fake, it) == "Blocked" and NEEDS_HUMAN in label_names(fake, it)
    assert "Which port?" in fake.comments[-1]["comment_html"]


def test_block_on_records_relation_and_cycle_refused(fake, orch):
    a, w = started(fake, orch, "worker-a", name="A")
    b = fake.seed_item(fake.pid, "B", "Ready")
    w.block_on(ref(a), [ref(b)], "needs B first")  # Scenario "Waiting on other work"
    assert {"issue": a["id"], "related": b["id"], "type": "blocked_by"} in fake.relations
    assert state_name(fake, a) == "Blocked"
    orch.dispatch(planned(orch, b), "worker-b")
    w2 = agent(fake, "worker-b")
    w2.claim(ref(b))
    n_rel = len(fake.relations)
    with pytest.raises(ProtocolRefusal) as e:  # Scenario "Cycle attempt": B blocked by A closes a cycle
        w2.block_on(ref(b), [ref(a)], "needs A")
    assert e.value.code == EXIT_REFUSED and len(fake.relations) == n_rel
    assert state_name(fake, b) == "Blocked" and NEEDS_HUMAN in label_names(fake, b)


# -- stale -------------------------------------------------------------------


def test_stale_ignores_comments_by_others(fake, orch):
    it, _w = started(fake, orch, "api")
    other, _ = started(fake, orch, "api-implementation", name="busy-neighbour")
    fake.tick(hours=23)
    orch.note(ref(it), "status?", force=True)  # Scenario "Nudge does not refresh a claim"
    agent(fake, "api-implementation").note(ref(other), "still going")  # a longer slug sharing the prefix
    assert orch.stale("HOME", 24, reclaim=False) == []
    fake.tick(hours=1, minutes=5)
    found = orch.stale("HOME", 24, reclaim=True)
    assert [(i["id"], slug) for i, slug, _ in found] == [(it["id"], "api")]
    assert state_name(fake, it) == "Ready" and it["assignees"] == []  # Scenario "Crashed session"


def test_stale_counts_dispatch_time_for_silent_worker(fake, orch):
    it = fake.seed_item(fake.pid, "A", "Ready")
    fake.tick(hours=48)  # created long ago
    orch.dispatch(planned(orch, it), "api-implementation")
    fake.tick(hours=2)
    assert orch.stale("HOME", 24, reclaim=False) == []  # the clock starts at dispatch, not creation


def test_stale_skips_items_held_by_a_human(fake, orch):
    fake.seed_item(fake.pid, "mine", "In Progress", assignees=[fake.uid("operator")])
    fake.tick(hours=48)
    assert orch.stale("HOME", 24, reclaim=False) == []


# -- context -----------------------------------------------------------------


def test_context_returns_trail_filtered_by_agent(fake, orch):
    it, w = started(fake, orch, "api-implementation")
    w.note(ref(it), "Rate limit is 60/min; using backoff.")
    orch.note(ref(it), "status?", force=True)
    ctx = orch.context(ref(it), agent="api-implementation")
    assert ctx["state"] == "In Progress" and "agent:api-implementation" in ctx["labels"]
    texts = [c["text"] for c in ctx["comments"]]
    assert any("Rate limit is 60/min" in t for t in texts)
    assert not any("status?" in t for t in texts)
    assert len(orch.context(ref(it))["comments"]) == len(comments_on(fake, it))


def test_openspec_change_signs_comments_and_descriptions(fake, orch):
    a = agent(fake, "orchestrator", change="add-meow-api")
    item = a.create("HOME", "connector", "feature", description="Write the connector", ready=True,
                    estimate="3h", target=TARGET)
    a.dispatch(ref(fake.items[item["id"]]), "api-implementation")
    assert "openspec=add-meow-api" in comments_on(fake, fake.items[item["id"]])[-1]


# -- create ------------------------------------------------------------------


def test_create_requires_exactly_one_kind_and_links(fake, orch):
    parent = fake.seed_item(fake.pid, "epic", "Backlog")
    blocker = fake.seed_item(fake.pid, "pre", "Ready")
    it = orch.create("HOME", "child", "chore", parent=ref(parent), blocked_by=[ref(blocker)], ready=True)
    stored = fake.items[it["id"]]
    assert label_names(fake, stored) == {"kind:chore"} and stored["parent"] == parent["id"]
    assert {"issue": it["id"], "related": blocker["id"], "type": "blocked_by"} in fake.relations
    with pytest.raises(ProtocolRefusal):
        orch.create("HOME", "bad", "epic")


# -- review findings (Fable, 2026-10-06) --------------------------------------


def test_sweep_leaves_operator_held_blocked_item_alone(fake, orch):
    a = fake.seed_item(fake.pid, "A", "Done")
    held = fake.seed_item(fake.pid, "H", "Blocked", assignees=[fake.uid("operator")])
    fake.relate(held, a)
    orch.queue("HOME", sweep=True)  # Scenario "Human transitions are authoritative"
    assert state_name(fake, held) == "Blocked" and held["assignees"] == [fake.uid("operator")]


def test_quoted_signature_does_not_refresh_stale_clock(fake, orch):
    it, _w = started(fake, orch, "api")
    fake.tick(hours=23)
    orch.note(ref(it), "Earlier, agent=api · session=s-api wrote: 'nearly done'. Status?", force=True)
    fake.tick(hours=1, minutes=5)
    assert [i["id"] for i, _, _ in orch.stale("HOME", 24, reclaim=False)] == [it["id"]]
    assert all("Status?" not in c["text"] for c in orch.context(ref(it), agent="api")["comments"])


def test_stale_without_dispatch_comment_uses_label_activity(fake, orch):
    it = fake.seed_item(fake.pid, "A", "Ready")
    fake.tick(hours=72)  # an old item
    orch.dispatch(planned(orch, it), "api")
    fake.comments = [c for c in fake.comments if "Dispatched to" not in c["comment_html"]]  # comment POST lost
    fake.tick(hours=1)
    assert orch.stale("HOME", 24, reclaim=True) == []
    assert state_name(fake, it) == "In Progress"


def test_dispatch_fails_if_a_second_agent_label_survives(fake, orch):
    proj = orch.project("HOME")
    it = fake.seed_item(fake.pid, "A", "Ready")
    stray = orch._agent_label_id(proj, "old")
    planned(orch, it)
    real_update = orch.client.update_item

    def operator_races(pid, iid, body):  # the operator re-adds a label between our PATCH and the re-read
        out = real_update(pid, iid, body)
        fake.items[iid]["labels"] = fake.items[iid]["labels"] + [stray]
        return out

    orch.client.update_item = operator_races
    with pytest.raises(ProtocolRefusal) as e:
        orch.dispatch(planned(orch, it), "api")
    assert e.value.code == EXIT_NOT_CLAIMABLE


def test_two_agent_labels_reported_not_reclaimed(fake, orch):
    proj = orch.project("HOME")
    it, _w = started(fake, orch, "api")
    it["labels"] = it["labels"] + [orch._agent_label_id(proj, "docs")]
    fake.tick(hours=48)
    found = orch.stale("HOME", 24, reclaim=True)
    assert [(i["id"], last) for i, _, last in found] == [(it["id"], None)]
    assert state_name(fake, it) == "In Progress"


def test_dispatch_refuses_needs_human_and_open_blockers(fake, orch):
    proj = orch.project("HOME")
    nh = fake.seed_item(fake.pid, "answered?", "Ready", labels=[proj.label_id(NEEDS_HUMAN)])
    with pytest.raises(ProtocolRefusal) as e:
        orch.dispatch(planned(orch, nh), "api")
    assert e.value.code == EXIT_REFUSED
    pre = fake.seed_item(fake.pid, "pre", "Backlog")
    it = fake.seed_item(fake.pid, "A", "Ready")
    fake.relate(it, pre)
    with pytest.raises(ProtocolRefusal) as e:
        orch.dispatch(planned(orch, it), "api")
    assert e.value.code == EXIT_NOT_CLAIMABLE


def test_operator_answers_question_then_dispatchable(fake, orch):
    it, w = started(fake, orch, "api")
    w.block_human(ref(it), "Which port?")
    proj = orch.project("HOME")
    it["labels"] = [lb for lb in it["labels"] if lb != proj.label_id(NEEDS_HUMAN)]  # Scenario "Operator answers"
    it["state"] = fake.state(fake.pid, "Ready")["id"]
    it["assignees"] = []
    assert [i["id"] for i in claimable(orch, sweep=True)] == [it["id"]]
    orch.dispatch(planned(orch, it), "api")
    assert any("Which port?" in c["text"] for c in orch.context(ref(it))["comments"])


def test_cli_gates_orchestrator_moves(fake, monkeypatch, capsys):
    from plane_agent import cli

    monkeypatch.setattr(cli.conf, "resolve_token", lambda _c, _f: "tok-claude")
    monkeypatch.setattr(cli, "PlaneClient", lambda *a, **k: PlaneClient(*a, **k, transport=fake.transport()))
    monkeypatch.setenv("PLANE_TZ", TZ)
    assert cli.main(["--agent", "api", "--session", "t", "--where", "x", "stale", "HOME", "--reclaim"]) == 5
    assert cli.main(["--agent", "api", "--session", "t", "--where", "x", "accept", "HOME-1", "--summary", "s"]) == 5
    assert cli.main(["--agent", "api", "--session", "t", "--where", "x", "close-epic", "HOME-1", "--summary", "s"]) == 5
    assert cli.main(["--agent", "api", "--session", "t", "--where", "x", "ready", "HOME-1"]) == 5
    assert cli.main(["--agent", "api", "--session", "t", "--where", "x", "cancel", "HOME-1", "--reason", "r"]) == 5
    assert "orchestrator move" in capsys.readouterr().err
    assert cli.main(["--agent", "orchestrator", "--session", "t", "--where", "x", "bootstrap-project", "HOME"]) == 0  # PLANE_TZ from the environment
    assert cli.main(["--agent", "api", "--session", "t", "--where", "x", "--json", "queue", "HOME"]) == 0


def test_cli_writes_need_a_session(fake, orch, monkeypatch, capsys, tmp_path):
    from plane_agent import cli

    monkeypatch.setattr(cli.conf, "resolve_token", lambda _c, _f: "tok-claude")
    monkeypatch.setattr(cli, "PlaneClient", lambda *a, **k: PlaneClient(*a, **k, transport=fake.transport()))
    it, _w = started(fake, orch, "api")
    assert cli.main(["--agent", "api", "--where", "x", "comment", ref(it), "hi"]) == 2
    assert "needs a session id" in capsys.readouterr().err
    assert cli.main(["--agent", "api", "--where", "x", "check", ref(it)]) == 0  # reads don't
    assert cli.main(["--agent", "api", "--where", "x", "queue", "HOME"]) == 0
    assert cli.main(["--agent", "orchestrator", "--where", "x", "queue", "HOME"]) == 2  # its default sweep writes
    monkeypatch.setenv("PLANE_AGENT_SESSION", "run-7")
    assert cli.main(["--agent", "api", "--where", "x", "comment", ref(it), "hi"]) == 0
    assert "session=run-7" in comments_on(fake, it)[-1]
    monkeypatch.delenv("PLANE_AGENT_SESSION")
    (tmp_path / "user.toml").write_text("require_session = false\n")
    assert cli.main(["--agent", "api", "--where", "x", "comment", ref(it), "again"]) == 0
    assert "session=unknown" in comments_on(fake, it)[-1]


def test_cli_where_keeps_only_the_basename(fake, orch, monkeypatch, capsys):
    from plane_agent import cli

    monkeypatch.setattr(cli.conf, "resolve_token", lambda _c, _f: "tok-claude")
    monkeypatch.setattr(cli, "PlaneClient", lambda *a, **k: PlaneClient(*a, **k, transport=fake.transport()))
    it, _w = started(fake, orch, "api")
    assert cli.main(["--agent", "api", "--session", "t", "--where", "/home/me/src/web@main", "comment", ref(it), "hi"]) == 0
    assert "where=web@main" in comments_on(fake, it)[-1] and "/home" not in comments_on(fake, it)[-1]
    assert "is a path" in capsys.readouterr().err


def test_cli_reads_a_dash_body_from_stdin(fake, orch, monkeypatch, capsys):
    import io

    from plane_agent import cli

    monkeypatch.setattr(cli.conf, "resolve_token", lambda _c, _f: "tok-claude")
    monkeypatch.setattr(cli, "PlaneClient", lambda *a, **k: PlaneClient(*a, **k, transport=fake.transport()))
    it, _w = started(fake, orch, "api")
    run = lambda *argv: cli.main(["--agent", "api", "--session", "t", "--where", "x", *argv])
    monkeypatch.setattr("sys.stdin", io.StringIO("Shipped:\n\n```sh\necho \"$HOME\" `x`\n```\n"))
    assert run("done", ref(it), "--summary", "-") == 0
    body = comments_on(fake, it)[-1]
    assert '<pre><code class="language-sh">echo &quot;$HOME&quot; `x`' in body and state_name(fake, it) == "Done"
    monkeypatch.setattr("sys.stdin", io.StringIO("  \n"))
    assert run("comment", ref(it), "-", "--force") == 2 and "stdin was empty" in capsys.readouterr().err


# -- cli & auth --------------------------------------------------------------


def test_help_lists_every_verb():
    sub = next(a for a in build_parser()._actions if a.dest == "verb")
    assert set(sub.choices) == set(VERBS)


def test_bad_token_is_rejected(fake):
    client = PlaneClient("https://plane.test", "acme", "wrong", transport=fake.transport())
    with pytest.raises(PlaneError) as e:
        client.me()
    assert e.value.status == 403


def _throttled(times: int, retry_after: str = "7"):
    """A transport that answers 429 `times` times, then 200."""
    calls = []

    def handle(req):
        calls.append(req.url.path)
        if len(calls) <= times:
            return httpx.Response(429, headers={"Retry-After": retry_after}, json={"detail": "throttled"})
        return httpx.Response(200, json={"id": "u1"})

    return httpx.MockTransport(handle), calls


def test_rate_limit_is_waited_out():
    transport, calls = _throttled(2)
    slept = []
    c = PlaneClient("https://plane.test", "acme", "t", transport=transport, sleep=slept.append)
    assert c.me() == {"id": "u1"}
    assert len(calls) == 3 and slept == [7.0, 7.0]


def test_rate_limit_gives_up_after_max_wait():
    transport, calls = _throttled(100, retry_after="60")
    slept = []
    c = PlaneClient("https://plane.test", "acme", "t", transport=transport, sleep=slept.append, max_wait=180)
    with pytest.raises(PlaneError) as e:
        c.me()
    assert e.value.status == 429 and sum(slept) == 180 and len(calls) == 4


def test_paced_reads_heed_the_remaining_count():
    left = iter(["50", "5", "x"])

    def handle(req):
        return httpx.Response(200, headers={"X-RateLimit-Remaining": next(left), "X-RateLimit-Reset": "12"},
                              json={"id": "u1"})  # fmt: skip

    slept = []
    c = PlaneClient("https://plane.test", "acme", "t", transport=httpx.MockTransport(handle), sleep=slept.append,
                    rate_limit=60)  # fmt: skip
    c.me()
    assert slept == []  # unpaced: the count is ignored
    c.pace(after=100)
    c.me()  # 5 left of 60: wait for the window to reset
    c.me()  # an unreadable count changes nothing
    assert slept == [12.0]


# -- optional summaries (optional-summaries) -----------------------------------

ST = "plan: rev=0 estimate=3h start=2026-10-06 target=2026-10-09"


def test_closing_without_a_summary(fake, orch):
    it, w = started(fake, orch, "api")
    w.done(ref(it))  # Scenario "Closing without a summary"
    assert state_name(fake, it) == "Done" and comments_on(fake, it)[-1].startswith("<p>Done.</p><hr><p>agent=api")
    b = reviewed(fake, orch, name="B")
    orch.accept(ref(b))
    assert comments_on(fake, b)[-1].startswith("<p>Accepted.</p><hr>")
    c, w = started(fake, orch, "web", name="C")
    w.review(ref(c))
    assert state_name(fake, c) == "In Review" and comments_on(fake, c)[-1].startswith("<p>Ready for review.</p><hr>")


@pytest.mark.parametrize(("kind", "move", "needs"), [
    ("kind:bug", "done", "what was wrong"),
    ("kind:bug", "review", "what was wrong"),
    ("kind:deploy", "review", "commands run"),
])  # fmt: skip
def test_bugfix_and_deploy_need_a_summary(fake, orch, kind, move, needs):
    it, w = started(fake, orch, "api", labels=[orch.project("HOME").label_id(kind)])
    before = len(comments_on(fake, it))
    with pytest.raises(ProtocolRefusal) as e:  # Scenario "Bugfix closed without a summary"
        getattr(w, move)(ref(it), "  ")
    assert e.value.code == EXIT_REFUSED and needs in str(e.value)
    assert state_name(fake, it) == "In Progress" and len(comments_on(fake, it)) == before
    getattr(w, move)(ref(it), "Null check was missing; added it and a test.")
    assert "Null check was missing" in comments_on(fake, it)[-1]


def test_close_epic_needs_a_summary(fake, orch):
    epic = fake.seed_item(fake.pid, "Epic", "Backlog")
    fake.seed_item(fake.pid, "a", "Done", parent=epic["id"])
    with pytest.raises(ProtocolRefusal) as e:
        orch.close_epic(ref(epic), "")
    assert e.value.code == EXIT_REFUSED and "what the epic delivered" in str(e.value)
    assert state_name(fake, epic) == "Backlog" and comments_on(fake, epic) == []


def test_no_body_ends_in_a_statement(fake, orch):
    it, w = started(fake, orch, "api")
    before = len(comments_on(fake, it))
    for move in (lambda: w.done(ref(it), f"Shipped.\n\n{ST}"),  # Scenario "Statement at the end of a summary"
                 lambda: w.review(ref(it), ST),
                 lambda: w.block_human(ref(it), f"Which port?\n\n{ST}"),
                 lambda: w.note(ref(it), ST)):  # fmt: skip
        with pytest.raises(ProtocolRefusal) as e:
            move()
        assert e.value.code == EXIT_REFUSED and "plan or actual statement" in str(e.value)
    assert state_name(fake, it) == "In Progress" and len(comments_on(fake, it)) == before
    w.done(ref(it), f"{ST}\n\nThe estimate above held.")  # a statement earlier in the body is fine
    assert state_name(fake, it) == "Done"
    unheld = fake.seed_item(fake.pid, "U", "Ready")
    planned(orch, unheld)
    for move in (lambda: orch.plan(ref(unheld), "5h", reason=f"Scope grew.\n\n{ST}"),
                 lambda: w.log("HOME", "retro", "chore", started="2026-10-06 09:00", ended="2026-10-06 10:00",
                               summary=ST)):  # fmt: skip
        with pytest.raises(ProtocolRefusal) as e:
            move()
        assert e.value.code == EXIT_REFUSED and "plan or actual statement" in str(e.value)
    assert len(comments_on(fake, unheld)) == 1  # its first plan only
    r = reviewed(fake, orch, name="R")
    with pytest.raises(ProtocolRefusal):
        orch.reject(ref(r), "actual: started=2026-10-06T09:00-07:00 ended=2026-10-06T11:00-07:00")
    assert state_name(fake, r) == "In Review"
