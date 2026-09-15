"""
Where the vault actually lives — a directory, or an object store.

The vault holds large tool outputs, and the awkward part is that it hands
*filesystem paths* to the model and to the Filesystem MCP server: `read_file`,
`search_files`, `grep` and the sandbox's `-v` mount all need a real directory.
That is fine when the vault is a directory. It is not fine when the vault is S3,
which has no paths — `BlobStore.path_for()` returns None for exactly that reason.

So there are two backends and a factory picks one:

    LocalVault   the directory *is* the vault. A plain install, unchanged: every
                 filesystem operation works because there is a real filesystem
                 under it, and nothing is uploaded anywhere.

    S3Vault      the object store is the vault; a directory under the scratch
                 root is a materialised working copy. Writes go to both and the
                 object store is authoritative. Reads hydrate on demand. A pod
                 that dies takes only the working copy with it, which is the
                 whole point — the next pod rebuilds it from the store.

The choice is not a mode flag. It is asked of the blob store: a store that can
give a path gets `LocalVault`, a store that cannot gets `S3Vault`. One question,
one answer, and an embedder that registers some third kind of storage gets the
right behaviour without this module knowing about it.

What this replaces
------------------
`maybe_vault` used to answer "is S3 configured?" through `core.s3_storage.get_s3()`
while everything else asked `get_blob_store()` — two sources of truth for one
fact. And the upload was fire-and-forget under `except Exception: pass`, so in a
deployment where the object store *is* the durable copy, a failed upload handed
the model a path to a file that would not survive the pod. Silently.

What is honestly still partial
------------------------------
Hydration is per file on demand, plus `hydrate()` for a directory before an
operation that needs to see the whole of one. A directory-shaped call made by a
*subprocess* we do not control — the Filesystem MCP server's own `search_files`
— sees whatever has been materialised on this pod. `build_for_tenant` hydrates
when a tenant becomes active on a worker, which covers the case that matters;
a cross-run search on a cold pod can still miss files until they are touched.
"""
from __future__ import annotations

import os
from pathlib import Path

#: Key prefix for everything the vault owns, inside the tenant's key space.
_PREFIX = "vault"
#: The same, for callers outside this module (`core/vault.py` builds keys from it).
VAULT_PREFIX = _PREFIX


class LocalVault:
    """The vault is a directory. What a plain install has always had."""

    #: Whether a caller has to hydrate before trusting a directory listing.
    materialises = False

    def __init__(self, root: Path):
        self.root = Path(root)

    def write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        try:
            path.chmod(0o644)
        except OSError:
            pass

    def ensure_local(self, path: Path) -> Path:
        return Path(path)

    def hydrate(self, subdir: str = "") -> int:
        return 0

    def forget(self, path: Path) -> None:
        Path(path).unlink(missing_ok=True)


class S3Vault:
    """The object store is the vault; `root` is a working copy of it.

    `root` lives under the scratch directory, which is already defined as
    rebuildable per-replica state. That is exactly what this is: losing it costs
    a re-download, not data.
    """

    materialises = True

    def __init__(self, root: Path, store):
        self.root = Path(root)
        self._store = store

    # ── keys ─────────────────────────────────────────────────────────────────

    def _key(self, path: Path) -> str:
        """The blob key for a path inside the working copy.

        Refuses anything outside it. A path that escaped would be written to a
        key derived from `..`, and the blob store's own guard is the last line
        rather than the first.
        """
        resolved = Path(path).resolve()
        root = self.root.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(f"path escapes the vault: {path!r}")
        return f"{_PREFIX}/{resolved.relative_to(root).as_posix()}"

    def _path(self, key: str) -> Path:
        return self.root / key[len(_PREFIX) + 1:]

    # ── operations ───────────────────────────────────────────────────────────

    def write(self, path: Path, content: str) -> None:
        """Write through to the object store, and fail if that does not work.

        Deliberately not fire-and-forget. The caller is about to hand the model
        a path and tell it the output is safely stored; if the durable copy did
        not land, the honest thing is to say so now rather than to discover it
        when the pod is replaced.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        try:
            path.chmod(0o644)
        except OSError:
            pass
        self._store.put(self._key(path), content)

    def ensure_local(self, path: Path) -> Path:
        """Materialise one file if this pod does not have it yet."""
        path = Path(path)
        if path.exists():
            return path
        try:
            key = self._key(path)
        except ValueError:
            return path  # not ours; the caller's own guard will reject it
        content = self._store.get(key)
        if content is None:
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def hydrate(self, subdir: str = "") -> int:
        """Materialise a whole directory. Returns how many files were pulled.

        For the operations that need to *see* a directory rather than open one
        file in it — a listing, a recursive search — where partial
        materialisation would report a smaller vault than the tenant has.
        """
        prefix = f"{_PREFIX}/{subdir.strip('/')}" if subdir.strip("/") else _PREFIX
        pulled = 0
        for key in self._store.list(prefix):
            local = self._path(key)
            if local.exists():
                continue
            content = self._store.get(key)
            if content is None:
                continue
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text(content, encoding="utf-8")
            pulled += 1
        return pulled

    def forget(self, path: Path) -> None:
        path = Path(path)
        try:
            self._store.delete(self._key(path))
        except ValueError:
            pass
        path.unlink(missing_ok=True)


def get_vault():
    """The vault backend for this deployment, for the current tenant.

    Asked fresh each call rather than cached: the tenant is a ContextVar, and a
    worker serving many tenants in one process would otherwise hand the second
    one the first one's directory.
    """
    from core.storage import get_blob_store

    store = get_blob_store()
    path = store.path_for(_PREFIX)
    if path is not None:
        return LocalVault(Path(path))

    from core.storage.scratch import scratch_dir

    return S3Vault(scratch_dir(_PREFIX), store)
