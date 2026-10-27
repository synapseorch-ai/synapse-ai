"""The screenshots a run or a chat keeps after its browser closes, for the filmstrip (BRW1 v2, F1103).

One JPEG per agent action, in the tenant's blob store beside what the run or chat already keeps:
`runs/<run_id>/browser/<n>.jpg`, `chats/<session_id>/browser/<n>.jpg`. Nothing is kept while the person
drives. They go when the run's log or the chat is deleted (DESIGN.md §3.7).
"""
from __future__ import annotations

import re

_SAFE = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
_KINDS = {"run": "runs", "chat": "chats"}


def _prefix(kind: str, ident: str) -> str | None:
    if kind not in _KINDS or not _SAFE.match(ident or "") or ".." in ident:
        return None
    return f"{_KINDS[kind]}/{ident}/browser/"


def keep(owner: str, n: int | str, jpeg: bytes) -> None:
    kind, _, ident = (owner or "").partition(":")
    prefix = _prefix(kind, ident)
    if prefix is None or not str(n).isdigit():
        return
    from core.storage import get_blob_store
    get_blob_store().put_bytes(f"{prefix}{int(n)}.jpg", jpeg, content_type="image/jpeg")


def read(kind: str, ident: str, n: str) -> bytes | None:
    prefix = _prefix(kind, ident)
    if prefix is None or not str(n).isdigit():
        return None
    from core.storage import get_blob_store
    return get_blob_store().get_bytes(f"{prefix}{int(n)}.jpg")


def forget(kind: str, ident: str) -> None:
    prefix = _prefix(kind, ident)
    if prefix is None:
        return
    from core.storage import get_blob_store
    store = get_blob_store()
    for key in store.list(prefix):
        store.delete(key)
