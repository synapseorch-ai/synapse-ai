"""
A real, writable, tenant-scoped directory for append-shaped work.

`BlobStore` deliberately has no `append`: S3 has no such operation, so
implementing one would mean either read-modify-write over the network — worse
than the local cost it was meant to save — or per-writer multipart state, which
puts a stateful session inside a protocol whose whole value is that it is six
stateless methods.

Logs are the append-shaped case. They are written a line at a time over the
life of a run and read as a whole afterwards. So they are appended to a real
file here and published with a single `put()` when the run ends.

Contents are safe to lose. Anything durable has been published; a scratch
directory that a container throws away costs at most the tail of a run that was
still in flight.
"""
from __future__ import annotations

import os
from pathlib import Path

from core.tenancy import get_tenant


def scratch_root() -> Path:
    """The base directory for scratch, per deployment.

    Prefers the blob store's own filesystem, so a local install keeps its logs
    beside the rest of its data and a user can open them. Stores with no
    filesystem — S3 — have no path to offer, so scratch falls back to a local
    directory that is genuinely temporary.
    """
    from core.storage import get_blob_store

    configured = os.getenv("SYNAPSE_SCRATCH_DIR")
    if configured:
        return Path(configured) / get_tenant()

    path = get_blob_store().path_for("scratch")
    if path is not None:
        return Path(path)

    from core.paths import var_root

    return var_root() / "scratch" / get_tenant()


def scratch_dir(*parts: str) -> Path:
    """A scratch subdirectory, created if missing."""
    path = scratch_root().joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path
