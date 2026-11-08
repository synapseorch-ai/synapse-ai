"""
`TENANT_SCOPED_TOOLS` is derived from the code, not maintained by hand.

A native tool server is a subprocess. If it resolves the vault, the store or the
tenant's settings *for itself*, it needs to be told which tenant it serves —
because a ContextVar does not survive a fork+exec, and one such server spawned
once and shared reads the default tenant's data whoever calls it. In a
single-tenant install that is invisible. On a shared fleet it is a cross-tenant
read.

The failure mode this guards against is the one that has recurred on this branch
five times: a collection moves, and a reader is left behind, silently. A tool
that starts importing `core.vault` in six months' time will not announce itself.
So the set is checked against the imports rather than trusted.
"""
import ast
import pathlib

import pytest

from core.tools_registry import ALL_NATIVE_TOOLS, TENANT_SCOPED_TOOLS, WORKER_NATIVE_TOOLS

#: Importing any of these means the module resolves tenant-scoped state itself.
#: `core.config` is here for `load_settings()`, which is per-tenant since D30.
#: `core.vault_tools` holds the vault tools' handlers, which read the blob store (F1118).
_TENANT_STATE_MODULES = ("core.vault", "core.storage", "core.store", "core.config", "core.vault_tools")


def _imported_modules(path: str) -> set[str]:
    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def _reads_tenant_state(path: str) -> bool:
    return any(
        module == watched or module.startswith(watched + ".")
        for module in _imported_modules(path)
        for watched in _TENANT_STATE_MODULES
    )


@pytest.mark.parametrize("tool_name,script_path", sorted(ALL_NATIVE_TOOLS.items()))
def test_a_tool_reading_tenant_state_is_declared_tenant_scoped(tool_name, script_path):
    reads = _reads_tenant_state(script_path)
    declared = tool_name in TENANT_SCOPED_TOOLS

    if reads and not declared:
        pytest.fail(
            f"{tool_name} ({script_path}) imports tenant-scoped state "
            f"({', '.join(_TENANT_STATE_MODULES)}) but is not in "
            "TENANT_SCOPED_TOOLS, so one shared instance of it would serve "
            "every tenant the default tenant's data. Add it to the set in "
            "core/tools_registry.py and call core.tool_server.bootstrap() at "
            "the top of its main()."
        )
    if declared and not reads:
        pytest.fail(
            f"{tool_name} is in TENANT_SCOPED_TOOLS but no longer reads tenant "
            "state. Remove it — a per-tenant subprocess that does not need to "
            "be one is pure cost at fleet scale."
        )


@pytest.mark.parametrize("tool_name", sorted(TENANT_SCOPED_TOOLS))
def test_a_tenant_scoped_tool_adopts_its_tenant_on_startup(tool_name):
    """Declaring it is not enough; the subprocess has to act on it.

    Both halves are required and they guard different failures. `serve()` is
    what runs the bootstrap at all — without it the subprocess resolves the
    default tenant's vault and the shipped default settings whoever spawned it.
    `ready()` is what makes a *handler* wait for it, and it exists because the
    bootstrap no longer completes before the server starts listening: it reads
    Postgres, and doing that ahead of `stdio_server()` put a database on the
    critical path of the MCP handshake, which a cold serverless resume then
    blew straight through.

    A tool with `serve()` and no `ready()` is the subtle one — it works on a
    warm database and races on a cold one, which is the shape of bug that gets
    reported as "sometimes the chat just does not start".
    """
    source = pathlib.Path(ALL_NATIVE_TOOLS[tool_name]).read_text(encoding="utf-8")

    assert "from core.tool_server import serve" in source, (
        f"{tool_name} is tenant-scoped but never serves through core.tool_server, "
        "so nothing adopts its tenant: it will resolve the default tenant's vault "
        "and the shipped default settings whoever spawned it."
    )
    assert "await serve(" in source, f"{tool_name} imports serve but never awaits it"
    assert "await ready()" in source, (
        f"{tool_name} never awaits ready(), so a handler can run before the "
        "bootstrap that binds its tenant has finished."
    )


def test_the_shared_and_tenant_halves_partition_the_worker_tools():
    """Every worker tool is spawned exactly once — shared or per tenant."""
    from core.scale.worker_server_module import (
        _BACKEND_ROOT,
        _TOOLS_DIR,
        _get_native_mcp_servers,
    )

    shared = set(_get_native_mcp_servers(_TOOLS_DIR, _BACKEND_ROOT, scope="shared"))
    per_tenant = set(_get_native_mcp_servers(_TOOLS_DIR, _BACKEND_ROOT, scope="tenant"))
    everything = set(_get_native_mcp_servers(_TOOLS_DIR, _BACKEND_ROOT, scope="all"))

    assert not (shared & per_tenant), f"spawned twice: {sorted(shared & per_tenant)}"
    assert shared | per_tenant == everything

    tenant_scoped_on_workers = WORKER_NATIVE_TOOLS & TENANT_SCOPED_TOOLS
    assert tenant_scoped_on_workers <= per_tenant
    assert not (tenant_scoped_on_workers & shared), (
        "a tool that reads tenant state is in the shared set, which is the leak "
        "this split exists to close"
    )


def test_a_tenant_scoped_server_is_spawned_knowing_its_tenant():
    from core.scale.worker_server_module import (
        _BACKEND_ROOT,
        _TOOLS_DIR,
        _get_native_mcp_servers,
    )
    from core.tenancy import get_tenant

    from core.scale.worker_server_module import InProcessServer

    per_tenant = _get_native_mcp_servers(_TOOLS_DIR, _BACKEND_ROOT, scope="tenant")

    for name in sorted(WORKER_NATIVE_TOOLS & TENANT_SCOPED_TOOLS):
        params = per_tenant[name]
        if isinstance(params, InProcessServer):
            # Served in this process, built in the tenant's own scope: its tenant is the
            # caller's, proved by tests/security/test_vault_in_process.py (F1118).
            continue
        assert params.env is not None, f"{name} was spawned with no environment at all"
        assert params.env.get("SYNAPSE_TENANT_ID") == get_tenant(), (
            f"{name} was not told which tenant it serves"
        )

    shared = _get_native_mcp_servers(_TOOLS_DIR, _BACKEND_ROOT, scope="shared")
    for name, params in shared.items():
        if params.env:
            assert "SYNAPSE_TENANT_ID" not in params.env, (
                f"{name} is shared between tenants but carries a tenant label"
            )
