"""
A tool subprocess is told which tenant it serves, and that is all it is told.

Native tools are MCP servers running as subprocesses, so a ContextVar does not
reach them: `tools/bash.py` and `tools/sandbox.py` resolved
`core.vault._vault_root()` to the *default* tenant's vault whoever called them,
and `load_settings()` returned the shipped defaults because nothing installs the
settings provider in a subprocess.

`core/tool_server.py` fixes both. It reads `SYNAPSE_TENANT_ID`, and this file
pins the two things that makes true and the one thing it must not:

* it is the only module in the tree allowed to read that variable;
* it fixes the tenant of a process that serves exactly one;
* it does **not** enable multi-tenancy. That still requires an embedder to
  register a resource provider, which is the boundary D30 describes — no
  environment variable can produce a second tenant.
"""
import ast
import os
import pathlib
import subprocess
import sys

import pytest

from core import tenancy, tool_server

_BACKEND = pathlib.Path(__file__).resolve().parent.parent.parent

#: Every variable a tool subprocess is configured with, and who may touch it.
#:
#: Named here so a third entry is a conversation rather than a silent widening —
#: the same arrangement as `core/store/importer.py` and SYNAPSE_DATA_DIR.
#:
#: `SYNAPSE_DOCUMENT_RESOLVER` names an import path installed as the store's
#: document resolver. It exists because a tool subprocess reads the store
#: itself, so an embedder keeping credentials outside `collections` cannot swap
#: references in the parent. It names code to import, which sounds worse than it
#: is: whatever can set it can already run code in the process it is spawning.
_TOOL_VARS = {
    #: Read by the module that tells a subprocess which tenant it serves;
    #: written where that subprocess is spawned. Writing is a different
    #: permission from reading — it labels a child, it does not change this
    #: process.
    "SYNAPSE_TENANT_ID": ({"core/tool_server.py"}, {"core/scale/worker_server_module.py"}),
    #: The spawning module both reads and writes this one, and the asymmetry
    #: with SYNAPSE_TENANT_ID is the point: that one is *computed* from
    #: `get_tenant()`, so it is only ever written. This one is forwarded — read
    #: from the parent's environment and handed to the child unchanged.
    #: Forwarding a value decides nothing, which is why it is allowed here and
    #: a third module still is not.
    "SYNAPSE_DOCUMENT_RESOLVER": (
        {"core/tool_server.py", "core/scale/worker_server_module.py"},
        {"core/scale/worker_server_module.py"},
    ),
    #: Forwarded exactly like the resolver, and for a symmetrical reason: that
    #: one says how a stored value becomes real, this one says who the
    #: connection is. A subprocess opens the store itself, so a deployment
    #: enforcing tenancy on the connection has no other way to be heard — and
    #: its absence reads as an empty table rather than an error, which is the
    #: harder failure to diagnose of the two.
    "SYNAPSE_SESSION_BINDER": (
        {"core/tool_server.py", "core/scale/worker_server_module.py"},
        {"core/scale/worker_server_module.py"},
    ),
}

_SEARCH_ROOTS = ("core", "tools", "services", "synapse")


def _sources():
    for root in _SEARCH_ROOTS:
        base = _BACKEND / root if root != "synapse" else _BACKEND.parent / "synapse"
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if "venv" in path.parts or "__pycache__" in path.parts:
                continue
            yield path


def _is_var(node, var: str) -> bool:
    return isinstance(node, ast.Constant) and node.value == var


def _accesses(source: str, var: str) -> tuple[bool, bool]:
    """(reads, writes) of the variable in `source`.

    Deliberately AST-based rather than a substring search: prose that explains
    the mechanism is not a use of it, and this file, `core/tenancy.py` and
    `core/tool_server.py` all have to be able to say the name out loud.
    """
    tree = ast.parse(source)
    reads = writes = False

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if (
                name in {"getenv", "get", "pop", "setdefault"}
                and node.args
                and _is_var(node.args[0], var)
            ):
                reads = True
        elif isinstance(node, ast.Subscript) and _is_var(node.slice, var):
            if isinstance(node.ctx, ast.Store):
                writes = True
            else:
                reads = True

    return reads, writes


@pytest.mark.parametrize("var", sorted(_TOOL_VARS))
def test_only_the_named_modules_touch_a_tool_subprocess_variable(var: str):
    readers, writers = _TOOL_VARS[var]
    read_by, written_by = [], []
    for path in _sources():
        source = path.read_text(encoding="utf-8")
        if var not in source:
            continue
        rel = path.relative_to(_BACKEND).as_posix()
        reads, writes = _accesses(source, var)
        if reads:
            read_by.append(rel)
        if writes:
            written_by.append(rel)

    assert set(read_by) <= readers, (
        f"{var} may only be read by {sorted(readers)} — the module that configures "
        f"a tool subprocess. Also read by: {sorted(set(read_by) - readers)}. "
        "A second reader is a tenancy decision, not an import."
    )
    assert set(written_by) <= writers, (
        f"{var} may only be written by {sorted(writers)}, where a tool server "
        f"is spawned. Also written by: {sorted(set(written_by) - writers)}."
    )
    # And the mechanism must still exist — a guard that passes because the thing
    # it guards was deleted is worse than no guard.
    assert set(read_by) == readers
    assert set(written_by) == writers


class TestAdoptingATenant:
    def test_the_tenant_comes_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("SYNAPSE_TENANT_ID", "acme")
        assert tool_server.process_tenant() == "acme"

    def test_an_unset_variable_leaves_the_default_alone(self, monkeypatch):
        monkeypatch.delenv("SYNAPSE_TENANT_ID", raising=False)
        assert tool_server.process_tenant() == ""

        tenancy.adopt_process_tenant(tool_server.process_tenant())
        assert tenancy.get_tenant() == "default"

    def test_adopting_changes_what_the_vault_resolves_to(self, monkeypatch, tmp_path):
        """The whole point: a subprocess reads *its* tenant's vault."""
        from core.storage import LocalBlobStore, set_blob_store

        set_blob_store(LocalBlobStore(tmp_path))
        try:
            from core.vault import _vault_root

            before = _vault_root()
            tenancy.adopt_process_tenant("acme")
            after = _vault_root()
        finally:
            set_blob_store(None)
            tenancy._current_tenant.set("default")

        assert before != after
        assert after.parts[-2] == "acme"


class TestItIsNotADoorToMultiTenancy:
    """Naveen's condition on this, and the reason it is safe.

    Setting one tenant in a process that serves one tenant is not the same
    capability as switching between two, and only the second is what the OSS
    product withholds.
    """

    def test_adopting_does_not_enable_tenant_scope(self, monkeypatch):
        monkeypatch.setenv("SYNAPSE_TENANT_ID", "acme")
        tenancy.adopt_process_tenant(tool_server.process_tenant())
        try:
            assert tenancy.is_multi_tenant() is False
            with pytest.raises(tenancy.SingleTenantError):
                with tenancy.tenant_scope("globex"):
                    pass
        finally:
            tenancy._current_tenant.set("default")

    def test_the_engine_itself_never_reads_the_variable(self, monkeypatch):
        """Importing the engine with it set must change nothing.

        Only a spawned tool server acts on it, and only because its parent put
        it there deliberately.
        """
        program = (
            "import os\n"
            "os.environ['SYNAPSE_TENANT_ID'] = 'globex'\n"
            "from core.tenancy import get_tenant, is_multi_tenant\n"
            "import core.server, core.scale.worker\n"
            "print(get_tenant(), is_multi_tenant())\n"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(_BACKEND)
        result = subprocess.run(
            [sys.executable, "-c", program],
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().endswith("default False"), result.stdout


class TestBootstrap:
    async def test_it_installs_the_settings_provider(self, monkeypatch):
        """The Phase 6 regression: a tool subprocess saw the shipped defaults.

        `load_settings()` in a subprocess returned `default_settings()` because
        nothing there ever called `install_provider()`, so `bash_allowed_dirs`
        and `vault_threshold` stopped being honoured wherever a tool read them.
        """
        from core import settings_runtime
        from core.config import get_settings_provider, set_settings_provider

        set_settings_provider(None)
        settings_runtime.reset_state()
        monkeypatch.delenv("SYNAPSE_TENANT_ID", raising=False)
        try:
            assert get_settings_provider() is None
            await tool_server.bootstrap()
            assert get_settings_provider() is not None
        finally:
            set_settings_provider(None)
            settings_runtime.reset_state()

    async def test_it_attaches_the_store_without_creating_the_schema(self, monkeypatch):
        """A tool subprocess must never run DDL.

        `get_store()` is lazy and calls `init_db()` on first use. A tool server
        is spawned by a process that already owns the schema, so DDL from here
        is redundant at best — and against an embedder's database it creates a
        second, unpoliced set of the engine's tables underneath one that carries
        a tenant column, foreign keys and row-level security.

        In the cloud fleet the application role deliberately cannot CREATE, so
        this surfaced as `permission denied for schema public` and every
        database-backed tool was dead before it ran. The grant was standing in
        for this call.
        """
        from core import store
        from core.store import engine as store_engine

        called: list[str] = []

        async def _init_db(_engine):
            called.append("init_db")

        monkeypatch.setattr(store_engine, "init_db", _init_db)
        monkeypatch.delenv("SYNAPSE_TENANT_ID", raising=False)
        monkeypatch.setenv("SYNAPSE_DB_URL", "sqlite+aiosqlite:///:memory:")
        await store.reset_store()
        try:
            tool_server._attach_store()
            # Installed, so the lazy path is unreachable...
            assert await store.get_store() is not None
            # ...and therefore init_db was never the thing that provided it.
            assert called == [], "a tool subprocess ran DDL against the store"
        finally:
            await store.reset_store()

    async def test_it_never_raises(self, monkeypatch):
        """A tool server that cannot reach the database still has to start."""
        from core import settings_runtime
        from core.config import set_settings_provider

        async def _boom(*args, **kwargs):
            raise RuntimeError("no database here")

        monkeypatch.setattr(settings_runtime, "refresh", _boom)
        try:
            await tool_server.bootstrap()  # must not raise
        finally:
            set_settings_provider(None)
            settings_runtime.reset_state()
