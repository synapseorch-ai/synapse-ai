"""
Code search MCP agent: vector similarity search over indexed code repositories.
Uses Gemini embeddings + pgvector for semantic code search.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from psycopg_pool import ConnectionPool

def _load_repo_paths() -> dict[str, str]:
    """Load repo_id → absolute_path mapping from repos.json (via core.config DATA_DIR)."""
    try:
        from core.config import DATA_DIR
        repos_file = os.path.join(DATA_DIR, "repos.json")
        with open(repos_file) as f:
            repos = json.load(f)
        return {r["id"]: r["path"].rstrip("/") for r in repos if r.get("id") and r.get("path")}
    except Exception:
        return {}


def _get_allowed_base_paths() -> list[str]:
    """Return allowed base paths: all configured repo paths + vault directory."""
    try:
        from core.config import DATA_DIR
        vault = os.path.join(DATA_DIR, "vault")
    except Exception:
        vault = None
    paths = list(_load_repo_paths().values())
    if vault:
        paths.append(vault)
    return paths


def _is_path_allowed(path: str) -> bool:
    """Return True if path resolves to within a configured repo or vault."""
    resolved = os.path.realpath(path)
    for base in _get_allowed_base_paths():
        base_real = os.path.realpath(base)
        if resolved == base_real or resolved.startswith(base_real + os.sep):
            return True
    return False


_pool: ConnectionPool | None = None
_pool_url: str | None = None
_VALID_REPO_ID = re.compile(r'^repo_\d+$')

app = Server("code-search-server")


def _get_pool() -> ConnectionPool:
    global _pool, _pool_url
    from core.config import load_settings as _load_settings, sanitize_db_url
    db_url = _load_settings().get("sql_connection_string", "")
    if db_url:
        db_url = sanitize_db_url(db_url)
    if not db_url:
        raise RuntimeError("No database URL configured. Set sql_connection_string in Settings → General.")
    if _pool is None or _pool_url != db_url:
        _pool = ConnectionPool(db_url, min_size=1, max_size=5)
        _pool_url = db_url
    return _pool


def _resolve_query_embedding_config() -> tuple[str, int, dict]:
    """Resolve the embedding model and dimension used for code search."""
    from core.config import load_settings
    from services.code_indexer import get_configured_embedding_model, probe_embedding_dim

    settings = load_settings()
    model = get_configured_embedding_model()
    dim = probe_embedding_dim(model, settings)
    return model, dim, settings


async def _get_query_embedding(query: str) -> tuple[list[float], str, int]:
    """Generate a query embedding using the same model and dimension as indexing."""
    from core.llm_providers import embed_batch

    model, dim, settings = await asyncio.to_thread(_resolve_query_embedding_config)
    embed_settings = dict(settings)
    embed_settings["__embed_output_dim"] = dim

    embeddings = await embed_batch([query], model, embed_settings)
    if not embeddings:
        raise RuntimeError("Embedding provider returned no query embedding.")

    query_vector = list(embeddings[0][:dim])
    if len(query_vector) != dim:
        raise RuntimeError(
            f"Embedding dimension mismatch for model '{model}': expected {dim}, got {len(query_vector)}"
        )

    return query_vector, model, dim



def _get_table_name(repo_id: str) -> str:
    return f"ci_{repo_id}__emb"


async def _search(query: str, repo_ids: list[str], top_k: int = 10) -> list[dict]:
    """Search indexed repos using cosine similarity."""
    try:
        pool = _get_pool()
    except Exception as e:
        return [{"error": f"Database connection failed: {e}"}]

    repo_path_map = _load_repo_paths()
    try:
        query_vector, model, dim = await _get_query_embedding(query)
    except Exception as e:
        return [{"error": f"Query embedding failed: {e}"}]

    vector_str = "[" + ",".join(str(v) for v in query_vector) + "]"
    all_results = []
    errors: list[str] = []

    for repo_id in repo_ids:
        if not _VALID_REPO_ID.match(repo_id):
            continue

        with pool.connection() as conn:
            with conn.cursor() as cur:
                try:
                    table_name = _get_table_name(repo_id)
                    cur.execute(f"""
                        SELECT filename, code, location,
                               embedding <=> %s::vector AS distance
                        FROM "{table_name}"
                        ORDER BY distance
                        LIMIT %s
                    """, (vector_str, top_k))

                    repo_root = repo_path_map.get(repo_id, "")
                    for row in cur.fetchall():
                        # location may be a psycopg Range object — convert to string
                        loc = row[2]
                        if hasattr(loc, 'lower') and hasattr(loc, 'upper'):
                            loc = f"{loc.lower}-{loc.upper}"
                        else:
                            loc = str(loc) if loc is not None else ""
                        filename = row[0].lstrip("/")
                        full_path = f"{repo_root}/{filename}" if repo_root else filename
                        all_results.append({
                            "repo_id": repo_id,
                            "filename": filename,
                            "full_path": full_path,
                            "code": row[1],
                            "location": loc,
                            "score": round(1.0 - row[3], 5)
                        })
                except Exception as e:
                    err_text = str(e)
                    if "different vector dimensions" in err_text:
                        err_text = (
                            f"{err_text}. search_codebase generated a {dim}-dim query vector using "
                            f"'{model}'. Reindex '{repo_id}' if it was indexed with a different embedding model."
                        )
                    sys.stderr.write(f"Error querying {table_name}: {err_text}\n")
                    errors.append(f"{repo_id}: {err_text}")

    all_results.sort(key=lambda x: x["score"], reverse=True)
    if not all_results and errors:
        return [{"error": "; ".join(errors)}]
    return all_results[:top_k]


def _grep_file(
    file_path: str,
    pattern: str,
    ignore_case: bool = False,
    fixed: bool = False,
    context: int = 0,
    max_matches: int = 1000,
) -> list[dict]:
    """Search for a pattern inside a single file (grep-like behavior).

    - `pattern` is treated as a regular expression unless `fixed` is True (literal match).
    - `ignore_case` enables case-insensitive matching.
    - `context` returns that many lines before/after each match.
    """
    resolved = file_path if os.path.isabs(file_path) else os.path.join(os.getcwd(), file_path)

    if not os.path.exists(resolved):
        return [{"error": f"File not found: {file_path}. Use an absolute path (e.g. as returned by list_directory or search_files)."}]
    if not os.path.isfile(resolved):
        return [{"error": f"Not a file: {file_path}. Use an absolute path to a file."}]

    # Detect binary files (simple heuristic)
    try:
        with open(resolved, "rb") as f:
            sample = f.read(1024)
            if b"\x00" in sample:
                return [{"error": f"Binary file: {file_path}"}]
    except Exception as e:
        return [{"error": str(e)}]

    flags = re.MULTILINE
    if ignore_case:
        flags |= re.IGNORECASE

    pat = re.escape(pattern) if fixed else pattern
    try:
        regex = re.compile(pat, flags)
    except re.error as e:
        return [{"error": f"Invalid regular expression: {e}"}]

    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return [{"error": str(e)}]

    results: list[dict] = []
    for i, line in enumerate(lines):
        for m in regex.finditer(line):
            item: dict = {
                "filename": file_path,
                "line_number": i + 1,
                "line": line.rstrip("\n"),
                "match": m.group(0),
                "start": m.start(),
                "end": m.end(),
            }
            if context > 0:
                start_ctx = max(0, i - context)
                end_ctx = min(len(lines), i + context + 1)
                item["pre_context"] = [l.rstrip("\n") for l in lines[start_ctx:i]]
                item["post_context"] = [l.rstrip("\n") for l in lines[i+1:end_ctx]]

            results.append(item)
            if len(results) >= max_matches:
                return results

    return results


def _grep_folder(
    folder_path: str,
    pattern: str,
    file_pattern: str = "*",
    ignore_case: bool = False,
    fixed: bool = False,
    context: int = 0,
    max_matches: int = 1000,
    recursive: bool = True,
) -> list[dict]:
    """Search for a pattern across all files in a folder matching file_pattern."""
    import glob as _glob

    resolved = folder_path if os.path.isabs(folder_path) else os.path.join(os.getcwd(), folder_path)
    if not os.path.exists(resolved):
        return [{"error": f"Folder not found: {folder_path}. Use an absolute path."}]
    if not os.path.isdir(resolved):
        return [{"error": f"Not a directory: {folder_path}."}]

    glob_pat = os.path.join(resolved, "**", file_pattern) if recursive else os.path.join(resolved, file_pattern)
    files = sorted(f for f in _glob.glob(glob_pat, recursive=recursive) if os.path.isfile(f))

    all_results: list[dict] = []
    for fpath in files:
        if len(all_results) >= max_matches:
            break
        remaining = max_matches - len(all_results)
        hits = _grep_file(fpath, pattern, ignore_case=ignore_case, fixed=fixed, context=context, max_matches=remaining)
        all_results.extend(h for h in hits if "error" not in h)

    return all_results


def _glob_files(
    folder_path: str,
    pattern: str = "**/*",
    recursive: bool = True,
    include_dirs: bool = False,
    include_hidden: bool = True,
    max_results: int = 1000,
) -> list[dict] | list[str]:
    """List files under `folder_path` matching `pattern` using Python glob rules.

    Returns relative paths from `folder_path`. Uses `glob` and supports `**` for recursion.
    """
    resolved = folder_path if os.path.isabs(folder_path) else os.path.join(os.getcwd(), folder_path)

    if not os.path.exists(resolved):
        return [{"error": f"Folder not found: {folder_path}. Use an absolute path (e.g. as returned by list_allowed_directories or list_directory)."}]
    if not os.path.isdir(resolved):
        return [{"error": f"Not a directory: {folder_path}. Use an absolute path to a folder."}]

    import glob as _glob

    search_pattern = os.path.join(resolved, pattern)
    iterator = _glob.iglob(search_pattern, recursive=recursive)

    results: list[str] = []
    for p in iterator:
        name = os.path.basename(p)
        if not include_hidden and name.startswith("."):
            continue
        if include_dirs or os.path.isfile(p):
            results.append(os.path.relpath(p, resolved))
            if len(results) >= max_results:
                break

    results.sort()
    return results


def _read_file_by_lines(file_path: str, start_line: int = 1, end_line: int = 100, repo_id: str | None = None) -> dict:
    """Read lines [start_line, end_line] (1-indexed, inclusive) from a file."""
    resolved = None
    if os.path.isabs(file_path):
        if os.path.exists(file_path):
            resolved = file_path
            
    # Fallback: absolute path from a different user/container context — try local vault by filename
    if not resolved and os.path.isabs(file_path) and "vault" in file_path:
        try:
            from core.config import DATA_DIR
            filename = os.path.basename(file_path)
            local_candidate = os.path.join(DATA_DIR, "vault", "tool_outputs", filename)
            if os.path.exists(local_candidate):
                resolved = local_candidate
        except Exception:
            pass

    if not resolved:
        cwd = os.getcwd()

        # Check vault directory relative to cwd
        if file_path.startswith("data/vault/"):
            candidate = os.path.join(cwd, file_path)
            if os.path.exists(candidate):
                resolved = candidate
                
        if not resolved:
            clean_path = file_path[11:] if file_path.startswith("data/vault/") else file_path
            candidate = os.path.join(cwd, "data", "vault", clean_path)
            if os.path.exists(candidate):
                resolved = candidate

        if not resolved:
            repo_path_map = _load_repo_paths()
            if repo_id and repo_id in repo_path_map:
                candidate = os.path.join(repo_path_map[repo_id], file_path)
                if os.path.exists(candidate):
                    resolved = candidate
            
            if not resolved:
                candidate = os.path.join(cwd, file_path)
                if os.path.exists(candidate):
                    resolved = candidate

    if not resolved or not os.path.exists(resolved):
        return {"error": f"File not found: {file_path}. Checked absolute path, vault directory, and passed repo."}
    if not os.path.isfile(resolved):
        return {"error": f"Not a file: {file_path}."}
    try:
        with open(resolved, "rb") as f:
            if b"\x00" in f.read(1024):
                return {"error": f"Binary file: {file_path}"}
    except Exception as e:
        return {"error": str(e)}
    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        total = len(lines)
        s = max(1, start_line) - 1
        e = min(end_line, total)
        chunk = [l.rstrip("\n") for l in lines[s:e]]
        return {
            "path": file_path,
            "start_line": s + 1,
            "end_line": e,
            "total_lines": total,
            "content": "\n".join(chunk),
        }
    except Exception as ex:
        return {"error": str(ex)}


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="search_codebase",
            description=(
                "Search indexed code repositories for relevant code snippets using semantic vector search. "
                "Returns matching code with filename, location, and relevance score. "
                "You MUST provide repo_ids — check the LINKED CODE REPOSITORIES section in your system prompt for available repo IDs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language or code search query"
                    },
                    "repo_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of repo IDs to search"
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of results to return (default 10)",
                        "default": 10
                    }
                },
                "required": ["query", "repo_ids"]
            },
        ),
        types.Tool(
            name="grep",
            description=(
                "Search for a pattern inside a file or across all files in a folder (grep-like). "
                "Pass a file path to search that file, or a folder path to search all matching files within it. "
                "Use `file_pattern` to filter by extension when searching a folder (e.g. '*.py', '*.ts'). "
                "Pattern is a regex by default; set `fixed` to true for literal matching. "
                "Returns matches with file path and line numbers. "
                "IMPORTANT: `path` must be an absolute path."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to a file or folder to search in"},
                    "pattern": {"type": "string", "description": "Regex or literal pattern to search for"},
                    "file_pattern": {"type": "string", "description": "When path is a folder: glob pattern to filter files (e.g. '*.py', '*.ts'). Default: all files", "default": "*"},
                    "recursive": {"type": "boolean", "description": "When path is a folder: search subdirectories recursively", "default": True},
                    "ignore_case": {"type": "boolean", "default": False},
                    "fixed": {"type": "boolean", "description": "Treat pattern as literal substring (like grep -F)", "default": False},
                    "context": {"type": "integer", "description": "Number of context lines before/after each match", "default": 0},
                    "max_matches": {"type": "integer", "default": 1000}
                },
                "required": ["path", "pattern"]
            },
        ),
        types.Tool(
            name="glob",
            description=(
                "List files under a folder matching a glob pattern. Returns paths relative to the provided folder. "
                "IMPORTANT: `folder_path` must be an absolute path (e.g. from list_allowed_directories or list_directory)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "folder_path": {"type": "string", "description": "Absolute path to the folder to search in (must be absolute, not relative)"},
                    "pattern": {"type": "string", "description": "Glob pattern (e.g. '**/*.py')", "default": "**/*"},
                    "recursive": {"type": "boolean", "default": True},
                    "include_dirs": {"type": "boolean", "default": False},
                    "include_hidden": {"type": "boolean", "default": True},
                    "max_results": {"type": "integer", "default": 1000}
                },
                "required": ["folder_path"]
            },
        ),
        types.Tool(
            name="read_file_by_lines",
            description=(
                "Read a specific range of lines from a file (1-indexed, inclusive). Returns the content, "
                "start/end line numbers, and total line count. Use this instead of read_file when you only "
                "need a slice of a large file or a vault file. "
                "You can provide an absolute path, or a relative file name (e.g. for vault files or repo files). "
                "The tool will automatically search in the active repositories and vault folder if a direct path is not found."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Absolute path, or relative path (e.g., filename in vault or repo)"},
                    "repo_id": {"type": "string", "description": "Optional: Repository ID if using a relative file_path"},
                    "start_line": {"type": "integer", "description": "First line to read (1-indexed, inclusive)", "default": 1},
                    "end_line": {"type": "integer", "description": "Last line to read (1-indexed, inclusive)", "default": 100},
                },
                "required": ["file_path"]
            },
        ),
    ]


@app.call_tool()
async def call_tool(
    name: str, arguments: dict
) -> list[types.TextContent]:
    try:
        if name == "search_codebase":
            query = arguments.get("query", "")
            repo_ids = arguments.get("repo_ids", [])
            top_k = arguments.get("top_k", 10)

            if not query or not repo_ids:
                return [types.TextContent(type="text", text=json.dumps({"error": "Both 'query' and 'repo_ids' are required."}))]

            results = await _search(query, repo_ids, top_k)
            return [types.TextContent(type="text", text=json.dumps({"results": results}, ensure_ascii=False))]

        if name == "grep":
            # Accept both new `path` and legacy `file_path` for backward compatibility
            path = arguments.get("path") or arguments.get("file_path")
            pattern = arguments.get("pattern", "")
            if not path or pattern == "":
                return [types.TextContent(type="text", text=json.dumps({"error": "Both 'path' and 'pattern' are required."}))]

            ignore_case = bool(arguments.get("ignore_case", False))
            fixed = bool(arguments.get("fixed", False))
            context = int(arguments.get("context", 0))
            max_matches = int(arguments.get("max_matches", 1000))

            resolved = path if os.path.isabs(path) else os.path.join(os.getcwd(), path)
            if not _is_path_allowed(resolved):
                return [types.TextContent(type="text", text=json.dumps({"error": f"Access denied: '{path}' is not within a configured repository or vault."}))]
            if os.path.isdir(resolved):
                file_pattern = arguments.get("file_pattern", "*")
                recursive = bool(arguments.get("recursive", True))
                results = _grep_folder(path, pattern, file_pattern=file_pattern, ignore_case=ignore_case, fixed=fixed, context=context, max_matches=max_matches, recursive=recursive)
            else:
                results = _grep_file(path, pattern, ignore_case=ignore_case, fixed=fixed, context=context, max_matches=max_matches)

            return [types.TextContent(type="text", text=json.dumps({"results": results}, ensure_ascii=False))]

        if name == "glob":
            folder_path = arguments.get("folder_path")
            if not folder_path:
                return [types.TextContent(type="text", text=json.dumps({"error": "'folder_path' is required."}))]

            _fp_resolved = folder_path if os.path.isabs(folder_path) else os.path.join(os.getcwd(), folder_path)
            if not _is_path_allowed(_fp_resolved):
                return [types.TextContent(type="text", text=json.dumps({"error": f"Access denied: '{folder_path}' is not within a configured repository or vault."}))]

            pattern = arguments.get("pattern", "**/*")
            recursive = bool(arguments.get("recursive", True))
            include_dirs = bool(arguments.get("include_dirs", False))
            include_hidden = bool(arguments.get("include_hidden", True))
            max_results = int(arguments.get("max_results", 1000))

            results = _glob_files(folder_path, pattern=pattern, recursive=recursive, include_dirs=include_dirs, include_hidden=include_hidden, max_results=max_results)
            return [types.TextContent(type="text", text=json.dumps({"results": results}, ensure_ascii=False))]

        if name == "read_file_by_lines":
            file_path = arguments.get("file_path")
            if not file_path:
                return [types.TextContent(type="text", text=json.dumps({"error": "'file_path' is required."}))]
            start_line = int(arguments.get("start_line", 1))
            end_line = int(arguments.get("end_line", 100))
            repo_id = arguments.get("repo_id")
            result = _read_file_by_lines(file_path, start_line=start_line, end_line=end_line, repo_id=repo_id)
            return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        return [types.TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]
    except Exception as e:
        sys.stderr.write(f"ERROR: call_tool error: {e}\n")
        return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
