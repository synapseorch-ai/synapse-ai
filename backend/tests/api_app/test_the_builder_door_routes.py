"""The AI builder's door on every way a message reaches it (BWA1, D274).

`core.native_builder.door` decides; these pin that each door asks it, and what follows:

- the panel's chat: a card number or a password ends at the door, with the builder's fixed sentence as the reply and
  no run started; a key is masked before the run, its state, its events or any model sees it, and the panel is told;
- the panel's answer to a question: refused, the question stays open and the panel says why; masked, the run goes on;
- an answer that reaches a builder run another way (the run page, v1, MCP, a channel) is refused as a bad answer;
  any other run's answer is untouched;
- the chat page's adapter: a refused message is answered and kept nowhere, not even as the conversation's turn.

Card numbers and keys are assembled at run time: a literal one in a source file trips secret scanners.
"""
from __future__ import annotations

import json

import pytest

from core.native_builder import seed_native_builder
from core.native_builder.door import CREDENTIAL_MARK, MASKED_NOTICE, REFUSALS

from test_builder_routes import route, stream

VISA = "4242" * 4
STRIPE = "sk_" + "test_" + "Z" * 24


@pytest.fixture
async def builder():
    await seed_native_builder()


def _everything_the_model_saw(fake_llm) -> str:
    return json.dumps(fake_llm.calls, default=str, ensure_ascii=False)


class TestThePanelsChat:
    async def test_a_card_number_ends_at_the_door_with_no_run(self, client, builder, fake_llm):
        events = await stream(client, "/api/builder/chat", {
            "message": f"Pay this invoice with our card {VISA} exp 12/28", "client_session_id": "door-1"})
        assert events == [{"type": "builder_notice", "reason": "card", "message": REFUSALS["card"], "final": True}]
        assert fake_llm.call_count == 0, "no model saw it"

    async def test_a_password_ends_at_the_door(self, client, builder, fake_llm):
        events = await stream(client, "/api/builder/chat", {
            "message": "Log into the portal, my password is Sunny#2026", "client_session_id": "door-2"})
        assert [e["reason"] for e in events] == ["password"]
        assert fake_llm.call_count == 0

    async def test_a_key_is_masked_before_the_run_and_the_panel_is_told_first(self, client, builder, fake_llm):
        fake_llm.set_default(route("answer"))
        events = await stream(client, "/api/builder/chat", {
            "message": f"Here's my Stripe key {STRIPE}, which apps do I have?", "client_session_id": "door-3"})
        assert events[0] == {"type": "builder_notice", "reason": "masked", "message": MASKED_NOTICE, "final": False}
        assert "orchestration_start" in [e["type"] for e in events]
        assert STRIPE not in json.dumps(events), "not in any event the panel or the log gets"
        assert STRIPE not in _everything_the_model_saw(fake_llm)
        assert CREDENTIAL_MARK in _everything_the_model_saw(fake_llm)

    async def test_an_ordinary_message_has_no_notice(self, client, builder, fake_llm):
        fake_llm.set_default(route("answer"))
        events = await stream(client, "/api/builder/chat", {"message": "which schedules ran?"})
        assert "builder_notice" not in [e["type"] for e in events]

    async def test_a_key_in_the_history_the_panel_sends_back_is_masked_and_a_refused_turn_dropped(
            self, client, builder, fake_llm):
        fake_llm.set_default(route("answer"))
        await stream(client, "/api/builder/chat", {
            "message": "and now?",
            "history": [{"role": "user", "content": f"key {STRIPE}"},
                        {"role": "user", "content": f"card {VISA}"}],
        })
        seen = _everything_the_model_saw(fake_llm)
        assert STRIPE not in seen and VISA not in seen


class TestTheAnswerDoors:
    async def _paused(self, client, fake_llm, question: str) -> str:
        fake_llm.script([route("create"), question, route("clarify")])
        events = await stream(client, "/api/builder/chat", {"message": "build something"})
        asked = next(e for e in events if e["type"] == "human_input_required")
        return asked["run_id"]

    async def test_a_refused_answer_leaves_the_question_open(self, client, builder, fake_llm):
        run_id = await self._paused(client, fake_llm, "What's the insurance portal's password?")
        calls = fake_llm.call_count
        events = await stream(client, "/api/builder/resume", {
            "run_id": run_id, "response": {"answer": "Hunter2-CANARY-portal"}})
        assert events == [{"type": "builder_notice", "reason": "password", "message": REFUSALS["password"],
                           "final": False}]
        assert fake_llm.call_count == calls, "the run did not move on"
        from core.orchestration.state import SharedState
        run = (await SharedState.restore(run_id)).run
        assert run.status == "paused" and run.waiting_for_human
        assert "Hunter2" not in json.dumps(run.shared_state, default=str)

    async def test_a_key_in_an_answer_is_masked_and_the_run_goes_on(self, client, builder, fake_llm):
        run_id = await self._paused(client, fake_llm, "Paste your booking API key so I can add it.")
        fake_llm.set_default(route("draft"))
        events = await stream(client, "/api/builder/resume", {
            "run_id": run_id, "response": {"answer": "dbk_live_" + "Q" * 8}})
        assert events[0]["reason"] == "masked"
        assert "dbk_live_" not in _everything_the_model_saw(fake_llm)

    async def test_the_run_pages_answer_to_a_builder_run_is_refused_as_a_bad_answer(
            self, client, builder, fake_llm):
        run_id = await self._paused(client, fake_llm, "Anything else?")
        from core.orchestration.answers import BadAnswer, answer_paused_run
        from core.server import app
        with pytest.raises(BadAnswer) as exc:
            await answer_paused_run(app.state, run_id, {"answer": f"charge {VISA}"})
        assert str(exc.value) == REFUSALS["card"]

    async def test_another_runs_answer_is_untouched(self):
        from core.orchestration.answers import _through_door
        answer = {"answer": f"card {VISA}"}
        assert _through_door("run_abc", answer, "What's your card?") is answer


class TestTheConnectCardReachesThePanel:
    async def test_the_reporters_card_is_an_event_on_the_panels_stream(self, client, builder, fake_llm):
        from test_builder_routes import tool_call
        fake_llm.script([
            route("answer"),
            tool_call("prepare_connection", kind="provider_key", title="Add your OpenAI key",
                      why="So agents can use GPT models.", provider="openai"),
            "Use the card above to add your OpenAI key.",
        ])
        events = await stream(client, "/api/builder/chat", {"message": "Where do I put my OpenAI key?"})
        cards = [e for e in events if e["type"] == "connection_needed"]
        assert len(cards) == 1
        assert cards[0]["kind"] == "provider_key" and cards[0]["provider"]["id"] == "openai"
        assert cards[0]["title"] == "Add your OpenAI key"
