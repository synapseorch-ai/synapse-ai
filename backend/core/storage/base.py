"""
The `BlobStore` protocol and the local-directory default.

Text and bytes. `put`/`get` carry UTF-8 text and are unchanged: `get` still
raises `ValueError` on bytes that are not UTF-8, which the Vault routes and the
mention reader read as "a binary file". The bytes half (`put_bytes`,
`get_bytes`, `iter_bytes`, `put_stream`, `stat`, `list_stats`) is what uploads
and Knowledge's ingestion use (D157): a PDF is not text, and a 100 MB upload is
assembled from its parts without holding it in memory.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from core.tenancy import get_tenant

#: The size of each piece `iter_bytes` yields by default.
CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class BlobStat:
    """What a store knows about one blob without reading it.

    `key` is relative to the tenant, the shape `list()` returns. `etag` is the
    object store's own (an MD5 for a single-part S3 object, not a sha256); a
    local file has none.
    """

    key: str
    size: int
    modified_at: datetime | None = None
    etag: str | None = None


class BlobStore(Protocol):
    """Content addressed by key, scoped to the current tenant.

    Implementations must apply the tenant prefix themselves — see
    ``tenant_key``. A caller never passes a tenant and never can.
    """

    def put(self, key: str, content: str) -> None: ...

    def get(self, key: str) -> str | None: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...

    def list(self, prefix: str = "") -> list[str]: ...

    def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> None: ...

    def get_bytes(self, key: str) -> bytes | None: ...

    def iter_bytes(self, key: str, chunk_size: int = CHUNK_SIZE) -> Iterator[bytes] | None:
        """The blob in pieces, or None when there is none. For reading big blobs."""
        ...

    def put_stream(self, key: str, chunks: Iterable[bytes], *,
                   content_type: str | None = None) -> tuple[int, str]:
        """Write `chunks` as one blob; returns (size in bytes, sha256 hex).

        All or nothing: a reader never sees a partial blob, and an iterator that
        raises leaves whatever was there before.
        """
        ...

    def stat(self, key: str) -> BlobStat | None: ...

    def list_stats(self, prefix: str = "") -> list[BlobStat]:
        """`list()` with each blob's size and modification time."""
        ...

    def path_for(self, key: str) -> Path | None:
        """A real filesystem path for `key`, if this store has one.

        Returns None for stores that do not (S3). Callers that genuinely need a
        path — handing a directory to a subprocess, for instance — must handle
        None rather than assuming a filesystem exists.
        """
        ...


def tenant_key(key: str) -> str:
    """Prefix `key` with the current tenant.

    The single place tenancy enters blob storage. Applied inside every store
    implementation rather than at the call sites, because a call site that
    forgets is a cross-tenant read and there are dozens of them.
    """
    return f"{get_tenant()}/{key.lstrip('/')}"


class LocalBlobStore:
    """Blobs in a directory tree. The default for a plain install."""

    def __init__(self, root: str | os.PathLike | None = None):
        if root is None:
            from core.paths import var_root

            root = os.getenv("SYNAPSE_BLOB_DIR") or (var_root() / "blobs")
        self.root = Path(root)

    def _tenant_root(self) -> Path:
        return (self.root / get_tenant()).resolve()

    def _resolved(self, key: str) -> Path:
        """Absolute path for `key`, refusing anything that leaves the tenant.

        The boundary is the *tenant's* directory, not the store root. Keys
        reach this from tool output names and cache namespaces, and a key like
        ``../notes.txt`` resolves to a sibling of the tenant directory — still
        inside the store, but belonging to another tenant or to nobody. A guard
        that only checked the store root would let that through, which is the
        whole leak this store exists to prevent.
        """
        tenant_root = self._tenant_root()
        target = (self.root / tenant_key(key)).resolve()
        if target != tenant_root and tenant_root not in target.parents:
            raise ValueError(f"blob key escapes the tenant's storage: {key!r}")
        return target

    def put(self, key: str, content: str) -> None:
        target = self._resolved(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: a reader never sees a half-written blob, and a
        # crash mid-write leaves the previous version rather than a truncated
        # one. Same reason the run checkpoint did it.
        fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, target)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def get(self, key: str) -> str | None:
        target = self._resolved(key)
        if not target.is_file():
            return None
        try:
            return target.read_text(encoding="utf-8")
        except OSError:
            return None

    def exists(self, key: str) -> bool:
        return self._resolved(key).is_file()

    def delete(self, key: str) -> None:
        target = self._resolved(key)
        if target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target, ignore_errors=True)

    def list(self, prefix: str = "") -> list[str]:
        base = self._resolved(prefix)
        if not base.is_dir():
            return []
        tenant_root = self._tenant_root()
        return sorted(
            str(p.relative_to(tenant_root)) for p in base.rglob("*") if p.is_file()
        )

    def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        self.put_stream(key, (data,))

    def get_bytes(self, key: str) -> bytes | None:
        target = self._resolved(key)
        if not target.is_file():
            return None
        try:
            return target.read_bytes()
        except OSError:
            return None

    def iter_bytes(self, key: str, chunk_size: int = CHUNK_SIZE) -> Iterator[bytes] | None:
        target = self._resolved(key)
        if not target.is_file():
            return None

        def pieces() -> Iterator[bytes]:
            with open(target, "rb") as f:
                while piece := f.read(chunk_size):
                    yield piece

        return pieces()

    def put_stream(self, key: str, chunks: Iterable[bytes], *,
                   content_type: str | None = None) -> tuple[int, str]:
        target = self._resolved(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        # Write-then-rename, as `put` does.
        fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                for piece in chunks:
                    f.write(piece)
                    digest.update(piece)
                    size += len(piece)
            os.replace(tmp, target)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return size, digest.hexdigest()

    def stat(self, key: str) -> BlobStat | None:
        target = self._resolved(key)
        try:
            st = target.stat()
        except OSError:
            return None
        if not target.is_file():
            return None
        return BlobStat(key=target.relative_to(self._tenant_root()).as_posix(), size=st.st_size,
                        modified_at=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc))

    def list_stats(self, prefix: str = "") -> list[BlobStat]:
        base = self._resolved(prefix)
        if not base.is_dir():
            return []
        tenant_root = self._tenant_root()
        out = []
        for p in base.rglob("*"):
            try:
                st = p.stat()
            except OSError:  # removed while we walked
                continue
            if p.is_file():
                out.append(BlobStat(key=p.relative_to(tenant_root).as_posix(), size=st.st_size,
                                    modified_at=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)))
        return sorted(out, key=lambda b: b.key)

    def path_for(self, key: str) -> Path | None:
        return self._resolved(key)


_store: BlobStore | None = None


def set_blob_store(store: BlobStore | None) -> None:
    """Install a blob store. None restores the local-directory default."""
    global _store
    _store = store


def get_blob_store() -> BlobStore:
    global _store
    if _store is None:
        _store = LocalBlobStore()
    return _store
