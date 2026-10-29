"""
Vault management REST API.

Handles CRUD operations on the user-managed vault directory (data/vault/ minus the
auto-generated tool_outputs/ subfolder).  Only .json, .md, .txt, .csv and .tsv files may be created; any text
file may be edited, except inside the engine's own folders (F1207).  Every file can be fetched as it is
(`/api/vault/raw`): a raster image to show in the page, anything else to download.

When the blob store has no filesystem — S3 — every operation goes through it, by key,
with the tenant applied inside the store (`core.storage`).  A store with a directory
is the vault itself, and the routes work on that directory.
"""
import hashlib
import json
import os
import shutil
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from posixpath import normpath
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel


router = APIRouter()

# Root directory exposed to the frontend (local fallback)
def VAULT_USER_DIR() -> Path:
    """The current tenant's vault root, from the blob store."""
    from core.vault import _vault_root
    return _vault_root()
#: Directories inside the vault that the engine writes and a person does not:
#: `tool_outputs/` is every vaulted tool result (`core/vault.py`), `playwright/`
#: is the Browser Automation server's output (`core/server.py`), and
#: `compaction_archives/` holds compacted conversations (`core/compaction.py`).
#: They stay visible and readable — seeing what a tool saved is the point of the
#: screen — but one recursive DELETE on the folder threw all of it away, so the
#: folder itself is not a person's to remove or to re-create by hand. The files
#: inside it still are.
_ENGINE_OWNED = {"tool_outputs", "playwright", "compaction_archives"}
#: What a person may create and edit here: text, with the plain-data tables Knowledge reads (its `csv` format),
#: as cloud's vault takes `csv` (F1202).
_ALLOWED_EXTENSIONS = {".json", ".md", ".txt", ".csv", ".tsv"}

#: The largest text the editor opens or saves (F1207): a longer log or table is downloaded instead.
MAX_TEXT_BYTES = 5 * 1024 * 1024

#: What `/api/vault/raw` shows in the page, with the type it is served as (F1207). Raster images only, and only in
#: an <img>: an SVG or an HTML file is a document that would run script on this origin, so it downloads, as does a
#: PDF (the browser's viewer refuses a sandboxed one) and everything else.
_INLINE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
                 ".gif": "image/gif"}

#: Every raw answer: never sniffed into another type, and a document opened from it runs nothing and loads nothing.
_RAW_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; img-src 'self'; sandbox",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "private, no-store",
}

# S3 key prefix for user vault files (relative to the bucket prefix)
_S3_VAULT_REL = "vault"


# ---------------------------------------------------------------------------
# Storage selection helper
# ---------------------------------------------------------------------------

def _get_storage(source: str = "auto"):
    """The object store to go through, or None for the directory.

    source="auto"  → the blob store, when it has no filesystem (S3)
    source="s3"    → the same
    source="local" → the directory, even then (the working copy)

    This used to hand back the raw S3 client (`core.s3_storage.get_s3`), whose
    keys carry no tenant — `<prefix>/vault/…` for everyone, a key space the
    engine never writes to (it writes `<prefix>/<tenant>/vault/…`) and one
    shared by every tenant on the deployment. The blob store applies the
    tenant inside every key, and a call site that forgets cannot.
    """
    if source == "local":
        return None
    from core.storage import get_blob_store
    store = get_blob_store()
    return None if store.path_for(_S3_VAULT_REL) is not None else store


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------

def _safe_rel(rel: str) -> str:
    """
    Sanitise a client-supplied relative path: strip leading slashes/dots,
    normalise away any '..' components, and reject anything that escapes the root.
    Raises HTTPException 403 on traversal attempts.
    """
    clean = rel.lstrip("/").lstrip("./")
    normalised = normpath(clean) if clean else "."
    if normalised.startswith("..") or normalised == ".":
        # Empty or traversal → treat as root (for tree listing) or reject for writes
        return ""
    # Reject explicit traversal
    for part in Path(normalised).parts:
        if part == "..":
            raise HTTPException(status_code=403, detail="Path traversal denied")
    return normalised


#: Unicode categories a name may not contain: control, format, surrogate,
#: private-use, unassigned, and the line and paragraph separators.
_REFUSED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


def _segment(name: str) -> str:
    """A file or folder name: exactly one path segment, or a 400.

    It used to be joined under the parent as typed, so `name="../../x.md"`
    wrote outside the vault (the `.md` suffix check passed). NFC, so the name
    the tree shows is the name `@[path]` finds; no slash of either kind, no
    bracket (it would end an `@[…]` early), no leading dot (the tree hides
    dotfiles, so the file would be invisible), no surrounding whitespace, no
    control characters, and at most 255 bytes.
    """
    name = unicodedata.normalize("NFC", name or "")
    if (
        not name
        or name != name.strip()
        or name in (".", "..")
        or any(ch in name for ch in "/\\[]")
        or name.startswith(".")
        or any(unicodedata.category(ch) in _REFUSED_CATEGORIES for ch in name)
        or len(name.encode("utf-8")) > 255
    ):
        raise HTTPException(
            status_code=400,
            detail="A name is one segment: no slashes or brackets, no leading dot, "
                   "no surrounding spaces or control characters, at most 255 bytes",
        )
    return name


async def _changed(rel: str, op: str, *, source: str = "auto", content: str | None = None,
                   is_dir: bool = False) -> None:
    """Tell whoever follows the Vault (Knowledge, D157) that `rel` changed — after the write, never raising.

    Only a write to the Vault itself: `source="local"` on an object store writes the
    working copy, which nothing reads back as the Vault.
    """
    try:
        from core.storage import get_blob_store
        from core.vault_events import notify_vault_change

        if source == "local" and get_blob_store().path_for(_S3_VAULT_REL) is None:
            return
        data = content.encode("utf-8") if content is not None else None
        await notify_vault_change(
            unicodedata.normalize("NFC", rel.replace("\\", "/")).strip("/"), op,  # type: ignore[arg-type]
            is_dir=is_dir, sha256=hashlib.sha256(data).hexdigest() if data is not None else None,
            size=len(data) if data is not None else None, source="route")
    except Exception as exc:  # noqa: BLE001 — a follower never fails a Vault write
        from core.log import get_logger
        get_logger(__name__).warning(f"vault change on {rel!r} not announced: {exc}")


def _rel_of(p: Path) -> str:
    """A local path as a Vault-relative POSIX path."""
    return p.resolve().relative_to(_vault_root().resolve()).as_posix()


def _s3_key_for(rel: str) -> str:
    """Return the S3 relative key for a vault file path, e.g. 'vault/research.md'."""
    if rel:
        return f"{_S3_VAULT_REL}/{rel}"
    return _S3_VAULT_REL


def _s3_build_tree(keys: list[str], stats: dict | None = None) -> list[dict]:
    """
    Convert a flat list of S3 relative keys (e.g. ['vault/a.md', 'vault/sub/b.json'])
    into the same nested tree structure the frontend expects. `stats` (key → `BlobStat`)
    gives each file its size and modified time.
    """
    # Filter to only vault/ keys and strip the vault/ prefix
    prefix = _S3_VAULT_REL + "/"
    rel_keys = []
    for k in keys:
        if k.startswith(prefix):
            rel_keys.append(k[len(prefix):])

    # Build a nested dict, then convert to list
    root_node: dict = {}
    for rel in rel_keys:
        if rel.endswith("/.keep") or rel == ".keep":
            continue  # skip folder placeholder files
        parts = Path(rel).parts
        node = root_node
        for part in parts[:-1]:
            node = node.setdefault(f"__dir__{part}", {})
        filename = parts[-1]
        node[filename] = rel  # leaf: store the full relative path

    def _to_list(node: dict, parent_rel: str) -> list[dict]:
        items = []
        dirs = sorted(k for k in node if k.startswith("__dir__"))
        files = sorted(k for k in node if not k.startswith("__dir__"))
        for d in dirs:
            name = d[7:]  # strip __dir__ prefix
            folder_rel = f"{parent_rel}/{name}" if parent_rel else name
            items.append({
                "name": name,
                "path": folder_rel,
                "type": "folder",
                "children": _to_list(node[d], folder_rel),
            })
        for f in files:
            rel_path = node[f]
            suffix = Path(f).suffix.lower()
            stat = (stats or {}).get(_s3_key_for(rel_path))
            items.append({
                "name": f,
                "path": rel_path,
                "type": "file",
                "ext": suffix,
                "size": stat.size if stat else None,
                "modified": stat.modified_at.isoformat() if stat and stat.modified_at else None,
                "read_only": _in_engine_folder(rel_path),
            })
        return items

    return _to_list(root_node, "")


# ---------------------------------------------------------------------------
# Local filesystem helpers (unchanged from original)
# ---------------------------------------------------------------------------

def _vault_root() -> Path:
    root = VAULT_USER_DIR()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _resolve(rel: str) -> Path:
    """The path inside the vault, or a 403.

    A string-prefix check let a sibling through: `vault2/` starts with
    `vault`, so `sub/../../vault2/secret.md` resolved outside the vault and
    passed. The check is on path components now.
    """
    root = _vault_root()
    clean = rel.lstrip("/").lstrip("./")
    resolved = (root / clean).resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise HTTPException(status_code=403, detail="Path traversal denied")
    return resolved


def _in_engine_folder(rel: str) -> bool:
    """A file inside one of `_ENGINE_OWNED`: runs write it, so a person reads it and may delete it, but never
    edits it (F1207, Naveen 10-29)."""
    parts = PurePosixPath(rel).parts
    return len(parts) > 1 and parts[0] in _ENGINE_OWNED


def _when(seconds: float | None) -> str | None:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat() if seconds is not None else None


def _is_engine_owned_dir(path: Path) -> bool:
    """True for `<vault>/tool_outputs` itself, not for anything inside it."""
    root = _vault_root().resolve()
    try:
        parts = path.resolve().relative_to(root).parts
    except ValueError:
        return False
    return len(parts) == 1 and parts[0] in _ENGINE_OWNED


def _build_tree(directory: Path, root: Path) -> list[dict]:
    items: list[dict] = []
    try:
        entries = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except PermissionError:
        return items

    for entry in entries:
        if entry.name.startswith("."):
            continue
        rel_path = str(entry.resolve().relative_to(root.resolve()))
        if entry.is_dir():
            items.append({
                "name": entry.name,
                "path": rel_path,
                "type": "folder",
                "children": _build_tree(entry, root),
            })
        elif entry.is_file():
            info = entry.stat()
            items.append({
                "name": entry.name,
                "path": rel_path,
                "type": "file",
                "ext": entry.suffix.lower(),
                "size": info.st_size,
                "modified": _when(info.st_mtime),
                "read_only": _in_engine_folder(Path(rel_path).as_posix()),
            })
    return items


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/api/vault/tree")
async def get_vault_tree(source: str = Query("auto")):
    """Return the full directory tree beneath the user vault root."""
    s3 = _get_storage(source)
    if s3:
        stats = {s.key: s for s in s3.list_stats(_S3_VAULT_REL + "/")}
        return {"tree": _s3_build_tree(list(stats), stats), "source": "s3"}
    root = _vault_root()
    return {"tree": _build_tree(root, root), "source": "local"}


@router.get("/api/vault/search")
async def search_vault_files(q: str = Query(default="", description="Search query"), source: str = Query("auto")):
    """Search vault files by name (for @ mention autocomplete)."""
    s3 = _get_storage(source)
    q_lower = q.strip().lower()

    if s3:
        keys = s3.list(_S3_VAULT_REL + "/")
        results = []
        for rel_key in keys:
            # rel_key is like "vault/sub/file.md" (relative to bucket prefix)
            name = Path(rel_key).name
            if name == ".keep":
                continue  # skip folder placeholders
            if not q_lower or q_lower in name.lower():
                # Strip the "vault/" prefix to get the path relative to vault root
                vault_prefix = _S3_VAULT_REL + "/"
                rel_path = rel_key[len(vault_prefix):] if rel_key.startswith(vault_prefix) else rel_key
                results.append({
                    "name": name,
                    "path": rel_path,
                    "ext": Path(name).suffix.lower(),
                })
            if len(results) >= 20:
                break
        return {"files": results}

    root = _vault_root()
    results: list[dict] = []
    for p in root.rglob("*"):
        if p.is_file():
            if not q_lower or q_lower in p.name.lower():
                rel = str(p.resolve().relative_to(root.resolve()))
                results.append({
                    "name": p.name,
                    "path": rel,
                    "ext": p.suffix.lower(),
                })
            if len(results) >= 20:
                break
    return {"files": results}


def _refuse_too_long(size: int) -> None:
    if size > MAX_TEXT_BYTES:
        raise HTTPException(status_code=413, detail=f"This file is {size / 1048576:.1f} MB, more than the "
                                                    f"{MAX_TEXT_BYTES // 1048576} MB the editor opens: download it.")


def _disposition(name: str, inline: bool) -> str:
    return f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(name, safe='')}"


@router.get("/api/vault/raw")
async def get_vault_raw(path: str = Query(..., description="Relative path to vault file"),
                        source: str = Query("auto"), download: bool = Query(False)):
    """A vault file as it is (F1207): a raster image as itself, to show in an <img>, and anything else (or any
    file, with `download`) as an attachment. Never a document that runs: see `_INLINE_TYPES`, `_RAW_HEADERS`."""
    rel = _safe_rel(path)
    if not rel:
        raise HTTPException(status_code=404, detail="File not found")
    name = PurePosixPath(rel).name
    shown = None if download else _INLINE_TYPES.get(PurePosixPath(name).suffix.lower())
    media = shown or "application/octet-stream"
    headers = {**_RAW_HEADERS, "Content-Disposition": _disposition(name, shown is not None)}
    s3 = _get_storage(source)
    if s3:
        chunks = s3.iter_bytes(_s3_key_for(rel))
        if chunks is None:
            raise HTTPException(status_code=404, detail="File not found")
        return StreamingResponse(chunks, media_type=media, headers=headers)
    p = _resolve(rel)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(p, media_type=media, headers=headers)


@router.get("/api/vault/file")
async def get_vault_file(path: str = Query(..., description="Relative path to vault file"), source: str = Query("auto")):
    """Return the content of a vault file."""
    s3 = _get_storage(source)
    rel = _safe_rel(path)
    name = Path(path).name
    ext = Path(name).suffix.lower()

    if s3:
        stat = s3.stat(_s3_key_for(rel)) if rel else None
        if stat is None:
            raise HTTPException(status_code=404, detail="File not found")
        _refuse_too_long(stat.size)
        try:
            content = s3.get(_s3_key_for(rel))
        except ValueError:
            raise HTTPException(status_code=415, detail="Not a text file")
        if content is None:
            raise HTTPException(status_code=404, detail="File not found")
        return {"path": rel, "name": name, "ext": ext, "content": content, "size": stat.size,
                "read_only": _in_engine_folder(rel)}

    p = _resolve(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    size = p.stat().st_size
    _refuse_too_long(size)
    try:
        content = p.read_text(encoding="utf-8")
        return {"path": path, "name": p.name, "ext": p.suffix.lower(), "content": content, "size": size,
                "read_only": _in_engine_folder(_rel_of(p))}
    except UnicodeDecodeError:
        # The object-store branch's answer too (F323): a PDF or an image is a file the
        # editor cannot open, not a server error.
        raise HTTPException(status_code=415, detail="Not a text file")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class CreateFileRequest(BaseModel):
    path: str          # relative parent folder path (empty = vault root)
    name: str          # filename including extension (`_ALLOWED_EXTENSIONS`)
    content: Optional[str] = ""
    source: str = "auto"


@router.post("/api/vault/file")
async def create_vault_file(req: CreateFileRequest):
    """Create a new .json, .md, .txt, .csv or .tsv file in the vault."""
    name = _segment(req.name)
    suffix = Path(name).suffix.lower()
    if suffix not in _ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Only {', '.join(_ALLOWED_EXTENSIONS)} files are allowed")

    parent_rel = _safe_rel(req.path) if req.path.strip() else ""
    file_rel = f"{parent_rel}/{name}" if parent_rel else name

    # Provide sensible default content
    initial_content = req.content or ""
    if not initial_content:
        if suffix == ".json":
            initial_content = "{}\n"
        elif suffix in (".txt", ".csv", ".tsv"):
            initial_content = ""
        else:
            initial_content = f"# {Path(name).stem}\n\n"

    s3 = _get_storage(req.source)
    if s3:
        if s3.exists(_s3_key_for(file_rel)):
            raise HTTPException(status_code=409, detail="File already exists")
        s3.put(_s3_key_for(file_rel), initial_content)
        await _changed(file_rel, "write", source=req.source, content=initial_content)
        return {"path": file_rel, "name": name, "ext": suffix, "content": initial_content}

    parent = _resolve(req.path) if req.path.strip() else _vault_root()
    if not parent.is_dir():
        raise HTTPException(status_code=400, detail="Parent path is not a directory")
    target = parent / name
    if target.exists():
        raise HTTPException(status_code=409, detail="File already exists")
    try:
        target.write_text(initial_content, encoding="utf-8")
        root = _vault_root()
        rel = str(target.resolve().relative_to(root.resolve()))
        await _changed(_rel_of(target), "write", source=req.source, content=initial_content)
        return {"path": rel, "name": target.name, "ext": suffix, "content": initial_content}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


_ENGINE_FILE = "Runs write the files in this folder, so they open read-only here. Copy one out to change it."


def _refuse_edit(rel: str, content: str) -> None:
    """What the editor may not save (F1207): a file in the engine's folders, one of the download-only kinds, text
    that is not UTF-8 or carries a NUL, or more than `MAX_TEXT_BYTES`."""
    from core.vault import binary_kind

    if not rel:
        raise HTTPException(status_code=404, detail="File not found")
    if _in_engine_folder(rel):
        raise HTTPException(status_code=403, detail=_ENGINE_FILE)
    if binary_kind(rel) or PurePosixPath(rel).suffix.lower() in _INLINE_TYPES:
        raise HTTPException(status_code=400, detail="Cannot edit this file type")
    try:
        size = len(content.encode("utf-8"))
    except UnicodeEncodeError:
        raise HTTPException(status_code=400, detail="The text is not valid UTF-8.")
    if "\x00" in content:
        raise HTTPException(status_code=400, detail="The text carries a NUL character, so it is not a text file.")
    if size > MAX_TEXT_BYTES:
        raise HTTPException(status_code=413, detail=f"A file saved here is at most {MAX_TEXT_BYTES // 1048576} MB.")


class UpdateFileRequest(BaseModel):
    path: str
    content: str
    source: str = "auto"


@router.put("/api/vault/file")
async def update_vault_file(req: UpdateFileRequest):
    """Update the content of an existing vault file: any text file (F1207), outside the engine's folders."""
    rel = _safe_rel(req.path)
    _refuse_edit(rel, req.content)

    s3 = _get_storage(req.source)
    if s3:
        try:
            s3.get(_s3_key_for(rel))
        except ValueError:  # bytes, not text: never replaced by the editor's text
            raise HTTPException(status_code=415, detail="Not a text file")
        s3.put(_s3_key_for(rel), req.content)
        await _changed(rel, "write", source=req.source, content=req.content)
        return {"status": "ok", "path": rel}

    p = _resolve(req.path)
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    if _in_engine_folder(_rel_of(p)):  # through a link, too
        raise HTTPException(status_code=403, detail=_ENGINE_FILE)
    try:
        p.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=415, detail="Not a text file")
    try:
        p.write_text(req.content, encoding="utf-8")
        await _changed(_rel_of(p), "write", source=req.source, content=req.content)
        return {"status": "ok", "path": req.path}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class CreateFolderRequest(BaseModel):
    path: str    # relative parent path (empty = vault root)
    name: str    # new folder name
    source: str = "auto"


@router.post("/api/vault/folder")
async def create_vault_folder(req: CreateFolderRequest):
    """Create a new folder inside the vault."""
    name = _segment(req.name)
    if name in _ENGINE_OWNED:
        raise HTTPException(
            status_code=400,
            detail=f"'{name}' is where the engine writes — pick another name",
        )

    parent_rel = _safe_rel(req.path) if req.path.strip() else ""
    folder_rel = f"{parent_rel}/{name}" if parent_rel else name

    s3 = _get_storage(req.source)
    if s3:
        # S3 has no real folders; a `.keep` marker makes the folder visible.
        # One byte, never empty: a zero-byte PUT stalls a MinIO connection for
        # ~30 s on the next PUT (the cloud vault learnt this the hard way).
        placeholder_key = _s3_key_for(f"{folder_rel}/.keep")
        s3.put(placeholder_key, "\n")
        await _changed(folder_rel, "mkdir", source=req.source, is_dir=True)
        return {"path": folder_rel, "name": name, "type": "folder"}

    parent = _resolve(req.path) if req.path.strip() else _vault_root()
    if not parent.is_dir():
        raise HTTPException(status_code=400, detail="Parent path is not a directory")
    target = parent / name
    if target.exists():
        raise HTTPException(status_code=409, detail="Folder already exists")
    try:
        target.mkdir(parents=True)
        root = _vault_root()
        rel = str(target.resolve().relative_to(root.resolve()))
        await _changed(_rel_of(target), "mkdir", source=req.source, is_dir=True)
        return {"path": rel, "name": name, "type": "folder"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class DeleteItemRequest(BaseModel):
    path: str
    source: str = "auto"


@router.delete("/api/vault/item")
async def delete_vault_item(req: DeleteItemRequest):
    """Delete a file or folder (recursive) from the vault."""
    rel = _safe_rel(req.path)

    s3 = _get_storage(req.source)
    if s3:
        if not rel:
            # `_safe_rel` answers "" for the root *and* for a traversal — either
            # way, not something to delete.
            raise HTTPException(status_code=400, detail="Cannot delete the vault root")
        if rel in _ENGINE_OWNED:
            # The same guard as the local branch: on object storage the sweep
            # below is what would remove every key under the prefix.
            raise HTTPException(
                status_code=403,
                detail=f"'{rel}' is written by the engine and holds every file it "
                       "has saved there — delete the files inside it instead",
            )
        # Delete as a file (idempotent if key doesn't exist)
        s3.delete(_s3_key_for(rel))
        # Also sweep any keys under a same-named folder prefix (recursive delete)
        swept = s3.list(f"{_S3_VAULT_REL}/{rel}/")
        for sub_key in swept:
            s3.delete(sub_key)
        await _changed(rel, "delete", source=req.source, is_dir=bool(swept))
        return {"status": "ok", "path": rel}

    p = _resolve(req.path)
    root = _vault_root().resolve()
    if p == root:
        # An empty path, ".", "./", "../" or "sub/.." all resolved to the root,
        # and the root is a directory — so this used to `rmtree` the vault.
        raise HTTPException(status_code=400, detail="Cannot delete the vault root")
    if not p.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    if _is_engine_owned_dir(p):
        raise HTTPException(
            status_code=403,
            detail=f"'{p.name}' is written by the engine and holds every file it "
                   "has saved there — delete the files inside it instead",
        )
    try:
        was_dir, rel = p.is_dir(), _rel_of(p)
        if was_dir:
            shutil.rmtree(p)
        else:
            p.unlink()
        await _changed(rel, "delete", source=req.source, is_dir=was_dir)
        return {"status": "ok", "path": req.path}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
