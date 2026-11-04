"""A key pasted while a Connect card is up is kept nowhere, whatever was asked (F1345, TRI1; CAP3's cap-03 diagnosis;
Naveen 11-04: "Build as planned", and a new message too).

cap-03: the builder asked for a connection at its plan's review ("connect the MCP server … on its card"), and the person
pasted the exact token as their feedback, twice. F1280's lone-key rule held only at the Connect card's own wait, and the
token has no shape the scrubber knows, so it reached three planner logs, the orchestration's log and the run's row. A
lone secret-looking word is now the card's key whenever the run put a Connect card up — read from the turn's own card
(`turn_facts`), never from words — in an answer to whatever it asks, and in the next message after a turn that put one
up. The person is told it was taken out (the key's notice, which sends them to the card), and the card stays theirs to
use. With no card up, a lone word is read as before.

The token below is assembled at run time: a literal one in a source file trips secret scanners.
"""
from __future__ import annotations

import json

import pytest

from core.native_builder import seed_native_builder
from core.native_builder.door import CREDENTIAL_MARK
from core.native_builder.turn_facts import keep

from test_builder_routes import route, stream

CANARY = "aaaaaaa-" + "aaaaaa-" + "aaaaa-" + "9999"
CARD = {"kind": "mcp_server", "what": "https://tracker.example/mcp", "title": "Connect Tracker"}


@pytest.fixture
async def builder():
    await seed_native_builder()


def _files_holding(root, *needles: str) -> list[str]:
    return [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()
            and any(n.encode() in p.read_bytes() for n in needles)]


class TestAnAnswerWhileItsCardIsUp:
    async def _answered(self, client, monkeypatch, run_id: str) -> tuple[list, list]:
        from core.orchestration.engine import OrchestrationEngine
        from core.routes import builder as routes

        async def at_the_review(_run_id):
            return "Here is the plan. Connect the tracker's server on its card, then approve it.", "step_human_review"

        got: list = []

        async def resume(run_id, response, *args, **kwargs):
            got.append(response)
            yield {"type": "human_input_required", "run_id": run_id, "prompt": "again", "fields": []}

        monkeypatch.setattr(routes, "_pending_question", at_the_review)
        monkeypatch.setattr(OrchestrationEngine, "resume", staticmethod(resume))
        events = await stream(client, "/api/builder/resume", {"run_id": run_id, "response": {"feedback": CANARY}})
        return events, got

    async def test_a_lone_key_as_review_feedback_is_masked_and_said(self, client, builder, monkeypatch, tmp_path):
        await keep("builder_f1345", None, connects=[CARD])
        events, got = await self._answered(client, monkeypatch, "builder_f1345")
        assert events[0]["type"] == "builder_notice" and events[0]["reason"] == "masked", events[0]
        assert events[0]["kept"] == CREDENTIAL_MARK and "Connect card" in events[0]["message"]
        assert got == [{"feedback": CREDENTIAL_MARK}], "the run is resumed with the key taken out"
        assert CANARY not in json.dumps(events, default=str)
        assert _files_holding(tmp_path, CANARY) == []

    async def test_with_no_card_up_a_lone_word_is_read_as_before(self, client, builder, monkeypatch):
        _events, got = await self._answered(client, monkeypatch, "builder_f1345_none")
        assert got == [{"feedback": CANARY}]

    async def test_every_way_an_answer_reaches_a_builder_run_reads_its_card(self, client, builder):
        from core.orchestration import answers

        card_up = getattr(answers, "_card_up", None)
        assert card_up is not None, "the answer door reads no card of the run's"
        await keep("builder_f1345_doors", None, connects=[CARD])
        assert await card_up("builder_f1345_doors") is True
        assert await card_up("builder_f1345_nothing") is False
        assert await card_up("run_a_workflow") is False  # a workflow's run is never asked
        masked = answers._through_door("builder_f1345_doors", {"feedback": CANARY}, "Approve the plan?",
                                       "step_human_review", (), True)
        assert masked == {"feedback": CREDENTIAL_MARK}


class TestTheNextMessageAfterACard:
    async def _turn(self, client, fake_llm, message: str, chat: str) -> tuple[str, list]:
        fake_llm.set_default(route("answer"))
        events = await stream(client, "/api/builder/chat", {"message": message, "client_session_id": chat})
        return next(e["run_id"] for e in events if e.get("run_id")), events

    async def test_a_lone_key_typed_next_is_the_cards(self, client, builder, fake_llm, tmp_path):
        first, _ = await self._turn(client, fake_llm, "connect my tracker", "f1345")
        await keep(first, None, connects=[CARD])
        calls_before = len(fake_llm.calls)
        _, events = await self._turn(client, fake_llm, CANARY, "f1345")
        assert events[0]["type"] == "builder_notice" and events[0]["reason"] == "masked", events[0]
        assert "Connect card" in events[0]["message"]
        assert CANARY not in json.dumps(fake_llm.calls[calls_before:], default=str)
        assert CANARY not in json.dumps(events, default=str)
        assert _files_holding(tmp_path, CANARY) == []

    async def test_after_a_turn_with_no_card_it_is_read_as_before(self, client, builder, fake_llm):
        await self._turn(client, fake_llm, "what can you do?", "f1345-none")
        calls_before = len(fake_llm.calls)
        _, events = await self._turn(client, fake_llm, CANARY, "f1345-none")
        assert not any(e.get("type") == "builder_notice" for e in events)
        assert CANARY in json.dumps(fake_llm.calls[calls_before:], default=str)
