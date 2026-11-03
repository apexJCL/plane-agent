"""Where plane-agent finds its instance, workspace, project, time zone, rate limit and token.

Settings, highest precedence first:
  1. environment: PLANE_BASE_URL, PLANE_WORKSPACE_SLUG, PLANE_PROJECT, PLANE_TZ, PLANE_RATE_LIMIT
  2. the project file: the nearest `.plane-agent.toml` from the current directory up (commit it; no secrets)
  3. the user file: $PLANE_AGENT_CONFIG, else $XDG_CONFIG_HOME/plane-agent/config.toml, else
     ~/.config/plane-agent/config.toml
  4. defaults: Plane Cloud (https://api.plane.so), 60 calls a minute

A token is always bound to the instance it belongs to, so a repository's project file can pick an instance but
can never supply a token or send yours somewhere else. In order:
  1. --token-file FILE (explicit, per command)
  2. PLANE_API_KEY, only together with PLANE_BASE_URL in the environment
  3. the user file's [hosts."<host>"] entry: token_file, or token_command (its stdout)
  4. the system keyring, service "plane-agent", account = the entry's keychain_account or else the host:
     macOS Keychain (`security`), or the Secret Service on Linux (`secret-tool`)
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

CLOUD_URL = "https://api.plane.so"
DEFAULT_RATE_LIMIT = 60  # Plane Cloud's limit, and self-hosted Plane's default API_KEY_RATE_LIMIT
PROJECT_FILE = ".plane-agent.toml"
KEYRING_SERVICE = "plane-agent"

SETTINGS = ("base_url", "workspace", "project", "timezone", "rate_limit")
HOST_KEYS = ("token_file", "token_command", "keychain_account", "rate_limit")
# Top-level keys only your user file may set: one runs a command, the other relaxes a safety check, and a project
# file comes with whatever repository you cloned.
USER_ONLY = ("require_session", "notify_command")
ENV = {
    "base_url": "PLANE_BASE_URL",
    "workspace": "PLANE_WORKSPACE_SLUG",
    "project": "PLANE_PROJECT",
    "timezone": "PLANE_TZ",
    "rate_limit": "PLANE_RATE_LIMIT",
}


class ConfigError(Exception):
    """A configuration the CLI can't act on; exit 2 with the message."""


@dataclass
class Config:
    base_url: str = CLOUD_URL
    workspace: str = ""
    project: str = ""
    timezone: str = ""
    rate_limit: int = DEFAULT_RATE_LIMIT
    require_session: bool = True  # every write needs PLANE_AGENT_SESSION or --session
    notify_command: str = ""  # run on events that need the operator; event JSON on stdin
    host_entry: dict = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)  # setting -> where it came from
    project_file: Path | None = None
    user_file: Path | None = None

    @property
    def host(self) -> str:
        return host_of(self.base_url)

    def require(self, *names: str) -> None:
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            hints = ", ".join(f"{n} ({ENV[n]})" for n in missing)
            raise ConfigError(f"missing {hints}: run `plane-agent init` or set them in the environment")


def host_of(url: str) -> str:
    parts = urlsplit(url if "://" in url else f"https://{url}")
    if not parts.hostname:
        raise ConfigError(f"base_url {url!r} has no host")
    return parts.hostname + (f":{parts.port}" if parts.port else "")


def user_file(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if p := env.get("PLANE_AGENT_CONFIG"):
        return Path(p).expanduser()
    base = Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else Path.home() / ".config"
    return base / "plane-agent" / "config.toml"


def find_project_file(start: Path | None = None) -> Path | None:
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if (d / PROJECT_FILE).is_file():
            return d / PROJECT_FILE
    return None


def _read(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None


def _check_keys(path: Path, data: dict, allowed: tuple[str, ...], where: str) -> None:
    secretish = {"token", "api_key", "token_file", "token_command", "keychain_account"}
    for k in data:
        if k in allowed:
            continue
        if k in secretish:
            raise ConfigError(f"{path}: `{k}` can't go in {where}; tokens belong in your user file's "
                              f'[hosts."<host>"] entry or the keyring')  # fmt: skip
        if k in USER_ONLY:
            raise ConfigError(f"{path}: `{k}` can't go in {where}; set it at the top level of your user file "
                              f"({user_file()})")  # fmt: skip
        raise ConfigError(f"{path}: unknown setting `{k}` (known: {', '.join(allowed)})")


def _rate(value: object, origin: str) -> int:
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        n = 0
    if n < 1:
        raise ConfigError(f"rate_limit from {origin} must be a positive whole number of calls a minute")
    return n


def load(env: dict[str, str] | None = None, cwd: Path | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    cfg = Config()
    layers: list[tuple[str, dict]] = []

    uf = user_file(env)
    user: dict = {}
    if uf.is_file():
        cfg.user_file = uf
        user = _read(uf)
        hosts = user.get("hosts", {})
        if not isinstance(hosts, dict):
            raise ConfigError(f'{uf}: `hosts` must be a table of [hosts."<host>"] entries')
        _check_keys(uf, {k: v for k, v in user.items() if k != "hosts"}, SETTINGS + USER_ONLY,
                    "the user file's top level")  # fmt: skip
        if not isinstance(user.get("require_session", True), bool):
            raise ConfigError(f"{uf}: `require_session` must be true or false")
        if not isinstance(user.get("notify_command", ""), str):
            raise ConfigError(f"{uf}: `notify_command` must be a string (a command line, run without a shell)")
        cfg.require_session = user.get("require_session", True)
        cfg.notify_command = user.get("notify_command", "")
        for h, entry in hosts.items():
            _check_keys(uf, entry, HOST_KEYS, f'[hosts."{h}"]')
        layers.append((str(uf), {k: v for k, v in user.items() if k not in ("hosts", *USER_ONLY)}))

    pf = find_project_file(cwd)
    if pf:
        cfg.project_file = pf
        data = _read(pf)
        _check_keys(pf, data, SETTINGS, "a project file")
        layers.append((str(pf), data))

    layers.append(("environment", {k: env[v] for k, v in ENV.items() if env.get(v)}))
    for origin, data in layers:
        for k in SETTINGS:
            if k in data:
                setattr(cfg, k, data[k])
                cfg.sources[k] = origin

    cfg.base_url = str(cfg.base_url).rstrip("/")
    cfg.project = str(cfg.project).upper()
    cfg.host_entry = dict(user.get("hosts", {}).get(cfg.host, {}))
    # The server's limit is a fact about the host: the host entry beats a project file, the environment beats both.
    if "rate_limit" in cfg.host_entry and cfg.sources.get("rate_limit") != "environment":
        cfg.rate_limit = cfg.host_entry["rate_limit"]
        cfg.sources["rate_limit"] = f'{uf} [hosts."{cfg.host}"]'
    cfg.rate_limit = _rate(cfg.rate_limit, cfg.sources.get("rate_limit", "the default"))
    return cfg


# -- token -------------------------------------------------------------------


def _keyring(account: str) -> str | None:
    if sys.platform == "darwin":
        cmd = ["security", "find-generic-password", "-s", KEYRING_SERVICE, "-a", account, "-w"]
    else:
        cmd = ["secret-tool", "lookup", "service", KEYRING_SERVICE, "account", account]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def keyring_store_hint(account: str) -> str:
    if sys.platform == "darwin":
        return f"security add-generic-password -U -s {KEYRING_SERVICE} -a {account} -w"
    return f"secret-tool store --label='plane-agent {account}' service {KEYRING_SERVICE} account {account}"


def _read_token_file(path: str) -> str:
    try:
        return Path(path).expanduser().read_text().strip()
    except OSError as e:
        raise ConfigError(f"token file {path}: {e.strerror}") from None


def token_source(cfg: Config, token_file: str | None, env: dict[str, str] | None = None) -> str:
    """Where the token would come from, without reading it (for `plane-agent config`)."""
    env = os.environ if env is None else env
    if token_file:
        return f"--token-file {token_file}"
    if env.get("PLANE_API_KEY") and env.get("PLANE_BASE_URL"):
        return "PLANE_API_KEY (with PLANE_BASE_URL)"
    e = cfg.host_entry
    if e.get("token_file"):
        return f'token_file {e["token_file"]} ([hosts."{cfg.host}"])'
    if e.get("token_command"):
        return f'token_command ([hosts."{cfg.host}"])'
    return f"keyring {KEYRING_SERVICE}/{e.get('keychain_account') or cfg.host}"


def resolve_token(cfg: Config, token_file: str | None, env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    if token_file:
        return _read_token_file(token_file)
    if env.get("PLANE_API_KEY"):
        if env.get("PLANE_BASE_URL"):
            return env["PLANE_API_KEY"].strip()
        raise ConfigError("PLANE_API_KEY is only used together with PLANE_BASE_URL in the environment, so a "
                          "project file can't send it to another instance")  # fmt: skip
    e = cfg.host_entry
    if e.get("token_file"):
        return _read_token_file(e["token_file"])
    if cmd := e.get("token_command"):
        try:
            out = subprocess.run(shlex.split(cmd), capture_output=True, text=True, check=True)
        except (FileNotFoundError, subprocess.CalledProcessError) as err:
            raise ConfigError(f'token_command for {cfg.host} failed: {err}') from None
        if tok := out.stdout.strip():
            return tok
        raise ConfigError(f"token_command for {cfg.host} printed nothing")
    account = e.get("keychain_account") or cfg.host
    if tok := _keyring(account):
        return tok
    raise ConfigError(
        f"no token for {cfg.host}. Store one in the keyring with `{keyring_store_hint(account)}`, add a "
        f'[hosts."{cfg.host}"] token_file or token_command to {cfg.user_file or user_file()}, or set '
        "PLANE_BASE_URL and PLANE_API_KEY together"
    )


# -- init --------------------------------------------------------------------


def _toml_value(v: object) -> str:
    if isinstance(v, int):
        return str(v)
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def render_settings(values: dict[str, object]) -> str:
    lines = ["# plane-agent settings. No secrets here: tokens live in the keyring or your user file."]
    lines += [f"{k} = {_toml_value(v)}" for k, v in values.items() if v not in (None, "")]
    return "\n".join(lines) + "\n"
