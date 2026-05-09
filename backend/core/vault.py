"""
Vault: Automatically saves large tool outputs to files and provides tools to query them.
Also resolves @[path] vault file mentions in user messages before they reach the LLM.

When any tool returns more than VAULT_THRESHOLD characters, the output is saved to
backend/data/vault/ and the LLM receives only the file path + metadata. The LLM can
then use the Filesystem MCP tools (read_file, search_files) to access parts of the
file without flooding its context window.
"""
import json
import re
from datetime import datetime
from pathlib import Path

from core.config import DATA_DIR
VAULT_DIR = Path(DATA_DIR) / "vault" / "tool_outputs"
VAULT_THRESHOLD = 100000  # characters (fallback default)


def _make_vault_path(tool_name: str, ext: str) -> Path:
    """Generate a unique, safe vault file path."""
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:20]
    safe_name = re.sub(r"[^\w]", "_", tool_name)[:40]
    return VAULT_DIR / f"{safe_name}_{timestamp}.{ext}"


def maybe_vault(tool_name: str, raw_output: str) -> str:
    """
    If raw_output exceeds the vault threshold (from settings), persist it to vault and return a
    compact JSON reference the LLM can act on with the vault read/search tools.
    Returns raw_output unchanged when under the threshold or vault is disabled.
    """
    from core.config import load_settings
    settings = load_settings()
    if not settings.get("vault_enabled", True):
        return raw_output
    threshold = settings.get("vault_threshold", VAULT_THRESHOLD)
    if len(raw_output) <= threshold:
        return raw_output

    # Decide extension: JSON gets pretty-validated, everything else is text.
    try:
        parsed = json.loads(raw_output)
        ext = "json"
        content = json.dumps(parsed, indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, ValueError):
        ext = "txt"
        content = raw_output

    path = _make_vault_path(tool_name, ext)
    path.write_text(content, encoding="utf-8")
    try:
        path.chmod(0o644)
    except Exception:
        pass

    total_lines = content.count("\n") + 1
    return json.dumps({
        "vault_file": str(path),
        "file_type": ext,
        "size_chars": len(raw_output),
        "total_lines": total_lines,
        "content_preview": content[:500],
        "message": (
            f"Output too large ({len(raw_output):,} chars, {total_lines:,} lines). Saved to vault at: {path}. "
            f"Instead: use read_file_by_lines with start_line/end_line to read a slice, "
            f"or grep to search for specific values."
        ),
    })


# ---------------------------------------------------------------------------
# Vault tool implementations — called directly by react_engine.py
# ---------------------------------------------------------------------------

_VAULT_USER_DIR = Path(DATA_DIR) / "vault"


def expand_vault_mentions(message: str) -> str:
    """
    Replace every @[relative/path] vault mention in the user message with the
    file's actual content, so the LLM receives the data inline.

    Example:
        "Log a job. Config: @[jobs-filepath.json]"
        →
        "Log a job. Config: @[jobs-filepath.json]\n<file content here>"
    """
    def _replace(match: re.Match) -> str:
        rel = match.group(1).strip()
        # Prevent path traversal
        try:
            resolved = (_VAULT_USER_DIR / rel).resolve()
            if not str(resolved).startswith(str(_VAULT_USER_DIR.resolve())):
                return match.group(0)  # leave as-is if traversal attempt
        except Exception:
            return match.group(0)

        if not resolved.exists() or not resolved.is_file():
            return match.group(0)  # leave as-is if file not found

        try:
            content = resolved.read_text(encoding="utf-8")
        except Exception:
            return match.group(0)

        return f"@[{rel}]\n```\n{content}\n```"

    return re.sub(r"@\[([^\]]+)\]", _replace, message)


def _safe_path(path: str) -> Path:
    """Return Path, rejecting obvious traversal attempts."""
    p = Path(path).resolve()
    return p


def tool_read_file_chunk(path: str, start_line: int, end_line: int) -> str:
    """Read lines [start_line, end_line] (1-indexed, inclusive) from any file."""
    try:
        p = _safe_path(path)
        if not p.exists():
            return json.dumps({"error": f"File not found: {path}"})
        lines = p.read_text(encoding="utf-8").splitlines()
        total = len(lines)
        s = max(1, start_line) - 1      # 0-indexed
        e = min(end_line, total)
        chunk = lines[s:e]
        return json.dumps({
            "path": path,
            "start_line": s + 1,
            "end_line": e,
            "total_lines": total,
            "content": "\n".join(chunk),
        })
    except Exception as ex:
        return json.dumps({"error": str(ex)})


def tool_search_file(path: str, query: str, context_lines: int = 5) -> str:
    """Grep-like search: returns matching lines with ±context_lines of surrounding context."""
    try:
        p = _safe_path(path)
        if not p.exists():
            return json.dumps({"error": f"File not found: {path}"})
        lines = p.read_text(encoding="utf-8").splitlines()
        q = query.lower()
        results = []
        covered: set[int] = set()

        for i, line in enumerate(lines):
            if q not in line.lower():
                continue
            start = max(0, i - context_lines)
            end = min(len(lines), i + context_lines + 1)
            if i in covered:
                continue
            covered.update(range(start, end))
            block = []
            for j in range(start, end):
                prefix = ">>>" if j == i else "   "
                block.append(f"{prefix} [L{j + 1}] {lines[j]}")
            results.append({
                "match_line": i + 1,
                "match": line,
                "context": "\n".join(block),
            })
            if len(results) >= 20:
                break

        return json.dumps({
            "path": path,
            "query": query,
            "matches_found": len(results),
            "results": results,
        })
    except Exception as ex:
        return json.dumps({"error": str(ex)})


def tool_read_json_chunk(path: str, offset: int = 0, limit: int = 50) -> str:
    """
    Read a slice of a JSON vault file.
    - Array root  → returns items[offset : offset+limit]
    - Object root → returns keys[offset : offset+limit] with their values
    """
    try:
        p = _safe_path(path)
        if not p.exists():
            return json.dumps({"error": f"File not found: {path}"})
        data = json.loads(p.read_text(encoding="utf-8"))

        if isinstance(data, list):
            total = len(data)
            chunk = data[offset: offset + limit]
            return json.dumps({
                "path": path,
                "root_type": "array",
                "total_items": total,
                "offset": offset,
                "limit": limit,
                "returned": len(chunk),
                "data": chunk,
            }, default=str)

        if isinstance(data, dict):
            keys = list(data.keys())
            total = len(keys)
            chunk_keys = keys[offset: offset + limit]
            chunk = {k: data[k] for k in chunk_keys}
            return json.dumps({
                "path": path,
                "root_type": "object",
                "total_keys": total,
                "all_keys": keys,
                "offset": offset,
                "limit": limit,
                "returned": len(chunk_keys),
                "data": chunk,
            }, default=str)

        # Scalar / other
        return json.dumps({
            "path": path,
            "root_type": type(data).__name__,
            "data": data,
        }, default=str)

    except Exception as ex:
        return json.dumps({"error": str(ex)})


def tool_search_json(path: str, query: str) -> str:
    """
    Recursively search JSON values for query string.
    Returns up to 20 matching {json_path, value} pairs.
    """
    try:
        p = _safe_path(path)
        if not p.exists():
            return json.dumps({"error": f"File not found: {path}"})
        data = json.loads(p.read_text(encoding="utf-8"))
        q = query.lower()
        results: list[dict] = []

        def _recurse(obj, jpath: str):
            if len(results) >= 20:
                return
            if isinstance(obj, dict):
                for k, v in obj.items():
                    _recurse(v, f"{jpath}.{k}")
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    _recurse(item, f"{jpath}[{i}]")
            else:
                val_str = str(obj)
                if q in val_str.lower():
                    results.append({"json_path": jpath, "value": val_str[:500]})

        _recurse(data, "$")
        return json.dumps({
            "path": path,
            "query": query,
            "matches_found": len(results),
            "results": results,
        })
    except Exception as ex:
        return json.dumps({"error": str(ex)})
