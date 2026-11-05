"""`search` (0.4.0): matching, filters, order, output and cost, against FakePlane."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from plane_agent.protocol import EXIT_REFUSED, ProtocolRefusal, _fold, _snippet

from .conftest import agent
from .conftest import cli_runner as _cli
from .test_protocol import comments_on, ref


def lid(fake, name):
    return next(lb["id"] for lb in fake.labels[fake.pid] if lb["name"] == name)


def refs(hits):
    return [h["ref"] for h in hits]


def test_title_description_and_terms_across_fields(fake, orch):
    a = fake.seed_item(fake.pid, "Telegram replies", "Ready", description_html="<p>Uses <code>sendRichMessage</code></p>")
    fake.seed_item(fake.pid, "Other", "Ready")
    assert refs(orch.search("HOME", ["telegram"])) == [ref(a)]
    hit = orch.search("HOME", ["sendrichmessage"])[0]
    assert (hit["ref"], hit["match"]) == (ref(a), "description")
    assert "sendRichMessage" in hit["snippet"]
    both = orch.search("HOME", ["telegram", "sendRichMessage"])  # one term in each field
    assert refs(both) == [ref(a)] and both[0]["match"] == "description"
    assert orch.search("HOME", ["telegram", "absent"]) == []


def test_every_term_must_match_and_a_phrase_stays_whole(fake, orch):
    a = fake.seed_item(fake.pid, "rich messages for alerts", "Ready")
    fake.seed_item(fake.pid, "messages that are rich", "Ready")
    assert refs(orch.search("HOME", ["rich messages"])) == [ref(a)]
    assert len(orch.search("HOME", ["rich", "messages"])) == 2


def test_a_phrase_matches_across_a_line_break(fake, orch):
    a = fake.seed_item(fake.pid, "x", "Ready", description_html="<p>rich</p><p>messages</p>")
    assert refs(orch.search("HOME", ["rich   messages"])) == [ref(a)]


def test_accents_and_case(fake, orch):
    a = fake.seed_item(fake.pid, "Migración de Pi-hole", "Ready")
    b = fake.seed_item(fake.pid, "x", "Ready", description_html="<p>MIGRACION</p>")
    assert set(refs(orch.search("HOME", ["migracion"]))) == {ref(a), ref(b)}
    assert refs(orch.search("HOME", ["MIGRACIÓN", "pi-hole"])) == [ref(a)]


def test_ref_matches_only_whole(fake, orch):
    a = fake.seed_item(fake.pid, "first", "Ready")
    fake.seed_item(fake.pid, "second", "Ready")
    n = str(a["sequence_id"])
    assert refs(orch.search("HOME", [ref(a).lower()])) == [ref(a)]
    assert orch.search("HOME", [ref(a)])[0]["match"] == "ref"
    assert refs(orch.search("HOME", [n])) == [ref(a)]
    assert orch.search("HOME", ["home"]) == []  # the project key alone doesn't match every item


def test_image_placeholder_is_not_content(fake, orch):
    fake.seed_item(fake.pid, "x", "Ready", description_html='<image-component src="abc"></image-component>')
    assert orch.search("HOME", ["removed"]) == [] and orch.search("HOME", ["image"]) == []


def test_filters(fake, orch):
    feature, bug = lid(fake, "kind:feature"), lid(fake, "kind:bug")
    nh = lid(fake, "needs-human")
    a = fake.seed_item(fake.pid, "topic a", "Ready", labels=[feature])
    b = fake.seed_item(fake.pid, "topic b", "Done", labels=[bug])
    c = fake.seed_item(fake.pid, "topic c", "Blocked", labels=[bug, nh])
    assert set(refs(orch.search("HOME", ["topic"]))) == {ref(a), ref(b), ref(c)}  # closed items by default
    assert set(refs(orch.search("HOME", ["topic"], open_only=True))) == {ref(a), ref(c)}
    assert refs(orch.search("HOME", ["topic"], states=["done"])) == [ref(b)]
    assert set(refs(orch.search("HOME", ["topic"], states=["Done", "Ready"]))) == {ref(a), ref(b)}
    assert set(refs(orch.search("HOME", ["topic"], kinds=["bug"]))) == {ref(b), ref(c)}
    assert set(refs(orch.search("HOME", ["topic"], kinds=["bug", "feature"]))) == {ref(a), ref(b), ref(c)}
    assert refs(orch.search("HOME", ["topic"], labels=["kind:bug", "NEEDS-HUMAN"])) == [ref(c)]


def test_unknown_filter_names_are_refused(fake, orch):
    for kw, says in (({"states": ["Nope"]}, "no state 'Nope'"), ({"labels": ["nope"]}, "no label 'nope'")):
        with pytest.raises(ProtocolRefusal, match=says) as e:
            orch.search("HOME", ["x"], **kw)
        assert e.value.code == EXIT_REFUSED
    with pytest.raises(ProtocolRefusal, match="at least one term") as e:
        orch.search("HOME", ["  "])
    assert e.value.code == 2


def test_order_ref_title_description_open_first_then_recent(fake, orch):
    desc = fake.seed_item(fake.pid, "a", "Ready", description_html="<p>topic</p>")
    old = fake.seed_item(fake.pid, "topic old", "Ready")
    new = fake.seed_item(fake.pid, "topic new", "Ready")
    done = fake.seed_item(fake.pid, "topic done", "Done")
    old["updated_at"] = (fake.clock - timedelta(days=3)).isoformat()
    done["updated_at"] = (fake.clock + timedelta(days=3)).isoformat()
    assert refs(orch.search("HOME", ["topic"])) == [ref(new), ref(old), ref(done), ref(desc)]
    exact = fake.seed_item(fake.pid, "x", "Done")
    assert refs(orch.search("HOME", [ref(exact)])) == [ref(exact)]


def test_each_item_appears_once_and_cost_is_two_plus_pages(fake, orch, monkeypatch):
    it = fake.seed_item(fake.pid, "topic", "Ready")
    before = it["updated_at"]
    fresh = agent(fake, "reader")
    assert len(fresh.search("HOME", ["topic"])) == 1
    # the project list, states, labels, one page of items; nothing written
    assert fresh.client.calls == 4 and comments_on(fake, it) == []
    assert it["updated_at"] == before
    real = orch.client.iter_items
    monkeypatch.setattr(orch.client, "iter_items", lambda pid: [*real(pid), *real(pid)])
    assert len(orch.search("HOME", ["topic"])) == 1


def test_big_project_gets_a_notice(fake, orch, monkeypatch):
    monkeypatch.setattr("plane_agent.protocol.SEARCH_BIG", 1)
    fake.seed_item(fake.pid, "a", "Ready")
    fake.seed_item(fake.pid, "b", "Ready")
    orch.search("HOME", ["a"])
    assert "--open" in orch.notices[-1]


def test_snippet_and_fold():
    assert _fold("Straße é")[0] == "strasse e"
    long = "word " * 100 + "Migración" + " word" * 100
    s = _snippet(long, "migracion")
    assert "Migración" in s and s.startswith("…") and s.endswith("…") and len(s) <= 122
    assert _snippet("short text", "text") == "short text"


def test_cli_text_needs_no_session_and_counts_on_stderr(fake, orch, monkeypatch, capsys):
    from plane_agent import cli

    _cli(fake, monkeypatch)  # wires FakePlane in
    a = fake.seed_item(fake.pid, "topic a", "Ready", labels=[lid(fake, "kind:bug")], description_html="<p>z</p>")
    fake.seed_item(fake.pid, "b", "Done", description_html="<p>the topic here</p>")
    monkeypatch.delenv("PLANE_AGENT_SESSION", raising=False)
    assert cli.main(["--agent", "any-role", "--where", "x", "search", "--project", "HOME", "topic", "--limit", "1"]) == 0
    out, err = capsys.readouterr()
    assert out.splitlines() == [f"{ref(a)}  Ready  kind:bug  (title)  topic a  https://app.plane.so/acme/browse/{ref(a)}/"]
    assert "2 items match; showing 1" in err


def test_cli_json_snippet_terms_after_dashes_and_no_hits(fake, orch, monkeypatch, capsys):
    cli = _cli(fake, monkeypatch)
    a = fake.seed_item(fake.pid, "b", "Ready", description_html="<p>flag --force here</p>")
    assert cli("--json", "search", "--project", "HOME", "--", "--force") == 0
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert lines[0]["ref"] == ref(a) and lines[0]["match"] == "description" and "--force" in lines[0]["snippet"]
    assert lines[0]["url"].endswith(f"/browse/{ref(a)}/") and lines[-1] == {"total": 1}
    assert cli("search", "--project", "HOME", "absent") == 0
    out, err = capsys.readouterr()
    assert out == "" and "no items match" in err
    assert cli("--json", "search", "--project", "HOME", "absent") == 0
    assert capsys.readouterr().out.strip() == '{"total": 0}'
    assert cli("search", "--project", "HOME", "--state", "Nope", "x") == EXIT_REFUSED


def test_cli_defaults_to_the_configured_project(fake, orch, monkeypatch, capsys):
    cli = _cli(fake, monkeypatch)
    monkeypatch.setenv("PLANE_PROJECT", "HOME")
    a = fake.seed_item(fake.pid, "topic", "Ready")
    assert cli("search", "topic") == 0
    assert ref(a) in capsys.readouterr().out
