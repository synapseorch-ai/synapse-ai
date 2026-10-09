"""Ids for the rows the engine mints itself (F431).

`orchestrations`, `agents` and `schedules` are keyed on `id` alone. In a plain
install that is one workspace's keyspace, and a short random id is plenty. An
embedder that keeps many organisations in one table has a single global
keyspace, where a short id can meet another organisation's row — a key
conflict on a row it cannot see, and an existence oracle. Such an embedder
mints its ids its own way and installs that here, so the engine's own writers
(the AI builder, a workflow's deploy) mint as its routes do.
"""
from __future__ import annotations

import random
import string
from typing import Callable

_minter: Callable[[str], str] | None = None


def set_id_minter(minter: Callable[[str], str] | None) -> None:
    """Install `minter(prefix) -> "{prefix}_…"` for every id the engine mints; None restores the default."""
    global _minter
    _minter = minter


def minter_installed() -> bool:
    return _minter is not None


def new_id(prefix: str, length: int = 7) -> str:
    """A new `{prefix}_…` id: the installed minter's, or a short random one."""
    if _minter is not None:
        return _minter(prefix)
    return f"{prefix}_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=length))
