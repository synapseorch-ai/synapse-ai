"""The native vault MCP server, and the two properties it exists to hold.

`tools/vault_fs.py` replaced the Node `@modelcontextprotocol/server-filesystem`
on the worker path. Both properties below were true of neither predecessor:
the Node server was rooted at a directory (a hydrated working copy, empty on a
replica that had not served the tenant) and it needed an `npx` no scale-mode
image ships, so it never actually ran.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from core.scale.context import set_resource_provider
from core.storage import set_blob_store
from core.storage.base import LocalBlobStore
from core.tenancy import tenant_scope


class _Provider:
    """Enough of a resource provider to unlock `tenant_scope`.

    Tenancy is deliberately coupled to having one — switching tenants without a
    tenant-aware way to resolve resources would give every tenant the same
    agents and tools, so `core/tenancy.py` refuses. See
    `test_tenancy.py::test_registering_a_provider_is_what_unlocks_tenancy`.
    """

    async def resolve_agents(self):
        return []

    async def resolve_custom_tools(self):
        return []

    async def resolve_mcp_servers(self):
        return []


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A local blob store, which applies `tenant_key` exactly as S3 does."""
    monkeypatch.setenv("SYNAPSE_BLOB_DIR", str(tmp_path))
    store = LocalBlobStore(tmp_path)
    set_blob_store(store)
    set_resource_provider(_Provider())
    yield store
    set_resource_provider(None)
    set_blob_store(None)


async def _call(name: str, arguments: dict) -> dict:
    from tools import vault_fs

    # `ready()` would otherwise run the bootstrap inline against a database that
    # is not here. The tenant is established by the `tenant_scope` in each test.
    async def _noop() -> None:
        return None

    from core import tool_server

    original, tool_server.ready = tool_server.ready, _noop
    try:
        result = await vault_fs.call_tool.__wrapped__(name, arguments)
    except AttributeError:
        result = await vault_fs.call_tool(name, arguments)
    finally:
        tool_server.ready = original
    return json.loads(result[0].text)


@pytest.mark.anyio
async def test_listing_cannot_see_another_tenants_files(vault):
    """The isolation is the blob store's, and this proves it rather than assumes it.

    `core/storage/base.py::tenant_key` prefixes every key with the current
    tenant *inside* the store — the module's own reasoning is that a call site
    which forgets is a cross-tenant read, and `tools/vault_fs.py` is one more
    call site. Nothing in that file mentions a tenant, which is the point; this
    is what makes that safe rather than lucky.
    """
    with tenant_scope("org-a"):
        vault.put("vault/secret.txt", "org a's private notes")

    with tenant_scope("org-b"):
        vault.put("vault/mine.txt", "org b's own file")
        listing = await _call("list_files", {})

    assert listing["files"] == ["mine.txt"], listing
    assert "secret.txt" not in listing["files"]


@pytest.mark.anyio
async def test_read_and_search_go_through_the_store_not_a_path(vault):
    """A key, not a directory — so this works where there is no filesystem."""
    body = "alpha\nbeta\ngamma\ndelta\n"
    with tenant_scope("org-a"):
        vault.put("vault/notes.txt", body)

        chunk = await _call("read_file", {"path": "notes.txt", "start_line": 2, "end_line": 3})
        assert chunk["content"] == "beta\ngamma"
        assert chunk["total_lines"] == 4

        found = await _call("search_files", {"query": "gamma"})

    assert found["files_with_matches"] == 1
    assert found["results"][0]["path"] == "notes.txt"


@pytest.mark.anyio
async def test_a_missing_key_is_an_error_not_a_crash(vault):
    with tenant_scope("org-a"):
        answer = await _call("read_file", {"path": "nope.txt"})
    assert "error" in answer and "Not found" in answer["error"]


@pytest.mark.anyio
async def test_a_multi_tenant_process_builds_no_chroma_index(vault, monkeypatch):
    """One index cannot serve many tenants, so a fleet process holds none.

    `MemoryStore` is ChromaDB, and its index path resolves through
    `get_tenant()`. `build_shared()` runs at worker startup *before any tenant is
    bound*, so the path it computes belongs to the default tenant — and
    `build_for_tenant()` then folds that same store into every tenant's module.
    One index, no tenant dimension, every org sharing it.

    The guard is on `is_multi_tenant()` rather than on an environment variable
    on purpose: it is a correctness rule, not a deployment preference, and a
    variable nothing sets is exactly the class of bug this codebase keeps
    paying for.
    """
    monkeypatch.setenv("SCALE_POSTGRES_URL", "postgresql+asyncpg://nobody@localhost/none")

    from core.scale.worker_server_module import WorkerServerModule

    module = WorkerServerModule()
    module._attach_memory_store()

    assert module.memory_store is None, (
        "a multi-tenant process built a single-index memory store; "
        "every org would share it"
    )


@pytest.mark.anyio
async def test_search_reads_the_store_concurrently(vault, monkeypatch):
    """Serially, a full-width search would outlive the MCP session bound.

    Each file is a `get`, which on S3 is a network round trip; `_SEARCH_FILE_LIMIT`
    of them in sequence is comfortably past the 60s `MCP_SESSION_READ_TIMEOUT`,
    and the failure would be the search returning nothing rather than returning
    what it had found. This asserts the reads actually overlap, because "make it
    concurrent" is the kind of change a later refactor quietly undoes.
    """
    from tools import vault_fs

    with tenant_scope("org-a"):
        for i in range(32):
            vault.put(f"vault/f{i:02d}.txt", f"line one\nneedle {i}\n")

        in_flight = 0
        peak = 0
        real_get = vault_fs._get_text

        def _slow_get(key: str):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            try:
                time.sleep(0.01)
                return real_get(key)
            finally:
                in_flight -= 1

        monkeypatch.setattr(vault_fs, "_get_text", _slow_get)
        found = await _call("search_files", {"query": "needle"})

    assert found["files_with_matches"] == 32
    assert peak > 1, "the store was read one file at a time"


@pytest.mark.anyio
async def test_a_key_cannot_escape_the_tenants_prefix(vault):
    """Traversal is refused before the store is asked.

    The tool's paths are relative to the vault (`vault/` inside the tenant's
    key space) and go through the same guard as `@[path]` in a prompt
    (`core.vault.mention_key`); the store applies the tenant underneath. Two
    guards, and this proves the first one holds end to end.
    """
    with tenant_scope("other-org"):
        vault.put("vault/secret.txt", "not yours")

    with tenant_scope("org-a"):
        answer = await _call("read_file", {"path": "../other-org/vault/secret.txt"})

    assert "error" in answer
    assert "not a vault path" in answer["error"].lower(), answer
    assert "not yours" not in json.dumps(answer)


@pytest.mark.anyio
async def test_serve_adopts_the_tenant_before_it_starts_the_bootstrap_task():
    """The tenant must be adopted in the *serving* task, not the child one.

    `adopt_process_tenant` sets a ContextVar and `asyncio.create_task` hands the
    child a **copy** of the context — so adopting inside the background bootstrap
    leaves this task, and every handler task spawned from it, resolving
    `get_tenant()` to the default. On a shared fleet that is one tenant reading
    another's vault.

    It was written that way first and a live probe caught it: `list_files`
    returned nothing because the server was looking in the default tenant's
    prefix. Nothing in the suite noticed, which is why this test exists.
    """
    import os

    import mcp.server.stdio as stdio_mod

    from core import tenancy, tool_server

    seen: list[str] = []

    class _App:
        async def run(self, *_args, **_kwargs):
            # Whatever a handler would see: this is the task the server runs in.
            seen.append(tenancy.get_tenant())

        def create_initialization_options(self):
            return None

    class _NullStdio:
        async def __aenter__(self):
            return (None, None)

        async def __aexit__(self, *exc):
            return False

    async def _slow_bootstrap():
        # Long enough that a serve() relying on the task would still be waiting
        # when app.run() is entered.
        await asyncio.sleep(0.05)

    original_stdio = stdio_mod.stdio_server
    original_bootstrap = tool_server.bootstrap
    os.environ["SYNAPSE_TENANT_ID"] = "org-from-the-parent"
    try:
        stdio_mod.stdio_server = lambda: _NullStdio()
        tool_server.bootstrap = _slow_bootstrap
        await tool_server.serve(_App())
    finally:
        stdio_mod.stdio_server = original_stdio
        tool_server.bootstrap = original_bootstrap
        os.environ.pop("SYNAPSE_TENANT_ID", None)

    assert seen == ["org-from-the-parent"], (
        "serve() started the server without adopting the tenant first; "
        f"the serving task saw {seen}"
    )


@pytest.mark.anyio
async def test_the_tool_sees_the_vault_and_nothing_else_of_the_tenant(vault):
    """F20: keys used to be relative to the whole tenant, so `list_files("")`
    reached `logs/`, `memory/` and `attachments/`, and `read_file` could open an
    orchestration log — rendered globals included."""
    with tenant_scope("org-a"):
        vault.put("vault/notes.md", "in the vault")
        vault.put("vault/tool_outputs/crawl.json", "[1]")
        vault.put("logs/orchestration/run_1.log", "STRIPE_KEY=sk_live_secret")
        vault.put("memory/agent/index.json", "{}")

        listing = await _call("list_files", {})
        assert sorted(listing["files"]) == ["notes.md", "tool_outputs/crawl.json"], listing

        narrowed = await _call("list_files", {"prefix": "tool_outputs/"})
        assert narrowed["files"] == ["tool_outputs/crawl.json"]

        as_a_key = await _call("read_file", {"path": "logs/orchestration/run_1.log"})
        assert "error" in as_a_key and "Not found" in as_a_key["error"]

        traversal = await _call("read_file", {"path": "../logs/orchestration/run_1.log"})
        assert "error" in traversal and "not a vault path" in traversal["error"].lower()

        found = await _call("search_files", {"query": "STRIPE_KEY"})
        assert found["files_with_matches"] == 0

    assert "sk_live_secret" not in json.dumps([listing, as_a_key, traversal, found])


@pytest.mark.anyio
async def test_a_folder_marker_is_not_a_file(vault):
    with tenant_scope("org-a"):
        vault.put("vault/.keep", "\n")
        vault.put("vault/notes/.keep", "\n")
        vault.put("vault/notes/a.md", "a")
        listing = await _call("list_files", {})
    assert listing["files"] == ["notes/a.md"]


@pytest.mark.anyio
async def test_a_binary_says_so_instead_of_a_decode_error(vault):
    """D94: the vault stores PDFs and images, download-only; a tool asked for
    one used to hand the model `'utf-8' codec can't decode byte …`."""
    with tenant_scope("org-a"):
        path = vault.path_for("vault/report.pdf")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4 " + b"\x00" * 16)
        blob = vault.path_for("vault/blob.dat")
        blob.write_bytes(b"\xff\xfe\x00junk")

        pdf = await _call("read_file", {"path": "report.pdf"})
        assert pdf["error"] == "report.pdf is a PDF \u2014 binary files are download-only and cannot be read by tools"

        as_json = await _call("read_json", {"path": "report.pdf"})
        assert "download-only" in as_json["error"]

        blob_answer = await _call("read_file", {"path": "blob.dat"})
        assert "not a text file" in blob_answer["error"] and "codec" not in blob_answer["error"]


@pytest.mark.anyio
async def test_the_tools_path_is_the_mentions_path(vault):
    """What `@[notes/q3 plan.md]` names in a prompt is what `read_file` takes:
    one form, so the reference line the reader writes for a file it did not
    inline is a path the tool accepts as it is."""
    with tenant_scope("org-a"):
        vault.put("vault/notes/q3 plan.md", "# Q3\n\nplan\n")
        answer = await _call("read_file", {"path": "notes/q3 plan.md"})
    assert answer["content"].startswith("# Q3")
