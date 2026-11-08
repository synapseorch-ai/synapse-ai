"""The Vault's write served by the vault server is held only by an agent that names it (F1289; 79, least privilege).

`"all"` means whatever a deployment makes available, and on cloud's fleet that now includes the vault server's
`vault_write` (F1289). A new write is not what an agent made by ticking nothing should arrive holding, so `"all"`
does not reach it there: `OPT_IN_TOOLS` names `("vault", "vault_write")`, as `OPT_IN_SERVERS` names `sql`, but for one
tool of a server whose reads `"all"` still covers. Naming it opts in. F1288 has a chart plan name it, so nothing a
chart needs is lost. Self-hosted, the sandbox server's own `vault_write` (`vault_sandbox`) is as it was: `"all"` holds
it there today, and this changes none of that.
"""
from __future__ import annotations

import types

from core.tool_router import ToolRouter
from core.tools import aggregate_all_tools, resolve_one_tool


class _Tool:
    def __init__(self, name):
        self.name = name
        self.description = f"{name}."
        self.inputSchema = {"type": "object", "properties": {}}


class _Session:
    def __init__(self, *names):
        self._tools = [_Tool(n) for n in names]

    async def list_tools(self):
        return type("Result", (), {"tools": self._tools})()


def _module(*sessions: str):
    known = {"vault": ("list_files", "read_file", "vault_write"), "vault_sandbox": ("vault_write", "vault_read")}
    built = {name: _Session(*known[name]) for name in sessions}
    router = ToolRouter()
    for name, session in built.items():
        for tool in session._tools:
            router.register(name, tool.name, session_key=name, alias=True)
    return types.SimpleNamespace(agent_sessions=built, tool_router=router, memory_store=None, _session_tools={})


async def _held(module, tools) -> set[str]:
    declared, *_ = await aggregate_all_tools(module, {"tools": tools}, [])
    return {getattr(t, "name", None) for t in declared}


class TestOnCloudsFleet:
    async def test_all_holds_the_reads_but_not_the_write(self):
        held = await _held(_module("vault"), ["all"])
        assert {"list_files", "read_file"} <= held and "vault_write" not in held

    async def test_naming_it_holds_it(self):
        assert "vault_write" in await _held(_module("vault"), ["all", "vault_write"])
        assert "vault_write" in await _held(_module("vault"), ["vault_write"])

    async def test_a_tool_step_finds_it_only_for_an_agent_that_names_it(self):
        assert await resolve_one_tool(_module("vault"), "vault_write", {"tools": ["all"]}, []) is None
        assert await resolve_one_tool(_module("vault"), "vault_write", {"tools": ["vault_write"]}, []) is not None

    def test_one_tool_of_one_server(self):
        from core.tools import OPT_IN_TOOLS, opt_in

        assert ("vault", "vault_write") in OPT_IN_TOOLS
        assert opt_in("vault", "vault_write") and not opt_in("vault", "read_file")
        assert opt_in("sql", "run_sql_query"), "a server opted in whole stays so"


class TestSelfHosted:
    async def test_the_sandboxs_write_is_as_it_was(self):
        assert "vault_write" in await _held(_module("vault_sandbox"), ["all"])


class TestAVisitor:
    def test_never_holds_the_vaults_write_whatever_the_owner_ticks(self):
        """A widget's or a channel's visitor is the public: a write into the owner's Vault is never theirs, as the
        agent's memory and the boards are not (NEVER_FOR_VISITORS); before, only a confirm's deny stood in the way."""
        from core.visitor import NEVER_FOR_VISITORS, VisitorScope, clean_tools

        assert "vault_write" in NEVER_FOR_VISITORS
        assert clean_tools(["read_file", "vault_write"]) == ["read_file"]
        assert not VisitorScope("widget", frozenset(clean_tools(["vault_write"]))).allows("vault_write")
