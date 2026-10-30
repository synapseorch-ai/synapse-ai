"""
Lightweight file-reading MCP tool — safe for headless worker processes.

Exposes read_file_by_lines and grep_file without loading any code-search
index or embeddings. Both tools call _ensure_local_path() from core.vault
so they transparently download vault files from S3 when the local file is
missing (scale mode, cross-worker execution).
"""

import asyncio
import json
import os

import mcp.types as types
from mcp.server import Server

from core.file_roots import allowed_roots, refusal, within

app = Server("file-reader-server")


def _ensure(path: str) -> str:
    """Resolve an S3-backed vault path to a local file if needed."""
    try:
        from core.vault import _ensure_local_path
        return _ensure_local_path(path)
    except Exception:
        return path


def _read_lines(file_path: str, start_line: int, end_line: int, *, roots: list[str], start_char: int = 0,
                max_chars: int | None = None) -> dict:
    local = _ensure(file_path)
    # Before the existence check, so a refused path says nothing about
    # whether it exists (S5).
    if not within(local, roots):
        return {"error": refusal(file_path)}
    if not os.path.exists(local):
        return {"error": f"File not found: {file_path}"}
    if not os.path.isfile(local):
        return {"error": f"Not a file: {file_path}"}
    try:
        with open(local, "rb") as f:
            if b"\x00" in f.read(1024):
                return {"error": f"Binary file: {file_path}"}
    except Exception as e:
        return {"error": str(e)}
    try:
        from core.vault import read_cap, slice_lines

        with open(local, encoding="utf-8", errors="replace") as f:
            text = f.read()
        # The vault's own slicing, capped (D339 P2): one line of a crawl held a whole page.
        return {"path": file_path, **slice_lines(text, start_line, end_line, max_chars=read_cap(max_chars),
                                                 start_char=max(0, start_char))}
    except Exception as ex:
        return {"error": str(ex)}


def _grep(file_path: str, query: str, context_lines: int, *, roots: list[str]) -> dict:
    local = _ensure(file_path)
    # Before the existence check, so a refused path says nothing about
    # whether it exists (S5).
    if not within(local, roots):
        return {"error": refusal(file_path)}
    if not os.path.exists(local):
        return {"error": f"File not found: {file_path}"}
    if not os.path.isfile(local):
        return {"error": f"Not a file: {file_path}"}
    try:
        with open(local, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as ex:
        return {"error": str(ex)}

    from core.vault import _around

    q = query.lower()
    results = []
    covered: set[int] = set()
    for i, line in enumerate(lines):
        if q not in line.lower():
            continue
        if i in covered:
            continue
        start = max(0, i - context_lines)
        end = min(len(lines), i + context_lines + 1)
        covered.update(range(start, end))
        block = []
        for j in range(start, end):
            prefix = ">>>" if j == i else "   "
            block.append(f"{prefix} [L{j + 1}] {_around(lines[j].rstrip(), q)}")
        results.append({
            "match_line": i + 1,
            "match": _around(line.rstrip(), q),
            "context": "\n".join(block),
        })
        if len(results) >= 20:
            break

    return {
        "path": file_path,
        "query": query,
        "matches_found": len(results),
        "results": results,
    }


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="read_file_by_lines",
            annotations=types.ToolAnnotations(readOnlyHint=True, idempotentHint=True),
            description=(
                "Read a specific line range from any file (1-indexed, inclusive). "
                "Use this to read slices of large vault files instead of loading the whole file. "
                "Each read is capped: when it is cut, `next` gives the start_line (and start_char) to read on from. "
                "Supports local paths and vault files stored in S3 (scale mode)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the file (e.g. from a vault_file reference).",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "First line to read (1-indexed). Default 1.",
                        "default": 1,
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Last line to read (1-indexed, inclusive). Default 100.",
                        "default": 100,
                    },
                    "start_char": {
                        "type": "integer",
                        "description": "Where in start_line to begin, from a cut read's `next`. Default 0.",
                        "default": 0,
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "At most this many characters (the cap applies anyway).",
                    },
                },
                "required": ["file_path"],
            },
        ),
        types.Tool(
            name="grep_file",
            annotations=types.ToolAnnotations(readOnlyHint=True, idempotentHint=True),
            description=(
                "Search a file for lines matching a query string and return matches with surrounding context. "
                "Use on large vault files when you know what value or key to look for. "
                "Supports local paths and vault files stored in S3 (scale mode)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the file.",
                    },
                    "query": {
                        "type": "string",
                        "description": "String to search for (case-insensitive).",
                    },
                    "context_lines": {
                        "type": "integer",
                        "description": "Lines of context to include around each match. Default 5.",
                        "default": 5,
                    },
                },
                "required": ["file_path", "query"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    # The tenant, the store and this tenant's settings are established by
    # `bootstrap()`, which now runs alongside the handshake rather than ahead
    # of it. A handler is the first thing that actually needs any of them.
    from core.tool_server import ready

    await ready()

    if name == "read_file_by_lines":
        result = _read_lines(
            file_path=arguments.get("file_path", ""),
            start_line=int(arguments.get("start_line", 1)),
            end_line=int(arguments.get("end_line", 100)),
            roots=await allowed_roots(),
            start_char=int(arguments.get("start_char", 0) or 0),
            max_chars=arguments.get("max_chars"),
        )
    elif name == "grep_file":
        result = _grep(
            file_path=arguments.get("file_path", ""),
            query=arguments.get("query", ""),
            context_lines=int(arguments.get("context_lines", 5)),
            roots=await allowed_roots(),
        )
    else:
        result = {"error": f"Unknown tool: {name}"}

    return [types.TextContent(type="text", text=json.dumps(result))]


async def main():
    # This process serves exactly one tenant, and it has to be told which —
    # see core/tool_server.py.
    # Serve first, bootstrap alongside. `bootstrap()` reads Postgres, and
    # putting that ahead of `stdio_server()` meant the MCP handshake waited
    # on a database — a cold serverless resume expired the 60s bound and
    # took the whole chat turn with it. Handlers wait via `ready()`.
    from core.tool_server import serve

    await serve(app)


if __name__ == "__main__":
    asyncio.run(main())
