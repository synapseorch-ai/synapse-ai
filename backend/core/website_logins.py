"""Website logins: a site's session, kept for runs to use (BRW2, D330, F1079; DESIGN §3.4).

The person logs in to a site in the browser's pane, in their own turn; the agent never sees or types a password. The
browser component (BRW1) then hands over Playwright's storage state, the site's cookies and localStorage, which is as
good as the password while it lasts. Kept here so a later run starts logged in.

How it is kept:

* **Encrypted.** AES-GCM, one fresh nonce per write. Everything else the record says (its id, owner, sites and both
  dates) is bound to the ciphertext as associated data, so a record edited on disk, a moved expiry included, reads as
  no login rather than as someone else's or as one that lasts longer.
* **The key is not in the store.** A 32-byte key in ``SYNAPSE_WEBSITE_LOGINS_KEY`` (base64), else a file under the
  install's ``var/keys/`` made on first use, readable by its owner only (a looser file is warned about). It is
  written whole and linked into place, so a reader racing its maker never sees half a key. The logins themselves live
  in the tenant's durable directory in the blob store, so a copy of the store alone opens nothing.
* **For a while.** Until the last of its cookies expires, and never more than 30 days. Past that a login reads as
  none and is removed: the run hands over to its owner to log in again.
* **Never in a log or an event.** Only `list_logins` leaves this module with anything but the state itself, and it
  carries the id, owner, sites and dates.

Cloud keeps its own (`browser_logins`, encrypted with its envelope; BRW3) behind the same two calls the component's
client makes: `keep` and `state_for`.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_log = logging.getLogger(__name__)

KEY_ENV = "SYNAPSE_WEBSITE_LOGINS_KEY"
#: The longest a login is kept, whatever its cookies say.
MAX_AGE_S = 30 * 86_400

_ID = re.compile(r"^wl_[0-9a-f]{24}$")
_FIELDS = ("id", "owner", "sites", "created_at", "expires_at")


def key_path() -> Path:
    """Where this install's key is kept when the environment does not give one."""
    from core.paths import var_root

    return var_root() / "keys" / "website-logins.key"


def _key() -> bytes:
    given = (os.getenv(KEY_ENV) or "").strip()
    if given:
        key = base64.b64decode(given)
        if len(key) != 32:
            raise ValueError(f"{KEY_ENV} must be 32 bytes, base64-encoded.")
        return key
    path = key_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written whole beside it, then linked into place: the link fails if another first write got there first,
        # and nobody ever reads a key file still being written. Both then use the one that is there.
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(base64.b64encode(AESGCM.generate_key(bit_length=256)))
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(tmp, path)
            except FileExistsError:
                pass
            except OSError:
                # No hard links here (FAT, some network mounts): moved into place instead, unless one appeared.
                if not path.exists():
                    os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
    if os.name == "posix" and path.stat().st_mode & 0o077:
        _log.warning("The website logins key at %s is readable by others; it should be readable by its owner only "
                     "(chmod 600).", path)
    key = base64.b64decode(path.read_bytes().strip())
    if len(key) != 32:
        raise ValueError(f"The website logins key at {path} is not a 32-byte key.")
    return key


def _dir() -> Path:
    from core.runtime_dirs import tenant_dir

    return tenant_dir("website_logins")


def _file(login_ref: str) -> Path | None:
    return _dir() / f"{login_ref}.json" if _ID.match(login_ref or "") else None


def _aad(record: dict) -> bytes:
    return json.dumps({k: record[k] for k in _FIELDS}, sort_keys=True).encode()


def _expiry(state: dict, now: float) -> float:
    """The last of the cookies' own expiries, or 30 days, whichever comes first. A session cookie (-1) sets none."""
    ends = [float(c["expires"]) for c in state.get("cookies") or []
            if isinstance(c, dict) and isinstance(c.get("expires"), (int, float)) and c["expires"] > now]
    return min(now + MAX_AGE_S, max(ends)) if ends else now + MAX_AGE_S


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None


def _write(path: Path, record: dict) -> None:
    tmp = path.with_suffix(f".{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(record), "utf-8")
    os.replace(tmp, path)


def _keep(login_ref: str | None, state: dict, sites: list[str], owner: str) -> str:
    ref = login_ref if login_ref and _ID.match(login_ref) else f"wl_{secrets.token_hex(12)}"
    path = _file(ref)
    assert path is not None
    now = time.time()
    had = _read(path) if path.exists() else None
    record: dict[str, Any] = {
        "id": ref,
        "owner": owner,
        "sites": list(dict.fromkeys(sites)),
        "created_at": (had or {}).get("created_at", now),
        "expires_at": _expiry(state, now),
    }
    nonce = os.urandom(12)
    sealed = AESGCM(_key()).encrypt(nonce, json.dumps(state).encode(), _aad(record))
    record["nonce"] = base64.b64encode(nonce).decode()
    record["sealed"] = base64.b64encode(sealed).decode()
    _write(path, record)
    return ref


def _state_for(login_ref: str) -> dict | None:
    path = _file(login_ref)
    if path is None or not path.exists():
        return None
    record = _read(path)
    if not record or record.get("id") != login_ref:
        return None
    if float(record.get("expires_at", 0)) <= time.time():
        path.unlink(missing_ok=True)
        return None
    try:
        plain = AESGCM(_key()).decrypt(base64.b64decode(record["nonce"]), base64.b64decode(record["sealed"]),
                                       _aad(record))
    except (InvalidTag, KeyError, ValueError, TypeError):
        return None
    return json.loads(plain)


def _list() -> list[dict]:
    now = time.time()
    rows = []
    for path in sorted(_dir().glob("wl_*.json")):
        record = _read(path)
        if not record or not _ID.match(str(record.get("id", ""))):
            continue
        if float(record.get("expires_at", 0)) <= now:
            path.unlink(missing_ok=True)
            continue
        rows.append({k: record.get(k) for k in _FIELDS})
    return sorted(rows, key=lambda r: r["created_at"] or 0, reverse=True)


def _forget(login_ref: str) -> bool:
    path = _file(login_ref)
    if path is None or not path.exists():
        return False
    path.unlink(missing_ok=True)
    return True


async def keep(login_ref: str | None, *, state: dict, sites: list[str], owner: str) -> str:
    """Keep a site's session, encrypted; returns its id. A known `login_ref` is replaced (logging in again)."""
    return await asyncio.to_thread(_keep, login_ref, state, sites, owner)


async def state_for(login_ref: str) -> dict | None:
    """The session kept under `login_ref`, decrypted; None once expired, forgotten, unreadable or never kept."""
    return await asyncio.to_thread(_state_for, login_ref)


async def list_logins() -> list[dict]:
    """Every login still in date, newest first: its id, owner, sites and dates, never its session."""
    return await asyncio.to_thread(_list)


async def forget(login_ref: str) -> bool:
    """Remove a login. False when there was none."""
    return await asyncio.to_thread(_forget, login_ref)
