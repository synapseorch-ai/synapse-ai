"""An agent's own call that sends uses only addresses its turn holds (F1744).

An agent writes each tool call itself, in a chat, an API or scheduled turn, a team, a channel or a workflow's Agent
step. Without a check it can send to an address it made up, and nothing a person approved or gave holds it back. Now
a call that leaves the engine (an MCP server's tool or a custom tool) and does more than read is held at the gate
every call goes through: each email, phone number and destination link (a url, endpoint or webhook input) must be one
the turn holds, or the call stops on the approval card naming it. The turn holds what the person said (an agent's
own earlier words only once a person answered them), the agent's instructions and memory, a workflow run's state, and
what its tools answered without an error; never a tool's description, an error's body, or what a person declined.
Where nobody can answer a card (a website visitor's turn, a channel's), the call is not run and the agent is told
why. A person who declines and writes the right address sees the corrected call before it goes out.

Near misses read as before: an address from the message, the instructions, the conversation a person answered, a
look-up's answer or the run; a link inside a message's words; a tool that only reads.
"""
from __future__ import annotations

import json
import types

import httpx
import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call

MARTA = "marta@vellamar.example"
MADE_UP = "enquiry.contact@customer.example"
EMAIL = {"type": "object", "required": ["to", "subject", "text"], "properties": {
    "to": {"type": "string", "description": "Who it goes to"}, "subject": {"type": "string"},
    "text": {"type": "string"}}}
LOOKUP = {"type": "object", "required": ["who"], "properties": {"who": {"type": "string"}}}
HOOK = {"type": "object", "required": ["webhook_url", "text"], "properties": {
    "webhook_url": {"type": "string"}, "text": {"type": "string"}}}


@pytest.fixture
def sent(monkeypatch):
    """Every request an HTTP tool sends, answered by `sent.answer(name, args)`: (status, text)."""
    import core.http_tool as http_tool

    record = types.SimpleNamespace(calls=[], answer=lambda name, args: (200, "{}"))

    async def send(client, tool, args, variables, **kw):
        record.calls.append((tool["name"], args))
        status, text = record.answer(tool["name"], args)
        return httpx.Response(status, text=text, request=httpx.Request(tool.get("method") or "POST", tool["url"]))

    monkeypatch.setattr(http_tool, "send", send)
    return record


@pytest.fixture(autouse=True)
async def tools_here():
    from core.routes.tools import _upsert_custom_tools

    await _upsert_custom_tools([
        {"name": n, "description": d, "tool_type": "http", "method": m, "url": f"https://api.harborpine.example/{n}",
         "inputSchema": schema}
        for n, d, m, schema in (("send_email", "Sends an email, e.g. to help@harborpine.example", "POST", EMAIL),
                                ("find_lead", "Finds a lead by name", "GET", LOOKUP),
                                ("post_hook", "Posts to a webhook", "POST", HOOK))])


def _agent(**extra):
    return S.make_agent(tools=["send_email", "find_lead", "post_hook"], skip_default_tools=True, **extra)


async def _turn(fake_llm, script, *, message="Reply to the new lead.", agent=None, **kw):
    from core.react_engine import run_agent_step

    fake_llm.script(script)
    agent = agent or _agent()
    server = types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})
    return [ev async for ev in run_agent_step(message=message, agent_id=agent["id"], session_id="s_f1744",
                                              server_module=server, agent_override=agent, max_turns=4, **kw)]


def _asked(events) -> dict | None:
    return next((e for e in events if e.get("type") == "human_input_required"), None)


def _email(to, text="Dear Marta, …"):
    return tool_call("send_email", to=to, subject="Your coffee deliveries", text=text)


def _to(sent) -> list:
    return [args.get("to") for name, args in sent.calls if name == "send_email"]


class TestItStops:
    async def test_a_made_up_address_stops_on_the_card_and_nothing_is_sent(self, fake_llm, sent):
        events = await _turn(fake_llm, [_email(MADE_UP), "done"])
        asked = _asked(events)
        assert _to(sent) == [] and asked and asked["kind"] == "tool_approval"
        assert f"**send_email** with {MADE_UP}, which isn't anywhere in this conversation" in asked["prompt"]
        assert "decline and write the right address in your reason" in asked["prompt"]
        assert asked["unseen"] == [MADE_UP]

    async def test_an_address_only_in_the_tools_description_stops(self, fake_llm, sent):
        events = await _turn(fake_llm, [_email("help@harborpine.example"), "done"])
        assert _to(sent) == [] and _asked(events)

    async def test_an_address_only_in_an_error_body_stops(self, fake_llm, sent):
        sent.answer = lambda name, args: ((422, "unknown mailbox; did you mean billing@vellamar.example?")
                                          if args.get("to") == MARTA else (200, "{}"))
        events = await _turn(fake_llm, [_email(MARTA), _email("billing@vellamar.example"), "done"],
                             message=f"Reply to the lead at {MARTA}.")
        assert _to(sent) == [MARTA] and _asked(events), "the error's address is never sent"

    async def test_its_own_earlier_words_nobody_answered_hold_nothing(self, fake_llm, sent):
        history = [{"role": "user", "content": "Find the lead."},
                   {"role": "assistant", "content": f"The lead is at {MADE_UP}."}]
        events = await _turn(fake_llm, [_email(MADE_UP), "done"], history_override=history)
        assert _to(sent) == [] and _asked(events)

    async def test_a_webhook_on_a_host_the_turn_does_not_hold_stops(self, fake_llm, sent):
        events = await _turn(fake_llm, [tool_call("post_hook", webhook_url="https://hooks.unseen.example/x",
                                                  text="New lead"), "done"])
        assert sent.calls == [] and "https://hooks.unseen.example/x" in _asked(events)["prompt"]


class TestItPasses:
    async def test_an_address_from_the_message(self, fake_llm, sent):
        events = await _turn(fake_llm, [_email(MARTA), "done"], message=f"Reply to the lead at {MARTA}.")
        assert _to(sent) == [MARTA] and not _asked(events)

    async def test_an_address_from_the_agents_instructions(self, fake_llm, sent):
        agent = _agent(system_prompt="You answer leads; copy every reply to desk@harborpine.example.")
        events = await _turn(fake_llm, [_email("desk@harborpine.example"), "done"], agent=agent)
        assert _to(sent) == ["desk@harborpine.example"] and not _asked(events)

    async def test_its_own_words_a_person_answered(self, fake_llm, sent):
        history = [{"role": "assistant", "content": f"The lead is at {MARTA}. Shall I reply?"},
                   {"role": "user", "content": "Yes please."}]
        events = await _turn(fake_llm, [_email(MARTA), "done"], history_override=history, message="Go ahead.")
        assert _to(sent) == [MARTA] and not _asked(events)

    async def test_an_address_a_look_up_answered(self, fake_llm, sent):
        sent.answer = lambda name, args: (200, json.dumps({"email": MARTA})) if name == "find_lead" else (200, "{}")
        events = await _turn(fake_llm, [tool_call("find_lead", who="Marta"), _email(MARTA), "done"])
        assert _to(sent) == [MARTA] and not _asked(events)

    async def test_a_link_inside_a_messages_words_sends_nothing_anywhere(self, fake_llm, sent):
        events = await _turn(fake_llm, [_email(MARTA, text="See https://plans.unseen.example/grinder"), "done"],
                             message=f"Reply to the lead at {MARTA}.")
        assert _to(sent) == [MARTA] and not _asked(events)

    async def test_a_tool_that_only_reads_is_not_held(self, fake_llm, sent):
        events = await _turn(fake_llm, [tool_call("find_lead", who=MADE_UP), "done"])
        assert [n for n, _ in sent.calls] == ["find_lead"] and not _asked(events)


class TestWhereNobodyCanAnswer:
    async def test_a_website_visitor_sees_the_agents_answer_and_nothing_is_sent(self, fake_llm, sent):
        from core import visitor
        from core.routes.widget import _visitor_view

        with visitor.scope(visitor.VisitorScope(surface="widget", tools=frozenset({"send_email"}))):
            events = await _turn(fake_llm, [_email(MADE_UP), "I can't send that without your email address."])
        seen = [item for e in events for item in _visitor_view(e)[0]]
        assert _to(sent) == [] and not _asked(events)
        assert [s["type"] for s in seen if s["type"] in ("final", "response")] == ["final"]
        assert MADE_UP not in json.dumps(seen), "nothing of the refused call reaches the visitor"
        told = fake_llm.calls[-1].get("prompt_msg") or ""
        assert f"'send_email' was not run: {MADE_UP} isn't anywhere" in told

    async def test_a_channels_turn_is_refused_in_words_not_parked(self, fake_llm, sent):
        from core.chat_approval import nobody_to_ask

        with nobody_to_ask():
            events = await _turn(fake_llm, [_email(MADE_UP), "What address should I use?"])
        finals = [e.get("response") for e in events if e.get("type") == "final"]
        assert _to(sent) == [] and not _asked(events) and finals == ["What address should I use?"]


class TestTheCard:
    def test_a_reopened_card_says_the_same_words(self):
        from core.chat_approval import parked_payload, public_view

        event = {"tool_name": "send_email", "tool_args": {"to": MADE_UP}, "approval_id": "a1", "category": "write",
                 "unseen": [MADE_UP], "corrected": False}
        view = public_view(parked_payload(event, message="m", progress={}))
        assert MADE_UP in view["prompt"] and MADE_UP in view["questions"][0]["prompt"]
        assert "decline and write the right address" in view["prompt"]


class TestInAWorkflow:
    """An Agent step after an approval: the run's state and its card hold the address; a declined call, corrected,
    is shown again before it goes out."""

    CHECK = {"id": "check", "name": "Check the reply", "type": "human", "output_key": "check",
             "human_prompt": "To: {{state.claim_and_draft.recipient_email}}", "next_step_id": "reply",
             "human_fields": [{"name": "approved", "type": "boolean", "label": "Send this reply?"}]}

    async def _run(self, run_id):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine

        agent = _agent(name="Sales desk")
        await S.seed_agents([agent])
        reply = {"id": "reply", "name": "Send the reply", "type": "agent", "agent_id": agent["id"],
                 "prompt_template": "Send the approved reply.", "output_key": "reply", "model": "claude-x",
                 "max_turns": 4, "next_step_id": None}
        orch = S.make_orchestration(id=f"orch_{run_id}", entry_step_id="check", steps=[self.CHECK, reply])
        await S.seed_orchestrations([orch])
        server = types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})
        return [e async for e in OrchestrationEngine(Orchestration.model_validate(orch), server).run(
            "A new lead's card", run_id=run_id, initial_state={"claim_and_draft": {"recipient_email": MARTA}})]

    async def _resume(self, run_id, answer):
        from core.orchestration.engine import OrchestrationEngine

        server = types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})
        return [e async for e in OrchestrationEngine.resume(run_id, answer, server)]

    async def test_the_address_the_card_showed_is_sent(self, fake_llm, sent):
        fake_llm.script([_email(MARTA), "Sent."])
        await self._run("r_f1744_w1")
        events = await self._resume("r_f1744_w1", {"approved": True})
        assert _to(sent) == [MARTA] and not _asked(events)

    async def test_declined_corrected_shown_again_then_sent_once(self, fake_llm, sent):
        fake_llm.script([_email(MADE_UP)])
        await self._run("r_f1744_w2")
        asked = _asked(await self._resume("r_f1744_w2", {"approved": True}))
        assert asked and MADE_UP in asked["prompt"] and _to(sent) == []
        fake_llm.script([_email("ana@lisbon.example"), "Sent."])
        again = _asked(await self._resume("r_f1744_w2", {"approve": False,
                                                          "reason": "Send it to ana@lisbon.example"}))
        assert again and again["tool_args"]["to"] == "ana@lisbon.example" and _to(sent) == []
        assert "again, written from what you said" in again["prompt"]
        await self._resume("r_f1744_w2", {"approve": True})
        assert _to(sent) == ["ana@lisbon.example"]

    async def test_declined_with_the_made_up_address_again_sends_nothing_to_it(self, fake_llm, sent):
        fake_llm.script([_email(MADE_UP)])
        await self._run("r_f1744_w3")
        await self._resume("r_f1744_w3", {"approved": True})
        fake_llm.script([_email(MADE_UP), "I couldn't send it."])
        events = await self._resume("r_f1744_w3", {"approve": False, "reason": f"{MADE_UP} isn't real"})
        assert MADE_UP not in _to(sent)
        told = " ".join(c.get("prompt_msg") or "" for c in fake_llm.calls)
        assert "the person declined sending 'send_email'" in told
        assert _asked(events) is None or MADE_UP in _asked(events)["prompt"]
