"""
Blob storage: vaulted tool outputs, cached responses, run logs.

Everything here is content the engine writes that is too big, too transient, or
too file-shaped to be a database row. It used to live under ``DATA_DIR`` —
which meant it lived on one machine, and meant a shared process had every
tenant writing into one directory.

There are two implementations and one rule: **keys are prefixed by tenant**.
The prefix is applied here rather than by callers, so a caller that forgets is
not a cross-tenant leak — there is no way to ask for an unprefixed key.

    LocalBlobStore   a directory. The default; what a plain install uses.
    S3BlobStore      an S3 bucket, via the existing SynapseS3 client.

An embedder registers its own with ``set_blob_store()``. The shipped product
installs ``S3BlobStore`` when a bucket is configured — see
``install_configured_blob_store``.
"""
from core.storage.base import BlobStore, LocalBlobStore, get_blob_store, set_blob_store
from core.storage.s3 import S3BlobStore



def install_configured_blob_store() -> bool:
    """Install ``S3BlobStore`` when a bucket is configured. Returns whether it did.

    Called by every process that reads the vault — the API server, a scale
    worker, and a tool subprocess (``core/tool_server.py::bootstrap``) — because
    a store is a module global and does not cross a process boundary. The bucket
    is read where ``get_s3()`` reads it (``S3_BUCKET``, else the saved
    ``s3_bucket``), so the store and its client cannot disagree.

    Nothing installed it before F57: ``S3_*`` fed only a mirror of the sandbox's
    vault writes, and a tool subprocess read local disk whatever its parent
    used.
    """
    from core.scale.config import get_scale_config

    if not get_scale_config().s3_bucket:
        return False
    set_blob_store(S3BlobStore())
    return True


__all__ = [
    "BlobStore",
    "LocalBlobStore",
    "S3BlobStore",
    "get_blob_store",
    "install_configured_blob_store",
    "set_blob_store",
]
