"""A key pasted at the Connect card's wait is kept nowhere (F1280; Naveen 10-31: the recommended rule, kept small).

CAP3's pass 2 (prod-13): the Connect card failed, and the simulated person answered the wait with the key alone,
`tidewell-…-2284`, five times. It has no shape the scrubber knows (Naveen 10-15: shapes only) and the wait's words never
say "key", so the door let it through: it reached the run's row, its events file and its logs. prod-02: a key typed with
U+2011 hyphens went past every pattern, and the planner wrote it back out in ASCII to its own log.

So the door folds lookalike dashes and zero-width characters while it reads (since F1551 it keeps the message as
written, each key masked where it stands), and at a wait for a Connect card — the
run's own state, never a guess from words — a lone key-like answer is masked as a key, and the person is told it goes on
the card. Anywhere else a lone word is read as before ("gpt-4.1-mini" to "which model?" stays). The same in rc5.3's
vendor: its door is this one.
"""
from __future__ import annotations

import json

import pytest

from core.native_builder import door, seed_native_builder
from core.native_builder.door import CREDENTIAL_MARK

from test_builder_routes import route, stream, tool_call

# Assembled: a literal key in a source file trips secret scanners.
CANARY = "tidewell-" + "canary-key-" + "2284"
STRIPE = "sk_" + "test_" + "Z" * 24


@pytest.fixture
async def builder():
    await seed_native_builder()


def _files_holding(root, *needles: str) -> list[str]:
    return [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()
            and any(n.encode() in p.read_bytes() for n in needles)]


class TestTheDoorAtTheConnectWait:
    @pytest.mark.parametrize("said", [CANARY, f"  `{CANARY}`  ", f"{CANARY}."])
    def test_a_lone_key_like_answer_is_masked_there(self, said):
        assert door.check_answer({"feedback": said}, "Connect Tidewell on the card above.", step="step_wait_connect") == (
            {"feedback": CREDENTIAL_MARK}, 1)

    @pytest.mark.parametrize("said", ["stop", "done", "build without Tidewell", "I connected it, carry on",
                                      "https://tidewell.example/settings"])
    def test_its_own_words_are_still_read(self, said):
        assert door.check_answer({"feedback": said}, "Connect Tidewell on the card above.", step="step_wait_connect") == (
            {"feedback": said}, 0)

    def test_elsewhere_a_lone_word_is_read_as_before(self):
        assert door.check_answer({"answer": "gpt-4.1-mini"}, "Which model should it use?") == (
            {"answer": "gpt-4.1-mini"}, 0)
        assert door.check_answer({"answer": CANARY}, "Which shop is it?", step="step_human_clarify") == (
            {"answer": CANARY}, 0)

    def test_every_way_an_answer_reaches_a_builder_run_reads_the_wait(self):
        from core.orchestration.answers import _through_door

        assert _through_door("builder_x", {"feedback": CANARY}, "Connect it on the card.", "step_wait_connect") == {
            "feedback": CREDENTIAL_MARK}
        assert _through_door("run_other", {"feedback": CANARY}, None, "step_wait_connect") == {"feedback": CANARY}


class TestLookalikesAreFolded:
    @pytest.mark.parametrize("dash", ["‑", "‐", "–", "−", "－"])
    def test_a_known_key_with_lookalike_dashes_is_masked(self, dash):
        key = "sk" + dash + "proj" + dash + "A1b2C3d4E5f6G7h8I9j0K1"
        checked = door.check(f"use {key} please")
        assert checked.removed == 1 and checked.text == f"use {CREDENTIAL_MARK} please"

    def test_a_zero_width_character_inside_a_key_hides_nothing(self):
        key = STRIPE[:6] + "​" + STRIPE[6:]
        assert door.check(f"key: {key}").removed == 1

    def test_at_the_wait_the_lookalike_form_is_masked_too(self):
        said = CANARY.replace("-", "‑")
        assert door.check_answer({"feedback": said}, None, step="step_wait_connect") == ({"feedback": CREDENTIAL_MARK}, 1)

    def test_folding_is_for_reading_and_the_message_is_kept_as_written(self):
        """F1551 (Naveen 11-12): the door reads through the fold, and what runs is what the person wrote."""
        assert door.check("Ship it Monday – not Friday").text == "Ship it Monday – not Friday"


class TestTheResumeRoute:
    async def test_the_wait_comes_from_the_runs_own_row(self, client, builder, fake_llm):
        from core.native_builder.door import CONNECT_WAITS
        from core.routes.builder import _pending_question

        fake_llm.script([route("create"), tool_call("ask_person", question="Which shop is it?", about="which")])
        events = await stream(client, "/api/builder/chat", {"message": "invoice the shop's orders"})
        run_id = next(e for e in events if e["type"] == "human_input_required")["run_id"]
        question, step = await _pending_question(run_id)
        assert question and step and step not in CONNECT_WAITS
        assert await _pending_question("builder_none") == (None, None)
        assert CONNECT_WAITS == {"step_wait_connect"}

    async def test_a_key_answered_at_the_wait_reaches_nothing_past_the_door(
            self, client, builder, fake_llm, monkeypatch, tmp_path):
        from core.orchestration.engine import OrchestrationEngine
        from core.routes import builder as routes

        async def at_the_wait(run_id):
            return "Tidewell is not connected yet: connect it on the card above.", "step_wait_connect"

        got: list = []

        async def resume(run_id, response, *args, **kwargs):
            got.append(response)
            yield {"type": "human_input_required", "run_id": run_id, "prompt": "again", "fields": []}

        monkeypatch.setattr(routes, "_pending_question", at_the_wait)
        monkeypatch.setattr(OrchestrationEngine, "resume", staticmethod(resume))
        events = await stream(client, "/api/builder/resume",
                              {"run_id": "builder_f1280", "response": {"feedback": CANARY}})
        assert events[0]["type"] == "builder_notice" and events[0]["reason"] == "masked"
        assert events[0]["kept"] == CREDENTIAL_MARK and "Connect card" in events[0]["message"]
        assert got == [{"feedback": CREDENTIAL_MARK}], "the run is resumed with the key taken out"
        assert CANARY not in json.dumps(events, default=str)
        assert _files_holding(tmp_path, CANARY) == []

    async def test_a_lookalike_key_in_a_message_is_kept_nowhere(self, client, builder, fake_llm, tmp_path):
        fake_llm.set_default(route("answer"))
        key = "sk‑proj‑" + "A1b2C3d4E5f6G7h8I9j0K1"
        events = await stream(client, "/api/builder/chat",
                              {"message": f"Here is my key {key}, which apps do I have?", "client_session_id": "f1280"})
        assert events[0]["reason"] == "masked"
        ascii_ = key.replace("‑", "-")
        seen = json.dumps(fake_llm.calls, default=str, ensure_ascii=False)
        for form in (key, ascii_, "A1b2C3d4E5f6G7h8I9j0K1"):
            assert form not in json.dumps(events, default=str, ensure_ascii=False) and form not in seen
        assert _files_holding(tmp_path, key, ascii_, "A1b2C3d4E5f6G7h8I9j0K1") == []


def test_the_card_says_the_key_goes_there_never_in_the_chat():
    from pathlib import Path

    card = (Path(__file__).resolve().parents[3] / "frontend/src/components/assistant/ConnectCard.tsx").read_text()
    assert "Paste it here, never in the chat" in card
