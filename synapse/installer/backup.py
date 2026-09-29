"""
Backups taken before anything is changed: a v1.x install's folders, or a 2.x database.

Written to `<home>/backups/`, readable by the owner only (they hold API keys),
and read back before anything else happens — a backup that was never opened is
a hope, not a backup. The newest few of each kind are kept.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tarfile
import time
from pathlib import Path
from typing import Callable

#: Rebuildable or transient: not worth the space, and chroma_db can be large.
_SKIP_DIRS = {"chroma_db", "cache", "tmp", "__pycache__", "venv", "node_modules"}
_KEEP = 5


class BackupError(Exception):
    pass


def free_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def _files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file() or name.endswith(".pid"):
                continue
            yield path


def backup_folders(sources: dict[str, Path], dest_dir: Path, label: str,
                   progress: Callable[[float], None] | None = None) -> Path:
    """`sources` is {name in the archive: folder or file}. Returns the archive.

    A manifest inside records where each part came from, so restoring by hand
    does not depend on remembering the old layout.
    """
    plan: list[tuple[Path, str]] = []
    for arc, src in sources.items():
        if src.is_file():
            plan.append((src, arc))
        elif src.is_dir():
            plan += [(f, f"{arc}/{f.relative_to(src).as_posix()}") for f in _files(src)]
    total = sum(f.stat().st_size for f, _ in plan) or 1
    dest_dir.mkdir(parents=True, exist_ok=True)
    if free_bytes(dest_dir) < total * 1.2 + 50 * 1024 * 1024:
        raise BackupError(
            f"not enough free space for the backup ({total // (1024 * 1024)} MB needed in {dest_dir})"
        )

    target = dest_dir / f"synapse-{label}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    partial = target.with_name(target.name + ".part")
    manifest = {"label": label, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "sources": {arc: str(src) for arc, src in sources.items()}}
    done = 0
    old_umask = os.umask(0o077)
    try:
        with tarfile.open(partial, "w:gz", compresslevel=6) as tar:
            data = json.dumps(manifest, indent=2).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size, info.mtime = len(data), int(time.time())
            import io
            tar.addfile(info, io.BytesIO(data))
            for path, arc in plan:
                tar.add(path, arcname=arc, recursive=False)
                done += path.stat().st_size
                if progress:
                    progress(done / total)
        _verify(partial, len(plan) + 1)
        partial.replace(target)
    except Exception:
        try:
            partial.unlink()
        except OSError:
            pass
        raise
    finally:
        os.umask(old_umask)
    _prune(dest_dir, "*.tar.gz")
    return target


def _verify(archive: Path, expected: int) -> None:
    try:
        with tarfile.open(archive, "r:gz") as tar:
            found = sum(1 for m in tar.getmembers() if m.isfile())
    except (tarfile.TarError, OSError) as exc:
        raise BackupError(f"the backup could not be read back: {exc}") from exc
    if found != expected:
        raise BackupError(f"the backup holds {found} files, expected {expected}")


def backup_sqlite(db: Path, dest_dir: Path, label: str) -> Path:
    """A consistent copy of a live SQLite database (sqlite's online backup)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"synapse-{label}-{time.strftime('%Y%m%d-%H%M%S')}.db"
    old_umask = os.umask(0o077)
    try:
        source = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        copy = sqlite3.connect(target)
        with copy:
            source.backup(copy)
        source.close()
        copy.close()
        check = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        ok = check.execute("PRAGMA integrity_check").fetchone()[0]
        check.close()
    finally:
        os.umask(old_umask)
    if ok != "ok":
        raise BackupError(f"the database backup failed its integrity check: {ok}")
    _prune(dest_dir, "*.db")
    return target


def _prune(dest_dir: Path, pattern: str) -> None:
    found = sorted(dest_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in found[_KEEP:]:
        try:
            old.unlink()
        except OSError:
            pass
