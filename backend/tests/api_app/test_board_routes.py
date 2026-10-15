"""Boards' routes for people (BOARD1b, D282; MEMORY 20 §5, C3/C4/C6/C7/C8).

Off means 404 on every route and no store query; on, a person's card is a person's (first in reading order,
amber on screen), a change names the version it read (a stale one is refused), any change can be taken back
from the card's history, caps refuse in the store's words, and a workflow's or a schedule's board is read
without being made.
"""
from __future__ import annotations

import re

import pytest

from _fakes import seed as S
from core.store import boards as store

AGENT = store.Actor(agent="desk", agent_name="Order desk", run="run_1", session="s1", source="orchestration")
HOLD = store.Holder(agent="desk", key="run_1/w", label="Order desk (workflow run run_1, step w)")


@pytest.fixture
async def on(client):
    assert (await client.post("/api/settings", json={"boards_enabled": True})).status_code == 200
    return client


@pytest.fixture
async def world():
    from core.store import schedules

    await S.seed_agents([S.make_agent(id="desk", name="Order desk"), S.make_agent(id="clerk", name="Invoice clerk")])
    await S.seed_orchestrations([S.make_orchestration(id="wf_review", name="Monday supplier review"),
                                 S.make_orchestration(id="wf_triage", name="Invoice triage")])
    await schedules.save({"id": "sch_daily", "name": "Daily stockist check", "target_type": "agent",
                          "target_id": "desk", "prompt": "check", "enabled": False})
    await schedules.save({"id": "sch_wf", "name": "Monday 9am", "target_type": "orchestration",
                          "target_id": "wf_review", "prompt": "go", "enabled": False})


def _routes():
    from core.routes.boards import router

    for route in router.routes:
        for method in route.methods:
            yield method, route.path


def _fill(path: str) -> str:
    path = path.replace("{kind}", "workflow")
    return re.sub(r"\{[^}]+\}", "x", path)


async def test_off_every_route_is_a_404_and_reads_nothing(client):
    from core.store import get_store
    from tests.unit.test_boards_engine import board_sql

    await get_store()
    seen = list(_routes())
    assert len(seen) >= 14
    with board_sql() as sql:
        for method, path in seen:
            resp = await client.request(method, _fill(path), json={})
            assert resp.status_code == 404, (method, path, resp.text)
    assert sql == []


async def test_every_route_is_mounted(client):
    paths = (await client.get("/openapi.json")).json()["paths"]
    for method, path in _routes():
        assert method.lower() in paths.get(path, {}), (method, path)


class TestTheBoardsList:
    async def test_it_names_each_board_and_counts_its_cards(self, on, world):
        shared = await store.create_shared_board("Bug intake")
        wf = await store.get_or_create_scoped_board("workflow", "wf_review")
        await store.get_or_create_scoped_board("run", "team:t1")
        card, _ = await store.add_card(wf.id, title="Corda back-orders", by=AGENT)
        await store.add_card(wf.id, title="Fenwick lead times", by=AGENT)
        await store.claim(card.id, HOLD, by=AGENT)
        body = (await on.get("/api/boards")).json()
        rows = {b["id"]: b for b in body["boards"]}
        assert set(rows) == {shared.id, wf.id}   # a team's run board is nobody's to open
        assert rows[wf.id]["name"] == "Monday supplier review"
        assert (rows[wf.id]["open"], rows[wf.id]["claimed"]) == (1, 1)
        assert body["shared_used"] == 1 and body["shared_limit"] is None

    async def test_a_deleted_workflows_board_says_so_and_can_go(self, on, world):
        from core.store.resources import delete_orchestration

        wf = await store.get_or_create_scoped_board("workflow", "wf_triage")
        refused = await on.delete(f"/api/boards/{wf.id}")
        assert refused.status_code == 409 and "close the cards" in refused.json()["detail"]
        await delete_orchestration("wf_triage")
        row = {b["id"]: b for b in (await on.get("/api/boards")).json()["boards"]}[wf.id]
        assert row["owner_gone"] is True
        assert (await on.delete(f"/api/boards/{wf.id}")).status_code == 204
        assert await store.get_board(wf.id) is None


class TestAPersonsCard:
    async def test_it_is_a_persons_and_read_first(self, on, world):
        wf = await store.get_or_create_scoped_board("workflow", "wf_review")
        await store.add_card(wf.id, title="Agent's task", by=AGENT)
        resp = await on.post(f"/api/boards/{wf.id}/cards", json={"title": "Skip Harbor Test Supplies"})
        assert resp.status_code == 201, resp.text
        card = resp.json()["card"]
        assert card["by_person"] is True and card["created_by"]["person"] == "You" and card["tainted"] is False
        cards = (await on.get(f"/api/boards/{wf.id}")).json()["cards"]
        assert [c["title"] for c in cards] == ["Skip Harbor Test Supplies", "Agent's task"]

    async def test_the_personal_details_name_signs_it(self, on, world):
        from core.personal_details import save_personal_details

        await save_personal_details({"first_name": "Priya", "last_name": "Raman"})
        board = await store.create_shared_board("Bugs")
        card = (await on.post(f"/api/boards/{board.id}/cards", json={"title": "Login fails"})).json()["card"]
        assert card["created_by"]["person"] == "Priya Raman"

    async def test_a_key_already_on_the_board_returns_that_card(self, on, world):
        board = await store.create_shared_board("Bugs")
        first = (await on.post(f"/api/boards/{board.id}/cards", json={"title": "Bug 1", "key": "BUG-1"})).json()
        again = (await on.post(f"/api/boards/{board.id}/cards", json={"title": "Bug 1 again", "key": "BUG-1"}))
        assert again.json()["added"] is False and again.json()["card"]["id"] == first["card"]["id"]

    async def test_a_secret_is_refused_in_the_stores_words(self, on, world):
        board = await store.create_shared_board("Bugs")
        resp = await on.post(f"/api/boards/{board.id}/cards",
                             json={"title": "key", "body": "use " + "sk-" + "proj-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3"})
        assert resp.status_code == 422 and "secret" in resp.json()["detail"]


class TestChangingACard:
    async def test_a_stale_version_is_refused(self, on, world):
        board = await store.create_shared_board("Bugs")
        card, _ = await store.add_card(board.id, title="Login fails", by=AGENT)
        await store.update_card(card.id, version=card.version, by=AGENT, holder=None, body="seen on Safari")
        resp = await on.patch(f"/api/boards/cards/{card.id}", json={"version": card.version, "title": "Login"})
        assert resp.status_code == 409 and "changed since you read it" in resp.json()["detail"]

    async def test_a_claim_does_not_hold_a_person_off(self, on, world):
        board = await store.create_shared_board("Bugs")
        card, _ = await store.add_card(board.id, title="Login fails", by=AGENT)
        held, _ = await store.claim(card.id, HOLD, by=AGENT)
        resp = await on.patch(f"/api/boards/cards/{card.id}",
                              json={"version": held.version, "status": "done", "result": "Fixed by hand"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "done" and resp.json()["outcome"] == "succeeded"

    async def test_a_failed_task_needs_its_reason(self, on, world):
        board = await store.create_shared_board("Bugs")
        card, _ = await store.add_card(board.id, title="Login fails", by=AGENT)
        resp = await on.patch(f"/api/boards/cards/{card.id}", json={"version": card.version, "status": "failed"})
        assert resp.status_code == 422

    async def test_a_comment_is_kept_on_the_card(self, on, world):
        board = await store.create_shared_board("Bugs")
        card, _ = await store.add_card(board.id, title="Login fails", by=AGENT)
        assert (await on.post(f"/api/boards/cards/{card.id}/comments", json={"text": "Also on Edge"})).status_code == 201
        view = (await on.get(f"/api/boards/cards/{card.id}")).json()
        assert [c["comment"] for c in view["comments"]] == ["Also on Edge"]


class TestUndo:
    async def test_a_person_takes_back_an_agents_edit(self, on, world):
        board = await store.create_shared_board("Bugs")
        card, _ = await store.add_card(board.id, title="Login fails", by=AGENT)
        await store.update_card(card.id, version=card.version, by=AGENT, holder=None, title="Nothing to do")
        view = (await on.get(f"/api/boards/cards/{card.id}")).json()
        edit = next(e for e in view["history"] if e["kind"] == "edited")
        assert edit["undoable"] is True
        resp = await on.post(f"/api/boards/cards/{card.id}/undo", json={"seq": edit["seq"]})
        assert resp.status_code == 200, resp.text
        assert resp.json()["title"] == "Login fails"
        again = (await on.get(f"/api/boards/cards/{card.id}")).json()
        assert next(e for e in again["history"] if e["seq"] == edit["seq"])["undoable"] is False
        twice = await on.post(f"/api/boards/cards/{card.id}/undo", json={"seq": edit["seq"]})
        assert twice.status_code == 409

    async def test_undoing_an_add_archives_the_card(self, on, world):
        board = await store.create_shared_board("Bugs")
        card, _ = await store.add_card(board.id, title="Spam", by=AGENT)
        created = (await store.history(card.id))[-1]
        assert (await on.post(f"/api/boards/cards/{card.id}/undo", json={"seq": created.seq})).json()["status"] \
            == "archived"
        found = (await on.get(f"/api/boards/{board.id}", params={"q": "spam"})).json()
        assert [c["id"] for c in found["archived"]] == [card.id] and found["cards"] == []


class TestCaps:
    async def test_shared_boards_stop_at_the_workspaces_cap(self, on, world):
        await on.post("/api/settings", json={"boards_max_shared": 1})
        assert (await on.post("/api/boards", json={"name": "Bugs"})).status_code == 201
        resp = await on.post("/api/boards", json={"name": "Suppliers"})
        assert resp.status_code == 409 and "its limit" in resp.json()["detail"]
        assert (await on.get("/api/boards")).json()["shared_limit"] == 1

    async def test_open_cards_stop_at_the_boards_cap(self, on, world):
        await on.post("/api/settings", json={"boards_max_open_cards": 1})
        board = await store.create_shared_board("Bugs")
        assert (await on.post(f"/api/boards/{board.id}/cards", json={"title": "one"})).status_code == 201
        resp = await on.post(f"/api/boards/{board.id}/cards", json={"title": "two"})
        assert resp.status_code == 409 and "close some cards first" in resp.json()["detail"]


class TestAccessLists:
    async def test_a_list_is_saved_with_names_and_read_back_for_the_agent(self, on, world):
        board = (await on.post("/api/boards", json={"name": "Bugs"})).json()
        resp = await on.put(f"/api/boards/{board['id']}/access", json={"entries": [
            {"kind": "agent", "id": "desk", "access": "work"},
            {"kind": "schedule", "id": "sch_daily", "access": "add"}]})
        assert resp.status_code == 200, resp.text
        assert {(a["name"], a["access"]) for a in resp.json()["access"]} == {("Order desk", "work"),
                                                                             ("Daily stockist check", "add")}
        mine = (await on.get("/api/boards/for/agent/desk")).json()["boards"]
        assert [(b["name"], b["access"]) for b in mine] == [("Bugs", "work")]

    async def test_an_unknown_principal_is_refused(self, on, world):
        board = (await on.post("/api/boards", json={"name": "Bugs"})).json()
        resp = await on.put(f"/api/boards/{board['id']}/access",
                            json={"entries": [{"kind": "agent", "id": "ghost", "access": "work"}]})
        assert resp.status_code == 422 and "no agent ghost" in resp.json()["detail"]

    async def test_a_fourth_shared_board_for_one_agent_is_refused(self, on, world):
        for name in ("A", "B", "C"):
            board = await store.create_shared_board(name)
            await store.set_access(board.id, [("agent", "desk", "work")])
        fourth = await store.create_shared_board("D")
        resp = await on.put(f"/api/boards/{fourth.id}/access",
                            json={"entries": [{"kind": "agent", "id": "desk", "access": "add"}]})
        assert resp.status_code == 409 and "3 shared boards" in resp.json()["detail"]


class TestScopedBoards:
    async def test_a_workflows_board_is_read_without_being_made(self, on, world):
        body = (await on.get("/api/boards/scoped/workflow/wf_review")).json()
        assert body["board"]["id"] is None and body["board"]["name"] == "Monday supplier review"
        assert await store.find_scoped_board("workflow", "wf_review") is None

    async def test_a_persons_first_card_makes_it(self, on, world):
        resp = await on.post("/api/boards/scoped/schedule/sch_daily/cards", json={"title": "Skip Monday"})
        assert resp.status_code == 201, resp.text
        board = await store.find_scoped_board("schedule", "sch_daily")
        assert board is not None and resp.json()["card"]["board_id"] == board.id

    async def test_a_schedule_that_runs_a_workflow_has_no_board_of_its_own(self, on, world):
        resp = await on.get("/api/boards/scoped/schedule/sch_wf")
        assert resp.status_code == 409 and "workflow's board" in resp.json()["detail"]

    async def test_an_unknown_workflow_is_a_404(self, on, world):
        assert (await on.get("/api/boards/scoped/workflow/wf_nope")).status_code == 404
        assert (await on.post("/api/boards/scoped/workflow/wf_nope/cards", json={"title": "x"})).status_code == 404

    async def test_this_run_comes_first_and_can_be_shown_alone(self, on, world):
        from datetime import datetime, timedelta, timezone

        from core.store import get_store
        from core.store.models import OrchestrationRunDB

        wf = await store.get_or_create_scoped_board("workflow", "wf_review")
        old = store.Actor(agent="desk", run="run_old", source="orchestration")
        now = store.Actor(agent="desk", run="run_now", source="orchestration")
        await store.add_card(wf.id, title="from last week", by=old)
        await store.add_card(wf.id, title="from this run", by=now)
        now_at = datetime.now(timezone.utc)
        async with (await get_store())() as s:
            for run_id, at in (("run_old", now_at - timedelta(days=7)), ("run_now", now_at)):
                s.add(OrchestrationRunDB(run_id=run_id, orchestration_id="wf_review", status="running",
                                         shared_state={}, step_history=[], created_at=at, started_at=at))
            await s.commit()
        body = (await on.get("/api/boards/scoped/workflow/wf_review")).json()
        assert body["current_run"]["run_id"] == "run_now"
        assert [c["title"] for c in body["cards"]] == ["from this run", "from last week"]
        only = (await on.get("/api/boards/scoped/workflow/wf_review", params={"run": "this"})).json()
        assert [c["title"] for c in only["cards"]] == ["from this run"]


async def test_a_run_board_is_nobodys_to_open(on, world):
    run_board = await store.get_or_create_scoped_board("run", "team:t9")
    card, _ = await store.add_card(run_board.id, title="member task", by=AGENT)
    assert (await on.get(f"/api/boards/{run_board.id}")).status_code == 404
    assert (await on.get(f"/api/boards/cards/{card.id}")).status_code == 404
    assert (await on.patch(f"/api/boards/cards/{card.id}", json={"version": 1, "title": "x"})).status_code == 404
