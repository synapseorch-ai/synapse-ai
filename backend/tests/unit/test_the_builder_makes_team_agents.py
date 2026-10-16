"""The AI builder makes Team agents (TEAM1e, D290; 20 Q1, Q9).

A Team agent is stored as `delegate` with a `team` block (TEAM1a): "how members work", one at a time or as a team,
how many at once, its task budget and an optional spend limit. The builder's `create_agent` wrote a fixed dict and
dropped `team`, so every delegate it made worked one at a time. Now `create_agent` and `create_agents` take it, and
a Team agent the builder makes starts as a team, as a new one in the editor does (Q1); `team` on any other type is
refused, there and in `update_agent`. The builder never uses a team itself (Q9, with BWA1): its own agents have none.
"""
import json
import types

from _fakes import seed as S


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def call(tool, args):
    from core.builder_tools import execute_builder_tool
    return json.loads(await execute_builder_tool(tool, args, _server()))


async def _members():
    await S.seed_agents([S.make_agent(id="agent_bt_ana", name="Ana", tools=[]),
                         S.make_agent(id="agent_bt_ben", name="Ben", tools=[])])


def _team_spec(**kw):
    return {"name": "Supplier review", "description": "Reviews suppliers with its analysts", "type": "delegate",
            "tools": [], "system_prompt": "Plan the review, assign each supplier, gather the findings.",
            "delegate_agent_ids": ["agent_bt_ana", "agent_bt_ben"], **kw}


async def _stored(agent_id):
    from core.routes.agents import load_user_agents
    return next(a for a in await load_user_agents() if a["id"] == agent_id)


class TestCreatingATeamAgent:
    async def test_a_team_agent_starts_as_a_team(self):
        await _members()
        result = await call("create_agent", _team_spec())
        assert result["status"] == "created"
        stored = await _stored(result["agent"]["id"])
        assert stored["team"]["mode"] == "team"
        assert stored["delegate_agent_ids"] == ["agent_bt_ana", "agent_bt_ben"]

    async def test_its_settings_are_kept_and_checked(self):
        await _members()
        result = await call("create_agent", _team_spec(team={"mode": "team", "at_once": 2, "task_budget": 4,
                                                              "spend_limit_usd": 0.5}))
        stored = await _stored(result["agent"]["id"])
        assert stored["team"] == {"mode": "team", "at_once": 2, "task_budget": 4, "spend_limit_usd": 0.5}
        bad = await call("create_agent", _team_spec(team={"mode": "team", "at_once": 0}))
        assert "error" in bad and "team settings" in bad["error"]

    async def test_one_at_a_time_when_asked(self):
        await _members()
        result = await call("create_agent", _team_spec(team={"mode": "one_at_a_time"}))
        assert (await _stored(result["agent"]["id"]))["team"]["mode"] == "one_at_a_time"

    async def test_a_team_block_on_another_type_is_refused(self):
        result = await call("create_agent", {"name": "Solo", "description": "d", "type": "conversational",
                                             "tools": [], "system_prompt": "p", "team": {"mode": "team"}})
        assert "error" in result and "Team agent" in result["error"]

    async def test_a_plain_agent_has_no_team(self):
        result = await call("create_agent", {"name": "Solo", "description": "d", "type": "conversational",
                                             "tools": [], "system_prompt": "p"})
        assert "team" not in await _stored(result["agent"]["id"])

    async def test_create_agents_makes_teams_too(self):
        await _members()
        result = await call("create_agents", {"agents": [
            {**_team_spec(role="lead")},
            {**_team_spec(name="Picky", role="bad"), "type": "conversational", "team": {"mode": "team"}},
        ]})
        (made,) = result["agents"]
        assert made["role"] == "lead"
        assert (await _stored(made["id"]))["team"]["mode"] == "team"
        (refused,) = result["errors"]
        assert refused["role"] == "bad" and "Team agent" in refused["error"]


class TestUpdatingOne:
    async def test_a_team_block_is_refused_on_an_agent_that_is_not_a_team(self):
        await S.seed_agents([S.make_agent(id="agent_bt_solo", name="Solo", tools=[])])
        result = await call("update_agent", {"agent_id": "agent_bt_solo", "fields": {"team": {"mode": "team"}}})
        assert "error" in result and "Team agent" in result["error"]
        assert "team" not in (await _stored("agent_bt_solo"))

    async def test_a_delegate_becomes_a_team(self):
        await _members()
        made = await call("create_agent", _team_spec(team={"mode": "one_at_a_time"}))
        result = await call("update_agent", {"agent_id": made["agent"]["id"], "fields": {"team": {"mode": "team"}}})
        assert result["status"] == "updated"
        assert (await _stored(made["agent"]["id"]))["team"]["mode"] == "team"


class TestTheSchemas:
    def test_they_name_members_and_how_they_work(self):
        from core.builder_tools import BUILDER_TOOL_SCHEMAS

        by_name = {s["function"]["name"]: s["function"] for s in BUILDER_TOOL_SCHEMAS}
        one = by_name["create_agent"]
        assert "Team agent" in one["description"]
        assert one["parameters"]["properties"]["team"]["properties"]["mode"]["enum"] == ["team", "one_at_a_time"]
        many = by_name["create_agents"]["parameters"]["properties"]["agents"]["items"]["properties"]
        assert "team" in many
        assert "team" in by_name["update_agent"]["parameters"]["properties"]["fields"]["description"]
