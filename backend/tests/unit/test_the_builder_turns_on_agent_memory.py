"""The AI builder turns an agent's memory on, as the agent editor's Memory switch does (F1199, BLD1; CAP3's F-b).

`memory` is one true/false on the agent (`core.models.Agent`), read by the engine with `bool()`. The builder's
`create_agent` and `create_agents` dropped it, so an agent asked to "remember each customer" never could; its
`update_agent` copied any value through, so a stored "false" was on. Now all three take it, true or false only,
in cloud's agent route's words (`core.agent_memory.memory_refusal`), and turning it on asks the host's allowance
(`set_memory_check`): once for a batch, never for an agent that already has it.
"""
import json
import types

import pytest

from _fakes import seed as S


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def call(tool, args):
    from core.builder_tools import execute_builder_tool
    return json.loads(await execute_builder_tool(tool, args, _server()))


def _spec(**kw):
    return {"name": "Account helper", "description": "Answers each customer, remembering them", "type": "conversational",
            "tools": [], "system_prompt": "Remember what each customer prefers.", **kw}


async def _stored(agent_id):
    from core.routes.agents import load_user_agents
    return next((a for a in await load_user_agents() if a["id"] == agent_id), None)


async def _names():
    from core.routes.agents import load_user_agents
    return {a["name"] for a in await load_user_agents()}


@pytest.fixture
def allowance():
    """The host's memory allowance, recording what it was asked; `left` is how many more it lets on."""
    from core import builder_tools
    asked = []
    state = {"left": 10}

    async def check(adding, agent_id):
        asked.append((adding, agent_id))
        return None if adding <= state["left"] else "Your plan keeps memory for 1 agent; turn it off on another first."

    builder_tools.set_memory_check(check)
    try:
        yield types.SimpleNamespace(asked=asked, state=state)
    finally:
        builder_tools.set_memory_check(None)


class TestTheToolsOfferIt:
    def test_create_and_create_many_take_memory_as_a_boolean(self):
        from core.builder_tools import BUILDER_TOOL_SCHEMAS
        by_name = {t["function"]["name"]: t["function"]["parameters"] for t in BUILDER_TOOL_SCHEMAS}
        assert by_name["create_agent"]["properties"]["memory"]["type"] == "boolean"
        item = by_name["create_agents"]["properties"]["agents"]["items"]
        assert item["properties"]["memory"]["type"] == "boolean"


class TestCreatingAnAgentWithMemory:
    async def test_memory_on_is_saved(self):
        result = await call("create_agent", _spec(memory=True))
        assert result["status"] == "created"
        assert (await _stored(result["agent"]["id"]))["memory"] is True

    async def test_memory_off_or_left_out_is_off(self):
        off = await call("create_agent", _spec(memory=False))
        assert (await _stored(off["agent"]["id"]))["memory"] is False
        plain = await call("create_agent", _spec(name="Plain helper"))
        assert not (await _stored(plain["agent"]["id"])).get("memory")

    @pytest.mark.parametrize("value", ["yes", "false", 1, {"on": True}])
    async def test_anything_but_true_or_false_is_refused(self, value):
        result = await call("create_agent", _spec(memory=value))
        assert result == {"error": "'memory' must be true or false."}
        assert "Account helper" not in await _names()

    async def test_create_agents_takes_it_per_agent(self):
        result = await call("create_agents", {"agents": [
            _spec(name="Rememberer", role="keeps", memory=True),
            _spec(name="Forgetter", role="fresh"),
            _spec(name="Muddled", role="bad", memory="on"),
        ]})
        made = {a["role"]: a["id"] for a in result["agents"]}
        assert set(made) == {"keeps", "fresh"}
        assert (await _stored(made["keeps"]))["memory"] is True
        assert not (await _stored(made["fresh"])).get("memory")
        (error,) = result["errors"]
        assert error["role"] == "bad" and error["error"] == "'memory' must be true or false."


class TestUpdatingAnAgentsMemory:
    async def test_memory_is_turned_on_and_off(self):
        await S.seed_agents([S.make_agent(id="agent_mem_a", name="Ana", tools=[])])
        assert (await call("update_agent", {"agent_id": "agent_mem_a", "fields": {"memory": True}}))["status"] == "updated"
        assert (await _stored("agent_mem_a"))["memory"] is True
        await call("update_agent", {"agent_id": "agent_mem_a", "fields": {"memory": False}})
        assert (await _stored("agent_mem_a"))["memory"] is False

    async def test_a_string_is_refused_and_nothing_changes(self):
        # Was saved as given: the engine's bool("false") is True, so the agent "turned off" kept remembering.
        await S.seed_agents([S.make_agent(id="agent_mem_b", name="Ben", tools=[], memory=False)])
        result = await call("update_agent", {"agent_id": "agent_mem_b",
                                             "fields": {"memory": "false", "description": "changed"}})
        assert result == {"error": "'memory' must be true or false."}
        stored = await _stored("agent_mem_b")
        assert stored["memory"] is False and stored["description"] != "changed"


class TestTheHostsAllowance:
    async def test_turning_memory_on_asks_it(self, allowance):
        allowance.state["left"] = 0
        result = await call("create_agent", _spec(memory=True))
        assert result["refused"] == "plan_limit" and "turn it off on another" in result["error"]
        assert "Account helper" not in await _names()
        assert allowance.asked == [(1, None)]

    async def test_an_agent_without_memory_never_asks(self, allowance):
        allowance.state["left"] = 0
        assert (await call("create_agent", _spec(memory=False)))["status"] == "created"
        assert (await call("create_agent", _spec(name="Plain helper")))["status"] == "created"
        assert allowance.asked == []

    async def test_a_batch_is_asked_once_for_all_of_it(self, allowance):
        allowance.state["left"] = 1
        result = await call("create_agents", {"agents": [_spec(name="One", memory=True), _spec(name="Two", memory=True),
                                                         _spec(name="Three")]})
        assert result["refused"] == "plan_limit"
        assert not {"One", "Two", "Three"} & await _names()
        assert allowance.asked == [(2, None)]

    async def test_an_edit_asks_for_its_own_agent_and_one_already_on_keeps_it(self, allowance):
        await S.seed_agents([S.make_agent(id="agent_mem_c", name="Cy", tools=[]),
                             S.make_agent(id="agent_mem_d", name="Di", tools=[], memory=True)])
        allowance.state["left"] = 0
        refused = await call("update_agent", {"agent_id": "agent_mem_c", "fields": {"memory": True}})
        assert refused["refused"] == "plan_limit"
        assert not (await _stored("agent_mem_c")).get("memory")
        assert allowance.asked == [(1, "agent_mem_c")]
        # A limit lowered later never strands an agent that has memory: its other edits save, unasked.
        kept = await call("update_agent", {"agent_id": "agent_mem_d", "fields": {"memory": True, "description": "new"}})
        assert kept["status"] == "updated" and allowance.asked == [(1, "agent_mem_c")]


class TestTheRuleIsTheAppsOwn:
    def test_the_words_are_cloud_agent_routes(self):
        from core.agent_memory import MEMORY_NOT_BOOLEAN, memory_refusal
        assert MEMORY_NOT_BOOLEAN == "'memory' must be true or false."
        assert [memory_refusal(v) for v in (True, False, None)] == [None, None, None]
        assert memory_refusal("true") == MEMORY_NOT_BOOLEAN
