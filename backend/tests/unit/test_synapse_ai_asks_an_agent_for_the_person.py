"""Synapse AI asks an agent something for the person, on a card (F1274, REACH1; D340 §9, Naveen 10-30).

Naveen: talking to an agent for the person is reach: an act card names the agent, the message and roughly what it
costs, then shows the agent's answer; the agent's own tool approvals still apply. These pin:

- the card names the agent and the message, and what a short reply costs from its model's listed price, or that it
  isn't known when the model has none; nothing is asked until the press;
- its button is the agent's own chat turn (`chat_with_agent`, a builder's), for that agent by id;
- the press asks it and the act route answers with what it said; one that waits on a person is said as waiting;
- an agent that isn't here, and a message left out, are said, never carded;
- the op talk_to_agent is the change table's, an act on an agent whose card's tool is chat_with_agent, and the one
  planner's rule 20 names it.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import models_menu as MM
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
def fresh():
    from core.mcp_server import runs

    confirm.set_claim_store(None)
    builder_access.forget_runs()
    before = runs.get_run_backend()
    yield
    runs.set_run_backend(before)
    confirm.set_claim_store(None)
    hooks.set_role_source(None)
    builder_access.forget_runs()


def _entry(id, price_in, price_out):
    return MM.ModelEntry(id=id, name=id, provider="p", source="key", tools="native", vision=False, reasoning=False,
                         price_in=price_in, price_out=price_out, can_pay=True)


@pytest.fixture
async def agent(monkeypatch):
    from core.store.resources import save_agent
    from tests._fakes.seed import make_agent

    made = make_agent(name="Support bot", model="priced-model", system_prompt="You answer support questions.")
    await save_agent(made)

    async def menu():
        return [_entry("priced-model", 3.0, 15.0)]

    monkeypatch.setattr(MM, "menu", menu)
    return made


async def _prepare(args):
    await SharedState(OrchestrationRun(run_id="builder_talk", orchestration_id="orch_native_builder",
                                       shared_state={builder_access.CALLER_KEY: {"user_id": None}})).checkpoint()
    raw, events = await execute_builder_tool_with_events("prepare_action", {"action": "talk_to_agent", **args},
                                                         _Server(), run_id="builder_talk")
    return json.loads(raw), [e for e in events if e.get("type") == "action_needed"]


class TestTheCard:
    async def test_it_names_the_agent_the_message_and_roughly_what_it_costs(self, client, agent):
        said, [card] = await _prepare({"agent": "support bot", "message": "What did customer 42 ask yesterday?"})
        assert "nothing has been done" in said["say"].lower()
        assert card["title"] == 'Ask "Support bot"'
        assert card["detail"].startswith("What did customer 42 ask yesterday?\n")
        # (29 + 35) chars / 4 + 1,500 tokens in at $3, and 600 out at $15, per 1M: $0.0135.
        assert card["detail"].endswith("A short reply on priced-model costs about $0.01; each tool it uses adds a model "
                                       "call.")
        [button] = card["buttons"]
        assert button["tool"] == "chat_with_agent"
        assert button["arguments"] == {"message": "What did customer 42 ask yesterday?", "agent": agent["id"],
                                       "wait_seconds": 90}

    async def test_a_model_with_no_listed_price_is_said(self, client, agent, monkeypatch):
        async def menu():
            return [_entry("priced-model", None, None)]

        monkeypatch.setattr(MM, "menu", menu)
        _, [card] = await _prepare({"agent": agent["id"], "message": "Hello"})
        assert "has no listed price here" in card["detail"]

    async def test_no_agent_and_no_message_are_said_not_carded(self, client, agent):
        said, cards = await _prepare({"agent": "nobody here", "message": "Hello"})
        assert cards == [] and "No agent is called 'nobody here'" in said["error"]
        said, cards = await _prepare({"agent": "Support bot", "message": "  "})
        assert cards == [] and said["error"] == "Say what to ask it."


class TestThePress:
    async def _press(self, client, agent, reply):
        from core.mcp_server import runs

        asked = []

        class Backend(runs.InProcessRuns):
            async def chat(self, *, message, agent, session_id, wait):
                asked.append((message, agent, wait))
                return reply

        runs.set_run_backend(Backend())
        _, [card] = await _prepare({"agent": "Support bot", "message": "Summarise today's tickets"})
        done = await client.post("/api/assistant/act", json=card["buttons"][0])
        assert done.status_code == 200, done.text
        assert asked == [("Summarise today's tickets", agent["id"], 90.0)]
        return done.json()["result"]

    async def test_it_asks_the_agent_and_answers_with_what_it_said(self, client, agent):
        result = await self._press(client, agent, {"response": "Three tickets, all about refunds.",
                                                   "status": "completed", "session_id": "s1"})
        assert result["response"] == "Three tickets, all about refunds." and result["status"] == "completed"

    async def test_its_own_approval_still_waits_on_the_person(self, client, agent):
        result = await self._press(client, agent, {"response": "Allow send_email?", "status": "awaiting_approval",
                                                   "note": "Answer it in the Synapse app."})
        assert result["status"] == "awaiting_approval"


def test_the_op_is_the_change_tables_and_the_planner_names_it():
    import re
    from pathlib import Path

    from core.builder_actions import ACTIONS
    from core.native_builder import change_ops as C
    from core.native_builder.answer_pack import ACT_TOOLS

    op = C.OPS["talk_to_agent"]
    assert op.is_act and op.target == "agent" and op.required == ("message",)
    assert "talk_to_agent" in ACTIONS and ACT_TOOLS["talk_to_agent"] == "chat_with_agent"
    assert C.act_arguments(op, "agent_1", {"message": "Hi"}) == {"action": "talk_to_agent", "agent": "agent_1",
                                                                  "message": "Hi"}
    planner = Path(__file__).resolve().parents[2] / "core" / "native_builder" / "agents" / "planner.json"
    prompt = json.loads(planner.read_text("utf-8"))["system_prompt"]
    rule = re.search(r"\n20\. (.*?)(?=\n\d+\. |\n\n)", prompt, re.S)
    assert rule and "talk_to_agent" in rule.group(1) and "roughly what it costs" in rule.group(1)
