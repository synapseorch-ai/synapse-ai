"""A workflow saved from a stale editor is refused, not written over a newer save; a deleted one is said deleted (F871).

QA1d on prod (rc3, 2026-10-22, two tabs): tab B's save, from a copy older than tab A's, overwrote it — both 200 — while
tab A still said "Saved"; and a save of a workflow another tab had deleted got a 404 the editor never showed. Here,
OSS's editor saves with an unconditional upsert, so the deleted workflow came back instead.

The editor says what it read with `If-Match: "<updated_at>"`. A save that sends it is refused 409 when the workflow
changed since — `{"message", "updated_at"}`, the other save's time — and 404 when it is gone; a save that sends none
(the builder, the CLI, an import, a seed, every older caller) is unchanged.
"""
from _fakes import seed as S


def _seen(doc: dict) -> dict:
    return {"If-Match": f'"{doc["updated_at"]}"'}


async def _saved(client, **over) -> dict:
    resp = await client.post("/api/orchestrations", json=S.make_orchestration(id="orch_f871", name="Desk", **over))
    assert resp.status_code == 200
    return resp.json()


class TestTheEditorsSave:
    async def test_a_save_from_the_copy_it_read_goes_in_and_moves_the_stamp(self, client):
        first = await _saved(client)
        resp = await client.post("/api/orchestrations", json={**first, "name": "Desk v2"}, headers=_seen(first))
        assert resp.status_code == 200
        assert resp.json()["name"] == "Desk v2"
        assert resp.json()["updated_at"] != first["updated_at"]

    async def test_a_save_from_a_stale_copy_is_refused_and_writes_nothing(self, client):
        a = await _saved(client)
        b = dict(a)                                            # tab B opened the same copy
        newer = (await client.post("/api/orchestrations", json={**a, "name": "A's"}, headers=_seen(a))).json()
        resp = await client.post("/api/orchestrations", json={**b, "name": "B's"}, headers=_seen(b))
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["updated_at"] == newer["updated_at"]
        assert "changed elsewhere" in detail["message"]
        assert (await client.get("/api/orchestrations/orch_f871")).json()["name"] == "A's"

    async def test_keeping_mine_is_a_save_from_the_time_it_was_told(self, client):
        a = await _saved(client)
        newer = (await client.post("/api/orchestrations", json={**a, "name": "A's"}, headers=_seen(a))).json()
        refused = await client.post("/api/orchestrations", json={**a, "name": "B's"}, headers=_seen(a))
        told = refused.json()["detail"]["updated_at"]
        assert told == newer["updated_at"]
        resp = await client.post("/api/orchestrations", json={**a, "name": "B's"}, headers={"If-Match": f'"{told}"'})
        assert resp.status_code == 200 and resp.json()["name"] == "B's"

    async def test_a_save_of_a_deleted_workflow_is_404_and_does_not_bring_it_back(self, client):
        a = await _saved(client)
        assert (await client.delete("/api/orchestrations/orch_f871")).status_code == 200
        resp = await client.post("/api/orchestrations", json={**a, "name": "Back?"}, headers=_seen(a))
        assert resp.status_code == 404
        assert "deleted" in resp.json()["detail"]["message"]
        assert (await client.get("/api/orchestrations/orch_f871")).status_code == 404

    async def test_a_builder_write_in_between_makes_the_editors_copy_stale(self, client, monkeypatch):
        from core.builder_tools import execute_builder_tool

        a = await _saved(client)

        class _Server:
            agent_sessions: dict = {}

        await execute_builder_tool("set_orchestration_meta", {"orch_id": "orch_f871", "name": "Builder's"}, _Server())
        resp = await client.post("/api/orchestrations", json={**a, "name": "Mine"}, headers=_seen(a))
        assert resp.status_code == 409


class TestEveryOtherCaller:
    async def test_a_save_with_no_if_match_is_unchanged_even_carrying_an_old_stamp(self, client):
        a = await _saved(client)
        await client.post("/api/orchestrations", json={**a, "name": "A's"}, headers=_seen(a))
        resp = await client.post("/api/orchestrations", json={**a, "name": "Imported"})
        assert resp.status_code == 200 and resp.json()["name"] == "Imported"

    async def test_an_exported_file_posted_for_a_new_id_is_created(self, client):
        doc = S.make_orchestration(id="orch_f871_new", name="From a file")
        resp = await client.post("/api/orchestrations", json={**doc, "updated_at": "2026-01-01T00:00:00Z"})
        assert resp.status_code == 200
