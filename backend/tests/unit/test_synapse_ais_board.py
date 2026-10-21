"""Synapse AI's own board: one card per build or change, keyed by its id, carrying its typed Plan / Change, its
stage, and the person it belongs to (SAI2-P1, F776; DESIGN.md decision 1, Naveen 10-20 and 10-21).

* It is the workspace's one `assistant`-scope board. Code writes and reads it (`core/boards/assistant.py`, called
  from workflow tool steps), never a model following a board protocol (BOARD2's lesson).
* **It works where Boards is off** — its functions never ask `boards_enabled`, as a team's run board never does —
  and it is shown nowhere else yet: the person-facing lists and reads keep it out until P3 wires the panel and the
  Boards screen's per-person view, and no agent's turn is ever given it.
* Each person sees their own cards; an admin sees every card.
* The card's typed object is checked for credentials like its body, has a size cap, and is never written into the
  card's history (so a person's undo can never put an old copy back in place of the live one).
"""
import pytest

from core.boards import assistant
from core.store import boards as store

PLAN = {"kind": "create", "title": "Weekly report", "summary": "Every Monday", "steps": []}


@pytest.fixture(autouse=True)
async def _store_ready():
    from core.store import get_store

    await get_store()
    yield


@pytest.fixture
def boards_off(monkeypatch):
    import core.config as config_mod

    real = config_mod.load_settings
    monkeypatch.setattr(config_mod, "load_settings", lambda *a, **k: {**real(*a, **k), "boards_enabled": False})


class TestACardPerBuild:
    async def test_put_makes_the_card_and_put_again_updates_it(self, boards_off):
        first = await assistant.put("bld_1", title="Weekly report", body="A plan for the weekly report.",
                                    stage="drafting", data=PLAN, owner_id="user_ana")
        again = await assistant.put("bld_1", title="Weekly report", body="Waiting for Ana's yes.", stage="waiting",
                                    data={**PLAN, "summary": "Every Monday at 9"}, owner_id="user_ana")
        assert again.id == first.id
        assert (again.stage, again.body, again.data["summary"]) == ("waiting", "Waiting for Ana's yes.",
                                                                    "Every Monday at 9")
        assert (await assistant.card("bld_1")).stage == "waiting"

    async def test_one_board_for_the_workspace(self):
        a = await assistant.board()
        b = await assistant.board()
        assert a.id == b.id and a.scope == "assistant" and a.scope_id == ""

    @pytest.mark.parametrize("stage,status,outcome", [("built", "done", "succeeded"),
                                                      ("changed", "done", "succeeded"),
                                                      ("set_aside", "done", "failed")])
    async def test_a_closing_stage_closes_the_card(self, stage, status, outcome):
        await assistant.put("bld_2", title="T", stage="waiting", data=PLAN, owner_id="u")
        card = await assistant.put("bld_2", title="T", stage=stage, data=PLAN, owner_id="u")
        assert (card.stage, card.status, card.outcome) == (stage, status, outcome)

    async def test_a_set_aside_card_comes_back_when_its_build_goes_on(self):
        await assistant.put("bld_3", title="T", stage="set_aside", data=PLAN, owner_id="u")
        card = await assistant.put("bld_3", title="T", stage="waiting", data=PLAN, owner_id="u")
        assert (card.stage, card.status, card.outcome) == ("waiting", "open", None)

    async def test_an_unknown_stage_is_refused(self):
        with pytest.raises(store.BadValue, match="stage"):
            await assistant.put("bld_4", title="T", stage="thinking", data=PLAN, owner_id="u")


class TestTheTypedObject:
    async def test_a_credential_in_it_is_refused(self):
        with pytest.raises(store.SecretInCard):
            await assistant.put("bld_5", title="T", stage="drafting", owner_id="u",
                                data={**PLAN, "summary": "use key sk-" + "proj-" + "a" * 40})

    async def test_it_has_a_size_cap(self):
        with pytest.raises(store.BadValue, match="characters"):
            await assistant.put("bld_6", title="T", stage="drafting", owner_id="u",
                                data={**PLAN, "summary": "x" * (store.DATA_MAX + 1)})

    async def test_it_is_never_in_the_cards_history(self):
        await assistant.put("bld_7", title="T", stage="drafting", data=PLAN, owner_id="u")
        card = await assistant.put("bld_7", title="T", stage="waiting", data={**PLAN, "title": "New"}, owner_id="u")
        events = await store.history(card.id, limit=10)
        assert all("data" not in (e.before or {}) and "data" not in (e.after or {}) for e in events)
        assert any((e.after or {}).get("stage") == "waiting" for e in events)


class TestWhoSeesWhat:
    async def test_each_person_their_own_and_an_admin_all(self):
        await assistant.put("bld_a", title="Ana's", stage="drafting", data=PLAN, owner_id="user_ana")
        await assistant.put("bld_b", title="Ben's", stage="waiting", data=PLAN, owner_id="user_ben")
        assert [c.key for c in await assistant.cards_for("user_ana", admin=False)] == ["bld_a"]
        assert sorted(c.key for c in await assistant.cards_for("user_ana", admin=True)) == ["bld_a", "bld_b"]
        assert await assistant.cards_for(None, admin=False) == []

    async def test_closed_cards_only_when_asked(self):
        await assistant.put("bld_c", title="Done", stage="built", data=PLAN, owner_id="u")
        assert [c.key for c in await assistant.cards_for("u", admin=False)] == []
        assert [c.key for c in await assistant.cards_for("u", admin=False, closed=True)] == ["bld_c"]


class TestItIsShownNowhereElseYet:
    async def test_the_person_facing_lists_and_reads_keep_it_out(self):
        from core.boards import service

        card = await assistant.put("bld_d", title="T", stage="drafting", data=PLAN, owner_id="u")
        listed = await service.boards_list(service.Directory(), max_shared=0)
        assert all(b["scope"] != "assistant" for b in listed["boards"])
        with pytest.raises(store.NoSuchBoard):
            await service.open_board(card.board_id, service.Directory())
        with pytest.raises(store.NoSuchBoard):
            await service.card_view(card.id)

    async def test_no_agents_turn_is_given_it(self):
        from core.boards.resolve import turn_boards

        await assistant.put("bld_e", title="T", stage="drafting", data=PLAN, owner_id="u")
        got = await turn_boards(agent={"id": "agent_native_builder", "name": "Synapse AI"},
                                agent_id="agent_native_builder", source="orchestration", run_id="builder_1",
                                session_id="s", board_scope={"kind": "workflow", "id": "orch_native_builder"},
                                settings={"boards_enabled": True}, tools_override=None, persist_turn=True,
                                visitor=None)
        assert got is None or all(b.scope != "assistant" for b in got.boards)
