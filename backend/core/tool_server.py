"""
What a native tool server has to do before it serves its first request.

Native tools are MCP servers running as subprocesses. That makes them ordinary
Python processes that happen to have been spawned by the engine — and it means
two things the engine sets up for itself do not reach them:

1. **A ContextVar does not cross a process boundary.** `tools/bash.py` and
   `tools/sandbox.py` call `core.vault._vault_root()`, which resolves
   `get_blob_store().path_for(...)` → `root / get_tenant()`. Inside a subprocess
   that is always the default tenant, whoever called the tool. Harmless in a
   single-tenant install; a cross-tenant read on a shared fleet.

2. **Nobody installs the settings provider.** Only `core/server.py` and
   `core/scale/worker.py` call `settings_runtime.install_provider()`, so
   `load_settings()` in a tool subprocess returns `default_settings()`. That
   became true when `_load_settings_from_disk` was deleted in Phase 6 and went
   unnoticed: `bash_allowed_dirs` and `vault_threshold` quietly stopped being
   honoured wherever a tool read them for itself.

`bootstrap()` fixes both, and every tool server in `TENANT_SCOPED_TOOLS` calls it
as the first thing in `main()`.

`SYNAPSE_TENANT_ID`, `SYNAPSE_DOCUMENT_RESOLVER` and `SYNAPSE_SESSION_BINDER`
------------------------------------------------------------------------------
This module is the only one permitted to read them, in the same way
`core/store/importer.py` is the only module permitted to name
`SYNAPSE_DATA_DIR`. `tests/unit/test_tool_server.py` asserts the allowlist, so a
third one is a conversation rather than a silent widening.

`SYNAPSE_DOCUMENT_RESOLVER` names an import path — `package.module:function` —
installed as `core/store/collections.py`'s document resolver. It exists because
a tool subprocess reads the store *itself*: an embedder that keeps credentials
outside `collections` and stores references cannot swap them in the parent, so
the subprocess has to be told how. Unset in the shipped product, where documents
hold their own values and there is nothing to resolve.

It names code to import, so whatever can set it can already run code in this
process — it widens nothing that spawning the subprocess did not already.

It is worth being precise about what it does and does not do. It fixes the
tenant of a process that already exists to serve exactly one tenant — the parent
spawns a separate server per tenant and labels each. It does **not** enable
multi-tenancy: `tenant_scope()` is still shut unless an embedder registers a
resource provider, so there is no path from this variable to a process serving
two tenants, and nothing in the shipped product sets it at all.

`SYNAPSE_SESSION_BINDER` is the same idea for the *connection*: an import path
installed as `core/store/engine.py`'s session binder, run on every session this
process opens. A deployment that gates the same rows a second time — row-level
security compares against a per-transaction setting — cannot otherwise be heard
from inside a subprocess, because the store is opened here rather than passed
in. Also unset in the shipped product.
"""
from __future__ import annotations

import asyncio
import os
import sys


def process_tenant() -> str:
    """The tenant this subprocess was spawned to serve, if it was told."""
    return os.getenv("SYNAPSE_TENANT_ID", "").strip()


def _import_named(spec: str) -> object | None:
    """Resolve a `package.module:function` spec, or None if it is not one."""
    spec = spec.strip()
    if not spec or ":" not in spec:
        return None

    import importlib

    module_name, _, attribute = spec.partition(":")
    return getattr(importlib.import_module(module_name), attribute)


def _install_document_resolver() -> None:
    """Point `collections` at the embedder's resolver, if one was named."""
    resolver = _import_named(os.getenv("SYNAPSE_DOCUMENT_RESOLVER", ""))
    if resolver is None:
        return

    from core.store import collections

    collections.set_document_resolver(resolver)


def _install_session_binder() -> None:
    """Let the deployment speak on every session this process opens.

    The sibling of the resolver above, and it exists for the same reason from
    the other direction. The resolver answers "this stored value is a
    reference"; this answers "this connection has not yet said who it is".

    A tool subprocess opens the store **itself**, so a deployment that enforces
    tenancy on the connection — Postgres row-level security, in the case that
    prompted this — has no way to make itself heard: the parent's binding does
    not cross `fork+exec`, and `collections.load()` opens its own session. The
    symptom is not an error. It is a table that reads as empty, which looks
    exactly like a workspace that has configured nothing.

    Unset in the shipped product, where the `tenant_id` column is the whole of
    tenancy and there is no second gate to satisfy.
    """
    binder = _import_named(os.getenv("SYNAPSE_SESSION_BINDER", ""))
    if binder is None:
        return

    from core.store import engine

    engine.set_session_binder(binder)


def _attach_store() -> None:
    """Bind the store without letting this process create the schema.

    `get_store()` is lazy and runs `init_db()` — `create_all` plus migrations —
    on first use. **A tool server must never be the process that does that.** It
    is spawned by a server or a worker that already owns the schema, so any DDL
    from here is at best redundant and at worst wrong: an embedder whose tables
    carry columns and policies the engine does not know about (a tenant column,
    a foreign key, a row-level security policy) would get a second, unpoliced
    set created underneath it. That is the same hazard the scale worker's
    `set_store()` call exists to prevent, one process further out.

    In the cloud fleet this surfaced as `permission denied for schema public`:
    the tool subprocess connects as an application role that deliberately cannot
    CREATE, so `collections.load()` raised before any tool ran and every
    database-backed tool was dead. The grant was doing its job; this is the
    thing it was standing in for.

    `set_store()` installs the factory directly, which is what makes `init_db()`
    unreachable — see `core/store/engine.py::get_store`.
    """
    from core.store import set_store
    from core.store.engine import build_engine, build_session_factory, default_url

    set_store(build_session_factory(build_engine(default_url())))


#: Where a diagnostic goes when this process IS an MCP stdio server.
#:
#: **stdout is the JSONRPC channel.** Anything written there is parsed as a
#: protocol message, so a single `print()` corrupts the stream and the client
#: fails with "Failed to parse JSONRPC message from server" — while the real
#: error, the one the print was trying to report, is consumed by the parser and
#: never reaches a log. The failure that follows is a chat turn that simply
#: never starts.
#:
#: The three calls below are deliberately non-fatal and deliberately loud; loud
#: has to mean stderr here, which the parent captures and which no protocol
#: reads.
def _diagnostic(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


async def bootstrap() -> None:
    """Adopt the tenant, point `load_settings()` at the store, install the blob store.

    Never raises. A tool server that cannot reach the database is still useful —
    most of its tools do not need it — and failing to start would take the whole
    session set down with it.
    """
    from core import tenancy

    tenancy.adopt_process_tenant(process_tenant())

    try:
        # Before anything can reach the store lazily, for the same reason
        # `set_store` is first in the scale worker's startup.
        _attach_store()
    except Exception as exc:  # noqa: BLE001 — see the docstring
        _diagnostic(f"[tool_server] store unavailable: {exc}")

    try:
        # **Before `settings_runtime.refresh()` below, which reads the store.**
        # A binder installed after the first read is a binder that missed it,
        # and the read would have come back empty rather than failed.
        _install_session_binder()
    except Exception as exc:  # noqa: BLE001 — see the docstring
        # Loud and non-fatal, like its neighbours — but worth reading twice if
        # it appears: with no binder, a deployment that gates rows on the
        # connection sees every table as empty, and nothing else will say so.
        _diagnostic(f"[tool_server] session binder unavailable: {exc}")

    try:
        _install_document_resolver()
    except Exception as exc:  # noqa: BLE001 — see the docstring
        # Deliberately not fatal, and deliberately loud. A tool whose documents
        # carry unresolvable references fails at the point of use with a real
        # error; a tool server that refused to start would take every other
        # tool in the set down with it.
        _diagnostic(f"[tool_server] document resolver unavailable: {exc}")

    try:
        from core import settings_runtime

        settings_runtime.install_provider()
        await settings_runtime.refresh()
    except Exception as exc:  # noqa: BLE001 — see the docstring
        _diagnostic(f"[tool_server] settings unavailable, using defaults: {exc}")

    try:
        # After the settings, which can name the bucket. The parent's store is
        # a module global and does not cross the fork: without this a vault
        # tool on object storage read this process's local disk — an empty
        # vault (F57, measured on a fleet).
        from core.storage import install_configured_blob_store

        install_configured_blob_store()
    except Exception as exc:  # noqa: BLE001 — see the docstring
        _diagnostic(f"[tool_server] object store unavailable, vault is local: {exc}")


# ── Serving, without the handshake waiting on a database ─────────────────────
#
# `bootstrap()` ends in `settings_runtime.refresh()`, which reads Postgres. Every
# tool server used to `await bootstrap()` *before* opening `stdio_server()`, so
# that read sat on the critical path of the MCP handshake — the client cannot
# send `initialize` until the server is listening, and the server was not
# listening until a database had answered.
#
# On a serverless database scaled to zero that is a cold resume, and it cost
# exactly what it looks like it costs: `sql dropped, 60s handshake bound
# expired`, on a fleet where the tenant's whole session set is built in sequence,
# so one slow server delays every server after it and the chat turn behind them
# all.
#
# The ordering was never needed. `bootstrap()` is best-effort by construction —
# it catches everything and its docstring says a tool server that cannot reach
# the database is still useful. What actually needs it is a *handler* that
# touches the store, and a handler runs long after `initialize`.
#
# So: answer the handshake immediately, bootstrap alongside, and have each
# handler wait for it. `list_tools` deliberately does not wait — advertising a
# tool needs no tenant and no settings, and making discovery block would put the
# database back on the path this exists to clear.

_bootstrap_task: "asyncio.Task | None" = None
_bootstrapped: "asyncio.Event | None" = None


def _bootstrapped_event() -> "asyncio.Event":
    # Created lazily: an Event binds to the running loop, and this module is
    # imported before one exists.
    global _bootstrapped
    if _bootstrapped is None:
        _bootstrapped = asyncio.Event()
    return _bootstrapped


async def ready() -> None:
    """Wait for `bootstrap()` to finish. Call first in every tool handler.

    Falls back to running the bootstrap inline when nothing started it — a tool
    server invoked some other way still works, rather than hanging forever on an
    event no one will set. Since `bootstrap()` never raises, this cannot turn a
    slow start into a failed one.
    """
    if _bootstrap_task is None and not _bootstrapped_event().is_set():
        await bootstrap()
        _bootstrapped_event().set()
        return
    await _bootstrapped_event().wait()


async def serve(app) -> None:
    """Run one stdio MCP server, bootstrapping in the background.

    The replacement for `await bootstrap()` followed by `stdio_server()`, which
    is what all five tenant-scoped tool servers used to do. One helper rather
    than five copies, because the ordering above is the kind of thing that is
    correct in four files and quietly wrong in the fifth.
    """
    global _bootstrap_task
    from mcp.server.stdio import stdio_server

    # **Adopt the tenant here, synchronously, before the task exists.**
    #
    # `adopt_process_tenant` sets a ContextVar, and `asyncio.create_task` gives
    # the child a *copy* of the current context — so a value set inside the task
    # is invisible to this one and to the handler tasks the server spawns from
    # it. Bootstrapping in the background without this line left every handler
    # resolving `get_tenant()` to the default, which on a shared fleet is one
    # tenant reading another's vault: the precise failure
    # `adopt_process_tenant`'s own docstring exists to prevent.
    #
    # It is also the half that needs no I/O — it reads one environment variable
    # — so there was never anything to gain by deferring it. What belongs in the
    # background is the store attach and the settings refresh, which touch a
    # database. `bootstrap()` still calls this too, and it is idempotent, so a
    # caller invoking `bootstrap()` directly keeps its existing contract.
    from core import tenancy

    tenancy.adopt_process_tenant(process_tenant())

    async def _run() -> None:
        try:
            await bootstrap()
        finally:
            # Set even if bootstrap somehow raised. It is documented never to,
            # but a handler waiting forever on a broken bootstrap is a hung chat
            # turn with no error anywhere — the exact failure this whole change
            # exists to remove.
            _bootstrapped_event().set()

    _bootstrap_task = asyncio.create_task(_run(), name="tool-server-bootstrap")

    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())
