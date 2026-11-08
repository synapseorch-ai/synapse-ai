"""What a turn can call, worked out before the fleet connects its MCP servers.

VOX-MCP (D262): the pool used to connect every server an org has before any
turn ran, and a phone call's first answer waited 12.9 s, ten of them for three
servers its agent never used. Now a job says what its turn may call
(`core/scale/mcp_needs.py`) and only those servers are connected.

A mistake here is invisible in the product: an agent quietly lacks a tool. So
the rule is "every server when in doubt", and each case that must get every
server is pinned below, as is the one guard that keeps the builder's list honest.
"""
import ast
import inspect
import textwrap

import pytest

from core import builder_tools
from core.scale import mcp_needs
from core.scale.mcp_needs import EVERY, bounded, for_turn, of_agent, servers_for
from core.visitor import VisitorScope


def _resolver(agents: dict):
    async def resolve(agent_id):
        return agents.get(agent_id)

    return resolve


_NOBODY = _resolver({})


class TestAnAgentsOwnList:
    async def test_named_server_tools_are_what_it_needs(self):
        agent = {"id": "a", "type": "conversational", "tools": ["deepwiki__ask", "web_search"]}
        needs = await of_agent(agent, resolve=_NOBODY)
        assert needs is not EVERY
        assert {"deepwiki__ask", "web_search"} <= needs

    async def test_all_is_every_server(self):
        assert await of_agent({"type": "conversational", "tools": ["all"]}, resolve=_NOBODY) is EVERY

    async def test_no_list_is_all(self):
        """`tools` missing is `["all"]` to the engine (tools.py), so here too."""
        assert await of_agent({"type": "conversational"}, resolve=_NOBODY) is EVERY

    async def test_an_empty_list_needs_no_server(self):
        needs = await of_agent({"type": "conversational", "tools": []}, resolve=_NOBODY)
        assert servers_for(needs, ["deepwiki", "google"]) == []

    @pytest.mark.parametrize("kind", ["orchestrator", "builder"])
    async def test_a_workflow_or_builder_agent_is_every_server(self, kind):
        agent = {"type": kind, "tools": ["deepwiki__ask"]}
        assert await of_agent(agent, resolve=_NOBODY) is EVERY

    @pytest.mark.parametrize("tool", sorted(builder_tools.READS_EVERY_SESSION))
    async def test_a_builder_tool_that_lists_every_session_is_every_server(self, tool):
        agent = {"type": "conversational", "tools": [tool]}
        assert await of_agent(agent, resolve=_NOBODY) is EVERY

    async def test_no_agent_is_every_server(self):
        assert await of_agent(None, resolve=_NOBODY) is EVERY


class TestADelegate:
    async def test_one_that_may_hand_work_to_anyone_is_every_server(self):
        agent = {"type": "delegate", "tools": [], "delegate_agent_ids": []}
        assert await of_agent(agent, resolve=_NOBODY) is EVERY

    async def test_one_with_a_list_needs_its_agents_servers_too(self):
        resolve = _resolver({
            "w": {"id": "w", "type": "conversational", "tools": ["google__search"]},
            "x": {"id": "x", "type": "conversational", "tools": ["vercel__deploy"]},
        })
        agent = {"type": "delegate", "tools": ["deepwiki__ask"], "delegate_agent_ids": ["w", "x"]}
        needs = await of_agent(agent, resolve=resolve)
        assert servers_for(needs, ["deepwiki", "google", "vercel", "other"]) == ["deepwiki", "google", "vercel"]

    async def test_a_workflow_among_them_is_every_server(self):
        resolve = _resolver({"w": {"id": "w", "type": "orchestrator", "orchestration_id": "o"}})
        agent = {"type": "delegate", "tools": [], "delegate_agent_ids": ["w"]}
        assert await of_agent(agent, resolve=resolve) is EVERY

    async def test_an_agent_that_is_gone_is_skipped(self):
        """The engine offers only agents that exist, so a deleted id adds nothing."""
        agent = {"type": "delegate", "tools": ["deepwiki__ask"], "delegate_agent_ids": ["gone"]}
        needs = await of_agent(agent, resolve=_NOBODY)
        assert servers_for(needs, ["deepwiki", "google"]) == ["deepwiki"]


class TestAVisitorsTurn:
    """F280 holds every agent and step a visitor reaches to the owner's list."""

    def test_a_member_is_not_bounded(self):
        assert bounded(EVERY, None) is EVERY

    def test_an_all_agent_gets_only_the_ticked_servers(self):
        visitor = VisitorScope("channel", frozenset({"google__search"}))
        assert servers_for(bounded(EVERY, visitor), ["deepwiki", "google"]) == ["google"]

    def test_the_list_and_the_agent_both_bound_it(self):
        visitor = VisitorScope("widget", frozenset({"google__search", "deepwiki__ask"}))
        needs = bounded(frozenset({"deepwiki__ask", "vercel__deploy"}), visitor)
        assert servers_for(needs, ["deepwiki", "google", "vercel"]) == ["deepwiki"]

    def test_nothing_ticked_is_no_server(self):
        assert bounded(EVERY, VisitorScope("channel")) == frozenset()

    def test_a_ticked_builder_listing_is_every_server(self):
        visitor = VisitorScope("widget", frozenset({"list_tool_servers"}))
        assert bounded(EVERY, visitor) is EVERY

    async def test_a_workflow_agent_on_a_phone_call_gets_only_the_ticked_servers(self, monkeypatch):
        """Naveen, D262: the visitor's list bounds a workflow agent's turn too."""
        async def resolve(agent_id):
            return {"id": agent_id, "type": "orchestrator", "orchestration_id": "o"}

        monkeypatch.setattr("core.scale.context.resolve_agent", resolve)
        visitor = VisitorScope("channel", frozenset({"google__search"}), approvals="park").to_dict()
        needs = await for_turn("harbor", visitor)
        assert servers_for(needs, ["deepwiki", "google", "vercel"]) == ["google"]


class TestForATurn:
    async def test_the_fleet_jobs_dict_is_read_as_a_visitor(self, monkeypatch):
        async def resolve(agent_id):
            return {"id": agent_id, "type": "conversational", "tools": ["all"]}

        monkeypatch.setattr("core.scale.context.resolve_agent", resolve)
        needs = await for_turn("a", {"surface": "widget", "tools": ["deepwiki__ask"]})
        assert needs == frozenset({"deepwiki__ask"})

    async def test_a_member_with_no_agent_gets_every_server(self):
        assert await for_turn(None, None) is EVERY

    async def test_an_agent_that_cannot_be_read_is_every_server(self, monkeypatch):
        async def resolve(agent_id):
            raise RuntimeError("the store is down")

        monkeypatch.setattr("core.scale.context.resolve_agent", resolve)
        assert await for_turn("a", None) is EVERY

    async def test_working_it_out_never_raises(self, monkeypatch):
        async def resolve(agent_id):
            return {"type": "conversational", "tools": ["deepwiki__ask"]}

        def broken(_agent):
            raise RuntimeError("settings unreadable")

        monkeypatch.setattr("core.scale.context.resolve_agent", resolve)
        monkeypatch.setattr("core.tools.effective_allowed_tools", broken)
        assert await for_turn("a", None) is EVERY


class TestWhichServersANameTakes:
    def test_every_server_for_none(self):
        assert servers_for(None, ["a", "b"]) == ["a", "b"]

    def test_by_prefix_in_the_servers_order(self):
        assert servers_for({"b__x", "a__y", "web_search"}, ["a", "b", "c"]) == ["a", "b"]

    def test_a_name_that_could_be_two_servers_takes_both(self):
        """OSS lets a server's name hold the separator (tool_router.py), so
        `a__b__c` is either `a`'s tool `b__c` or `a__b`'s tool `c`."""
        assert servers_for({"a__b__c"}, ["a", "a__b", "ab"]) == ["a", "a__b"]

    def test_a_bare_name_takes_none(self):
        assert servers_for({"deepwiki"}, ["deepwiki"]) == []


class TestTheBuildersListStaysTrue:
    def test_every_builder_tool_that_reads_the_sessions_is_listed(self):
        """A builder tool added later that reads `agent_sessions` (or aggregates
        over them) must join READS_EVERY_SESSION, or an agent holding it would
        list only the servers its turn happened to connect."""
        source = textwrap.dedent(inspect.getsource(builder_tools._dispatch))
        tree = ast.parse(source)
        readers: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            if not (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                    and test.left.id == "tool_name" and isinstance(test.ops[0], ast.Eq)
                    and isinstance(test.comparators[0], ast.Constant)):
                continue
            body = "\n".join(ast.unparse(stmt) for stmt in node.body)
            if "agent_sessions" in body or "aggregate_all_tools" in body or "_session_tools(" in body:
                readers.add(test.comparators[0].value)
        assert readers, "the scan found nothing: _dispatch changed shape, fix the scan"
        assert readers <= builder_tools.READS_EVERY_SESSION, readers - builder_tools.READS_EVERY_SESSION

    def test_the_list_names_real_builder_tools(self):
        names = {schema["function"]["name"] for schema in builder_tools.BUILDER_TOOL_SCHEMAS}
        assert builder_tools.READS_EVERY_SESSION <= names


def test_the_module_says_every_server_with_none():
    assert mcp_needs.EVERY is None
