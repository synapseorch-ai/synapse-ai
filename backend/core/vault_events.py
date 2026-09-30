# FROZEN CONTRACT (KB0, D153): change it through a contract-change request in MEMORY/16-knowledge.md §8.
"""One seam for "a Vault file changed" — so something can follow the Vault without scanning it.

Nothing in the engine could subscribe to Vault writes before this: the Vault
routes, `vault_backend`'s `write`/`forget`, the `vault_fs` tools and the
Filesystem MCP subprocess all write directly. Knowledge (D153) is the first
subscriber: a write under a knowledge base's folder queues that document.

* `notify_vault_change(...)` is what every write point calls, after the write
  succeeded. It **never raises**: a Vault write must not fail because
  something downstream could not be told. Whatever misses a notification — a
  write from the Filesystem MCP subprocess, a crash between the write and the
  call — is repaired by Knowledge's reconcile, which compares the folder with
  its documents by hash.
* `set_vault_observer(fn)` installs the one subscriber; None removes it.

`path` is Vault-relative (no `vault/` key prefix), NFC, POSIX. Wiring the calls
into each write point is A1's; cloud's Vault routes are cloud's own code and
mark documents queued in their own transaction instead (T1).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from core.log import get_logger

_log = get_logger(__name__)

VaultOp = Literal["write", "delete", "move", "mkdir"]
VaultSource = Literal["route", "upload", "tool", "fetch", "record", "reconcile"]


@dataclass(frozen=True)
class VaultChange:
    path: str
    op: VaultOp
    is_dir: bool = False
    old_path: str | None = None  # for a move
    sha256: str | None = None
    size: int | None = None
    source: VaultSource = "route"


VaultObserver = Callable[[VaultChange], Awaitable[None]]

_observer: VaultObserver | None = None


def set_vault_observer(observer: VaultObserver | None) -> None:
    """Install the Vault's one subscriber. None removes it."""
    global _observer
    _observer = observer


def get_vault_observer() -> VaultObserver | None:
    return _observer


async def notify_vault_change(path: str, op: VaultOp, *, is_dir: bool = False, old_path: str | None = None,
                              sha256: str | None = None, size: int | None = None,
                              source: VaultSource = "route") -> None:
    """Tell the subscriber, if any, that `path` changed. Never raises."""
    observer = _observer
    if observer is None:
        return
    change = VaultChange(path=path, op=op, is_dir=is_dir, old_path=old_path, sha256=sha256, size=size,
                         source=source)
    try:
        await observer(change)
    except Exception as exc:  # noqa: BLE001 — a subscriber never fails a Vault write
        _log.warning(f"vault observer failed on {op} {path!r}: {exc}")
