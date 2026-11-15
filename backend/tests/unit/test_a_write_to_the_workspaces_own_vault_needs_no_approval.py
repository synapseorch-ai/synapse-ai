"""A write to the workspace's own Vault needs no approval step; what leaves the workspace still does (F1608).

A browser test on self-hosted: O2, "Every Friday at 5pm, write a short motivational quote for the team and save it in
the Vault.", was refused by the plan check: "Step 'Save to Vault' runs `vault_write`, which changes something outside
this workspace, with nobody approving it first." The Vault is the workspace's own. `builder_guard.acts` counted every
declared tool that is not read-only as acting outside (TAG1), and ignored MCP's openWorldHint.

The rule: the workspace's own Vault needs no approval. What that means:
- Inside means a tool we list as our own (`builder_guard.OWN_INSIDE_TOOLS`: the Vault's create, write, patch and
  delete, the sandbox's and the fleet's) whose own declaration says its world is closed (openWorldHint False, set
  explicitly by our code). Never the `trusted` flag alone: the bundled third-party Filesystem server writes the
  person's disk and claims a closed world. Never a name alone: a third-party tool called vault_write is not ours.
- An absent openWorldHint is never inside: execute_python and tool_code (network, packages) declare it True.
- A person's custom tools always keep their approval, whatever their own "reaches outside" tick says.
"""
from __future__ import annotations

import pytest

from core.builder_guard import acts, unapproved
from core.tools import VirtualTool, hinted

OURS = ("vault_create", "vault_write", "vault_patch", "vault_delete")


async def _sandbox() -> dict:
    from tools import sandbox

    return {t.name: t for t in await sandbox.list_tools()}


def _fleet() -> dict:
    """The fleet's write (F1289), offered where a host installs a writer: the tool as declared."""
    from core import vault_tools

    return {vault_tools.WRITE: vault_tools._WRITE_TOOL}


class TestOurVaultIsInside:
    @pytest.mark.parametrize("name", OURS)
    async def test_the_sandboxs_vault_writers_do_not_act(self, name):
        tool = (await _sandbox())[name]
        assert (tool.annotations.openWorldHint is False), "declared closed by our own code"
        assert acts(name, tool) is False

    def test_the_fleets_vault_write_does_not_act(self):
        tool = _fleet()["vault_write"]
        assert tool.annotations.openWorldHint is False and acts("vault_write", tool) is False

    def test_the_list_is_ours(self):
        from core.builder_guard import OWN_INSIDE_TOOLS

        assert set(OURS) <= set(OWN_INSIDE_TOOLS)


class TestWhatLeavesStillActs:
    @pytest.mark.parametrize("name", ["execute_python", "tool_code"])
    async def test_code_with_the_network_declares_it_and_acts(self, name):
        tool = (await _sandbox())[name]
        assert tool.annotations.openWorldHint is True and acts(name, tool) is True

    def test_a_tool_with_no_open_world_hint_is_never_inside(self):
        from mcp.types import Tool, ToolAnnotations

        bare = Tool(name="vault_write", description="x", inputSchema={"type": "object"},
                    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
        assert acts("vault_write", bare) is True, "listed, but nothing says its world is closed"

    def test_a_third_party_tool_named_like_ours_acts(self):
        from mcp.types import ToolAnnotations

        theirs = VirtualTool("vault_write", "x", {"type": "object"},
                             annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=False), trusted=False)
        assert acts("vault_write", theirs) is True

    def test_a_trusted_third_party_server_claiming_a_closed_world_acts(self):
        """The bundled Filesystem server: trusted by its key, writes the person's disk, says openWorldHint False."""
        from mcp.types import Tool, ToolAnnotations

        write_file = Tool(name="write_file", description="x", inputSchema={"type": "object"},
                          annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False))
        assert acts("write_file", write_file) is True

    def test_a_persons_own_tool_keeps_its_approval_whatever_its_tick(self):
        from core.tools import custom_virtual_tool

        mine = custom_virtual_tool(hinted({"name": "post_note", "description": "Posts.", "method": "POST",
                                           "url": "https://n.example/x",
                                           "annotations": {"readOnlyHint": False, "openWorldHint": False}}))
        assert acts("post_note", mine) is True


class _Server:
    agent_sessions: dict = {}


def _flow(*steps):
    return {"id": "orch_o2", "entry_step_id": steps[0]["id"], "steps": [*steps, {"id": "end", "type": "end"}]}


class TestThePlanCheck:
    """O2's plan builds, with no Human step; a send still needs its approval; the wording never calls ours outside."""

    @pytest.fixture
    def resolved(self, monkeypatch):
        async def resolve(server_module, name, agent, custom):
            from tools import sandbox

            found = {t.name: t for t in await sandbox.list_tools()}
            if name in found:
                return found[name]
            from mcp.types import Tool, ToolAnnotations

            return Tool(name=name, description="Sends an email.", inputSchema={"type": "object"},
                        annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=True))
        monkeypatch.setattr("core.tools.resolve_one_tool", resolve)

    async def test_o2s_save_to_vault_needs_no_human_step(self, resolved):
        flow = _flow({"id": "s1", "type": "llm", "next_step_id": "s2"},
                     {"id": "s2", "name": "Save to Vault", "type": "tool", "forced_tool": "vault_write",
                      "next_step_id": "end"})
        issues, warnings = await unapproved([flow], {}, _Server(), made_agents=set(), exempt=set())
        assert issues == [] and not any("outside this workspace" in w for w in warnings)

    async def test_a_send_still_needs_its_approval(self, resolved):
        flow = _flow({"id": "s1", "type": "llm", "next_step_id": "s2"},
                     {"id": "s2", "name": "Email it", "type": "tool", "forced_tool": "send_email",
                      "next_step_id": "end"})
        issues, _ = await unapproved([flow], {}, _Server(), made_agents=set(), exempt=set())
        assert len(issues) == 1 and "send_email" in issues[0] and "nobody approving it" in issues[0]
