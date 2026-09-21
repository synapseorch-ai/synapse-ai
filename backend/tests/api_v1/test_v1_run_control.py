"""v1's orchestration runs: answered once, stoppable, and a finished run stays finished (F49).

A v1 run carries on when its caller stops reading (`test_outlives_the_client.py`),
so an API caller needs the controls the app has: a resume that refuses a run not
waiting for one, and a way to stop a run.
"""
from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

_ASK_THEN_THINK = [
    {"id": "ask", "name": "Ask", "type": "human", "human_prompt": "ok?",
     "output_key": "answer", "next_step_id": "s"},
    {"id": "s", "name": "Think", "type": "llm",
     "prompt_template": "hi", "output_key": "out", "next_step_id": None},
]
_THINK = [{"id": "s", "name": "Think", "type": "llm",
           "prompt_template": "hi", "output_key": "out", "next_step_id": None}]


async def _status(run_id: str) -> str | None:
    from core.orchestration.state import SharedState
    try:
        return (await SharedState.restore(run_id)).run.status
    except FileNotFoundError:
        return None


async def _until_status(run_id: str, status: str, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await _status(run_id) == status:
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.fixture
async def paused_run(client, api_key, seed_orchestration):
    orch = await seed_orchestration(entry_step_id="ask", steps=_ASK_THEN_THINK)
    started = await client.post(f"/api/v1/orchestrations/{orch['id']}/run",
                                json={"message": "go"}, headers=api_key["headers"])
    run_id = started.json()["run_id"]
    assert await _until_status(run_id, "paused")
    return run_id


@pytest.fixture
async def completed_run(client, api_key, seed_orchestration):
    orch = await seed_orchestration()
    started = await client.post(f"/api/v1/orchestrations/{orch['id']}/run",
                                json={"message": "go"}, headers=api_key["headers"])
    run_id = started.json()["run_id"]
    assert await _until_status(run_id, "completed")
    return run_id


class TestAResumeIsAnAnswer:
    @pytest.mark.parametrize("route", ["resume", "resume/stream"])
    async def test_a_run_not_waiting_is_refused(self, client, api_key, completed_run, route):
        answer = await client.post(f"/api/v1/orchestrations/runs/{completed_run}/{route}",
                                   json={"response": "yes"}, headers=api_key["headers"])
        assert answer.status_code == 409, answer.text
        assert await _status(completed_run) == "completed"

    @pytest.mark.parametrize("route", ["resume", "resume/stream"])
    async def test_an_unknown_run_is_a_404(self, client, api_key, route):
        answer = await client.post(f"/api/v1/orchestrations/runs/no_such_run/{route}",
                                   json={"response": "yes"}, headers=api_key["headers"])
        assert answer.status_code == 404, answer.text

    async def test_a_second_answer_while_the_first_runs_is_refused(
        self, client, api_key, live_server, gated_llm, paused_run
    ):
        async with live_server.client() as c:
            async with c.stream("POST", f"/api/v1/orchestrations/runs/{paused_run}/resume/stream",
                                json={"response": "yes"}, headers=api_key["headers"]) as first:
                assert first.status_code == 200
                await asyncio.wait_for(gated_llm.started.wait(), 10)
                second = await client.post(
                    f"/api/v1/orchestrations/runs/{paused_run}/resume/stream",
                    json={"response": "yes again"}, headers=api_key["headers"])
                assert second.status_code == 409, second.text
                gated_llm.release.set()
                await first.aread()
        assert await _until_status(paused_run, "completed")


class TestAV1CallerCanStopARun:
    async def test_a_running_run_is_stopped(
        self, client, api_key, live_server, gated_llm, seed_orchestration
    ):
        orch = await seed_orchestration(entry_step_id="s", steps=_THINK)
        async with live_server.client() as c:
            async with c.stream("POST", f"/api/v1/orchestrations/{orch['id']}/run/stream",
                                json={"message": "go"}, headers=api_key["headers"]) as response:
                assert response.status_code == 200
                await asyncio.wait_for(gated_llm.started.wait(), 10)
                from core.orchestration.runner import _active_tasks
                run_id = next(r for r in _active_tasks if r.startswith(f"run_{orch['id']}"))
                stopped = await client.post(f"/api/v1/orchestrations/runs/{run_id}/cancel",
                                            headers=api_key["headers"])
                assert stopped.status_code == 200
                assert stopped.json() == {"status": "cancellation_requested",
                                          "run_id": run_id, "stopped": True}
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(response.aread(), 10)
        assert await _status(run_id) == "cancelled"

    async def test_an_unknown_run_is_a_404(self, client, api_key):
        stopped = await client.post("/api/v1/orchestrations/runs/no_such_run/cancel",
                                    headers=api_key["headers"])
        assert stopped.status_code == 404

    async def test_a_finished_run_keeps_its_status(self, client, api_key, completed_run):
        stopped = await client.post(f"/api/v1/orchestrations/runs/{completed_run}/cancel",
                                    headers=api_key["headers"])
        assert stopped.json()["stopped"] is False
        assert await _status(completed_run) == "completed"


class TestCancellingAFinishedRunInTheApp:
    async def test_a_completed_run_is_not_rewritten_as_cancelled(self, client, completed_run):
        """A double click, a stale tab, a retried request: the run had finished,
        and cancelling it rewrote its history as `cancelled`."""
        response = await client.post(f"/api/orchestrations/runs/{completed_run}/cancel")
        assert response.status_code == 200
        assert await _status(completed_run) == "completed"


class TestARunCanBeReadBack:
    """F117: `GET /api/v1/orchestrations/runs/{run_id}` answers with what the
    sync route would have — a v1 run outlives its caller (F49), so a caller
    needs a way back to it that does not depend on having kept the stream."""

    @staticmethod
    def _private(key: str) -> bool:
        from core.routes.orchestrations import _PRIVATE_STATE_PREFIXES
        return key.startswith(_PRIVATE_STATE_PREFIXES)

    async def test_an_ended_run_reads_as_the_sync_route_answered(self, client, api_key, seed_orchestration):
        orch = await seed_orchestration()
        ran = (await client.post(f"/api/v1/orchestrations/{orch['id']}/run",
                                 json={"message": "go"}, headers=api_key["headers"])).json()

        read = await client.get(f"/api/v1/orchestrations/runs/{ran['run_id']}",
                                headers=api_key["headers"])

        assert read.status_code == 200, read.text
        body = read.json()
        for field in ("status", "run_id", "response", "step_history"):
            assert body[field] == ran[field], field
        # Engine bookkeeping is left out, as the app's own read leaves it out.
        assert body["shared_state"] == {k: v for k, v in ran["shared_state"].items()
                                        if not self._private(k)}

    async def test_a_paused_run_names_its_question_as_the_sync_route_did(
        self, client, api_key, seed_orchestration
    ):
        orch = await seed_orchestration(entry_step_id="ask", steps=_ASK_THEN_THINK)
        ran = (await client.post(f"/api/v1/orchestrations/{orch['id']}/run",
                                 json={"message": "go"}, headers=api_key["headers"])).json()
        assert await _until_status(ran["run_id"], "paused")

        body = (await client.get(f"/api/v1/orchestrations/runs/{ran['run_id']}",
                                 headers=api_key["headers"])).json()

        assert body["status"] == "paused"
        asked = ran["human_input_required"]
        assert body["human_input_required"] == {k: asked[k] for k in ("step_id", "prompt", "fields")}

    async def test_an_unknown_run_is_a_404(self, client, api_key):
        read = await client.get("/api/v1/orchestrations/runs/no_such_run", headers=api_key["headers"])
        assert read.status_code == 404, read.text

    async def test_it_needs_a_key(self, client, completed_run):
        read = await client.get(f"/api/v1/orchestrations/runs/{completed_run}")
        assert read.status_code in (401, 403), read.text
