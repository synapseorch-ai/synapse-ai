"""Which Synapse the CLI talks to, and with what key (D126).

**The origin** is `--url`, else `SYNAPSE_API_URL`, else this machine's backend
(`http://127.0.0.1:{port}`, the port resolved as `synapse start` resolves it).
It is an origin, as D124 set it for every client: the CLI appends `/api/v1`.
A URL that carries a path is taken as the API's base instead — the escape hatch
for a proxy that mounts it elsewhere.

**The key** is `--api-key`, else `SYNAPSE_API_KEY`. A remote Synapse needs one.
This machine's does not: being installed is enough, as it is in the browser.
With no key given and a loopback origin, the CLI uses a key of its own — minted
through the store on first use, named "Synapse CLI" so it is plain in Settings →
API keys why it is there, and kept in `~/.synapse/cli.json` (0600). Revoking it
rotates it: the next call mints another.

**The login gate.** When the install's own login is on, the browser asks for it
and so does the CLI: the key is not used or minted until `synapse login` has
succeeded, and the session lasts as the browser's does (7 days). This is parity
with the browser, not a wall against someone with a shell on the machine —
they hold `.env` and the database already.
"""
from __future__ import annotations

import contextlib
import functools
import ipaddress
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from synapse.client.errors import CliError

#: The name the CLI's own key carries in Settings → API keys.
KEY_NAME = "Synapse CLI"
API_PREFIX = "/api/v1"


def config_path() -> Path:
    override = os.getenv("SYNAPSE_CLI_CONFIG")
    return Path(override) if override else Path.home() / ".synapse" / "cli.json"


def _read_config() -> dict:
    try:
        return json.loads(config_path().read_text())
    except (OSError, ValueError):
        return {}


def _write_config(config: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    # Created 0600 rather than chmod-ed after: the key is never readable by others,
    # not even for the moment between the write and the chmod.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, path)


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _quietly(fn, *args):
    """Call a store helper with its notes sent to stderr.

    stdout is `--json`'s, and under `synapse mcp` it is the protocol's: a store
    that opens for the first time prints what it migrated.
    """
    with contextlib.redirect_stdout(sys.stderr):
        return fn(*args)


@functools.cache
def _install_settings() -> dict:
    """This machine's settings, read once per command."""
    from synapse import _store
    return _quietly(_store.load_settings)


def local_port() -> int:
    """The backend port, as `synapse start` resolves it: stored > env > 8765."""
    try:
        port = _install_settings().get("backend_port")
        if port:
            return int(port)
    except Exception:
        pass
    return int(os.getenv("SYNAPSE_BACKEND_PORT", "8765"))


def _login_settings() -> dict | None:
    """The install's login, when it is on and set up; None when it is off."""
    settings = _install_settings()
    if settings.get("login_enabled") and settings.get("login_username") and settings.get("login_password_hash"):
        return settings
    return None


def _session_user(token: str | None) -> str | None:
    if not token:
        return None
    from synapse import _store
    _store.prepare()
    from core.user_auth import verify_session_token
    return verify_session_token(token)


@dataclass
class Target:
    origin: str
    api_base: str
    api_key: str | None
    #: The key is the CLI's own for this machine, so a refused one can be replaced.
    managed: bool = False
    _config: dict = field(default_factory=dict, repr=False)

    @property
    def is_local(self) -> bool:
        return _is_loopback(urlsplit(self.origin).hostname)

    # ── the CLI's own key ────────────────────────────────────────────────────

    def _entry(self) -> dict:
        return self._config.setdefault("targets", {}).setdefault(self.origin, {})

    def require_session(self) -> None:
        """Refuse when this install's login is on and the CLI is not logged in."""
        login = _login_settings()
        if login is None:
            return
        if _session_user(self._entry().get("session")) != login["login_username"]:
            raise CliError("This Synapse has login turned on. Run `synapse login` first.")

    def ensure_key(self) -> None:
        """Use the CLI's stored key for this origin, minting one if there is none."""
        if self.api_key:
            return
        self.require_session()
        entry = self._entry()
        if entry.get("api_key"):
            self.api_key = entry["api_key"]
            return
        self.mint_key()

    def mint_key(self) -> None:
        """Mint the CLI's key through the store and keep it; a previous one is removed."""
        from synapse import _store
        _store.prepare()
        from core.api_keys import delete_api_key, generate_api_key

        entry = self._entry()
        old_id = entry.get("key_id")
        if old_id:
            with contextlib.suppress(Exception):
                _quietly(_store.run, lambda: delete_api_key(old_id))
        raw, record = _quietly(_store.run, lambda: generate_api_key(KEY_NAME))
        entry.update(api_key=raw, key_id=record["id"])
        _write_config(self._config)
        self.api_key = raw

    def forget_key(self) -> None:
        """Remove a minted key that the server refused, so none is left behind."""
        entry = self._entry()
        key_id = entry.pop("key_id", None)
        entry.pop("api_key", None)
        _write_config(self._config)
        if key_id:
            from synapse import _store
            from core.api_keys import delete_api_key
            with contextlib.suppress(Exception):
                _quietly(_store.run, lambda: delete_api_key(key_id))

    # ── the login session ───────────────────────────────────────────────────

    def save_session(self, token: str | None) -> None:
        entry = self._entry()
        if token:
            entry["session"] = token
        else:
            entry.pop("session", None)
        _write_config(self._config)


def resolve_origin(url: str | None = None) -> tuple[str, str]:
    """(origin, API base) from `--url`, else `SYNAPSE_API_URL`, else this machine's backend."""
    url = (url or os.getenv("SYNAPSE_API_URL") or "").strip()
    if not url:
        origin = f"http://127.0.0.1:{local_port()}"
        return origin, origin + API_PREFIX
    if "://" not in url:
        raise CliError(f"'{url}' is not a URL. Give an origin such as https://api.synapseorch.com.")
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    return origin, origin + (parts.path.rstrip("/") or API_PREFIX)


def resolve(url: str | None = None, api_key: str | None = None) -> Target:
    """The target a command talks to, from its flags, the environment and the defaults."""
    key = (api_key or os.getenv("SYNAPSE_API_KEY") or "").strip() or None
    origin, api_base = resolve_origin(url)
    target = Target(origin=origin, api_base=api_base, api_key=key, _config=_read_config())
    if key is None:
        if not target.is_local:
            raise CliError(
                f"{origin} needs an API key. Pass --api-key or set SYNAPSE_API_KEY."
            )
        target.managed = True
    return target
