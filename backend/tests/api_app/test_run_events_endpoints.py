"""
Run event replay endpoints (GET /api/orchestrations/runs/{run_id}/events[,/stream]).

Backward compat is asserted first: the POST /run SSE payload is unchanged by
journaling. Then: JSON replay with `after`, SSE replay→live handoff, paused
runs keeping the tail open across a resume pump, and the Redis fallback for
V2/worker runs that have no local journal file.
"""
import asyncio
import json

from _fakes import engine_events as E
from _fakes.fake_redis_stream import data_events, load_run_events


def _sse_json(text: str) -> list[dict]:
    return data_events(text.split("\n\n"))


def _sse_ids(text: str) -> list[int]:
    return [int(line[4:]) for line in text.split("\n")
            if line.startswith("id: ")]


class _FakeEngine:
    events: list[dict] = []

    def __init__(self, orch=None, server_module=None):
        pass

    async def run(self, user_input, run_id, **kwargs):
        for ev in type(self).events:
            yield ev


def _one_journaled_run_id():
    import core.orchestration.journal as journal_mod
    files = list(journal_mod._events_dir().glob("*.jsonl"))
    assert len(files) == 1
    return files[0].stem


async def _run_orch(client, seed_orchestration, monkeypatch, events):
    orch = await seed_orchestration()
    import core.orchestration.engine as engine_mod
    _FakeEngine.events = events
    monkeypatch.setattr(engine_mod, "OrchestrationEngine", _FakeEngine)
    resp = await client.post(f"/api/orchestrations/{orch['id']}/run",
                             json={"message": "go"})
    assert resp.status_code == 200
    return resp, _one_journaled_run_id()


LIFECYCLE = [
    E.orch_start(),
    E.step_start("s1", "Step 1"),
    E.thinking("mulling it over"),
    E.step_complete("s1", "Step 1"),
    E.orch_complete(status_str="completed"),
]


class TestPostRunUnchanged:
    async def test_post_sse_payload_is_byte_compatible(self, client, seed_orchestration, monkeypatch):
        """Journaling is a pump side-effect: the POST stream must not change."""
        resp, _run_id = await _run_orch(client, seed_orchestration, monkeypatch, LIFECYCLE)
        events = _sse_json(resp.text)
        assert events == LIFECYCLE + [{"type": "done"}]
        # No id: lines and no run_stream_end on the POST stream.
        assert "id: " not in resp.text
        assert "run_stream_end" not in resp.text


class TestEventsJson:
    async def test_replay_matches_live_stream(self, client, seed_orchestration, monkeypatch):
        _resp, run_id = await _run_orch(client, seed_orchestration, monkeypatch, LIFECYCLE)

        resp = await client.get(f"/api/orchestrations/runs/{run_id}/events")
        assert resp.status_code == 200
        body = resp.json()
        replayed = [e["event"] for e in body["events"]]
        assert replayed[:-1] == LIFECYCLE  # identical events, in order
        assert replayed[-1]["type"] == "run_stream_end"
        assert body["last_id"] == body["events"][-1]["id"]

    async def test_after_pages_forward(self, client, seed_orchestration, monkeypatch):
        _resp, run_id = await _run_orch(client, seed_orchestration, monkeypatch, LIFECYCLE)
        resp = await client.get(f"/api/orchestrations/runs/{run_id}/events?after=3")
        events = resp.json()["events"]
        assert all(e["id"] > 3 for e in events)
        assert [e["event"]["type"] for e in events][:2] == ["step_complete", "orchestration_complete"]

    async def test_unknown_run_is_404(self, client):
        resp = await client.get("/api/orchestrations/runs/run_nope/events")
        assert resp.status_code == 404

    async def test_pre_journal_run_returns_checkpoint_only(self, client):
        """A run from before the journal existed: checkpoint but no .jsonl."""
        import core.orchestration.state as state_mod
        from core.models_orchestration import OrchestrationRun
        run = OrchestrationRun(run_id="run_old", orchestration_id="orch_1",
                              status="completed")
        await state_mod.SharedState(run).checkpoint()

        resp = await client.get("/api/orchestrations/runs/run_old/events")
        assert resp.status_code == 200
        body = resp.json()
        assert body["events"] == []
        assert body["run"]["status"] == "completed"

    async def test_redis_fallback_for_worker_runs(self, client, scale_app, fake_redis,
                                                  seed_orchestration, monkeypatch):
        """No journal file + Redis events (a V2/worker run) → served from Redis."""
        await load_run_events(fake_redis, "run_v2", LIFECYCLE)
        resp = await client.get("/api/orchestrations/runs/run_v2/events")
        assert resp.status_code == 200
        body = resp.json()
        assert [e["event"]["type"] for e in body["events"]] == [e["type"] for e in LIFECYCLE]
        assert body["last_id"] == body["events"][-1]["id"]


class TestEventsStream:
    async def test_terminal_run_replays_and_completes(self, client, seed_orchestration, monkeypatch):
        _resp, run_id = await _run_orch(client, seed_orchestration, monkeypatch, LIFECYCLE)

        resp = await client.get(f"/api/orchestrations/runs/{run_id}/events/stream")
        assert resp.status_code == 200
        events = _sse_json(resp.text)
        assert events[:-1] == LIFECYCLE  # run_stream_end is never forwarded
        assert events[-1] == {"type": "stream_complete"}
        # id: lines are present and monotonic for replayed events.
        ids = _sse_ids(resp.text)
        assert ids == sorted(ids) and len(ids) == len(LIFECYCLE)

    async def test_last_event_id_header_resumes(self, client, seed_orchestration, monkeypatch):
        _resp, run_id = await _run_orch(client, seed_orchestration, monkeypatch, LIFECYCLE)
        resp = await client.get(f"/api/orchestrations/runs/{run_id}/events/stream",
                                headers={"Last-Event-ID": "3"})
        events = _sse_json(resp.text)
        assert [e["type"] for e in events] == ["step_complete", "orchestration_complete",
                                              "stream_complete"]

    async def test_crashed_run_without_checkpoint_still_completes(self, client, seed_orchestration, monkeypatch):
        """Pump died with an error before any checkpoint: the journal's own
        terminal marker must close the stream (no checkpoint to consult)."""
        _resp, run_id = await _run_orch(client, seed_orchestration, monkeypatch, [
            E.orch_start(),
            {"type": "orchestration_error", "error": "boom"},
        ])
        resp = await client.get(f"/api/orchestrations/runs/{run_id}/events/stream")
        events = _sse_json(resp.text)
        assert events[-1] == {"type": "stream_complete"}

    async def test_paused_run_tails_through_resume(self, monkeypatch, test_app):
        """The reattach contract: a stream opened on a paused run stays open
        and receives the resume pump's events live, deduped by id."""
        import core.routes.orchestrations as orch_routes
        from core.orchestration.journal import broker, get_journal, close_journal

        run_id = "run_tail"
        journal = get_journal(run_id)
        journal.append(E.step_start())
        journal.append(E.human_input_required())
        journal.append({"type": "run_stream_end", "paused": True})
        close_journal(run_id)

        agen = orch_routes._stream_journal_sse(run_id, 0)
        chunks = [await agen.__anext__(), await agen.__anext__()]
        assert _sse_json("".join(chunks))[0]["type"] == "step_start"
        assert _sse_json("".join(chunks))[1]["type"] == "human_input_required"

        # Resume pump comes alive and publishes fresh events.
        next_chunk = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0)  # let the generator subscribe & block on the queue
        journal2 = get_journal(run_id)
        eid = journal2.append(E.orch_complete())
        broker.publish(run_id, {"id": eid, "event": E.orch_complete()})
        assert _sse_json(await next_chunk)[0]["type"] == "orchestration_complete"

        # Terminal marker closes the tail.
        broker.publish(run_id, {"id": eid + 1, "event": {"type": "run_stream_end", "paused": False}})
        assert _sse_json(await agen.__anext__())[0]["type"] == "stream_complete"
        with __import__("pytest").raises(StopAsyncIteration):
            await agen.__anext__()
        close_journal(run_id)

    async def test_unknown_run_is_404(self, client):
        resp = await client.get("/api/orchestrations/runs/run_nope/events/stream")
        assert resp.status_code == 404


class TestResumeGuard:
    async def test_double_resume_is_409(self, client, monkeypatch):
        """A second pump for the same run would interleave its journal."""
        from core.orchestration.runner import _active_tasks

        async def _forever():
            await asyncio.sleep(30)

        task = asyncio.ensure_future(_forever())
        _active_tasks["run_busy"] = task
        try:
            resp = await client.post("/api/orchestrations/runs/run_busy/resume", json={})
            assert resp.status_code == 409
            resp = await client.post("/api/orchestrations/runs/run_busy/human-input",
                                     json={"response": {"answer": "yes"}})
            assert resp.status_code == 409
        finally:
            task.cancel()
            _active_tasks.pop("run_busy", None)
