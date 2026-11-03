"""Shared fixtures: a FakePlane with the operator and the one `claude` agent account, and a bootstrapped orchestrator."""

from __future__ import annotations

import pytest

from plane_agent import config as conf
from plane_agent.client import PlaneClient
from plane_agent.protocol import Agent

from .fakeplane import FakePlane

TZ = "America/Los_Angeles"  # the agents' "today"; the fake's clock starts 2026-10-06 12:00 UTC, 05:00 here


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """No test reads the developer's own settings files or PLANE_* environment."""
    for name in ("PLANE_BASE_URL", "PLANE_WORKSPACE_SLUG", "PLANE_PROJECT", "PLANE_TZ", "PLANE_RATE_LIMIT",
                 "PLANE_API_KEY", "XDG_CONFIG_HOME", "PLANE_AGENT_SESSION", "PLANE_AGENT"):  # fmt: skip
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PLANE_AGENT_CONFIG", str(tmp_path / "user.toml"))
    monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "acme")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(conf, "_keyring", lambda account: None)  # never the real keyring


@pytest.fixture
def fake():
    f = FakePlane()
    for name in ("operator", "claude"):
        f.add_user(name)
    f.pid = f.add_project("HOME")
    return f


def agent(fake: FakePlane, slug: str, account: str = "claude", change: str = "", now=None, tz=TZ,
          **client_kw) -> Agent:  # fmt: skip
    client = PlaneClient("https://plane.test", "acme", f"tok-{account}", transport=fake.transport(), **client_kw)
    return Agent(client, slug, session=f"s-{slug}", where="repo@main", change=change,
                 sleep=lambda s: None, now=now or (lambda: fake.clock), tz=tz)  # fmt: skip


@pytest.fixture
def orch(fake):
    a = agent(fake, "orchestrator")
    a.bootstrap_project("HOME")
    return a


def cli_runner(fake: FakePlane, monkeypatch, slug: str = "orchestrator"):
    """`plane-agent --agent SLUG …` against the fake, on its clock."""
    from plane_agent import cli

    monkeypatch.setenv("PLANE_TZ", TZ)
    monkeypatch.setattr(cli.conf, "resolve_token", lambda _c, _f: "tok-claude")
    monkeypatch.setattr(cli, "PlaneClient", lambda *a, **k: PlaneClient(*a, **k, transport=fake.transport()))
    real = cli.Agent
    monkeypatch.setattr(cli, "Agent", lambda *a, **k: real(*a, **k, now=lambda: fake.clock))
    return lambda *argv: cli.main(["--agent", slug, "--session", "t", "--where", "x", *argv])
