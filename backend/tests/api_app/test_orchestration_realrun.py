"""
Real orchestration runs through the HTTP routes (no engine patching): the
internal SSE run endpoint and the v1 sync endpoint execute an actual print-step
orchestration end to end, covering the route handlers + engine + step executors.
"""
import pytest

from _fakes import seed as S
from _fakes.fake_redis_stream import data_events


def _sse_json(text):
    return data_events(text.split("\n\n"))


class TestRealAppRun:
    async def test_internal_run_streams_real_engine(self, client, seed_orchestration):
        orch = seed_orchestration()  # single print step
        resp = await client.post(f"/api/orchestrations/{orch['id']}/run", json={"message": "go"})
        assert resp.status_code == 200
        types_seen = [e.get("type") for e in _sse_json(resp.text)]
        assert "orchestration_start" in types_seen
        assert "orchestration_complete" in types_seen
        assert types_seen[-1] == "done"


class TestRealV1Run:
    async def test_v1_sync_run_real_engine(self, client, api_key, seed_orchestration):
        orch = seed_orchestration(
            entry_step_id="p",
            steps=[{"id": "p", "name": "Say", "type": "print",
                    "print_content": "hello {state.user_input}",
                    "output_key": "out", "next_step_id": None}],
        )
        resp = await client.post(f"/api/v1/orchestrations/{orch['id']}/run",
                                 json={"message": "world"}, headers=api_key["headers"])
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] in ("completed", "running")

    async def test_v1_run_stream_real_engine(self, client, api_key, seed_orchestration):
        orch = seed_orchestration()
        resp = await client.post(f"/api/v1/orchestrations/{orch['id']}/run/stream",
                                 json={"message": "go"}, headers=api_key["headers"])
        assert resp.status_code == 200
        types_seen = [e.get("type") for e in _sse_json(resp.text)]
        assert "orchestration_complete" in types_seen
