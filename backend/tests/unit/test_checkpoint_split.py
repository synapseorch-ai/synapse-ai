"""
A per-turn checkpoint writes one column, not the run.

An agent step's transcript is checkpointed after every tool turn so a crash
resumes mid-step. That write used to serialise the entire run — every
execution-memory blob, the whole history — to store one step's progress, on
every turn. Progress has its own column now and the checkpoint names it.
"""
import asyncio
import types

import pytest

from _fakes import seed as S


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _agent_orch(step_id="s1"):
    return S.make_orchestration(
        id="orch_split", entry_step_id=step_id,
        steps=[{"id": step_id, "name": "Agent", "type": "agent", "agent_id": "a1",
                "output_key": "result", "next_step_id": None, "max_turns": 5}],
    )


async def _run(orch_dict, run_id="run_split", initial_state=None):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    engine = OrchestrationEngine(Orchestration.model_validate(orch_dict), _server())
    return [ev async for ev in engine.run("go", run_id=run_id, initial_state=initial_state)]


class _SaveSpy:
    """Records every backend save: which columns, and whether saves overlapped."""

    def __init__(self, monkeypatch, delay: float = 0.0):
        import core.orchestration.state as state_mod
        backend = state_mod.get_state_backend()
        real = backend.save
        self.calls: list = []
        self.active = 0
        self.max_active = 0

        async def spy(run, *a, **kw):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                if delay:
                    await asyncio.sleep(delay)
                self.calls.append(tuple(kw["columns"]) if kw.get("columns") else "full")
                return await real(run, *a, **kw)
            finally:
                self.active -= 1

        monkeypatch.setattr(backend, "save", spy)


class TestPerTurnWritesAreNarrow:
    async def test_progress_and_tasks_write_their_own_column(self, monkeypatch):
        import core.react_engine as re_mod

        async def five_turns(**kwargs):
            for turn in range(1, 6):
                yield {"type": "_step_progress", "turn": turn, "context_text": f"CTX{turn}",
                       "has_compacted": False, "browser_actions": [], "tools_used": []}
                if turn in (2, 4):
                    yield {"type": "tasks_updated", "tasks": [{"content": "t", "status": "pending"}]}
            yield {"type": "final", "response": "ok"}

        monkeypatch.setattr(re_mod, "run_agent_step", five_turns)
        spy = _SaveSpy(monkeypatch)

        events = await _run(_agent_orch())

        assert any(e.get("type") == "orchestration_complete" for e in events)
        assert spy.calls.count(("step_progress",)) == 5
        assert spy.calls.count(("tasks",)) == 2
        # Full writes: the initial checkpoint, the step boundary, the finalize.
        assert spy.calls.count("full") == 3
        assert set(spy.calls) == {("step_progress",), ("tasks",), "full"}

    async def test_the_partial_write_lands_and_is_readable(self, monkeypatch):
        import core.react_engine as re_mod
        import core.orchestration.state as state_mod

        async def crash_after_two(**kwargs):
            yield {"type": "_step_progress", "turn": 1, "context_text": "CTX1",
                   "has_compacted": False, "browser_actions": [], "tools_used": []}
            yield {"type": "_step_progress", "turn": 2, "context_text": "CTX2",
                   "has_compacted": False, "browser_actions": [], "tools_used": []}
            raise RuntimeError("died")

        monkeypatch.setattr(re_mod, "run_agent_step", crash_after_two)
        await _run(_agent_orch())

        run = (await state_mod.SharedState.restore("run_split")).run
        assert run.step_progress["s1"]["turn"] == 2
        assert run.step_progress["s1"]["context_text"] == "CTX2"
        assert "_step_progress_s1" not in run.shared_state


class TestPartialSaveSafety:
    async def test_a_partial_save_with_no_row_falls_back_to_a_full_save(self):
        """Never an insert with only the named columns: orchestration_id is
        NOT NULL with no default, and a run with no orchestration is worse
        than a full write."""
        from core.models_orchestration import OrchestrationRun
        from core.orchestration.state import SharedState
        from core.orchestration.state_store import StoreStateBackend

        run = OrchestrationRun(run_id="run_norow", orchestration_id="o1",
                               step_progress={"s1": {"turn": 1, "context_text": "X"}})
        await StoreStateBackend().save(run, columns=("step_progress",))

        restored = (await SharedState.restore("run_norow")).run
        assert restored.orchestration_id == "o1"
        assert restored.step_progress == {"s1": {"turn": 1, "context_text": "X"}}

    async def test_a_partial_save_touches_only_its_column(self):
        from core.models_orchestration import OrchestrationRun
        from core.orchestration.state import SharedState
        from core.orchestration.state_store import StoreStateBackend

        run = OrchestrationRun(run_id="run_only", orchestration_id="o1",
                               shared_state={"a": 1}, tasks=[{"content": "x", "status": "pending"}])
        await StoreStateBackend().save(run)

        # Change everything in memory, save only step_progress.
        run.shared_state["a"] = 2
        run.tasks = []
        run.step_progress["s1"] = {"turn": 3}
        await StoreStateBackend().save(run, columns=("step_progress",))

        restored = (await SharedState.restore("run_only")).run
        assert restored.step_progress == {"s1": {"turn": 3}}
        assert restored.shared_state == {"a": 1}          # untouched on disk
        assert restored.tasks == [{"content": "x", "status": "pending"}]

    async def test_a_backend_without_the_parameter_gets_full_saves(self, monkeypatch):
        """An embedder's own StateBackend predates `columns`. It must keep
        receiving the call it has always received."""
        import core.orchestration.state as state_mod
        from core.models_orchestration import OrchestrationRun

        class Legacy:
            def __init__(self):
                self.saves = []

            async def save(self, run):
                self.saves.append(run.run_id)

            async def load(self, run_id):
                return None

            async def list_runs(self, limit=20, orchestration_ids=None):
                return []

        legacy = Legacy()
        monkeypatch.setattr(state_mod, "_backend", legacy)
        run = OrchestrationRun(run_id="run_legacy", orchestration_id="o1")
        await state_mod.SharedState(run).checkpoint(columns=("step_progress",))
        assert legacy.saves == ["run_legacy"]


class TestOneSaveAtATimePerRun:
    async def test_concurrent_checkpoints_serialise(self, monkeypatch):
        from core.models_orchestration import OrchestrationRun
        from core.orchestration.state import SharedState

        spy = _SaveSpy(monkeypatch, delay=0.02)
        run = OrchestrationRun(run_id="run_lock", orchestration_id="o1")
        state = SharedState(run)
        await asyncio.gather(state.checkpoint(), state.checkpoint(columns=("tasks",)), state.checkpoint())
        assert len(spy.calls) == 3
        assert spy.max_active == 1

    async def test_the_lock_is_released_when_the_run_ends(self):
        import core.orchestration.state as state_mod
        from core.models_orchestration import OrchestrationRun

        run = OrchestrationRun(run_id="run_done", orchestration_id="o1")
        await state_mod.SharedState(run).checkpoint()
        assert "run_done" in state_mod._checkpoint_locks
        run.status = "completed"
        await state_mod.SharedState(run).checkpoint()
        assert "run_done" not in state_mod._checkpoint_locks


class TestTheWorkerLeavesProgressAlone:
    async def test_paused_and_final_sync_do_not_clobber_the_column(self, fake_redis):
        """The worker's explicit UPDATEs name their columns; step_progress
        is not among them, so a row they touch keeps its transcript."""
        from core.models_orchestration import Orchestration, OrchestrationRun
        from core.orchestration.state import SharedState
        from core.orchestration.state_store import StoreStateBackend
        from core.scale.pubsub import RunEventPublisher
        from core.scale.worker_engine_adapter import WorkerEngineAdapter
        from core.store import get_store

        run = OrchestrationRun(run_id="run_worker", orchestration_id="orch_split",
                               step_progress={"s1": {"turn": 4, "context_text": "KEEP"}})
        await StoreStateBackend().save(run)

        adapter = WorkerEngineAdapter(
            orch=Orchestration.model_validate(_agent_orch()), run_id="run_worker",
            worker_server_module=_server(), publisher=RunEventPublisher(fake_redis, "run_worker"),
            session_factory=await get_store(), redis_client=fake_redis,
            worker_id="w", job_id="j",
        )
        await adapter._sync_paused_state({"orch_step_id": "s1", "prompt": "?", "fields": []})
        await adapter._sync_final_state("completed")

        restored = (await SharedState.restore("run_worker")).run
        assert restored.step_progress == {"s1": {"turn": 4, "context_text": "KEEP"}}
        assert restored.status == "completed"


class TestRunDetailHidesEngineBookkeeping:
    async def test_private_keys_and_progress_are_stripped_but_user_keys_survive(self, client):
        from core.models_orchestration import OrchestrationRun
        from core.orchestration.state_store import StoreStateBackend

        run = OrchestrationRun(
            run_id="run_view", orchestration_id="o1", status="completed",
            shared_state={
                "result": "public", "_mine": "a user key that happens to start with _",
                "_exec_memory_s1": [{"big": "x" * 100}], "_step_progress_s1": {"context_text": "y"},
                "_loop_progress_l1": {"completed_iterations": 2}, "_routing_decision_e1": "ok",
                "_loop_out": [1, 2], "_if_decision_i1": "true",
            },
            step_progress={"s1": {"turn": 9, "context_text": "z" * 100}},
        )
        await StoreStateBackend().save(run)

        resp = await client.get("/api/orchestrations/runs/run_view")
        assert resp.status_code == 200
        doc = resp.json()
        assert doc["status"] == "completed"
        assert "step_progress" not in doc
        assert doc["shared_state"] == {"result": "public",
                                       "_mine": "a user key that happens to start with _"}
