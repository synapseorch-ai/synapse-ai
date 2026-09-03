"""
Which names `aggregate_all_tools` puts in front of the model.

The router's keys became `{server}__{tool}`, and the router is what decides
which of two colliding implementations a call reaches. The names *declared* to
the model are a separate thing, and deliberately did not move: an agent's stored
`tools: [...]` array holds bare names for native tools and `{server}__{tool}` for
external ones, so renaming a declaration would make an existing agent's list
silently stop matching and every call would be refused by the execution guard in
`core/react_engine.py`.

So these tests pin the contract from the model's side — the half that has a
migration cost if it is wrong, and that no test covered before.
"""
import pytest

from core.tool_router import SEP, ToolRouter


class _Tool:
    def __init__(self, name, description="", schema=None):
        self.name = name
        self.description = description
        self.inputSchema = schema if schema is not None else {"type": "object"}


class _Session:
    def __init__(self, *tools):
        self._tools = list(tools)

    async def list_tools(self):
        return type("Result", (), {"tools": self._tools})()


@pytest.fixture
def declared():
    """Run aggregation over a set of sessions and return the declared MCP names.

    Builder tools are filtered out: they are added from `BUILDER_TOOL_SCHEMAS`
    whenever an agent says "all", and they are not what any of this is about.
    """
    import types

    import core.tools as tools_mod
    from core.builder_tools import BUILDER_TOOL_SCHEMAS

    builder_names = {bt["function"]["name"] for bt in BUILDER_TOOL_SCHEMAS}

    async def _run(sessions: dict, agent: dict, custom_tools=None):
        router = ToolRouter()
        for session_name, session in sessions.items():
            external = session_name.startswith("ext_mcp_")
            namespace = session_name[len("ext_mcp_"):] if external else session_name
            for tool in session._tools:
                router.register(
                    namespace,
                    tool.name,
                    session_key=session_name,
                    alias=not external,
                )

        # The same shape a worker's WorkerServerModule has, which is the point:
        # aggregation now reads the router and the tool cache off whatever owns
        # the sessions rather than off core.server.
        server_module = types.SimpleNamespace(
            agent_sessions=sessions,
            tool_router=router,
            memory_store=None,
            _session_tools={},
        )

        all_tools, _, _, _ = await tools_mod.aggregate_all_tools(
            server_module, agent, custom_tools or []
        )
        return [t.name for t in all_tools if t.name not in builder_names]

    return _run


def _agent(tools, **extra):
    return {"tools": list(tools), "type": "conversational", "skip_default_tools": True, **extra}


class TestNativeTools:
    async def test_a_native_tool_is_declared_under_its_bare_name(self, declared):
        names = await declared(
            {"bash": _Session(_Tool("run_command"))}, _agent(["run_command"])
        )

        assert names == ["run_command"]

    async def test_all_declares_every_native_tool_bare(self, declared):
        names = await declared(
            {"bash": _Session(_Tool("run_command"), _Tool("which"))}, _agent(["all"])
        )

        assert names == ["run_command", "which"]

    async def test_a_tool_the_agent_did_not_ask_for_is_not_declared(self, declared):
        names = await declared(
            {"bash": _Session(_Tool("run_command"), _Tool("which"))}, _agent(["which"])
        )

        assert names == ["which"]


class TestCollisions:
    """`read_file_by_lines` is advertised by file_reader and code_vault_search."""

    def _sessions(self):
        # Registration order decides the bare-name winner; file_reader last,
        # which is the order core/server.py's TOOLS_LIST produces.
        return {
            "code_vault_search": _Session(_Tool("read_file_by_lines")),
            "file_reader": _Session(_Tool("read_file_by_lines")),
        }

    async def test_the_bare_name_is_declared_once(self, declared):
        """Gemini rejects duplicate function declarations outright."""
        names = await declared(self._sessions(), _agent(["read_file_by_lines"]))

        assert names == ["read_file_by_lines"]

    async def test_all_also_declares_it_once(self, declared):
        names = await declared(self._sessions(), _agent(["all"]))

        assert names == ["read_file_by_lines"]

    async def test_the_shadowed_copy_is_declared_when_asked_for_by_key(self, declared):
        """The escape hatch that retires `_KNOWN_TOOL_NAME_COLLISIONS`.

        code_vault_search's repo-aware reader was unreachable for the whole life
        of the project. Naming it explicitly now declares it.
        """
        key = f"code_vault_search{SEP}read_file_by_lines"
        names = await declared(self._sessions(), _agent([key]))

        assert names == [key]

    async def test_both_can_be_declared_together(self, declared):
        key = f"code_vault_search{SEP}read_file_by_lines"
        names = await declared(self._sessions(), _agent([key, "read_file_by_lines"]))

        assert sorted(names) == sorted([key, "read_file_by_lines"])


class TestExternalTools:
    async def test_an_external_tool_keeps_its_namespaced_declaration(self, declared):
        """Unchanged from before the refactor, and it must stay that way.

        Every agent that uses an external MCP server has `{server}__{tool}` in
        its stored tools array.
        """
        names = await declared(
            {"ext_mcp_github": _Session(_Tool("create_issue"))},
            _agent([f"github{SEP}create_issue"]),
        )

        assert names == [f"github{SEP}create_issue"]

    async def test_all_declares_external_tools_namespaced_not_bare(self, declared):
        names = await declared(
            {"ext_mcp_github": _Session(_Tool("create_issue"))}, _agent(["all"])
        )

        assert names == [f"github{SEP}create_issue"]

    async def test_the_bare_name_alone_does_not_declare_an_external_tool(self, declared):
        names = await declared(
            {"ext_mcp_github": _Session(_Tool("create_issue"))}, _agent(["create_issue"])
        )

        assert names == []

    async def test_an_external_server_does_not_shadow_a_native_tool(self, declared):
        """A tenant registering an MCP server exposing `read_file` must not take
        over the native Filesystem tool for that tenant's runs."""
        names = await declared(
            {
                "Filesystem": _Session(_Tool("read_file")),
                "ext_mcp_sneaky": _Session(_Tool("read_file")),
            },
            _agent(["all"]),
        )

        assert names == ["read_file", f"sneaky{SEP}read_file"]


class TestDefaultsStillUseBareNames:
    async def test_auto_injected_defaults_resolve(self, declared):
        """`DEFAULT_TOOLS_BY_TYPE` holds bare names and was left alone."""
        names = await declared(
            {"Filesystem": _Session(_Tool("read_file"), _Tool("glob"))},
            {"tools": [], "type": "conversational"},
        )

        assert sorted(names) == ["glob", "read_file"]


class TestToolGroups:
    """`group:<id>` in an agent's list admits every custom tool carrying that
    group — including one added after the agent was saved, which is what makes
    a group a unit rather than a picker heading (D68 tools pass)."""

    CUSTOM = [
        {"name": "list_pets", "description": "d", "inputSchema": {"type": "object"}, "group": "grp_petstore"},
        {"name": "add_pet", "description": "d", "inputSchema": {"type": "object"}, "group": "grp_petstore"},
        {"name": "send_mail", "description": "d", "inputSchema": {"type": "object"}, "group": "grp_mail"},
        {"name": "lone_tool", "description": "d", "inputSchema": {"type": "object"}},
    ]

    async def test_a_group_entry_admits_its_tools_and_nothing_else(self, declared):
        names = await declared({}, _agent(["group:grp_petstore"]), custom_tools=self.CUSTOM)
        assert {"list_pets", "add_pet"} <= set(names)
        assert "send_mail" not in names and "lone_tool" not in names

    async def test_groups_and_names_combine(self, declared):
        names = await declared({}, _agent(["group:grp_mail", "lone_tool"]), custom_tools=self.CUSTOM)
        assert {"send_mail", "lone_tool"} <= set(names)
        assert "list_pets" not in names

    async def test_a_tool_added_to_the_group_later_is_included(self, declared):
        later = [*self.CUSTOM, {"name": "delete_pet", "description": "d", "inputSchema": {"type": "object"}, "group": "grp_petstore"}]
        names = await declared({}, _agent(["group:grp_petstore"]), custom_tools=later)
        assert "delete_pet" in names

    async def test_all_still_means_all(self, declared):
        names = await declared({}, _agent(["all"]), custom_tools=self.CUSTOM)
        assert {"list_pets", "add_pet", "send_mail", "lone_tool"} <= set(names)
