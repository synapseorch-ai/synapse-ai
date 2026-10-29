"""The vault's four read-only tools, for whichever process serves them (F1118).

`tools/vault_fs.py` serves them as a tool subprocess, told its tenant on
`SYNAPSE_TENANT_ID` and bootstrapped by `core/tool_server.py`. A multi-tenant
worker serves them in its own process instead (`build_server`, wired by
`core/scale/worker_server_module.py`): one server per tenant's pool entry, built
in that tenant's scope, so every read goes through the worker's blob store under
the caller's tenant. **Nothing here bootstraps or adopts a tenant**: in a worker
that would be one tenant for the whole process.

Every key goes through `core.vault.mention_key` before the vault prefix is added,
and the blob store adds the tenant (`core/storage/base.py::tenant_key`).
"""

import asyncio
import json

import mcp.types as types
from mcp.server import Server

from core.tool_result import as_result, failed

#: How many keys one `list_files` call will return.
#:
#: A vault with ten thousand objects would otherwise put ten thousand keys into
#: a model's context and spend most of a turn's budget on a directory listing.
_LIST_LIMIT = 500

#: How many files `search_files` will open before it stops.
#:
#: Each one is a `get` against the blob store — a network round trip on S3 — so
#: this bounds a tool call's wall-clock time, not just its output size. Read
#: with `_SEARCH_CONCURRENCY` in mind: serially, 200 S3 round trips would run
#: past the 60s MCP session bound and the search would fail rather than return
#: what it had found.
_SEARCH_FILE_LIMIT = 200

#: How many of those reads are in flight at once.
#:
#: `BlobStore.get` is synchronous, so each runs on a thread. Sixteen is chosen
#: against the bound above rather than for throughput: it keeps a full-width
#: search inside a few seconds while staying far below anything that would look
#: like a burst to S3 from a worker already running several jobs.
_SEARCH_CONCURRENCY = 16

#: A file bigger than this is not searched or read whole.
#:
#: Vault files are tool output and can be very large; `read_file` takes a line
#: range for exactly that reason.
_MAX_BYTES = 2_000_000


def _store():
    from core.storage import get_blob_store

    return get_blob_store()


_BINARY = "binary files are download-only and cannot be read by tools"


def _get_text(path: str) -> tuple[str | None, str | None]:
    """`(content, error)` for one vault path. Never raises."""
    from core.vault import binary_kind, mention_key

    key = mention_key(path)
    if key is None:
        return None, f"Not a vault path: {path}"
    kind = binary_kind(path)
    if kind is not None:
        return None, f"{path} is {kind} — {_BINARY}"
    try:
        raw = _store().get(key)
    except ValueError:
        # A decode error (`UnicodeDecodeError` is one): bytes, not text.
        return None, f"{path} is not a text file — {_BINARY}"
    except Exception as exc:  # noqa: BLE001 — reported to the model, not the log
        return None, f"Could not read {path}: {exc}"
    if raw is None:
        return None, f"Not found: {path}"
    if len(raw) > _MAX_BYTES:
        return None, (
            f"{path} is {len(raw)} bytes, over the {_MAX_BYTES} limit. "
            f"Use read_file with a line range."
        )
    return raw, None


def _listing(prefix: str) -> tuple[list[str] | None, str | None]:
    """`(paths, error)`: the vault's files under `prefix`, relative to the vault.

    Folder markers (`.keep`) are the vault page's, not files. A prefix that
    tries to climb out of the vault is refused rather than resolved.
    """
    from core.vault_backend import VAULT_PREFIX

    prefix = (prefix or "").strip().lstrip("/")
    if any(part == ".." for part in prefix.split("/")):
        return None, f"Not a vault path: {prefix}"
    root = f"{VAULT_PREFIX}/"
    keys = _store().list(root + prefix)
    paths = [
        k[len(root):] for k in keys
        if k.startswith(root) and not (k == f"{root}.keep" or k.endswith("/.keep"))
    ]
    return sorted(paths), None


def _err(message: str) -> list[types.TextContent]:
    # A failure, said the MCP way where a server answers (F1208): content its callers read, and `isError`.
    return failed(json.dumps({"error": message}))


def _ok(payload: dict) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps(payload))]


def tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="list_files",
            annotations=types.ToolAnnotations(readOnlyHint=True, idempotentHint=True),
            description=(
                "List files in the vault. Returns paths — relative to the vault, the "
                "same form as @[path] in a prompt — you can pass to read_file, "
                "read_json or search_files. Use a prefix to narrow the listing."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prefix": {
                        "type": "string",
                        "description": "Only list paths starting with this, e.g. 'tool_outputs/'. Empty lists the whole vault.",
                        "default": "",
                    },
                },
            },
        ),
        types.Tool(
            name="read_file",
            annotations=types.ToolAnnotations(readOnlyHint=True, idempotentHint=True),
            description=(
                "Read a line range from a vault file (1-indexed, inclusive). "
                "Prefer a range over reading a whole file — vault files are tool "
                "output and are often very large. Each read is capped: when it is "
                "cut, `next` gives the start_line (and start_char) to read on from."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "The path, relative to the vault, as returned by list_files or named by @[path]."},
                    "start_line": {"type": "integer", "description": "First line. Default 1.", "default": 1},
                    "end_line": {"type": "integer", "description": "Last line, inclusive. Default 200.", "default": 200},
                    "start_char": {"type": "integer", "description": "Where in start_line to begin, from a cut read's `next`. Default 0.", "default": 0},
                    "max_chars": {"type": "integer", "description": "At most this many characters (the cap applies anyway)."},
                },
                "required": ["path"],
            },
        ),
        types.Tool(
            name="search_files",
            annotations=types.ToolAnnotations(readOnlyHint=True, idempotentHint=True),
            description=(
                "Search the text of vault files for a string, returning matching "
                "lines with surrounding context. Narrow with a prefix when you can."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Text to look for. Case-insensitive."},
                    "prefix": {"type": "string", "description": "Only search paths starting with this.", "default": ""},
                    "context_lines": {"type": "integer", "description": "Lines of context each side. Default 3.", "default": 3},
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="read_json",
            annotations=types.ToolAnnotations(readOnlyHint=True, idempotentHint=True),
            description=(
                "Read one part of a JSON vault file: the value at json_path "
                "(e.g. results[3].markdown; empty for the whole file). A list "
                "returns items [offset:offset+limit], an object its keys from "
                "offset, a text its characters from offset. Each read is capped: "
                "`next_offset` reads on, and a part too big to show comes back as "
                "its outline, to read by its own json_path."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "The path, relative to the vault, as returned by list_files or named by @[path]."},
                    "json_path": {"type": "string", "description": "Keys and indexes to the part, e.g. results[3].links. Default: the root.", "default": ""},
                    "offset": {"type": "integer", "description": "Where to start. Default 0.", "default": 0},
                    "limit": {"type": "integer", "description": "How many. Default 50.", "default": 50},
                    "max_chars": {"type": "integer", "description": "At most this many characters (the cap applies anyway)."},
                },
                "required": ["path"],
            },
        ),
    ]


async def call(name: str, arguments: dict) -> list[types.TextContent]:
    from core.vault import json_slice, read_cap, search_text, slice_lines

    try:
        if name == "list_files":
            prefix = str(arguments.get("prefix", "") or "")
            paths, error = _listing(prefix)
            if error:
                return _err(error)
            return _ok({
                "prefix": prefix,
                "count": len(paths),
                "truncated": len(paths) > _LIST_LIMIT,
                "files": paths[:_LIST_LIMIT],
            })

        if name == "read_file":
            path = str(arguments.get("path", "") or "")
            if not path:
                return _err("path is required")
            text, error = _get_text(path)
            if error:
                return _err(error)
            return _ok({
                "path": path,
                **slice_lines(
                    text,
                    int(arguments.get("start_line", 1) or 1),
                    int(arguments.get("end_line", 200) or 200),
                    max_chars=read_cap(arguments.get("max_chars")),
                    start_char=max(0, int(arguments.get("start_char", 0) or 0)),
                ),
            })

        if name == "search_files":
            query = str(arguments.get("query", "") or "")
            if not query:
                return _err("query is required")
            prefix = str(arguments.get("prefix", "") or "")
            context = int(arguments.get("context_lines", 3) or 3)

            paths, error = _listing(prefix)
            if error:
                return _err(error)
            searched = paths[:_SEARCH_FILE_LIMIT]

            # Concurrently, and on threads, because `BlobStore.get` is
            # synchronous and each call is a network round trip on S3. Done
            # serially this loop is the tool call's whole latency budget.
            gate = asyncio.Semaphore(_SEARCH_CONCURRENCY)

            async def _scan(path: str) -> dict | None:
                async with gate:
                    text, error = await asyncio.to_thread(_get_text, path)
                if error or not text:
                    # A file that cannot be read is skipped rather than fatal: one
                    # oversized, binary or deleted object must not lose the
                    # matches in every other file.
                    return None
                found = search_text(text, query, context)
                return {"path": path, **found} if found["matches_found"] else None

            hits = [h for h in await asyncio.gather(*(_scan(p) for p in searched)) if h]
            # The whole answer within one read's cap (D339 P2): files in order until it is spent.
            room, kept, truncated = read_cap(arguments.get("max_chars")), [], False
            for hit in hits:
                cost = len(json.dumps(hit, ensure_ascii=False))
                if cost > room:
                    truncated = True
                    if not kept:
                        kept.append({**hit, "results": hit["results"][:1], "truncated": True})
                    break
                kept.append(hit)
                room -= cost
            hits_shown = kept

            return _ok({
                "query": query,
                "prefix": prefix,
                "files_searched": len(searched),
                "files_skipped": max(0, len(paths) - _SEARCH_FILE_LIMIT),
                "files_with_matches": len(hits),
                "results": hits_shown,
                **({"truncated": True, "note": "More matches than one read holds: narrow the prefix or the query."}
                   if truncated else {}),
            })

        if name == "read_json":
            path = str(arguments.get("path", "") or "")
            if not path:
                return _err("path is required")
            text, error = _get_text(path)
            if error:
                return _err(error)
            offset = max(0, int(arguments.get("offset", 0) or 0))
            limit = int(arguments.get("limit", 50) or 50)
            try:
                doc = json.loads(text)
            except json.JSONDecodeError as exc:
                return _err(f"{path} is not valid JSON: {exc}")
            part = json_slice(doc, str(arguments.get("json_path", "") or ""), offset, limit,
                              max_chars=read_cap(arguments.get("max_chars")))
            if "error" in part:
                return _err(part["error"])
            return _ok({"path": path, **part})

        return _err(f"Unknown tool: {name}")
    except Exception as exc:  # noqa: BLE001 — the model gets the reason
        return _err(str(exc))


def build_server() -> Server:
    """A fresh vault server, for one tenant's in-process session. Built per session, never shared."""
    server = Server("vault-server")

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return tools()

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        return as_result(await call(name, arguments))

    return server
