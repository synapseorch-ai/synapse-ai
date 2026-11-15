"""A write to the workspace's own Vault needs no approval step; what leaves the workspace still does (F1608).

A browser test on self-hosted: O2, "Every Friday at 5pm, write a short motivational quote for the team and save it in
the Vault.", was refused by the plan check: "Step 'Save to Vault' runs `vault_write`, which changes something outside
this workspace, with nobody approving it first." The Vault is the workspace's own. `builder_guard.acts` counted every
declared tool that is not read-only as acting outside (TAG1), and ignored MCP's openWorldHint.

The rule: the workspace's own Vault needs no approval. What that means:
- Inside means a tool served by one of our own Vault servers, named by the session that serves it and its name there
  (`builder_guard.OWN_INSIDE_TOOLS`: the sandbox's create, write, patch and delete; the fleet's write), marked trusted,
  whose own declaration says its world is closed (openWorldHint False, set explicitly by our code).
- Never a name alone: a bundled third-party server (the Filesystem server is one: trusted, it writes the person's disk)
  that shipped a tool called vault_write would not be ours, and nor is an outside server's tool of that name.
- This removes an approval, so it fails closed: a tool with no `trusted` mark, or with no server it came from, is never
  inside, and an absent openWorldHint is never closed (execute_python and tool_code, network and packages, declare it
  True).
- A person's custom tools always keep their approval, whatever their own "reaches outside" tick says.

The tools are resolved the way the plan check resolves them (`core.tools.resolve_one_tool`, from a server module's
sessions and its router), so what is tested is the declaration the check sees.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.types import Tool, ToolAnnotations

from core.builder_guard import acts, unapproved
from core.tool_router import ToolRouter
from core.tools import hinted, resolve_one_tool

OURS = ("vault_create", "vault_write", "vault_patch", "vault_delete")


class _Session:
    def __init__(self, tools):
        self._tools = list(tools)

    async def list_tools(self):
        return SimpleNamespace(tools=self._tools)


def _server(*served) -> SimpleNamespace:
    """A server module as the plan check reads it. `served`: (session key, its tools, how it registers: "native", as
    our own servers do; "beside", as the bundled Filesystem server does (F79); or "ext", an outside MCP server)."""
    sessions, router = {}, ToolRouter()
    for key, tools, how in served:
        sessions[key] = _Session(tools)
        for t in tools:
            if how == "ext":
                router.register(key[len("ext_mcp_"):], t.name, session_key=key, alias=False)
            elif how == "beside":
                router.register_beside(key, t.name)
            else:
                router.register(key, t.name)
    return SimpleNamespace(agent_sessions=sessions, tool_router=router)


async def _sandbox_tools() -> list:
    from tools import sandbox

    return list(await sandbox.list_tools())


def _fleet_tools() -> list:
    """The fleet's write (F1289), offered where a host installs a writer: the tool as declared."""
    from core import vault_tools

    return [vault_tools._WRITE_TOOL]


async def _resolved(server, name: str):
    return await resolve_one_tool(server, name, {"tools": [name]}, [])


def _closed(name: str, *, hint: bool | None = False) -> Tool:
    """A tool some other server declares, writing, with its own openWorldHint (None: absent)."""
    return Tool(name=name, description="x", inputSchema={"type": "object"},
                annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=hint))


class TestOurVaultIsInside:
    @pytest.mark.parametrize("name", OURS)
    async def test_the_sandboxs_vault_writers_do_not_act(self, name):
        tool = await _resolved(_server(("vault_sandbox", await _sandbox_tools(), "native")), name)
        assert tool.annotations.openWorldHint is False, "declared closed by our own code"
        assert acts(name, tool) is False

    async def test_the_fleets_vault_write_does_not_act(self):
        tool = await _resolved(_server(("vault", _fleet_tools(), "native")), "vault_write")
        assert tool.annotations.openWorldHint is False and acts("vault_write", tool) is False

    async def test_a_resolved_tool_carries_the_server_it_came_from(self):
        tool = await _resolved(_server(("vault_sandbox", await _sandbox_tools(), "native")), "vault_write")
        assert tool.origin == ("vault_sandbox", "vault_write")

    async def test_ours_are_named_by_the_server_that_serves_them(self):
        from core import vault_tools
        from core.builder_guard import OWN_INSIDE_TOOLS
        from core.tools_registry import ALL_NATIVE_TOOLS

        assert OWN_INSIDE_TOOLS == {*(("vault_sandbox", n) for n in OURS), ("vault", vault_tools.WRITE)}
        assert Path(ALL_NATIVE_TOOLS["vault_sandbox"]).parts[-2:] == ("tools", "sandbox.py")
        assert Path(ALL_NATIVE_TOOLS["vault"]).parts[-2:] == ("tools", "vault_fs.py")
        assert set(OURS) <= {t.name for t in await _sandbox_tools()}


class TestWhatLeavesStillActs:
    @pytest.mark.parametrize("name", ["execute_python", "tool_code"])
    async def test_code_with_the_network_declares_it_and_acts(self, name):
        tool = await _resolved(_server(("vault_sandbox", await _sandbox_tools(), "native")), name)
        assert tool.annotations.openWorldHint is True and acts(name, tool) is True

    async def test_a_tool_of_ours_with_no_open_world_hint_is_never_inside(self):
        tool = await _resolved(_server(("vault_sandbox", [_closed("vault_write", hint=None)], "native")), "vault_write")
        assert acts("vault_write", tool) is True, "ours by its server, but nothing says its world is closed"

    @pytest.mark.parametrize("origin", [("vault_sandbox", "vault_write"), None])
    def test_a_tool_with_no_trusted_mark_is_never_inside(self, origin):
        """It removes an approval, so it fails closed: a declaration that does not say it is trusted is not."""
        bare = SimpleNamespace(name="vault_write", annotations=SimpleNamespace(readOnlyHint=False, openWorldHint=False),
                               **({"origin": origin} if origin else {}))
        assert acts("vault_write", bare) is True

    async def test_an_outside_servers_vault_write_acts(self):
        server = _server(("ext_mcp_acme", [_closed("vault_write")], "ext"))
        assert await _resolved(server, "vault_write") is None, "an outside server never takes a bare name"
        tool = await _resolved(server, "acme__vault_write")
        assert tool.trusted is False and acts("acme__vault_write", tool) is True

    async def test_a_bundled_servers_vault_write_acts(self):
        """A trusted bundled server that ever ships a tool of our name, with a closed hint, is still not ours."""
        tool = await _resolved(_server(("Filesystem", [_closed("vault_write")], "beside")), "vault_write")
        assert tool.trusted is True and acts("vault_write", tool) is True

    async def test_beside_ours_the_bare_name_is_ours_and_the_bundled_ones_acts(self):
        server = _server(("vault_sandbox", await _sandbox_tools(), "native"),
                         ("Filesystem", [_closed("vault_write")], "beside"))
        assert acts("vault_write", await _resolved(server, "vault_write")) is False
        assert acts("Filesystem__vault_write", await _resolved(server, "Filesystem__vault_write")) is True

    async def test_a_trusted_bundled_server_claiming_a_closed_world_acts(self):
        """The bundled Filesystem server: trusted by its key, writes the person's disk, says openWorldHint False."""
        tool = await _resolved(_server(("Filesystem", [_closed("write_file")], "beside")), "write_file")
        assert acts("write_file", tool) is True

    def test_a_persons_own_tool_keeps_its_approval_whatever_its_tick(self):
        from core.tools import custom_virtual_tool

        mine = custom_virtual_tool(hinted({"name": "post_note", "description": "Posts.", "method": "POST",
                                           "url": "https://n.example/x",
                                           "annotations": {"readOnlyHint": False, "openWorldHint": False}}))
        assert acts("post_note", mine) is True

    def test_a_persons_own_tool_named_like_ours_keeps_its_approval(self):
        from core.tools import custom_virtual_tool

        mine = custom_virtual_tool(hinted({"name": "vault_write", "description": "Mine.", "method": "POST",
                                           "url": "https://n.example/x",
                                           "annotations": {"readOnlyHint": False, "openWorldHint": False}}))
        assert mine.custom is True and acts("vault_write", mine) is True


def _flow(*steps):
    return {"id": "orch_o2", "entry_step_id": steps[0]["id"], "steps": [*steps, {"id": "end", "type": "end"}]}


class TestThePlanCheck:
    """O2's plan builds, with no Human step; a send still needs its approval; the wording never calls ours outside."""

    @pytest.fixture
    async def server(self):
        send = Tool(name="send_email", description="Sends an email.", inputSchema={"type": "object"},
                    annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=True))
        return _server(("vault_sandbox", await _sandbox_tools(), "native"), ("mail", [send], "native"))

    async def test_o2s_save_to_vault_needs_no_human_step(self, server):
        flow = _flow({"id": "s1", "type": "llm", "next_step_id": "s2"},
                     {"id": "s2", "name": "Save to Vault", "type": "tool", "forced_tool": "vault_write",
                      "next_step_id": "end"})
        issues, warnings = await unapproved([flow], {}, server, made_agents=set(), exempt=set())
        assert issues == [] and not any("outside this workspace" in w for w in warnings)

    async def test_a_send_still_needs_its_approval(self, server):
        flow = _flow({"id": "s1", "type": "llm", "next_step_id": "s2"},
                     {"id": "s2", "name": "Email it", "type": "tool", "forced_tool": "send_email",
                      "next_step_id": "end"})
        issues, _ = await unapproved([flow], {}, server, made_agents=set(), exempt=set())
        assert len(issues) == 1 and "send_email" in issues[0] and "nobody approving it" in issues[0]
