"""
The few things that genuinely need a real directory.

`DATA_DIR` conflated three kinds of thing: documents, tenant content, and
actual directories. The first two have homes now — `core/store/` and
`core/storage/` — and this module is the third, which turned out to be much
smaller than it looked. Of the six things that supposedly needed a filesystem,
three stopped needing one (datasets became blob keys, chat sessions became
rows, the Google mirror became materialised scratch), one is trivially
ephemeral, one is a rebuildable cache, and exactly one is a genuine unresolved
requirement.

So there are two kinds of directory left, and the difference between them is
what happens when the disk goes away:

`state_dir()` — **rebuildable.** ChromaDB's index lives here. It is derived
data: the embeddings can be regenerated from their sources, and D10 keeps
vectors out of the store deliberately (SQLite has no vector type). On a
multi-replica deployment each replica has its own, which is correct for a cache
and would be wrong for anything else.

`tenant_dir()` — **durable tenant state, and it refuses rather than pretend.**
The Playwright/WhatsApp browser profile is the only member: a logged-in
WhatsApp Web session, which cannot be regenerated because regenerating it means
a human scanning a QR code. When the blob store has no filesystem — S3 —
there is nowhere correct to put it, so this raises instead of writing to
container storage that the next deploy discards. A user who has to re-scan a QR
code on every deploy is worse served by the silent version.

Scratch — for genuinely throwaway work — lives in `core/storage/scratch.py`
and is unchanged.

Paths stay configurable: `SYNAPSE_STATE_DIR`, and `SYNAPSE_BLOB_DIR` for
anything tenant-scoped.
"""
from __future__ import annotations

import os
from pathlib import Path

from core.tenancy import get_tenant


class NoDurableStorage(RuntimeError):
    """Durable per-tenant state was requested where none can be provided."""


def state_root() -> Path:
    """The base directory for rebuildable per-replica state."""
    configured = os.getenv("SYNAPSE_STATE_DIR")
    if configured:
        return Path(configured)
    from core.paths import var_root

    return var_root() / "state"


def state_dir(*parts: str) -> Path:
    """A per-tenant subdirectory of the state dir, created if missing.

    Rebuildable. Losing it costs a re-index, not data.
    """
    path = state_root().joinpath(get_tenant(), *parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def tenant_dir(*parts: str) -> Path:
    """A durable per-tenant directory, or a refusal.

    Backed by the blob store's own filesystem, so it lands wherever the rest of
    the tenant's content does and a local install can open it. Raises
    `NoDurableStorage` when the blob store has no filesystem, because the
    alternative — writing durable state to ephemeral container storage — fails
    silently and only at the worst moment.
    """
    from core.storage import get_blob_store

    root = get_blob_store().path_for("runtime")
    if root is None:
        raise NoDurableStorage(
            "This feature needs a durable directory on disk, and the configured "
            "blob store does not provide one. Point SYNAPSE_BLOB_DIR at a "
            "persistent volume, or disable the feature."
        )
    path = Path(root).joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path
