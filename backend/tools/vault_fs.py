"""
The vault, as an MCP server, addressed by key rather than by path.

## Why this exists

The worker fleet used to reach vault files through the Node
`@modelcontextprotocol/server-filesystem`, rooted at `core/vault.py::_vault_root()`.
Two things were wrong with that and only the first was obvious.

It spawned `npx`, and no scale-mode image ships Node — so it failed on every
tenant's module build, forever, taking about seven seconds each time to raise
`FileNotFoundError` for a binary that does not exist.

The deeper problem is that it took a **directory**. On a fleet the vault
directory is a working copy that `core/vault_backend.py` materialises from the
blob store on demand, so the server read whatever had been hydrated — reporting
an empty vault on any replica that had not served that tenant before. Where the
blob store is an object store there is no directory to root at at all.

This server goes through `core.storage.get_blob_store()` instead. It reads the
vault itself rather than a copy of it, one implementation serves a local install
and an object store alike, and there is no Node.

## Tenancy, and the vault

**Every key is scoped by `core/storage/base.py::tenant_key`, inside the store.**
Nothing here adds the tenant, which is deliberate: that module says the prefixing
lives in the store "because a call site that forgets is a cross-tenant read and
there are dozens of them". This file is one more call site and gets no exception.

The tenant itself arrives on `SYNAPSE_TENANT_ID` and is adopted by
`core/tool_server.py::bootstrap`, which is why every handler awaits `ready()`
before touching the store.

**Inside the tenant, this server sees `vault/` and nothing else.** A path the
model passes is relative to the vault — `notes/q3 plan.md`, `tool_outputs/x.json`
— exactly what `@[path]` names in a prompt, and it goes through the same guard
(`core.vault.mention_key`) before the prefix is added. It used to be a key
relative to the whole tenant, so `list_files("")` listed `logs/`, `memory/` and
`attachments/`, and `read_file` could open an orchestration log with its
rendered globals. A binary the vault holds (a PDF, an image — download-only) is
named as such rather than handed to the model as a decode error.

## Read-only, for now

`list_files`, `read_file`, `search_files`, `read_json`. No write and no delete.
Tool output still reaches the vault through `core/vault.py::maybe_vault`, so
agents can still produce files; letting one write arbitrary keys raises quota,
abuse and metering questions that belong with the document-RAG work rather than
here.

`prefix` narrows the listing and search tools within the vault (`tool_outputs/`,
`notes/`); it cannot reach above it.
"""

import asyncio
import json

import mcp.types as types
from mcp.server import Server

app = Server("vault-server")

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
    return [types.TextContent(type="text", text=json.dumps({"error": message}))]


def _ok(payload: dict) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps(payload))]


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    # Deliberately not awaiting `ready()`. Advertising a tool needs no tenant and
    # no settings, and making discovery wait on a database would put it back on
    # the handshake's critical path — the thing `core/tool_server.py::serve`
    # exists to clear.
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
                "output and are often very large."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "The path, relative to the vault, as returned by list_files or named by @[path]."},
                    "start_line": {"type": "integer", "description": "First line. Default 1.", "default": 1},
                    "end_line": {"type": "integer", "description": "Last line, inclusive. Default 200.", "default": 200},
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
                "Read a slice of a JSON vault file. An array root returns items "
                "[offset:offset+limit]; an object root returns that slice of its keys."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "The path, relative to the vault, as returned by list_files or named by @[path]."},
                    "offset": {"type": "integer", "description": "Where to start. Default 0.", "default": 0},
                    "limit": {"type": "integer", "description": "How many. Default 50.", "default": 50},
                },
                "required": ["path"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    # The tenant and the store are established by `bootstrap()`, which runs
    # alongside the handshake rather than ahead of it. A handler is the first
    # thing that actually needs either.
    from core.tool_server import ready

    await ready()

    from core.vault import search_text, slice_lines

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

            return _ok({
                "query": query,
                "prefix": prefix,
                "files_searched": len(searched),
                "files_skipped": max(0, len(paths) - _SEARCH_FILE_LIMIT),
                "files_with_matches": len(hits),
                "results": hits,
            })

        if name == "read_json":
            path = str(arguments.get("path", "") or "")
            if not path:
                return _err("path is required")
            text, error = _get_text(path)
            if error:
                return _err(error)
            offset = int(arguments.get("offset", 0) or 0)
            limit = int(arguments.get("limit", 50) or 50)
            try:
                doc = json.loads(text)
            except json.JSONDecodeError as exc:
                return _err(f"{path} is not valid JSON: {exc}")

            if isinstance(doc, list):
                return _ok({
                    "path": path, "root": "array", "total": len(doc),
                    "offset": offset, "items": doc[offset:offset + limit],
                })
            if isinstance(doc, dict):
                keys_slice = list(doc.keys())[offset:offset + limit]
                return _ok({
                    "path": path, "root": "object", "total": len(doc),
                    "offset": offset, "items": {k: doc[k] for k in keys_slice},
                })
            return _ok({"path": path, "root": "scalar", "value": doc})

        return _err(f"Unknown tool: {name}")
    except Exception as exc:  # noqa: BLE001 — the model gets the reason
        return _err(str(exc))


async def main():
    # Serve first, bootstrap alongside — see `core/tool_server.py::serve`.
    from core.tool_server import serve

    await serve(app)


if __name__ == "__main__":
    asyncio.run(main())
