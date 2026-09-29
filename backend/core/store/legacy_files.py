"""
The files a pre-database install kept beside its JSON, copied into the blob store.

`importer.py` brings the documents across — agents, orchestrations, chats. A
v1.x data folder also held three trees of plain files that are the user's, and
that v2 keeps under blob keys instead:

* ``vault/**`` — tool outputs, compaction archives and anything an agent saved
  with the vault tools. v2's local vault is the blob store's ``vault/`` prefix
  (`core/vault_backend.py`), same subfolders.
* ``datasets/*.jsonl`` — generated datasets, listed from ``datasets/``
  (`services/synthetic_data.py`).
* ``whatsapp_sessions/<channel>/`` — a logged-in WhatsApp Web browser profile.
  Losing it means a human re-scanning a QR code; v2 keeps it under the blob
  store's ``runtime/`` (`core/runtime_dirs.tenant_dir`).

Copied, not moved: the source folder is retired whole by the importer, and a
copy that fails half way leaves the original intact. A key that already exists
is left alone — the store's copy is newer than anything in a legacy folder.

Not copied: ``memory/memory.jsonl`` (left unread on purpose, see
`core/server.py`), ``chroma_db`` and ``cache`` (rebuildable), and
``google-credentials/`` (rebuilt from the imported OAuth documents).
"""
from __future__ import annotations

import shutil
from pathlib import Path

#: (folder in the legacy data dir, blob key prefix, glob, what one count is).
_TREES = (
    ("vault", "vault", "**/*"),
    ("datasets", "datasets", "*.jsonl"),
    ("whatsapp_sessions", "runtime/whatsapp_sessions", "**/*"),
)


def import_legacy_files(root: str | Path) -> dict:
    """Copy `root`'s vault, datasets and WhatsApp sessions into the blob store.

    Returns ``{"vault": files, "datasets": files, "whatsapp_sessions": sessions,
    "skipped": files}``. `skipped` counts files a store without a filesystem
    (S3) could not take, because they are not text.
    """
    from core.storage import get_blob_store

    store = get_blob_store()
    root = Path(root)
    counts = {"vault": 0, "datasets": 0, "whatsapp_sessions": 0, "skipped": 0}

    for folder, prefix, pattern in _TREES:
        source = root / folder
        if not source.is_dir():
            continue
        sessions: set[str] = set()
        for path in sorted(source.glob(pattern)):
            # A browser profile holds lock symlinks and sockets; only regular
            # files are content.
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
            elif folder == "whatsapp_sessions":
                # `tenant_dir` refuses a store with no filesystem, so a
                # profile uploaded here could never be opened again.
                counts["skipped"] += 1
                continue
            else:
                try:
                    store.put(key, path.read_text(encoding="utf-8"))
                except (UnicodeDecodeError, OSError):
                    counts["skipped"] += 1
                    continue
            if folder == "whatsapp_sessions":
                sessions.add(relative.split("/", 1)[0])
            else:
                counts[folder] += 1
        if folder == "whatsapp_sessions":
            counts[folder] = len(sessions)

    return counts
