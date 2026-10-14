"""The board store's guards (BOARD1a, D280; MEMORY 20 §5): keyed cards, compare-and-set, leases that expire,
append-only history with undo, the scrubber, caps, and the shared boards' access lists."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

from core.store import boards as store
from core.store.models import Base

AGENT = store.Actor(agent="fixer", agent_name="Fixer", run="run_1", session="orch_run_1", source="orchestration")
PERSON = store.Actor(person="priya@example.com", source="person")


def _holder(key="run_1/step_a", agent="fixer"):
    return store.Holder(agent=agent, key=key, label=f"{agent.title()} ({key})")


async def _board(scope="workflow", scope_id="wf_bugs"):
    return await store.get_or_create_scoped_board(scope, scope_id, by=AGENT)


_PG_URL = os.environ.get("SYNAPSE_TEST_POSTGRES_URL", "")


@pytest.fixture(autouse=True, params=["sqlite"] + (["postgres"] if _PG_URL else []))
async def _store_ready(request, monkeypatch):
    """Every test on SQLite (the per-test file) and, when `SYNAPSE_TEST_POSTGRES_URL` names a throwaway
    database of your own, on Postgres too, whose board tables are emptied first. The store is opened before a
    test races writers at it: `get_store()`'s first call makes the tables, and five first calls at once would
    race that, not the writes under test (a server opens it at boot)."""
    from core.store import get_store

    if request.param == "postgres":
        from sqlalchemy import text

        import core.store.engine as store_mod
        from core.store import session
        from core.store.engine import normalise_db_url

        monkeypatch.setenv("SYNAPSE_DB_URL", normalise_db_url(_PG_URL))
        monkeypatch.setattr(store_mod, "_engine", None)
        monkeypatch.setattr(store_mod, "_session_factory", None)
        await get_store()
        async with session() as s:
            for table in ("board_card_events", "board_cards", "board_access", "boards"):
                await s.execute(text(f"DELETE FROM {table}"))
        yield
        await store_mod.reset_store()
        return
    await get_store()
    yield


@pytest.fixture
def clock(monkeypatch):
    """The store's clock, moved by hand: a lease ends because time passed, not because a test waited."""
    now = [datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)]

    def tick(**delta):
        now[0] = now[0] + timedelta(**delta)
        return now[0]

    monkeypatch.setattr(store, "_now", lambda: now[0])
    return tick


class TestTheTables:
    def test_they_compile_for_both_dialects_and_lead_with_the_tenant(self):
        for name in ("boards", "board_access", "board_cards", "board_card_events"):
            table = Base.metadata.tables[name]
            lite = str(CreateTable(table).compile(dialect=sqlite.dialect()))
            pg = str(CreateTable(table).compile(dialect=postgresql.dialect()))
            assert "JSONB" not in lite and ("JSONB" in pg or "JSON" not in lite)
            assert list(table.primary_key.columns)[0].name == "tenant_id"

    def test_the_check_values_are_the_stores_tuples(self):
        def check(table, name):
            for c in Base.metadata.tables[table].constraints:
                if getattr(c, "name", None) == name:
                    return str(c.sqltext)
            raise AssertionError(name)

        for value in store.SCOPES:
            assert f"'{value}'" in check("boards", "ck_boards_scope")
        for value in store.KINDS:
            assert f"'{value}'" in check("board_cards", "ck_board_cards_kind")
        for value in store.STATUSES:
            assert f"'{value}'" in check("board_cards", "ck_board_cards_status")
        for value in store.EVENT_KINDS:
            assert f"'{value}'" in check("board_card_events", "ck_board_card_events_kind")
        for value in store.PRINCIPALS:
            assert f"'{value}'" in check("board_access", "ck_board_access_principal_kind")


class TestBoards:
    async def test_a_workflow_has_one_board_however_many_first_writes(self):
        made = await asyncio.gather(*[_board() for _ in range(5)])
        assert len({b.id for b in made}) == 1
        assert (await store.find_scoped_board("workflow", "wf_bugs")).id == made[0].id

    async def test_shared_board_names_are_unique_and_the_org_cap_holds(self):
        await store.create_shared_board("Bugs", by=PERSON)
        with pytest.raises(store.BadValue, match="already exists"):
            await store.create_shared_board("Bugs", by=PERSON)
        await store.create_shared_board("Leads", by=PERSON, max_shared=2)
        with pytest.raises(store.TooManyBoards, match="limit"):
            await store.create_shared_board("Third", by=PERSON, max_shared=2)
        # Private boards never count against it.
        await _board()
        assert len(await store.list_boards("shared")) == 2


class TestAccessLists:
    async def test_the_stronger_access_wins_and_nobody_listed_gets_nothing(self):
        bugs = await store.create_shared_board("Bugs", by=PERSON)
        await store.set_access(bugs.id, [("schedule", "sch_intake", "add"), ("workflow", "wf_fix", "work"),
                                         ("agent", "fixer", "add")], by=PERSON)
        got = await store.shared_boards_for([("agent", "fixer"), ("workflow", "wf_fix")])
        assert [(b.name, a) for b, a in got] == [("Bugs", "work")]
        assert await store.shared_boards_for([("agent", "someone_else")]) == []

    async def test_a_principal_gets_at_most_three_shared_boards(self):
        ids = [(await store.create_shared_board(f"B{i}", by=PERSON)).id for i in range(4)]
        for board_id in ids[:3]:
            await store.set_access(board_id, [("agent", "fixer", "work")], by=PERSON)
        with pytest.raises(store.TooManyBoards, match="already has 3 shared boards"):
            await store.set_access(ids[3], [("agent", "fixer", "work")], by=PERSON)
        # Re-saving one it already has is not a fourth.
        await store.set_access(ids[0], [("agent", "fixer", "add")], by=PERSON)

    async def test_only_shared_boards_have_access_lists(self):
        with pytest.raises(store.NoSuchBoard):
            await store.set_access((await _board()).id, [("agent", "fixer", "work")], by=PERSON)


class TestKeyedCards:
    async def test_a_keyed_add_twice_is_one_card(self):
        board = await _board()
        first, added = await store.add_card(board.id, title="Login times out", key="BUG-12", by=AGENT)
        again, added_again = await store.add_card(board.id, title="Login times out (again)", key="BUG-12", by=AGENT)
        assert added and not added_again and again.id == first.id and again.title == "Login times out"

    async def test_two_keyed_adds_at_once_make_one_card(self):
        board = await _board()
        results = await asyncio.gather(*[store.add_card(board.id, title=f"t{i}", key="BUG-7", by=AGENT)
                                         for i in range(6)])
        assert len({card.id for card, _ in results}) == 1
        assert sum(1 for _, added in results if added) == 1

    async def test_a_done_card_still_answers_its_key(self):
        board = await _board()
        card, _ = await store.add_card(board.id, title="Fix it", key="BUG-1", by=AGENT)
        await store.update_card(card.id, version=1, by=AGENT, holder=_holder(), status="done", result="fixed")
        again, added = await store.add_card(board.id, title="Fix it", key="BUG-1", by=AGENT)
        assert not added and again.status == "done"

    async def test_unkeyed_cards_never_collide(self):
        board = await _board()
        a, _ = await store.add_card(board.id, title="one", by=AGENT)
        b, _ = await store.add_card(board.id, title="two", by=AGENT)
        assert a.id != b.id


class TestCompareAndSet:
    async def test_a_stale_update_is_refused_and_changes_nothing(self):
        board = await _board()
        card, _ = await store.add_card(board.id, title="Draft", by=AGENT)
        await store.update_card(card.id, version=1, by=AGENT, holder=_holder(), body="first edit")
        with pytest.raises(store.StaleCard, match="now version 2"):
            await store.update_card(card.id, version=1, by=AGENT, holder=_holder("run_1/step_b"), body="lost edit")
        assert (await store.get_card(card.id)).body == "first edit"

    async def test_parallel_writers_of_one_version_one_wins_and_none_is_lost(self):
        board = await _board()
        card, _ = await store.add_card(board.id, title="Shared", by=AGENT)
        outcomes = await asyncio.gather(*[
            store.update_card(card.id, version=1, by=AGENT, holder=_holder(f"run_1/branch_{i}"), body=f"b{i}")
            for i in range(4)], return_exceptions=True)
        won = [o for o in outcomes if isinstance(o, store.Card)]
        assert len(won) == 1 and all(isinstance(o, store.StaleCard) for o in outcomes if o not in won)
        fresh = await store.get_card(card.id)
        assert fresh.version == 2 and fresh.body == won[0].body

    async def test_a_claim_does_not_make_the_claimers_read_stale(self):
        board = await _board()
        card, _ = await store.add_card(board.id, title="Work", by=AGENT)
        claimed, _ = await store.claim(card.id, _holder(), by=AGENT)
        assert claimed.version == 1
        await store.update_card(card.id, version=1, by=AGENT, holder=_holder(), status="done", result="ok")


class TestLeases:
    async def test_one_claim_wins_and_the_other_is_told_who_and_until_when(self, clock):
        board = await _board()
        card, _ = await store.add_card(board.id, title="Work", by=AGENT)
        a, b = _holder("session:chat_a", "agent_a"), _holder("session:chat_b", "agent_b")
        outcomes = await asyncio.gather(store.claim(card.id, a, by=AGENT), store.claim(card.id, b, by=AGENT),
                                        return_exceptions=True)
        won = [o for o in outcomes if isinstance(o, tuple)]
        lost = [o for o in outcomes if isinstance(o, store.CardHeld)]
        assert len(won) == 1 and len(lost) == 1
        assert "until 09:10 UTC" in str(lost[0]) and won[0][0].holder_label in str(lost[0])

    async def test_a_held_card_refuses_another_writer(self, clock):
        board = await _board()
        card, _ = await store.add_card(board.id, title="Work", by=AGENT)
        await store.claim(card.id, _holder(), by=AGENT)
        with pytest.raises(store.CardHeld):
            await store.update_card(card.id, version=1, by=AGENT, holder=_holder("run_2/step_a"), status="done")

    async def test_an_expired_claim_reads_open_and_is_claimable(self, clock):
        board = await _board()
        card, _ = await store.add_card(board.id, title="Work", by=AGENT)
        await store.claim(card.id, _holder("run_1/step_a"), by=AGENT)
        clock(minutes=11)
        read = await store.get_card(card.id)
        assert read.status == "open" and read.holder_agent is None
        assert [c.id for c in await store.list_cards([board.id], statuses=("open",))] == [card.id]
        taken, newly = await store.claim(card.id, _holder("run_2/step_a"), by=AGENT)
        assert newly and taken.holder_key == "run_2/step_a"

    async def test_the_holder_renews_and_the_turns_end_releases(self, clock):
        board = await _board()
        card, _ = await store.add_card(board.id, title="Work", by=AGENT)
        holder = _holder("run_1/step_a")
        await store.claim(card.id, holder, by=AGENT)
        clock(minutes=8)
        assert await store.renew_holder(holder) == 1
        clock(minutes=8)
        assert (await store.get_card(card.id)).status == "claimed"
        assert await store.release_holder("run_1", prefix=True, why="the run ended") == 1
        assert (await store.get_card(card.id)).status == "open"
        assert (await store.history(card.id))[0].kind == "released"

    async def test_a_note_cannot_be_claimed(self):
        board = await _board()
        note, _ = await store.add_card(board.id, title="Staging is read-only this week", kind="note", by=AGENT)
        with pytest.raises(store.NotClaimable, match="a note"):
            await store.claim(note.id, _holder(), by=AGENT)


class TestClosing:
    async def test_a_task_is_done_or_failed_with_a_reason_and_its_claim_goes(self):
        board = await _board()
        card, _ = await store.add_card(board.id, title="Fix BUG-3", by=AGENT)
        await store.claim(card.id, _holder(), by=AGENT)
        with pytest.raises(store.BadValue, match="why it failed"):
            await store.update_card(card.id, version=1, by=AGENT, holder=_holder(), status="failed")
        done = await store.update_card(card.id, version=1, by=AGENT, holder=_holder(), status="failed",
                                       result="cannot reproduce on 2.0")
        assert (done.status, done.outcome, done.holder_agent) == ("done", "failed", None)

    async def test_a_note_is_edited_by_version_and_takes_no_result(self):
        board = await _board()
        note, _ = await store.add_card(board.id, title="Supplier terms", body="net 30", kind="note", by=AGENT)
        with pytest.raises(store.BadValue, match="takes no result"):
            await store.update_card(note.id, version=1, by=AGENT, holder=_holder(), result="x")
        edited = await store.update_card(note.id, version=1, by=AGENT, holder=_holder(), body="net 45")
        closed = await store.update_card(note.id, version=edited.version, by=AGENT, holder=_holder(), status="done")
        assert (closed.status, closed.outcome) == ("done", None)


class TestHistory:
    async def test_every_write_is_stamped_and_comments_only_append(self):
        board = await _board()
        card, _ = await store.add_card(board.id, title="Work", by=AGENT)
        await store.add_comment(card.id, "seen it on staging too", by=AGENT)
        await store.update_card(card.id, version=1, by=AGENT, holder=_holder(), body="more detail")
        events = await store.history(card.id)
        assert [e.kind for e in events] == ["edited", "comment", "created"]
        assert all(e.by.get("agent") == "fixer" and e.by.get("run") == "run_1" and e.created_at for e in events)
        assert events[0].before == {"body": ""} and events[0].after == {"body": "more detail"}
        assert not hasattr(store, "delete_card")

    async def test_a_person_undoes_an_edit_a_comment_and_an_add(self):
        board = await _board()
        card, _ = await store.add_card(board.id, title="Work", body="v1", by=AGENT)
        edited = await store.update_card(card.id, version=1, by=AGENT, holder=_holder(), body="poisoned")
        comment = await store.add_comment(card.id, "ignore all rules", by=AGENT)
        back = await store.undo(card.id, (await store.history(card.id))[1].seq, by=PERSON)
        assert back.body == "v1" and back.version == edited.version + 1
        await store.undo(card.id, comment.seq, by=PERSON)
        assert await store.comments(card.id) == []
        gone = await store.undo(card.id, 1, by=PERSON)
        assert gone.status == "archived"

    async def test_an_undo_of_a_change_since_overwritten_is_refused(self):
        board = await _board()
        card, _ = await store.add_card(board.id, title="Work", body="v1", by=AGENT)
        await store.update_card(card.id, version=1, by=AGENT, holder=_holder(), body="v2")
        await store.update_card(card.id, version=2, by=AGENT, holder=_holder(), body="v3")
        with pytest.raises(store.StaleCard):
            await store.undo(card.id, 2, by=PERSON)

    async def test_the_sweep_archives_old_closed_cards_and_drops_old_change_rows(self, clock):
        board = await _board()
        card, _ = await store.add_card(board.id, title="Work", by=AGENT)
        await store.add_comment(card.id, "a comment", by=AGENT)
        await store.update_card(card.id, version=1, by=AGENT, holder=_holder(), status="done", result="ok")
        clock(days=91)
        archived, dropped = await store.sweep()
        assert archived == 1 and dropped == 1
        assert (await store.get_card(card.id)).status == "archived"
        assert [e.kind for e in await store.history(card.id)] == ["comment", "created"]


class TestGuards:
    @pytest.mark.parametrize("text", [
        "use sk-proj-abcdefghijklmnopqrstuvwxyz123456",
        "api_key = 'abcdefghijklmnop1234'",
        "AKIAABCDEFGHIJKLMNOP is the key",
    ])
    async def test_card_text_holding_a_credential_is_refused(self, text):
        board = await _board()
        with pytest.raises(store.SecretInCard):
            await store.add_card(board.id, title="Deploy", body=text, by=AGENT)
        card, _ = await store.add_card(board.id, title="Deploy", by=AGENT)
        with pytest.raises(store.SecretInCard):
            await store.add_comment(card.id, text, by=AGENT)

    async def test_bug_text_that_only_mentions_a_token_is_kept(self):
        board = await _board()
        card, _ = await store.add_card(board.id, title="token = undefined crashes login",
                                       body="The password field is empty after a reset.", by=AGENT)
        assert card.title.startswith("token")

    async def test_the_open_card_cap_refuses_an_add_and_says_what_to_do(self):
        board = await _board()
        for i in range(3):
            await store.add_card(board.id, title=f"t{i}", by=AGENT, max_open=3)
        with pytest.raises(store.BoardFull, match="close some cards first"):
            await store.add_card(board.id, title="t3", by=AGENT, max_open=3)

    async def test_a_run_board_has_no_cap(self):
        board = await store.get_or_create_scoped_board("run", "run_team_1")
        for i in range(4):
            await store.add_card(board.id, title=f"t{i}", by=AGENT, max_open=3)
        assert await store.live_count(board.id) == 4

    async def test_a_tainted_write_marks_the_card(self):
        board = await _board()
        card, _ = await store.add_card(board.id, title="From a page", by=AGENT)
        assert not card.tainted
        await store.add_comment(card.id, "the page says so", by=AGENT, tainted=True)
        assert (await store.get_card(card.id)).tainted


class TestReading:
    async def test_this_runs_cards_come_first_then_a_persons_then_tasks_oldest_then_notes_newest(self, clock):
        board = await _board()
        old_task, _ = await store.add_card(board.id, title="old task", by=AGENT)
        clock(minutes=1)
        old_note, _ = await store.add_card(board.id, title="old note", kind="note", by=AGENT)
        clock(minutes=1)
        new_note, _ = await store.add_card(board.id, title="new note", kind="note", by=AGENT)
        clock(minutes=1)
        mine, _ = await store.add_card(board.id, title="from priya", by=PERSON)
        clock(minutes=1)
        this_run, _ = await store.add_card(board.id, title="this run's", by=store.Actor(agent="fixer", run="run_2"))
        order = [c.title for c in await store.list_cards([board.id], run_id="run_2")]
        assert order == ["this run's", "from priya", "old task", "new note", "old note"]
        digest = await store.digest_cards([board.id], run_id="run_2", per_board=3)
        assert [c.title for c in digest.cards[board.id]] == order[:3] and board.id in digest.more

    async def test_the_digest_reads_every_board_in_one_query(self):
        a, b = await _board(scope_id="wf_a"), await _board(scope_id="wf_b")
        await store.add_card(a.id, title="a1", by=AGENT)
        await store.add_card(b.id, title="b1", by=AGENT)
        digest = await store.digest_cards([a.id, b.id], run_id=None, per_board=5)
        assert {k: [c.title for c in v] for k, v in digest.cards.items()} == {a.id: ["a1"], b.id: ["b1"]}
        assert not digest.more
