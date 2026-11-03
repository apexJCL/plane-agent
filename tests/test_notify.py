"""The notify hook: events, the JSON on stdin, no shell, and failures that only warn."""

from __future__ import annotations

import json
import shlex
import sys

from plane_agent.notify import notifier

from .conftest import cli_runner as _cli
from .test_protocol import ref, started
from .test_triage import _deploy, _sweep

KEYS = {"event", "ref", "title", "url", "agent", "session", "state", "text"}


def _script(tmp_path, body: str) -> str:
    f = tmp_path / "hook.py"
    f.write_text(body)
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(f))} {shlex.quote(str(tmp_path / 'got.jsonl'))}"


RECORD = "import sys\nopen(sys.argv[1], 'a').write(sys.stdin.read() + '\\n')\n"


def test_events_reach_the_hook(fake, orch, tmp_path):
    got = []
    orch.notify = got.append
    it, w = started(fake, orch, "api")
    w.notify = got.append
    w.block_human(ref(it), "Which port?")
    dep, d = _deploy(fake, orch)  # its worker has no hook: nothing pushed
    d.notify = got.append
    other, w3 = started(fake, orch, "ops", name="deploy 2", labels=[orch.project("HOME").label_id("kind:deploy")])
    w3.notify = got.append
    w3.review(ref(other), "ran it")
    wt, w4 = started(fake, orch, "web", name="wait")
    w4.block_wait(ref(wt), "the ISP", until="2026-10-06")
    fake.tick(days=1)
    _sweep(orch)
    assert [(e["event"], e["ref"]) for e in got] == [("needs_human", ref(it)), ("deploy_review", ref(other)),
                                                     ("wait_expired", ref(wt))]  # fmt: skip
    assert all(set(e) == KEYS for e in got)
    assert got[0]["url"] == f"https://plane.test/acme/browse/{ref(it)}/" and got[0]["state"] == "Blocked"
    assert got[2]["agent"] == "orchestrator" and got[2]["state"] == "Ready" and got[1]["session"] == "s-ops"
    assert ref(dep)


def test_hook_gets_json_on_stdin_without_a_shell(tmp_path):
    push = notifier(_script(tmp_path, RECORD) + " '; touch pwned'")
    push({"event": "needs_human", "ref": "HOME-1"})
    line = (tmp_path / "got.jsonl").read_text().splitlines()[0]
    assert json.loads(line) == {"event": "needs_human", "ref": "HOME-1"} and not (tmp_path / "pwned").exists()


def test_hook_failures_only_warn(tmp_path, capsys):
    notifier(_script(tmp_path, "import sys\nsys.exit('bot API said 401')\n"))({"event": "x", "ref": "HOME-1"})
    assert "notify_command exited 1 for x on HOME-1: bot API said 401" in capsys.readouterr().err
    notifier(_script(tmp_path, "import time\ntime.sleep(5)\n"), timeout=0.2)({"event": "x", "ref": "HOME-1"})
    assert "notify_command failed for x on HOME-1" in capsys.readouterr().err
    notifier("/no/such/hook")({"event": "x", "ref": "HOME-2"})
    assert "notify_command failed" in capsys.readouterr().err


def test_cli_runs_the_user_files_hook_and_keeps_the_exit_code(fake, orch, monkeypatch, capsys, tmp_path):
    run = _cli(fake, monkeypatch)
    hook = _script(tmp_path, RECORD)
    (tmp_path / "user.toml").write_text(f"notify_command = {json.dumps(hook)}\n")
    it = fake.seed_item(fake.pid, "a", "Ready")
    assert run("block", ref(it), "--wait", "x", "--until", "2026-10-06") == 0
    fake.tick(days=1)
    assert run("queue", "HOME") == 0
    assert json.loads((tmp_path / "got.jsonl").read_text().splitlines()[0])["event"] == "wait_expired"
    (tmp_path / "user.toml").write_text('notify_command = "/no/such/hook"\n')
    it2 = fake.seed_item(fake.pid, "b", "Ready")
    assert run("block", ref(it2), "--wait", "y", "--until", "2026-10-07") == 0
    fake.tick(days=1)
    capsys.readouterr()
    assert run("queue", "HOME") == 0 and "notify_command failed" in capsys.readouterr().err
