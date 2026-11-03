"""What the builder writes never keeps a secret the workspace holds, and a bearer token said in words is a credential
(F1307's follow-up, TRI1; CAP3's tier 1 P1 prod-02; 79's rulings 11-03).

prod-02's person pasted a token in a sentence — "using the bearer token …", "with a bearer token (…)" — before
anything held it, so the door's held mask could not know it and no shape caught it; the builder then wrote it back into
its own plan text ("Authenticates with bearer token … in the Authorization header"), and it reached the run's rows, its
work cards, the cards' events and the builder's logs. Three answers:

* **a shape:** a value after "bearer token" or "authorization token" is a credential — never after a bare "the token"
  (a model's name, a limit);
* **the output side:** at each step's start the engine loads the workspace's held secrets (a host's loader, or this
  install's own values), and every scrub — events, logs, run errors, a checkpoint's stored copy — marks them, so a
  value held since, or one the model writes back, is never kept; a loader that fails leaves the shapes and logs no value;
* **boards mask** a held value in a card's title, body, data or comment instead of refusing the card, so the builder's
  own work card still writes, and its events are built from the masked text.

Every secret below is assembled at run time: a literal one in a source file trips secret scanners.
"""
from __future__ import annotations

import json
import logging

import pytest

from core import secret_scrub
from core.native_builder import seed_native_builder
from core.secret_scrub import CREDENTIAL_MARK, scrub_credentials

from test_a_held_secret_pasted_in_chat_is_kept_nowhere import CANARY, _files_holding, _rows_holding, held  # noqa: F401
from test_builder_routes import route, stream, tool_call

SAID = "chirp-" + "canary-token-" + "6620"


class _One:
    def __init__(self, *values):
        self.values = values

    def find(self, text):
        return [v for v in self.values if v in text]


@pytest.fixture
def holding(monkeypatch):
    """This step's matcher holds CANARY, as the engine's refresh would."""
    async def loader():
        return _One(CANARY)

    monkeypatch.setattr(secret_scrub, "_LOADER", loader, raising=False)


class TestABearerTokenSaidInWords:
    @pytest.mark.parametrize("said", [f"using the bearer token {SAID}.", f"with a bearer token ({SAID})",
                                      f"Authorization token: {SAID}", f"bearer token '{SAID}'"])
    def test_is_a_credential(self, said):
        out, n = scrub_credentials(said)
        assert n == 1 and SAID not in out and CREDENTIAL_MARK in out

    @pytest.mark.parametrize("said", ["a bearer token header", "the token is expired", "token (from the card)",
                                      "the token limit is 4096", "the token 'gpt-4.1-mini'",
                                      "bearer token for https://api.example.com/v1"])
    def test_what_only_mentions_one_is_not(self, said):
        assert scrub_credentials(said) == (said, 0)


class TestTheOutputSide:
    async def test_every_scrub_marks_a_held_value(self, holding):
        await secret_scrub.refresh_held()
        assert secret_scrub.scrub_secrets(f"plan: uses {CANARY}") == f"plan: uses {CREDENTIAL_MARK}"
        assert secret_scrub.scrub_secrets_in({"say": [f"x {CANARY}"]}) == {"say": [f"x {CREDENTIAL_MARK}"]}
        assert CANARY not in json.dumps(secret_scrub.for_storage({"text": CANARY}))
        assert secret_scrub.scrub_held((CANARY, 1)) == (CREDENTIAL_MARK, 1)

    async def test_a_loader_that_fails_leaves_the_shapes_and_logs_no_value(self, monkeypatch, caplog):
        async def broken():
            raise RuntimeError(f"no store for {CANARY}")

        monkeypatch.setattr(secret_scrub, "_LOADER", broken, raising=False)
        with caplog.at_level(logging.WARNING):
            await secret_scrub.refresh_held()
        assert not secret_scrub.holding() and secret_scrub.scrub_secrets(CANARY) == CANARY
        assert CANARY not in caplog.text and "RuntimeError" in caplog.text

    async def test_a_checkpoint_stores_the_mark_and_the_run_keeps_its_value(self, client, holding):
        from core.models_orchestration import OrchestrationRun
        from core.orchestration.state import SharedState

        await secret_scrub.refresh_held()
        run = OrchestrationRun(run_id="run_f1307_out", orchestration_id="orch_x",
                               shared_state={"plan": f"auth with {CANARY}"})
        await SharedState(run).checkpoint()
        assert run.shared_state["plan"] == f"auth with {CANARY}", "the live run keeps what it holds"
        assert await _rows_holding(CANARY) == [], "the row never does"


class TestTheBuildersPlanText:
    async def test_a_held_value_the_planner_writes_back_is_kept_nowhere(self, client, held, fake_llm, tmp_path):
        """prod-02's turn 19, held since: the planner's plan carries the token; the review, its card, the card's
        events, the run's rows, events and logs mark it."""
        await seed_native_builder()
        fake_llm.script([
            route("create"),
            tool_call("submit_plan", kind="create", title="Post to Chirp", summary=f"Authenticates with bearer "
                      f"{CANARY} in the Authorization header.", steps=[
                          {"id": "post", "type": "llm", "name": "Write the post", "does": "Writes one post",
                           "prompt": "Write a short post."}]),
        ])
        events = await stream(client, "/api/builder/chat", {"message": "make a chirp poster",
                                                            "client_session_id": "f1307-out"})
        assert CANARY not in json.dumps(events, default=str, ensure_ascii=False)
        assert _files_holding(tmp_path, CANARY) == [], "no log, blob or run-events file"
        rows = await _rows_holding(CANARY)
        assert rows == [], rows
        assert "board_cards" not in rows and "board_card_events" not in rows


async def test_a_card_masks_a_held_value_and_its_events_are_the_masked_text(client, held):
    from core.store import boards

    person = boards.Actor(person="u1")
    board = await boards.create_shared_board("Triage", by=person)
    card, added = await boards.add_card(board.id, title="Ticket triage", body=f"Use {CANARY} for the tracker",
                                        by=person)
    assert added and card.body == f"Use {CREDENTIAL_MARK} for the tracker"
    history = await boards.history(card.id)
    assert CANARY not in json.dumps([e.after for e in history], default=str)
    assert "board_card_events" not in await _rows_holding(CANARY)
