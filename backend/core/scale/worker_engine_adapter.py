"""
WorkerEngineAdapter — wraps OrchestrationEngine.run() for use inside an ARQ job.

Differences from the in-process (V1) execution path:
  - Uses SharedStatePG (Postgres) instead of SharedState (JSON files)
  - Publishes every SSE event to a Redis Stream via RunEventPublisher
  - Polls Redis for distributed cancellation signal at each step boundary
  - Handles HUMAN step pause/resume via Redis keys + ARQ re-enqueue
"""
from typing import AsyncGenerator

from core.models_orchestration import Orchestration, OrchestrationRun
from core.orchestration.engine import OrchestrationEngine
from core.scale.pubsub import RunEventPublisher, is_cancelled, clear_cancellation
from core.scale.state_pg import SharedStatePG


class WorkerEngineAdapter:
    """Runs an OrchestrationEngine inside an ARQ worker job."""

    def __init__(
        self,
        orch: Orchestration,
        run_id: str,
        worker_server_module,
        publisher: RunEventPublisher,
        session_factory,
        redis_client,
        worker_id: str = "",
        job_id: str = "",
    ):
        self._orch = orch
        self._run_id = run_id
        self._server_module = worker_server_module
        self._publisher = publisher
        self._session_factory = session_factory
        self._redis = redis_client
        self._worker_id = worker_id
        self._job_id = job_id

    async def run(
        self,
        initial_input: str,
        session_id: str | None = None,
        initial_state: dict | None = None,
    ) -> str:
        """
        Run the orchestration and publish events to Redis.
        Returns the final status string.
        Raises on unrecoverable error (ARQ will retry).
        """
        engine = self._build_engine()
        final_status = "completed"

        async for event in engine.run(
            initial_input=initial_input,
            run_id=self._run_id,
            session_id=session_id,
            initial_state=initial_state,
        ):
            await self._publisher.publish(event)

            # Capture the real final status from the engine event
            if event.get("type") == "orchestration_complete":
                final_status = event.get("status", "completed")

            # If the engine paused for human input, stop here.
            # The run is checkpointed; a resume job will pick it up.
            if event.get("type") == "human_input_required":
                await self._sync_paused_state(event)
                await self._publisher.publish_paused()
                return "paused"

        # Sync final run state from the in-memory engine state back to Postgres
        await self._sync_final_state(final_status)
        await self._publisher.publish_done()
        return final_status

    async def resume(
        self,
        human_response: dict | str,
    ) -> str:
        """Resume a paused run after human input.

        Uses self._orch (loaded from Postgres by the worker) instead of the
        OrchestrationEngine.resume() classmethod, which loads from local disk
        files that don't exist in distributed worker deployments.
        """
        from core.orchestration.state import SharedState
        from core.orchestration.logger import OrchestrationLogger

        # Restore in-progress run state from the JSON checkpoint written during the initial run.
        print(f"[adapter.resume] ▶ restoring JSON checkpoint run_id={self._run_id}", flush=True)
        restored = SharedState.restore(self._run_id)
        run = restored.run
        print(f"[adapter.resume] 📋 restored: status={run.status} current_step_id={run.current_step_id!r} waiting_for_human={run.waiting_for_human} step_history_len={len(run.step_history)}", flush=True)

        # Build engine using self._orch (Postgres-loaded) so step_map is always populated.
        engine = self._build_engine()
        print(f"[adapter.resume] 🗺  step_map keys: {list(engine.step_map.keys())}", flush=True)

        engine.logger = OrchestrationLogger(
            run_id=self._run_id,
            orchestration_id=run.orchestration_id,
            orchestration_name=self._orch.name,
            user_input=f"(resumed) human_response={human_response}",
            session_id=run.session_id,
        )

        final_status = "completed"

        # Nested-orchestration pause: delegate to the classmethod's nested handler.
        if run.nested_run_id:
            print(f"[adapter.resume] 🔗 nested run — delegating to _resume_nested_orch nested_run_id={run.nested_run_id}", flush=True)
            event_count = 0
            async for event in OrchestrationEngine._resume_nested_orch(run, engine, human_response, self._server_module):
                event_count += 1
                print(f"[adapter.resume] 📨 nested event #{event_count} type={event.get('type', '?')}", flush=True)
                await self._publisher.publish(event)
                if event.get("type") == "orchestration_complete":
                    final_status = event.get("status", "completed")
                if event.get("type") == "human_input_required":
                    await self._sync_paused_state(event)
                    await self._publisher.publish_paused()
                    return "paused"
            await self._sync_final_state(final_status)
            await self._publisher.publish_done()
            return final_status

        # Normal path: human step is directly in this orchestration.
        current_step = engine.step_map.get(run.current_step_id)
        print(f"[adapter.resume] 🔍 current_step lookup: id={run.current_step_id!r} found={current_step is not None} type={current_step.type.value if current_step else 'N/A'}", flush=True)

        output_key = (current_step.output_key if current_step else None) or "human_response"
        run.shared_state[output_key] = human_response
        if output_key != "human_response":
            run.shared_state["human_response"] = human_response

        run.waiting_for_human = False
        run.status = "running"

        if current_step:
            next_id, _ = engine._resolve_next(current_step, run)
            run.current_step_id = next_id
            print(f"[adapter.resume] ➡  _resolve_next → next_step_id={next_id!r}", flush=True)
        else:
            print(f"[adapter.resume] ⚠️  current_step not in step_map — step_id={run.current_step_id!r} will fail in _execute_loop", flush=True)

        print(f"[adapter.resume] 🚀 entering _execute_loop with current_step_id={run.current_step_id!r} status={run.status}", flush=True)
        state = SharedState(run)
        event_count = 0
        async for event in engine._execute_loop(run, state):
            event_count += 1
            etype = event.get("type", "unknown")
            print(f"[adapter.resume] 📨 event #{event_count} type={etype} step={event.get('orch_step_id', '')} status={event.get('status', '')}", flush=True)
            await self._publisher.publish(event)

            if event.get("type") == "orchestration_complete":
                final_status = event.get("status", "completed")
                print(f"[adapter.resume] 🏁 orchestration_complete → final_status={final_status}", flush=True)

            if event.get("type") == "human_input_required":
                print(f"[adapter.resume] ⏸ another human step — pausing again", flush=True)
                await self._sync_paused_state(event)
                await self._publisher.publish_paused()
                return "paused"

        print(f"[adapter.resume] ✅ loop done after {event_count} events, calling _sync_final_state(status={final_status})", flush=True)
        await self._sync_final_state(final_status)
        await self._publisher.publish_done()
        return final_status

    async def resume_failed(self) -> str:
        """Resume a failed or cancelled run."""
        engine = self._build_engine()
        final_status = "completed"

        async for event in OrchestrationEngine.resume_failed(
            run_id=self._run_id,
            server_module=self._server_module,
        ):
            await self._publisher.publish(event)

            if event.get("type") == "orchestration_complete":
                final_status = event.get("status", "completed")

            if event.get("type") == "human_input_required":
                await self._publisher.publish_paused()
                return "paused"

        await self._sync_final_state(final_status)
        await self._publisher.publish_done()
        return final_status

    # ------------------------------------------------------------------

    def _build_engine(self) -> OrchestrationEngine:
        redis = self._redis

        async def _cancel_hook() -> bool:
            return await is_cancelled(redis, self._run_id)

        engine = OrchestrationEngine(
            self._orch,
            self._server_module,
            cancel_hook=_cancel_hook,
        )
        return engine

    async def _sync_paused_state(self, human_event: dict) -> None:
        """Update Postgres to reflect the run is paused waiting for human input."""
        try:
            async with self._session_factory() as session:
                from sqlalchemy import update
                from core.scale.models_db import OrchestrationRunDB
                await session.execute(
                    update(OrchestrationRunDB)
                    .where(OrchestrationRunDB.run_id == self._run_id)
                    .values(
                        status="paused",
                        waiting_for_human=True,
                        current_step_id=human_event.get("orch_step_id"),
                        human_prompt=human_event.get("prompt"),
                        human_fields=human_event.get("fields") or [],
                    )
                )
                await session.commit()
        except Exception as e:
            print(f"[worker_engine_adapter] _sync_paused_state error: {e}", flush=True)

    async def _sync_final_state(self, status: str = "completed") -> None:
        """Persist the final run status, ended_at, worker attribution, and cost/tokens to Postgres."""
        from datetime import datetime, timezone
        try:
            async with self._session_factory() as session:
                from sqlalchemy import update
                from core.scale.models_db import OrchestrationRunDB
                values: dict = {
                    "status": status,
                    "ended_at": datetime.now(timezone.utc),
                    "waiting_for_human": False,
                    "current_step_id": None,
                }
                if self._worker_id:
                    values["worker_id"] = self._worker_id
                    values["job_id"] = self._job_id

                # Aggregate cost/token totals from usage_tracker for this run
                try:
                    from core.usage_tracker import get_usage_logs
                    usage_records = get_usage_logs(run_id=self._run_id, limit=100_000)
                    values["total_tokens_used"] = sum(r.get("total_tokens", 0) for r in usage_records)
                    values["total_cost_usd"] = round(sum(r.get("estimated_cost", 0.0) for r in usage_records), 8)
                    values["cache_read_tokens"] = sum(r.get("cache_read_tokens", 0) for r in usage_records)
                    values["cache_write_tokens"] = sum(r.get("cache_write_tokens", 0) for r in usage_records)
                    values["cache_hit_count"] = sum(1 for r in usage_records if r.get("response_cache_hit"))
                    values["estimated_savings_usd"] = round(sum(r.get("estimated_savings", 0.0) for r in usage_records), 8)
                except Exception as e:
                    print(f"[worker_engine_adapter] cost aggregation error: {e}", flush=True)

                await session.execute(
                    update(OrchestrationRunDB)
                    .where(OrchestrationRunDB.run_id == self._run_id)
                    .values(**values)
                )
                await session.commit()
        except Exception as e:
            print(f"[worker_engine_adapter] _sync_final_state error: {e}", flush=True)

        # Clear any stale cancel signal
        try:
            await clear_cancellation(self._redis, self._run_id)
        except Exception:
            pass
