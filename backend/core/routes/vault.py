"""
Vault management REST API.

Handles CRUD operations on the user-managed vault directory (data/vault/ minus the
auto-generated tool_outputs/ subfolder).  Only .json, .md, and .txt files may be created.

When the blob store has no filesystem — S3 — every operation goes through it, by key,
with the tenant applied inside the store (`core.storage`).  A store with a directory
is the vault itself, and the routes work on that directory.
"""
import json
import os
import shutil
import unicodedata
from pathlib import Path
from posixpath import normpath
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel


router = APIRouter()

# Root directory exposed to the frontend (local fallback)
def VAULT_USER_DIR() -> Path:
    """The current tenant's vault root, from the blob store."""
    from core.vault import _vault_root
    return _vault_root()
_EXCLUDED = set()
_ALLOWED_EXTENSIONS = {".json", ".md", ".txt"}

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


def _s3_key_for(rel: str) -> str:
    """Return the S3 relative key for a vault file path, e.g. 'vault/research.md'."""
    if rel:
        return f"{_S3_VAULT_REL}/{rel}"
    return _S3_VAULT_REL


def _s3_build_tree(keys: list[str]) -> list[dict]:
    """
    Convert a flat list of S3 relative keys (e.g. ['vault/a.md', 'vault/sub/b.json'])
    into the same nested tree structure the frontend expects.
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
            items.append({
                "name": f,
                "path": rel_path,
                "type": "file",
                "ext": suffix,
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


def _is_excluded(path: Path) -> bool:
    root = _vault_root().resolve()
    rel = path.resolve().relative_to(root)
    parts = rel.parts
    return len(parts) > 0 and parts[0] in _EXCLUDED


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
            if entry.name in _EXCLUDED:
                continue
            items.append({
                "name": entry.name,
                "path": rel_path,
                "type": "folder",
                "children": _build_tree(entry, root),
            })
        elif entry.is_file():
            items.append({
                "name": entry.name,
                "path": rel_path,
                "type": "file",
                "ext": entry.suffix.lower(),
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
        keys = s3.list(_S3_VAULT_REL + "/")
        return {"tree": _s3_build_tree(keys), "source": "s3"}
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
        if p.is_file() and not _is_excluded(p):
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


@router.get("/api/vault/file")
async def get_vault_file(path: str = Query(..., description="Relative path to vault file"), source: str = Query("auto")):
    """Return the content of a vault file."""
    s3 = _get_storage(source)
    rel = _safe_rel(path)
    name = Path(path).name
    ext = Path(name).suffix.lower()

    if s3:
        try:
            content = s3.get(_s3_key_for(rel))
        except ValueError:
            raise HTTPException(status_code=415, detail="Not a text file")
        if content is None:
            raise HTTPException(status_code=404, detail="File not found")
        return {"path": rel, "name": name, "ext": ext, "content": content}

    p = _resolve(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    if _is_excluded(p):
        raise HTTPException(status_code=403, detail="Access denied")
    try:
        content = p.read_text(encoding="utf-8")
        return {"path": path, "name": p.name, "ext": p.suffix.lower(), "content": content}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class CreateFileRequest(BaseModel):
    path: str          # relative parent folder path (empty = vault root)
    name: str          # filename including extension (.json or .md)
    content: Optional[str] = ""
    source: str = "auto"


@router.post("/api/vault/file")
async def create_vault_file(req: CreateFileRequest):
    """Create a new .json, .md, or .txt file in the vault."""
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
        elif suffix == ".txt":
            initial_content = ""
        else:
            initial_content = f"# {Path(name).stem}\n\n"

    s3 = _get_storage(req.source)
    if s3:
        if s3.exists(_s3_key_for(file_rel)):
            raise HTTPException(status_code=409, detail="File already exists")
        s3.put(_s3_key_for(file_rel), initial_content)
        return {"path": file_rel, "name": name, "ext": suffix, "content": initial_content}

    parent = _resolve(req.path) if req.path.strip() else _vault_root()
    if not parent.is_dir():
        raise HTTPException(status_code=400, detail="Parent path is not a directory")
    if _is_excluded(parent):
        raise HTTPException(status_code=403, detail="Cannot create files in excluded directories")
    target = parent / name
    if target.exists():
        raise HTTPException(status_code=409, detail="File already exists")
    try:
        target.write_text(initial_content, encoding="utf-8")
        root = _vault_root()
        rel = str(target.resolve().relative_to(root.resolve()))
        return {"path": rel, "name": target.name, "ext": suffix, "content": initial_content}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class UpdateFileRequest(BaseModel):
    path: str
    content: str
    source: str = "auto"


@router.put("/api/vault/file")
async def update_vault_file(req: UpdateFileRequest):
    """Update the content of an existing vault file."""
    rel = _safe_rel(req.path)
    suffix = Path(req.path).suffix.lower()

    s3 = _get_storage(req.source)
    if s3:
        if suffix not in _ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=400, detail="Cannot edit this file type")
        s3.put(_s3_key_for(rel), req.content)
        return {"status": "ok", "path": rel}

    p = _resolve(req.path)
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    if _is_excluded(p):
        raise HTTPException(status_code=403, detail="Access denied")
    if p.suffix.lower() not in _ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Cannot edit this file type")
    try:
        p.write_text(req.content, encoding="utf-8")
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
    if name in _EXCLUDED:
        raise HTTPException(status_code=400, detail="Reserved folder name")

    parent_rel = _safe_rel(req.path) if req.path.strip() else ""
    folder_rel = f"{parent_rel}/{name}" if parent_rel else name

    s3 = _get_storage(req.source)
    if s3:
        # S3 has no real folders; a `.keep` marker makes the folder visible.
        # One byte, never empty: a zero-byte PUT stalls a MinIO connection for
        # ~30 s on the next PUT (the cloud vault learnt this the hard way).
        placeholder_key = _s3_key_for(f"{folder_rel}/.keep")
        s3.put(placeholder_key, "\n")
        return {"path": folder_rel, "name": name, "type": "folder"}

    parent = _resolve(req.path) if req.path.strip() else _vault_root()
    if not parent.is_dir():
        raise HTTPException(status_code=400, detail="Parent path is not a directory")
    if _is_excluded(parent):
        raise HTTPException(status_code=403, detail="Cannot create folders in excluded directories")
    target = parent / name
    if target.exists():
        raise HTTPException(status_code=409, detail="Folder already exists")
    try:
        target.mkdir(parents=True)
        root = _vault_root()
        rel = str(target.resolve().relative_to(root.resolve()))
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
        # Delete as a file (idempotent if key doesn't exist)
        s3.delete(_s3_key_for(rel))
        # Also sweep any keys under a same-named folder prefix (recursive delete)
        for sub_key in s3.list(f"{_S3_VAULT_REL}/{rel}/"):
            s3.delete(sub_key)
        return {"status": "ok", "path": rel}

    p = _resolve(req.path)
    root = _vault_root().resolve()
    if p == root:
        # An empty path, ".", "./", "../" or "sub/.." all resolved to the root,
        # and the root is a directory — so this used to `rmtree` the vault.
        raise HTTPException(status_code=400, detail="Cannot delete the vault root")
    if not p.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    if _is_excluded(p):
        raise HTTPException(status_code=403, detail="Cannot delete system directories")
    rel_parts = p.resolve().relative_to(root).parts
    if len(rel_parts) == 1 and rel_parts[0] in _EXCLUDED:
        raise HTTPException(status_code=403, detail="Cannot delete system directories")
    try:
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        return {"status": "ok", "path": req.path}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
