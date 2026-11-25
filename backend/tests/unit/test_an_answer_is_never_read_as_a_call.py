"""An agent's answer is never read as a tool call, whatever its fields are named (F1745).

A workflow's Agent step that answers `welcome_email_body`, `recipient_name` and `recipient_email` ends with that
object. Read loosely, `recipient_name` is OpenAI's pseudo-format name for a call (F537), so the answer reads as a call
to a tool named after the customer; the agent, answering the same each time, is stopped for repeating that call, and
the step fails without the answer it gave. Now the pseudo-format is read only in its own shape: `recipient_name` and
a `parameters` object and nothing else, its name namespaced, listed in `tool_uses`, or one the turn offers. And an
Agent step's answer that names its declared fields is its answer: a "call" to a tool the turn does not offer, holding
one of them (an answer field named `tool` among them), is never dispatched.

Near misses read as before: F537's recorded calls, a bare namespaced pseudo-call, an offered tool called with an
argument named like an answer field, and a bare name the turn offers.
"""
from __future__ import annotations

import json
import types

import httpx
import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call

ANSWER = {"welcome_email_body": "Hi Marta, welcome to Harborpine.", "recipient_name": "Marta",
          "recipient_email": "marta@vellamar.example"}


def _calls_in(obj, offered=None):
    from core.tool_call_shapes import calls_in

    return calls_in(obj, offered) if offered is not None else calls_in(obj)


class TestThePseudoFormatInItsOwnShape:
    @pytest.mark.parametrize("obj", [
        ANSWER,
        {"recipient_name": "Marta"},
        {"recipient_name": "functions.lookup", "parameters": {}, "note": "and more"},
        {"recipient_name": "Not given in the brief", "parameters": "none"},
    ])
    def test_an_object_that_only_names_a_recipient_is_no_call(self, obj):
        assert _calls_in(obj) == []

    def test_f537s_calls_still_read(self):
        uses = {"tool_uses": [{"recipient_name": "functions.lookup_product", "parameters": {"name": "hubspot"}},
                              {"recipient_name": "lookup_product", "parameters": {"name": "zoho"}}]}
        assert [c["tool"] for c in _calls_in(uses)] == ["lookup_product", "lookup_product"]
        one = {"recipient_name": "functions.lookup_product", "parameters": {"name": "zoho"}}
        assert _calls_in(one) == [{"tool": "lookup_product", "arguments": {"name": "zoho"}}]
        wrapped = {"recipient_name": "multi_tool_use.parallel", "parameters": uses}
        assert len(_calls_in(wrapped)) == 2

    def test_a_bare_name_is_a_call_only_when_the_turn_offers_it(self):
        bare = {"recipient_name": "lookup_product", "parameters": {"name": "zoho"}}
        assert _calls_in(bare) == [], "with no offered tools to hold it to, a bare name is text"
        assert _calls_in(bare, {"lookup_product"}) == [{"tool": "lookup_product", "arguments": {"name": "zoho"}}]
        assert _calls_in(bare, {"something_else"}) == []

    def test_an_answer_naming_a_recipient_never_looks_like_a_call(self):
        from core.react_engine import looks_like_a_tool_call

        assert not looks_like_a_tool_call(json.dumps(ANSWER))
        assert looks_like_a_tool_call('{"recipient_name": "functions.lookup", "parameters": {"q": "x",}}')


@pytest.fixture
def sent(monkeypatch):
    import core.http_tool as http_tool

    record = types.SimpleNamespace(calls=[])

    async def send(client, tool, args, variables, **kw):
        record.calls.append((tool["name"], args))
        return httpx.Response(200, text="{}", request=httpx.Request("POST", tool["url"]))

    monkeypatch.setattr(http_tool, "send", send)
    return record


async def _step(fake_llm, run_id, fields, script, tools=()):
    """One Agent step that answers `fields`, its agent replying `script`: the run's events and its state."""
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    from core.orchestration.state_store import StoreStateBackend
    from core.routes.tools import _upsert_custom_tools

    if tools:
        await _upsert_custom_tools([{"name": n, "description": "d", "tool_type": "http", "method": "POST",
                                     "url": f"https://api.harborpine.example/{n}",
                                     "inputSchema": {"type": "object", "properties": {}}} for n in tools])
    agent = S.make_agent(name="Onboarding lead", tools=list(tools), skip_default_tools=True)
    await S.seed_agents([agent])
    orch = S.make_orchestration(id=f"orch_{run_id}", entry_step_id="lead", steps=[
        {"id": "lead", "name": "Onboarding lead", "type": "agent", "agent_id": agent["id"],
         "prompt_template": "Run the onboarding and answer its fields.", "output_key": "lead", "model": "claude-x",
         "max_turns": 6, "output_fields": [{"name": f, "type": "text"} for f in fields], "next_step_id": None}])
    await S.seed_orchestrations([orch])
    fake_llm.script(list(script))
    server = types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})
    events = [e async for e in OrchestrationEngine(Orchestration.model_validate(orch), server).run(
        "go", run_id=run_id)]
    return events, await StoreStateBackend().load(run_id)


def _status(events):
    return [e for e in events if e.get("type") == "orchestration_complete"][-1]["status"]


class TestAnAgentStepsAnswer:
    async def test_an_answer_naming_its_recipient_is_the_answer(self, fake_llm, sent):
        events, run = await _step(fake_llm, "r_f1745_a", list(ANSWER), [json.dumps(ANSWER)])
        assert _status(events) == "completed", [e.get("error") for e in events if e.get("error")]
        assert run.shared_state["lead"]["recipient_name"] == "Marta"
        assert not [e for e in events if e.get("type") == "repeat_stopped"]

    async def test_an_answer_with_a_field_named_tool_is_the_answer(self, fake_llm, sent):
        events, run = await _step(fake_llm, "r_f1745_b", ["tool", "price"], ['{"tool": "hammer", "price": 3}'])
        assert _status(events) == "completed"
        assert run.shared_state["lead"]["tool"] == "hammer"

    async def test_an_offered_tool_called_with_a_field_like_argument_still_runs(self, fake_llm, sent):
        # A name, not an address: an address the turn does not hold would stop on the card (F1744), not run.
        events, run = await _step(fake_llm, "r_f1745_c", ["recipient_name"],
                                  [tool_call("look_up_lead", recipient_name="Marta"), '{"recipient_name": "Marta"}'],
                                  tools=("look_up_lead",))
        assert [n for n, _ in sent.calls] == ["look_up_lead"] and _status(events) == "completed"

    async def test_a_bare_pseudo_call_to_an_offered_tool_runs(self, fake_llm, sent):
        events, _ = await _step(fake_llm, "r_f1745_d", ["recipient_email"],
                                ['{"recipient_name": "look_up_lead", "parameters": {"who": "Marta"}}',
                                 '{"recipient_email": "marta@vellamar.example"}'], tools=("look_up_lead",))
        assert sent.calls == [("look_up_lead", {"who": "Marta"})]
