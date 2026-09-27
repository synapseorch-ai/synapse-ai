"""Two runs started in the same millisecond are two runs (F259).

Run ids were `run_{orch}_{ms}`, so two requests in one millisecond shared an
id: one run's record overwrote the other's, and a caller polling its run read
another request's input and output (8 concurrent starts → 6 ids, measured).
Every place that mints an id now adds six random hex characters after the
millisecond, which keeps the id sortable by start time.

The clock is frozen for each test, so "the same millisecond" is certain rather
than likely.
"""
from __future__ import annotations

import json
import time

import pytest


@pytest.fixture
def same_millisecond(monkeypatch):
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    return now


def _events(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                out.append(json.loads(line[len("data: "):]))
            except ValueError:
                pass
    return out


def _started_id(text: str) -> str:
    return next(e["run_id"] for e in _events(text) if e.get("type") == "orchestration_start")


class TestEveryDoorMintsADistinctId:
    async def test_the_app_run_route(self, client, seed_orchestration, same_millisecond):
        orch = await seed_orchestration()
        ids = []
        for _ in range(2):
            resp = await client.post(f"/api/orchestrations/{orch['id']}/run", json={"message": "go"})
            assert resp.status_code == 200
            ids.append(_started_id(resp.text))
        assert ids[0] != ids[1]
        assert all(i.startswith(f"run_{orch['id']}_") for i in ids)

    async def test_v1_run(self, client, api_key, seed_orchestration, same_millisecond):
        orch = await seed_orchestration()
        ids = []
        for _ in range(2):
            resp = await client.post(f"/api/v1/orchestrations/{orch['id']}/run",
                                     json={"message": "go"}, headers=api_key["headers"])
            assert resp.status_code == 200, resp.text
            ids.append(resp.json()["run_id"])
        assert ids[0] != ids[1]

    async def test_v1_run_stream(self, client, api_key, seed_orchestration, same_millisecond):
        orch = await seed_orchestration()
        ids = []
        for _ in range(2):
            resp = await client.post(f"/api/v1/orchestrations/{orch['id']}/run/stream",
                                     json={"message": "go"}, headers=api_key["headers"])
            assert resp.status_code == 200, resp.text
            ids.append(_started_id(resp.text))
        assert ids[0] != ids[1]

    async def test_the_mcp_server(self, seed_orchestration, same_millisecond):
        from core.mcp_server.runs import InProcessRuns

        orch = await seed_orchestration()
        backend = InProcessRuns()
        first, _ = await backend.start(orch, "go", None)
        second, _ = await backend.start(orch, "go", None)
        assert first != second

    async def test_an_orchestrator_agent(self, seed_orchestration, same_millisecond):
        import core.server as server
        from core.react_engine import run_orchestrator_agent

        orch = await seed_orchestration()
        agent = {"id": "orch_agent_x", "name": "X", "type": "orchestrator", "orchestration_id": orch["id"]}
        ids = []
        for _ in range(2):
            events = [e async for e in run_orchestrator_agent(agent, "go", server)]
            ids.append(next(e["run_id"] for e in events if e.get("type") == "orchestration_start"))
        assert ids[0] != ids[1]

    def test_a_schedule(self, same_millisecond):
        from core.schedule_logger import ScheduleLogger

        logs = [ScheduleLogger("sched_abc", "Nightly", "orchestration", "orch_x", "go") for _ in range(2)]
        try:
            assert logs[0].run_id != logs[1].run_id
            assert logs[0].run_id.startswith("schedulerun_abc_")
        finally:
            for log in logs:
                log.close()

    def test_an_agent_log(self, same_millisecond):
        from core.agent_logger import AgentLogger

        logs = [AgentLogger("agent_abc", "A", "sess", "chat", "hi") for _ in range(2)]
        try:
            assert logs[0].run_id != logs[1].run_id
            assert logs[0].run_id.startswith("agentrun_abc_")
        finally:
            for log in logs:
                log.close()

    def test_v2_names_the_orchestration(self, same_millisecond):
        from core.routes.api_v2 import _new_run_id

        first, second = _new_run_id("orch_x"), _new_run_id("orch_x")
        assert first != second
        assert first.startswith("run_orch_x_")


class TestAnIdStillNamesItsOrchestration:
    """A run inside its first step has no checkpoint: its id is all there is."""

    @pytest.mark.parametrize("run_id, orch_id", [
        ("run_orch_a_b_1759000000123_0a1b2c", "orch_a_b"),   # the new shape
        ("run_orch_a_b_1759000000123", "orch_a_b"),           # an id minted before it
        ("run_orch_123456_1759000000123", "orch_123456"),     # an orch id ending in digits
        ("run_orch_abcdef_1759000000123", "orch_abcdef"),     # ... or in six hex characters
    ])
    async def test_before_the_first_checkpoint(self, run_id, orch_id):
        from core.orchestration.run_view import run_orchestration_id

        assert await run_orchestration_id(run_id) == orch_id

    async def test_the_minted_id_parses_back(self, same_millisecond):
        from core.orchestration.run_view import run_orchestration_id
        from core.run_ids import new_run_id

        assert await run_orchestration_id(new_run_id("orch_with_underscores_9")) == "orch_with_underscores_9"

    async def test_new_ids_still_sort_by_start_time(self, monkeypatch):
        from core.run_ids import new_run_id

        monkeypatch.setattr(time, "time", lambda: 1_759_000_000.001)
        earlier = new_run_id("orch_x")
        monkeypatch.setattr(time, "time", lambda: 1_759_000_000.002)
        later = new_run_id("orch_x")
        assert sorted([later, earlier], reverse=True) == [later, earlier]
