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
                await self._publisher.publish_done()
                return "paused"

        # Sync final run state from the in-memory engine state back to Postgres
        await self._sync_final_state(final_status)
        await self._publisher.publish_done()
        return final_status

    async def resume(
        self,
        human_response: dict | str,
    ) -> str:
        """Resume a paused run after human input."""
        engine = self._build_engine()
        final_status = "completed"

        async for event in OrchestrationEngine.resume(
            run_id=self._run_id,
            human_response=human_response,
            server_module=self._server_module,
        ):
            await self._publisher.publish(event)

            if event.get("type") == "orchestration_complete":
                final_status = event.get("status", "completed")

            if event.get("type") == "human_input_required":
                await self._publisher.publish_done()
                return "paused"

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
                await self._publisher.publish_done()
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

    async def _sync_final_state(self, status: str = "completed") -> None:
        """Persist the final run status, ended_at, and worker attribution to Postgres."""
        from datetime import datetime, timezone
        try:
            async with self._session_factory() as session:
                from sqlalchemy import update
                from core.scale.models_db import OrchestrationRunDB
                values: dict = {
                    "status": status,
                    "ended_at": datetime.now(timezone.utc),
                }
                if self._worker_id:
                    values["worker_id"] = self._worker_id
                    values["job_id"] = self._job_id
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
