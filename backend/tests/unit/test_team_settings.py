"""A Team agent's `team` block and the install's ceiling (TEAM1a, D283, Q1/Q2a)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from core import team
from core.config import default_settings, load_settings
from core.models import Agent, AgentTeam, Settings


def _agent(**kw) -> dict:
    return {"id": "lead", "name": "Lead", "description": "", "tools": [], "system_prompt": "",
            "type": "delegate", **kw}


def test_the_engine_defaults_carry_every_key_with_the_same_value():
    for key, value in team.DEFAULTS.items():
        assert default_settings()[key] == value, key
        assert Settings.model_fields[key].default == value, key


def test_a_saved_agent_keeps_its_team_block():
    saved = Agent(**_agent(team={"mode": "team", "at_once": 4, "task_budget": 8, "spend_limit_usd": 0.5})).dict()
    assert saved["team"] == {"mode": "team", "at_once": 4, "task_budget": 8, "spend_limit_usd": 0.5}


def test_no_block_reads_as_one_at_a_time_for_every_existing_delegate():
    assert Agent(**_agent()).dict()["team"] is None
    assert team.config_of(_agent(), settings={}) is None
    assert team.config_of(_agent(team={"mode": "one_at_a_time"}), settings={}) is None


@pytest.mark.parametrize("bad", [{"mode": "swarm"}, {"at_once": 0}, {"task_budget": 0}, {"task_budget": 51},
                                 {"spend_limit_usd": 0}, {"spend_limit_usd": -1}])
def test_a_bad_block_is_refused(bad):
    with pytest.raises(ValidationError):
        AgentTeam(**bad)


def test_members_at_once_is_capped_by_the_install_ceiling():
    cfg = team.config_of(_agent(team={"mode": "team", "at_once": 30}), settings={"team_at_once_ceiling": 10})
    assert cfg.at_once == 10
    assert team.config_of(_agent(team={"mode": "team"}), settings=load_settings()).at_once == 3


def test_only_a_delegate_is_a_team():
    assert team.config_of(_agent(type="conversational", team={"mode": "team"}), settings={}) is None


async def test_the_builder_refuses_a_bad_team_block_and_keeps_a_good_one(monkeypatch):
    from core import builder_tools
    from core.store import resources

    stored = {"lead": _agent()}

    async def _load(agent_id):
        return dict(stored[agent_id]) if agent_id in stored else None

    async def _save(agent):
        stored[agent["id"]] = agent

    monkeypatch.setattr(resources, "get_agent", _load)
    monkeypatch.setattr(resources, "save_agent", _save)
    bad = await builder_tools.execute_builder_tool(
        "update_agent", {"agent_id": "lead", "fields": {"team": {"mode": "swarm"}}}, None)
    assert "error" in bad and "team" in bad
    assert "team" not in stored["lead"] or stored["lead"].get("team") is None
    await builder_tools.execute_builder_tool(
        "update_agent", {"agent_id": "lead", "fields": {"team": {"mode": "team", "at_once": 2}}}, None)
    assert stored["lead"]["team"] == {"mode": "team", "at_once": 2, "task_budget": 6, "spend_limit_usd": None}
