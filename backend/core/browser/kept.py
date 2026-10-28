"""The screenshots a run or a chat keeps after its browser closes, for the filmstrip (BRW1 v2, F1103), and its
browser's log: the record of who pressed and sent what (F1125).

One JPEG per agent action, in the tenant's blob store beside what the run or chat already keeps:
`runs/<run_id>/browser/<n>.jpg`, `chats/<session_id>/browser/<n>.jpg`. Nothing is kept while the person
drives. The log is `…/browser/log.json`: every browser the run or chat had, each line once (its session and
number), in order; a person's line names a method and a host, never what was sent. All of it goes when the run's
log or the chat is deleted (DESIGN.md §3.7).
"""
from __future__ import annotations

import json
import re
import threading

_SAFE = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
_KINDS = {"run": "runs", "chat": "chats"}


_store = None


def set_store(store) -> None:
    """Where kept screenshots and logs go instead (F1135, BRW3): an object with `keep`, `read`, `forget`,
    `keep_lines` and `read_log`, taking the arguments these functions take. Cloud's writes S3 under the org with the
    run-log retention tag. None restores the blob store below."""
    global _store
    _store = store


def _prefix(kind: str, ident: str) -> str | None:
    if kind not in _KINDS or not _SAFE.match(ident or "") or ".." in ident:
        return None
    return f"{_KINDS[kind]}/{ident}/browser/"


def keep(owner: str, n: int | str, jpeg: bytes) -> None:
    if _store is not None:
        return _store.keep(owner, n, jpeg)
    kind, _, ident = (owner or "").partition(":")
    prefix = _prefix(kind, ident)
    if prefix is None or not str(n).isdigit():
        return
    from core.storage import get_blob_store
    get_blob_store().put_bytes(f"{prefix}{int(n)}.jpg", jpeg, content_type="image/jpeg")


def read(kind: str, ident: str, n: str) -> bytes | None:
    if _store is not None:
        return _store.read(kind, ident, n)
    prefix = _prefix(kind, ident)
    if prefix is None or not str(n).isdigit():
        return None
    from core.storage import get_blob_store
    return get_blob_store().get_bytes(f"{prefix}{int(n)}.jpg")


def forget(kind: str, ident: str) -> None:
    if _store is not None:
        return _store.forget(kind, ident)
    prefix = _prefix(kind, ident)
    if prefix is None:
        return
    from core.storage import get_blob_store
    store = get_blob_store()
    for key in store.list(prefix):
        store.delete(key)


_log_lock = threading.Lock()


def keep_lines(owner: str, entries: list[dict]) -> None:
    """Add log lines to the run's or chat's record, each once."""
    if _store is not None:
        return _store.keep_lines(owner, entries)
    kind, _, ident = (owner or "").partition(":")
    prefix = _prefix(kind, ident)
    lines = [e for e in entries or [] if isinstance(e, dict) and e.get("session_id") and e.get("n")]
    if prefix is None or not lines:
        return
    from core.storage import get_blob_store
    store = get_blob_store()
    with _log_lock:
        try:
            kept = json.loads(store.get(f"{prefix}log.json") or "[]")
        except ValueError:
            kept = []
        seen = {(e.get("session_id"), e.get("n")) for e in kept}
        kept += [e for e in lines if (e["session_id"], e["n"]) not in seen]
        kept.sort(key=lambda e: (e.get("at") or 0, e.get("n") or 0))
        store.put(f"{prefix}log.json", json.dumps(kept))


def read_log(kind: str, ident: str) -> list[dict] | None:
    if _store is not None:
        return _store.read_log(kind, ident)
    prefix = _prefix(kind, ident)
    if prefix is None:
        return None
    from core.storage import get_blob_store
    try:
        return json.loads(get_blob_store().get(f"{prefix}log.json") or "[]")
    except ValueError:
        return []
