"""
The `BlobStore` protocol and the local-directory default.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Protocol

from core.tenancy import get_tenant


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
