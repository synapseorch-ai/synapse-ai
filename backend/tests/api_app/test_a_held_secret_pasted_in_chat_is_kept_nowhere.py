"""A secret the workspace holds, pasted into Synapse AI's chat, is kept nowhere (F1307, TRI1; CAP3's tier 1 cap-03 A3;
Naveen 11-03: build the OSS part as restated).

The person pasted their tracker's MCP token bare, on its own turn. It has no shape the scrubber knows, and the turn was
no answer at a Connect card's wait (F1280), so it reached the builder's runs, two of its work cards, its logs and blobs —
while the workspace already held that exact value, saved on the tracker's Connect card. Now the door masks any value
the workspace holds as a secret by its exact value, before anything stores, logs or shows the text: a held value of 8+
characters with a digit or a symbol, from the MCP servers, their OAuth tokens, the channels, the HTTP tools' headers,
and the settings and variables under a secret-named key. A connection's value gets the key's notice (connect it on its
card); the workspace's own gets "it is still saved where it was". A board card holding one is refused however it is
written. A finder that fails lets the message through with the shape rules alone, and logs no value.

Every secret below is assembled at run time: a literal one in a source file trips secret scanners.
"""
from __future__ import annotations

import json
import logging

import pytest

from core import secret_scrub
from core.native_builder import door, seed_native_builder
# The door's names read through the module, so on an engine before F1307 the tests fail by behaviour, not at import.
from core.native_builder.door import CREDENTIAL_MARK, MASKED_NOTICE

from test_builder_routes import route, stream, tool_call

CANARY = "tracker-" + "canary-token-" + "4410"
SETTING = "acme-" + "workspace-secret-" + "77"


@pytest.fixture
async def builder():
    await seed_native_builder()


@pytest.fixture
async def held():
    """The tracker's MCP server, saved with its token on the Connect card; a workspace setting holding a secret."""
    from core.store.resources import save_mcp_server
    from core.store.settings import save_setting

    await save_mcp_server({"name": "tracker", "label": "Tracker", "server_type": "http",
                           "url": "http://localhost:9/mcp", "token": CANARY})
    await save_setting("acme_api_key", SETTING)


#: Where the workspace keeps its secrets by design: the held store itself, never a leak.
HOLDERS = ("mcp_servers", "scale_settings")


def _files_holding(root, *needles: str) -> list[str]:
    """The logs, blobs and run events holding a needle: every file but the database, whose tables are read below."""
    return [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()
            and not p.name.endswith((".db", ".db-wal", ".db-shm", ".sqlite"))
            and any(n.encode() in p.read_bytes() for n in needles)]


async def _rows_holding(needle: str) -> list[str]:
    """Every table but the holders whose rows hold a needle: run rows, board cards, conversation turns and the rest."""
    from sqlalchemy import select

    from core.store import session
    from core.store.models import Base

    found = []
    async with session() as s:
        for table in Base.metadata.sorted_tables:
            if table.name in HOLDERS:
                continue
            rows = (await s.execute(select(table))).all()
            if any(needle in json.dumps([str(c) for c in row], ensure_ascii=False) for row in rows):
                found.append(table.name)
    return found


class TestPastedAlone:
    async def test_in_a_message_it_is_masked_and_kept_nowhere(self, client, builder, held, fake_llm, tmp_path):
        fake_llm.set_default(route("answer"))
        events = await stream(client, "/api/builder/chat", {"message": CANARY, "client_session_id": "f1307"})
        assert events[0]["type"] == "builder_notice" and events[0]["reason"] == "masked"
        assert events[0]["kept"] == CREDENTIAL_MARK and events[0]["message"] == MASKED_NOTICE
        assert CANARY not in json.dumps(events, default=str)
        assert CANARY not in json.dumps(fake_llm.calls, default=str, ensure_ascii=False), "no model saw it"
        assert _files_holding(tmp_path, CANARY) == [], "not in a log, a blob or a run's events"
        assert await _rows_holding(CANARY) == [], "not in a run row, a board card or a conversation turn"

    async def test_in_an_answer_it_is_masked(self, client, builder, held, fake_llm, tmp_path):
        fake_llm.script([route("create"), tool_call("ask_person", question="Which tracker project?", about="which")])
        events = await stream(client, "/api/builder/chat", {"message": "triage my tracker's new tickets"})
        run_id = next(e for e in events if e["type"] == "human_input_required")["run_id"]
        fake_llm.set_default(tool_call("ask_person", question="Which labels?", about="what"))
        events = await stream(client, "/api/builder/resume", {"run_id": run_id, "response": {"answer": CANARY}})
        assert events[0]["reason"] == "masked" and events[0]["kept"] == CREDENTIAL_MARK
        assert CANARY not in json.dumps(fake_llm.calls, default=str, ensure_ascii=False)
        assert _files_holding(tmp_path, CANARY) == [] and await _rows_holding(CANARY) == []

    async def test_the_workspaces_own_secret_says_it_is_still_saved(self, client, builder, held, fake_llm, tmp_path):
        fake_llm.set_default(route("answer"))
        events = await stream(client, "/api/builder/chat", {"message": f"is {SETTING} right?",
                                                            "client_session_id": "f1307b"})
        assert events[0]["reason"] == "held" and events[0]["message"] == getattr(door, "HELD_NOTICE", None)
        assert events[0]["kept"] == f"is {CREDENTIAL_MARK} right?"
        assert _files_holding(tmp_path, SETTING) == [] and await _rows_holding(SETTING) == []


class TestOnlyWhatIsHeldAndLikeASecret:
    def test_a_short_or_plain_word_value_is_never_matched(self):
        assert not secret_scrub.secret_like("ab1-x")
        assert not secret_scrub.secret_like("passwordpassword")
        assert secret_scrub.secret_like(CANARY)

    async def test_a_value_the_workspace_does_not_hold_is_left_to_the_shapes(self, client, held):
        other = "other-" + "bare-value-" + "9911"
        assert await secret_scrub.held_in(f"use {other}") == []
        assert door.check(other, await secret_scrub.held_in(other)).text == other

    async def test_only_the_values_in_the_text_come_back(self, client, held):
        found = await secret_scrub.held_in(f"the token is {CANARY}, ok")
        assert found == [(CANARY, "connection")]

    async def test_a_store_that_fails_lets_the_message_through_with_the_shapes_and_logs_no_value(
            self, client, held, monkeypatch, caplog):
        async def broken(text):
            raise RuntimeError(f"could not read {CANARY}")

        monkeypatch.setattr(secret_scrub, "_FINDER", broken)
        with caplog.at_level(logging.WARNING):
            assert await secret_scrub.held_in(CANARY) == []
        assert door.check(CANARY, []).text == CANARY, "the shape rules alone, as before F1307"
        assert CANARY not in caplog.text and "RuntimeError" in caplog.text


async def test_a_board_card_never_keeps_one(client, held):
    from core.store import boards

    board = await boards.create_shared_board("Triage", by=None)
    with pytest.raises(boards.SecretInCard):
        await boards.add_card(board.id, title="Ticket triage", body=f"Use {CANARY} for the tracker", by=None)
