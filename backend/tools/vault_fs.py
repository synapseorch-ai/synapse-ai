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

import mcp.types as types
from mcp.server import Server

import core.vault_tools as vault_tools

app = Server("vault-server")


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    # Deliberately not awaiting `ready()`. Advertising a tool needs no tenant and
    # no settings, and making discovery wait on a database would put it back on
    # the handshake's critical path — the thing `core/tool_server.py::serve`
    # exists to clear.
    return vault_tools.tools()


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    # The tenant and the store are established by `bootstrap()`, which runs
    # alongside the handshake rather than ahead of it. A handler is the first
    # thing that actually needs either.
    from core.tool_server import ready

    await ready()

    return await vault_tools.call(name, arguments)


async def main():
    # Serve first, bootstrap alongside — see `core/tool_server.py::serve`.
    from core.tool_server import serve

    await serve(app)


if __name__ == "__main__":
    asyncio.run(main())
