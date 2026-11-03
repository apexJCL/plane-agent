"""Settings files, precedence, token binding, init/config verbs and the default project."""

from __future__ import annotations

import json

import httpx
import pytest

from plane_agent import cli
from plane_agent import config as conf
from plane_agent.client import PlaneClient

from .conftest import TZ


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def user(tmp_path):
    return tmp_path / "user.toml"  # PLANE_AGENT_CONFIG points here (conftest)


def env(**kw):
    return {"PLANE_AGENT_CONFIG": kw.pop("cfg"), **kw}


def test_defaults_are_plane_cloud(tmp_path, user):
    cfg = conf.load(env(cfg=str(user)), cwd=tmp_path)
    assert (cfg.base_url, cfg.rate_limit, cfg.host) == ("https://api.plane.so", 60, "api.plane.so")
    with pytest.raises(conf.ConfigError, match="workspace"):
        cfg.require("workspace")


def test_precedence_env_project_user(tmp_path, user):
    write(user, 'base_url = "https://user.example"\nworkspace = "u"\nproject = "usr"\ntimezone = "UTC"\n')
    repo = tmp_path / "repo" / "sub"
    repo.mkdir(parents=True)
    write(tmp_path / "repo" / ".plane-agent.toml", 'base_url = "https://plane.example.com/"\nworkspace = "acme"\n')
    cfg = conf.load(env(cfg=str(user), PLANE_WORKSPACE_SLUG="override"), cwd=repo)
    assert cfg.base_url == "https://plane.example.com" and cfg.workspace == "override"
    assert cfg.project == "USR" and cfg.timezone == "UTC"
    assert cfg.sources["workspace"] == "environment" and cfg.sources["base_url"].endswith(".plane-agent.toml")


@pytest.mark.parametrize("key", ["token", "api_key", "token_file", "token_command", "keychain_account"])
def test_project_file_cannot_hold_or_aim_a_token(tmp_path, user, key):
    write(tmp_path / ".plane-agent.toml", f'{key} = "x"\n')
    with pytest.raises(conf.ConfigError, match="can't go in a project file"):
        conf.load(env(cfg=str(user)), cwd=tmp_path)


def test_unknown_setting_is_named(tmp_path, user):
    write(tmp_path / ".plane-agent.toml", 'workspce = "x"\n')
    with pytest.raises(conf.ConfigError, match="unknown setting `workspce`"):
        conf.load(env(cfg=str(user)), cwd=tmp_path)


def test_token_is_bound_to_the_host(tmp_path, user, monkeypatch):
    tok = write(tmp_path / "t.token", "secret-1\n")
    write(user, f'[hosts."plane.example.com"]\ntoken_file = "{tok}"\nrate_limit = 300\n')
    looked_up = []
    monkeypatch.setattr(conf, "_keyring", lambda account: looked_up.append(account))
    write(tmp_path / ".plane-agent.toml", 'base_url = "https://plane.example.com"\nrate_limit = 30\n')
    cfg = conf.load(env(cfg=str(user)), cwd=tmp_path)
    assert conf.resolve_token(cfg, None, {}) == "secret-1" and cfg.rate_limit == 300  # host entry beats project
    # A repository pointing elsewhere gets no token from that entry; only the keyring item for its own host.
    write(tmp_path / ".plane-agent.toml", 'base_url = "https://evil.example"\n')
    cfg = conf.load(env(cfg=str(user)), cwd=tmp_path)
    with pytest.raises(conf.ConfigError, match="no token for evil.example"):
        conf.resolve_token(cfg, None, {})
    assert looked_up == ["evil.example"]


def test_api_key_needs_base_url_in_the_environment(tmp_path, user):
    cfg = conf.load(env(cfg=str(user)), cwd=tmp_path)
    with pytest.raises(conf.ConfigError, match="only used together with PLANE_BASE_URL"):
        conf.resolve_token(cfg, None, {"PLANE_API_KEY": "k"})
    assert conf.resolve_token(cfg, None, {"PLANE_API_KEY": "k", "PLANE_BASE_URL": "https://x"}) == "k"


def test_keychain_account_and_token_command(tmp_path, user, monkeypatch):
    write(user, '[hosts."api.plane.so"]\nkeychain_account = "claude"\n[hosts."b.example"]\ntoken_command = "echo tok-b"\n')
    monkeypatch.setattr(conf, "_keyring", lambda account: f"kc-{account}")
    cfg = conf.load(env(cfg=str(user)), cwd=tmp_path)
    assert conf.resolve_token(cfg, None, {}) == "kc-claude"
    assert conf.token_source(cfg, None, {}) == "keyring plane-agent/claude"
    cfg = conf.load(env(cfg=str(user), PLANE_BASE_URL="https://b.example"), cwd=tmp_path)
    assert conf.resolve_token(cfg, None, {}) == "tok-b"


def test_bad_rate_limit(tmp_path, user):
    with pytest.raises(conf.ConfigError, match="rate_limit"):
        conf.load(env(cfg=str(user), PLANE_RATE_LIMIT="lots"), cwd=tmp_path)


def test_rate_limit_reset_header():
    hits = []

    def handle(req):
        hits.append(1)
        if len(hits) == 1:
            return httpx.Response(429, headers={"X-RateLimit-Reset": "1000000030"})
        return httpx.Response(200, json={"ok": True})

    slept = []
    c = PlaneClient("https://x", "acme", "t", transport=httpx.MockTransport(handle), sleep=slept.append,
                    wall=lambda: 1000000000.0)  # fmt: skip
    assert c.me() == {"ok": True} and slept == [30.0]


def test_pace_follows_the_rate_limit():
    c = PlaneClient("https://x", "acme", "t", rate_limit=60)
    c.pace()
    assert c._pace_after == 24 and abs(c._gap - 60 / 50) < 1e-9


def test_init_writes_project_file_and_refuses_overwrite(tmp_path, capsys):
    argv = ["init", "--url", "https://plane.example.com/", "--workspace", "acme", "--project", "web",
            "--timezone", TZ, "--rate-limit", "300"]  # fmt: skip
    assert cli.main(argv) == 0
    text = (tmp_path / ".plane-agent.toml").read_text()
    assert 'base_url = "https://plane.example.com"' in text and 'project = "WEB"' in text and "rate_limit = 300" in text
    assert "plane.example.com" in capsys.readouterr().out  # the keyring hint names the host
    assert cli.main(argv) == 5
    assert cli.main([*argv, "--force"]) == 0
    assert cli.main(["init", "--url", "https://x", "--workspace", "a", "--timezone", "Mars/Base"]) == 2


def test_config_verb_needs_no_slug_or_token(tmp_path, capsys):
    write(tmp_path / ".plane-agent.toml", 'base_url = "https://plane.example.com"\nproject = "WEB"\n')
    assert cli.main(["--json", "config"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["settings"]["project"] == "WEB" and out["token"] == "keyring plane-agent/plane.example.com"


def test_default_project_and_create_words(fake, orch, monkeypatch, tmp_path, capsys):
    from .test_planning import _cli

    run = _cli(fake, monkeypatch)
    assert run("queue") == 2 and "no project" in capsys.readouterr().err
    write(tmp_path / ".plane-agent.toml", 'project = "home"\n')
    assert run("queue") == 0
    assert run("create", "Fix the header", "--kind", "bug") == 0
    assert run("create", "HOME", "Another", "--kind", "chore") == 0
    assert run("create", "HOME", "Unquoted", "title", "--kind", "chore") == 2
    assert {"Fix the header", "Another"} <= {i["name"] for i in fake.items.values()}


def test_where_outside_git_is_the_directory_name_only(tmp_path):
    d = tmp_path / "scratch"
    d.mkdir()
    import os

    os.chdir(d)
    assert cli._where() == "scratch"


def test_user_only_keys_are_refused_in_a_project_file(tmp_path):
    (tmp_path / ".plane-agent.toml").write_text('notify_command = "curl evil"\n')
    with pytest.raises(conf.ConfigError, match="user file"):
        conf.load(env={"PLANE_AGENT_CONFIG": str(tmp_path / "none.toml")}, cwd=tmp_path)
    (tmp_path / ".plane-agent.toml").write_text("require_session = false\n")
    with pytest.raises(conf.ConfigError, match="user file"):
        conf.load(env={"PLANE_AGENT_CONFIG": str(tmp_path / "none.toml")}, cwd=tmp_path)


def test_user_file_sets_require_session_and_notify_command(tmp_path):
    uf = tmp_path / "user.toml"
    uf.write_text('require_session = false\nnotify_command = "notify-me --json"\n')
    cfg = conf.load(env={"PLANE_AGENT_CONFIG": str(uf)}, cwd=tmp_path)
    assert cfg.require_session is False and cfg.notify_command == "notify-me --json"
    assert conf.load(env={"PLANE_AGENT_CONFIG": str(tmp_path / "none.toml")}, cwd=tmp_path).require_session is True
    uf.write_text('require_session = "no"\n')
    with pytest.raises(conf.ConfigError, match="true or false"):
        conf.load(env={"PLANE_AGENT_CONFIG": str(uf)}, cwd=tmp_path)


def test_init_maps_the_cloud_app_host_and_says_cloud_is_untested(tmp_path, capsys):
    assert cli.main(["init", "--url", "https://app.plane.so", "--workspace", "acme"]) == 0
    assert 'base_url = "https://api.plane.so"' in (tmp_path / ".plane-agent.toml").read_text()
    err = capsys.readouterr().err
    assert "its API is https://api.plane.so" in err and "Plane Cloud is untested" in err
    assert cli.main(["config"]) == 0 and "Plane Cloud is untested" in capsys.readouterr().err
    write(tmp_path / ".plane-agent.toml", 'base_url = "https://plane.example.com"\n')
    assert cli.main(["config"]) == 0
    out = capsys.readouterr()
    assert "untested" not in out.err and "require_session=true; notify_command=-" in out.out
