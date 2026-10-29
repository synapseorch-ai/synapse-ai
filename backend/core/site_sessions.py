"""Kept sessions: a site's signed-in session, kept for agents and runs to use (BRW1, D328, F1138; DESIGN §v5.1).

"Keep me signed in to example.com for your agents. Synapse keeps the site's session, never your password." The
person signs in to a site in the browser's pane, in their own turn; the agent never sees or types a password. The
browser component then hands over Playwright's storage state for that site, its cookies and localStorage, which is
as good as the password while it lasts. Kept here so a later chat or run starts signed in. (BRW2 built it as
"website logins", F1079; renamed with Naveen's word, 10-28.)

What one is: one **site**, a registrable domain (`core.sites`), with its owner, and created / last used / expiry
dates. Several may be picked for an agent, a chat, a run or a step; code matches each to its site, and the model never
chooses one.

How it is kept:

* **Encrypted.** AES-GCM, one fresh nonce per write. Its id, owner, site and both dates are bound to the ciphertext
  as associated data, so a record edited on disk, a moved expiry included, reads as no session rather than as someone
  else's or as one that lasts longer. A record kept before the rename binds `sites` (a list) instead, and is read as
  the registrable domain of its first host; keeping it again writes it the new way under the same id.
* **The key is not in the store.** A 32-byte key in ``SYNAPSE_WEBSITE_LOGINS_KEY`` (base64), else a file under the
  install's ``var/keys/`` made on first use, readable by its owner only. Both keep their old names, so the sessions
  kept before the rename still open. It is written whole and linked into place, so a reader racing its maker never
  sees half a key.
* **For a while.** Until the last of its cookies expires, and never more than 30 days. Past that its session is
  dropped and the record stays, marked expired, so the owner can sign in again into the same one (and every pick of
  it keeps working); Remove deletes it.
* **Only for its owner.** `state_for(id, for_user=…)` opens a session only for the person who kept it. Sharing is
  cloud's (BRW3's `site_sessions`); here one person uses the install.
* **Never in a log or an event.** Only `list_sessions` leaves this module with anything but the state itself, and it
  carries the id, owner, site and dates.

Cloud keeps its own behind the same calls the browser's client makes: `keep` and `state_for`.
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

#: New ids are `ss_`; those kept before the rename (`wl_`) are still read, and kept again under the same id.
_ID = re.compile(r"^(?:ss|wl)_[0-9a-f]{24}$")
_FIELDS = ("id", "owner", "site", "created_at", "expires_at")
#: What a record kept before the rename binds instead.
_FIELDS_BEFORE = ("id", "owner", "sites", "created_at", "expires_at")


def install_person() -> str:
    """Who uses this install: its one sign-in, or "you" without one. Whose a session is, here."""
    from core.config import load_settings
    return load_settings().get("login_username") or "you"


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

    return tenant_dir("website_logins")  # the old name: the sessions kept before the rename are here


def _file(session_id: str) -> Path | None:
    return _dir() / f"{session_id}.json" if _ID.match(session_id or "") else None


def _aad(record: dict) -> bytes:
    fields = _FIELDS if "site" in record else _FIELDS_BEFORE
    return json.dumps({k: record[k] for k in fields}, sort_keys=True).encode()


def _site(record: dict) -> str:
    """The record's site; one kept before the rename names hosts, and its first one's registrable domain is it."""
    from core.sites import registrable_domain
    if "site" in record:
        return str(record["site"])
    hosts = record.get("sites") or []
    return registrable_domain(str(hosts[0]).removeprefix("*.")) if hosts else ""


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


def _keep(session_id: str | None, state: dict, site: str, owner: str) -> str:
    from core.sites import registrable_domain
    ref = session_id if session_id and _ID.match(session_id) else f"ss_{secrets.token_hex(12)}"
    path = _file(ref)
    assert path is not None
    now = time.time()
    had = _read(path) if path.exists() else None
    record: dict[str, Any] = {
        "id": ref,
        "owner": owner,
        "site": registrable_domain(site) or (_site(had) if had else ""),
        "created_at": (had or {}).get("created_at", now),
        "expires_at": _expiry(state, now),
    }
    nonce = os.urandom(12)
    sealed = AESGCM(_key()).encrypt(nonce, json.dumps(state).encode(), _aad(record))
    record["nonce"] = base64.b64encode(nonce).decode()
    record["sealed"] = base64.b64encode(sealed).decode()
    _write(path, record)
    return ref


def _used_file(path: Path) -> Path:
    """When a session was last used, beside it: a read only ever writes this, never the sealed record (a stamp
    rewriting the record could put back a session that was just replaced)."""
    return path.with_suffix(".used")


def _last_used(path: Path) -> float | None:
    try:
        return float(_used_file(path).read_text("utf-8"))
    except (OSError, ValueError):
        return None


def _expire(path: Path, record: dict) -> None:
    """Past its expiry: the session is dropped, the record stays (marked expired) for signing in again into it."""
    if "sealed" in record:
        record = {k: v for k, v in record.items() if k not in ("nonce", "sealed")}
        _write(path, record)


def _state_for(session_id: str, for_user: str | None) -> dict | None:
    path = _file(session_id)
    if path is None or not path.exists():
        return None
    record = _read(path)
    if not record or record.get("id") != session_id:
        return None
    if record.get("owner") != (for_user if for_user is not None else install_person()):
        return None  # another person's (F1167): shown in the library, never opened for anyone else
    if float(record.get("expires_at", 0)) <= time.time():
        _expire(path, record)
        return None
    if "sealed" not in record:
        return None
    try:
        plain = AESGCM(_key()).decrypt(base64.b64decode(record["nonce"]), base64.b64decode(record["sealed"]),
                                       _aad(record))
    except (InvalidTag, KeyError, ValueError, TypeError):
        return None
    try:
        _used_file(path).write_text(str(time.time()), "utf-8")
    except OSError:
        pass
    return json.loads(plain)


def _row(record: dict, now: float, path: Path) -> dict:
    return {"id": record.get("id"), "site": _site(record), "owner": record.get("owner"),
            "created_at": record.get("created_at"), "last_used_at": _last_used(path),
            "expires_at": record.get("expires_at"),
            "expired": "sealed" not in record or float(record.get("expires_at", 0)) <= now}


def _get(session_id: str) -> dict | None:
    path = _file(session_id)
    record = _read(path) if path is not None and path.exists() else None
    if not record or record.get("id") != session_id:
        return None
    return _row(record, time.time(), path)


def _list() -> list[dict]:
    now = time.time()
    rows = []
    for path in sorted([*_dir().glob("ss_*.json"), *_dir().glob("wl_*.json")]):
        record = _read(path)
        if not record or not _ID.match(str(record.get("id", ""))):
            continue
        if float(record.get("expires_at", 0)) <= now:
            _expire(path, record)
            record = _read(path) or record
        rows.append(_row(record, now, path))
    return sorted(rows, key=lambda r: r["created_at"] or 0, reverse=True)


def _forget(session_id: str) -> bool:
    path = _file(session_id)
    if path is None or not path.exists():
        return False
    path.unlink(missing_ok=True)
    _used_file(path).unlink(missing_ok=True)
    return True


async def keep(session_id: str | None, *, state: dict, site: str, owner: str) -> str:
    """Keep a site's session, encrypted; returns its id. A known `session_id` is replaced (signing in again), keeping
    its id, so every pick of it keeps working. `site` is reduced to its registrable domain."""
    return await asyncio.to_thread(_keep, session_id, state, site, owner)


async def state_for(session_id: str, *, for_user: str | None = None) -> dict | None:
    """The session, decrypted, for `for_user` (its owner; None = this install's person); None once expired,
    removed, unreadable, never kept or not theirs. Marks it used."""
    return await asyncio.to_thread(_state_for, session_id, for_user)


async def get(session_id: str) -> dict | None:
    """One session's row (id, site, owner, dates, expired), never its state; None if there is none."""
    return await asyncio.to_thread(_get, session_id)


async def list_sessions() -> list[dict]:
    """Every kept session, newest first: its id, site, owner, dates and whether it expired, never its state."""
    return await asyncio.to_thread(_list)


async def forget(session_id: str) -> bool:
    """Remove a session at once. False when there was none."""
    return await asyncio.to_thread(_forget, session_id)


# ── who may use which (BRW-API, F1188/F1189) ────────────────────────────────

_lister = None


def set_lister(fn) -> None:
    """Where the sessions a caller may see come from: async `fn(caller) -> rows`, each a `list_sessions` row with
    `owner_name`, `mine` and `usable` (theirs, or shared with them). Cloud lists its own table for the person
    (`caller.user_id`, or a key's creator through `caller.principal`). None puts this install's back."""
    global _lister
    _lister = fn


async def listed(caller: Any = None) -> list[dict]:
    """Every session, each marked `mine` and `usable` for `caller`. Here one person uses the install: each is theirs."""
    if _lister is not None:
        return await _lister(caller)
    me = install_person()
    return [{**r, "owner_name": r["owner"], "mine": r["owner"] == me, "usable": r["owner"] == me}
            for r in await list_sessions()]
