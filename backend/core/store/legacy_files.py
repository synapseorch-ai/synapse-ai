"""
The files a pre-database install kept beside its JSON, copied into the blob store.

`importer.py` brings the documents across — agents, orchestrations, chats. A
v1.x data folder also held two trees of plain files that are the user's, and
that v2 keeps under blob keys instead:

* ``vault/**`` — tool outputs, compaction archives and anything an agent saved
  with the vault tools. v2's local vault is the blob store's ``vault/`` prefix
  (`core/vault_backend.py`), same subfolders.
* ``datasets/*.jsonl`` — generated datasets, listed from ``datasets/``
  (`services/synthetic_data.py`).

Copied, not moved: the source folder is retired whole by the importer, and a
copy that fails half way leaves the original intact. A key that already exists
is left alone — the store's copy is newer than anything in a legacy folder.

Not copied: ``memory/memory.jsonl`` (left unread on purpose, see
`core/server.py`), ``chroma_db`` and ``cache`` (rebuildable),
``google-credentials/`` (rebuilt from the imported OAuth documents), and
``whatsapp_sessions/`` — a WhatsApp Web browser profile, a live sign-in to the
person's WhatsApp that nothing in 2.0 reads, since WhatsApp Web is removed
(F1355). It stays where it was, so the 1.x backup holds it.
"""
from __future__ import annotations

import shutil
from pathlib import Path

#: (folder in the legacy data dir, blob key prefix, glob).
_TREES = (
    ("vault", "vault", "**/*"),
    ("datasets", "datasets", "*.jsonl"),
)


def import_legacy_files(root: str | Path) -> dict:
    """Copy `root`'s vault and datasets into the blob store.

    Returns ``{"vault": files, "datasets": files, "skipped": files}``. `skipped`
    counts files a store without a filesystem (S3) could not take, because they
    are not text.
    """
    from core.storage import get_blob_store

    store = get_blob_store()
    root = Path(root)
    counts = {"vault": 0, "datasets": 0, "skipped": 0}

    for folder, prefix, pattern in _TREES:
        source = root / folder
        if not source.is_dir():
            continue
        for path in sorted(source.glob(pattern)):
            # Lock symlinks and sockets are not content; only regular files are.
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(source).as_posix()
            key = f"{prefix}/{relative}"
            try:
                if store.exists(key):
                    continue
                target = store.path_for(key)
            except ValueError:
                # A name that resolves outside the tenant's storage.
                counts["skipped"] += 1
                continue
            if target is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
            else:
                try:
                    store.put(key, path.read_text(encoding="utf-8"))
                except (UnicodeDecodeError, OSError):
                    counts["skipped"] += 1
                    continue
            counts[folder] += 1

    sessions = root / "whatsapp_sessions"
    if sessions.is_dir() and any(p.is_dir() for p in sessions.iterdir()):
        print("[import] WhatsApp Web sign-ins left out: WhatsApp Web was removed in Synapse 2.0, and the old "
              "data's backup keeps them", flush=True)
    return counts
