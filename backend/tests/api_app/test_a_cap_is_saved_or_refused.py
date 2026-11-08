"""A workflow's cap per run is saved as given, and one that is not a cap is refused (CAP1, D272).

The editor's save is the place a person can be told: a credit cap under 1 (every
run is charged a credit) or a dollar cap of 0 is a 422 naming the field. Read
anywhere else — a run, a resume — a stored nonsense cap is no cap
(`tests/unit/test_a_run_stops_at_its_cap.py`).
"""
from _fakes import seed as S


class TestTheEditorsSave:
    async def test_a_cap_round_trips(self, client):
        orch = S.make_orchestration(id="orch_cap_save", max_run_credits=5, max_run_usd=0.25)
        saved = await client.post("/api/orchestrations", json=orch)
        assert saved.status_code == 200
        assert saved.json()["max_run_credits"] == 5 and saved.json()["max_run_usd"] == 0.25

        got = (await client.get("/api/orchestrations/orch_cap_save")).json()
        assert got["max_run_credits"] == 5 and got["max_run_usd"] == 0.25

    async def test_cleared_it_is_no_cap(self, client):
        orch = S.make_orchestration(id="orch_cap_clear", max_run_credits=5)
        await client.post("/api/orchestrations", json=orch)
        saved = await client.post("/api/orchestrations", json={**orch, "max_run_credits": None})
        assert saved.status_code == 200 and saved.json()["max_run_credits"] is None

    async def test_a_cap_that_is_not_one_is_refused(self, client):
        for field, value in (("max_run_credits", 0.5), ("max_run_usd", 0)):
            resp = await client.post(
                "/api/orchestrations", json=S.make_orchestration(id="orch_cap_bad", **{field: value}))
            assert resp.status_code == 422, (field, value)
            assert field in resp.text
        assert (await client.get("/api/orchestrations/orch_cap_bad")).status_code == 404
