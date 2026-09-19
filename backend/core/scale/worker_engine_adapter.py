"""
WorkerEngineAdapter — wraps OrchestrationEngine.run() for use inside an ARQ job.

Differences from the in-process (V1) execution path:
  - Publishes every SSE event to a Redis Stream via RunEventPublisher
  - Polls Redis for distributed cancellation signal at each step boundary
  - Handles HUMAN step pause/resume via Redis keys + ARQ re-enqueue

`resume()` below builds its engine from the orchestration this process already
loaded from the store, rather than reloading a definition it may not have — and
then calls `OrchestrationEngine.resume_paused`, the same method the in-process
`resume()` calls. It used to be a hand copy of that logic instead, and on a
shared fleet *every* resume runs here and none runs there, so each fix to the
classmethod was invisible here until someone found it broken on a fleet: the
human step's history entry never closed, the routing events after an answer
were dropped, and a tool call's approval was treated as an answer and stepped
past (F109). `tests/unit/test_adapter_resume.py` keeps it a call.
"""
from typing import AsyncGenerator

from core.models_orchestration import Orchestration, OrchestrationRun
from core.orchestration.engine import OrchestrationEngine, apply_model_override
from core.scale.pubsub import RunEventPublisher, is_cancelled, clear_cancellation


import asyncio as _asyncio
import contextlib as _contextlib
import functools as _functools

from core.log import get_logger

_log = get_logger(__name__)

#: How often a running job looks for its cancel key. The engine also checks
#: between steps; this is what reaches a step in progress.
CANCEL_POLL_SECONDS = 2.0


def _cancel_guarded(method):
    """Run an engine-driving method under the cancel watcher.

    A cancel used to be a Redis key the engine polled between steps, so a
    run inside a twenty-minute agent step ignored it until the step returned.
    The watcher cancels the task driving the engine — which is the ARQ job
    task itself, since the adapter is awaited inline — the moment the key
    appears. A cancel that is NOT ours (ARQ's own timeout or shutdown) is not
    a request: it propagates, so the job is retried or abandoned as it always
    was.

    A decorator rather than an edit to each method: `test_adapter_resume.py`
    reads `resume` at the source level, and a wrapper is transparent to
    `inspect.getsource`.
    """
    @_functools.wraps(method)
    async def wrapper(self, *args, **kwargs):
        try:
            async with self._cancel_watch():
                return await method(self, *args, **kwargs)
        except _asyncio.CancelledError:
            # We asked for this cancel, so we handle it — and the task must
            # be told so: the outstanding cancellation count (3.11+) is what
            # asyncio.timeout and the redis client's own read timeouts consult,
            # and with it still raised every await in the handler below would
            # be cancelled again.
            task = _asyncio.current_task()
            if task is not None and hasattr(task, "uncancel"):
                task.uncancel()
            if not await self._cancelled_by_request():
                raise
            return await self._finish_cancelled()
    return wrapper


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

    @_cancel_guarded
    async def run(
        self,
        initial_input: str,
        session_id: str | None = None,
        initial_state: dict | None = None,
        images: list[str] | None = None,
    ) -> str:
        """
        Run the orchestration and publish events to Redis.
        Returns the final status string.
        Raises on an error outside the engine; the job dead-letters it (a step
        failure is recorded by the engine and returned as `failed`).

        `images` are the attachments this run starts with, given to steps that
        set `pass_images`. They are set on the engine and deliberately not put
        in `initial_state`: that becomes `shared_state`, which is echoed to the
        browser twice per run, checkpointed at every step boundary, returned by
        the run-detail endpoint and fed to the memory summariser. Fine for a
        model name, not for twenty megabytes of image.

        The in-process route sets `engine.images` directly, which is why this
        parameter did not exist until something ran a builder conversation on a
        worker instead.
        """
        engine = self._build_engine()
        engine.images = list(images or [])
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

    @_cancel_guarded
    async def resume(
        self,
        human_response: dict | str,
        images: list[str] | None = None,
    ) -> str:
        """Resume a paused run after human input.

        What an answer does is decided by `OrchestrationEngine.resume_paused`,
        the same method the in-process `resume()` calls. This method only does
        what is the fleet's own: it builds the engine from `self._orch` (loaded
        from the store by the worker) instead of reloading a definition this
        process may not have, and it publishes what the engine yields.

        `images` are the attachments sent *with the answer*, matching
        `OrchestrationEngine.resume`. The ones the run started with are not
        persisted anywhere, by design, so a step re-entered after a pause sees
        only what the person attached to their reply.
        """
        from core.orchestration.state import SharedState

        # Restore in-progress run state. This reads the store, so the process
        # resuming need not be the one that paused — which is what lets a
        # single shared fleet drain one queue.
        _log.debug(f"[adapter.resume] ▶ restoring checkpoint run_id={self._run_id}")
        restored = await SharedState.restore(self._run_id)
        run = restored.run
        _log.debug(f"[adapter.resume] 📋 restored: status={run.status} current_step_id={run.current_step_id!r} waiting_for_human={run.waiting_for_human} step_history_len={len(run.step_history)}")

        # The model the run was started with, re-applied from the checkpoint.
        # This is the whole reason the override rides in shared_state: the
        # worker resuming is rarely the worker that paused, and it loaded
        # `self._orch` fresh from Postgres with every step's own model.
        apply_model_override(self._orch, run.shared_state.get(OrchestrationEngine.MODEL_OVERRIDE_KEY))

        # Build engine using self._orch (Postgres-loaded) so step_map is always populated.
        engine = self._build_engine()
        engine.images = list(images or [])

        final_status = "completed"
        async for event in engine.resume_paused(run, human_response):
            _log.debug(f"[adapter.resume] 📨 type={event.get('type', '?')} step={event.get('orch_step_id', '')} status={event.get('status', '')}")
            await self._publisher.publish(event)

            if event.get("type") == "orchestration_complete":
                final_status = event.get("status", "completed")

            if event.get("type") == "human_input_required":
                _log.debug(f"[adapter.resume] ⏸ paused again")
                await self._sync_paused_state(event)
                await self._publisher.publish_paused()
                return "paused"

        _log.debug(f"[adapter.resume] ✅ done, calling _sync_final_state(status={final_status})")
        await self._sync_final_state(final_status)
        await self._publisher.publish_done()
        return final_status

    @_cancel_guarded
    async def resume_failed(
        self, *, interrupted: bool = False, images: list[str] | None = None,
    ) -> str:
        """Resume a failed or cancelled run — or, `interrupted`, one whose job was stopped mid-run.

        Raises when the engine refuses or cannot find the orchestration. This
        used to fall through to `completed`, so a refused resume was recorded
        as a finished run that had run nothing.
        """
        final_status = None
        refusal = None

        async for event in OrchestrationEngine.resume_failed(
            run_id=self._run_id,
            server_module=self._server_module,
            interrupted=interrupted,
            images=images,
        ):
            await self._publisher.publish(event)

            if event.get("type") == "orchestration_complete":
                final_status = event.get("status", "completed")
            elif event.get("type") == "orchestration_error" and refusal is None:
                refusal = event.get("error")

            if event.get("type") == "human_input_required":
                await self._sync_paused_state(event)
                await self._publisher.publish_paused()
                return "paused"

        if final_status is None:
            raise RuntimeError(refusal or f"Run {self._run_id} could not be resumed")

        await self._sync_final_state(final_status)
        await self._publisher.publish_done()
        return final_status

    # ------------------------------------------------------------------

    @_contextlib.asynccontextmanager
    async def _cancel_watch(self):
        """Poll for the cancel key and cancel the current task when it appears."""
        driver = _asyncio.current_task()
        stop = _asyncio.Event()

        async def watch():
            while not stop.is_set():
                try:
                    if await is_cancelled(self._redis, self._run_id):
                        driver.cancel()
                        return
                except Exception:  # noqa: BLE001 — a Redis hiccup is not a cancel
                    pass
                with _contextlib.suppress(_asyncio.TimeoutError):
                    await _asyncio.wait_for(stop.wait(), timeout=CANCEL_POLL_SECONDS)

        watcher = _asyncio.create_task(watch())
        try:
            yield
        finally:
            stop.set()
            watcher.cancel()
            with _contextlib.suppress(BaseException):
                await watcher

    async def _cancelled_by_request(self) -> bool:
        try:
            return await is_cancelled(self._redis, self._run_id)
        except Exception:  # noqa: BLE001
            return False

    async def _finish_cancelled(self) -> str:
        """Record and announce a requested cancel the way the in-process pump does."""
        try:
            await self._publisher.publish({"type": "orchestration_error", "error": "Cancelled"})
        except Exception:  # noqa: BLE001
            pass
        # keep_position: a cancelled run resumes from the step it was in,
        # and its mid-step progress is already in its column.
        await self._sync_final_state("cancelled", keep_position=True)
        try:
            await self._publisher.publish_done()
        except Exception:  # noqa: BLE001
            pass
        return "cancelled"

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
        """Update Postgres to reflect the run is paused waiting for human input.

        Not the position: the engine's pause checkpoint already wrote it, and it
        is the step the engine drives — the container, for a question asked
        inside a Loop, a Parallel branch or a nested orchestration. This used to
        overwrite it with the event's `orch_step_id`, the step that asked, left
        over from when this row was a display mirror; so a fleet run resumed
        from inside the loop body as if that step stood alone, and ended there
        (F109). The step that asked is the run's newest `paused` history entry.
        """
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
                        human_prompt=human_event.get("prompt"),
                        human_fields=human_event.get("fields") or [],
                    )
                )
                await session.commit()
        except Exception as e:
            _log.debug(f"[worker_engine_adapter] _sync_paused_state error: {e}")

    async def _sync_final_state(self, status: str = "completed", *, keep_position: bool = False) -> None:
        """Persist the final run status, ended_at, worker attribution, and cost/tokens to Postgres.

        `keep_position` leaves current_step_id alone — a cancelled run is
        resumable from where it stopped, a finished one has nowhere to go. A
        failed run keeps it too: the engine left it on the failed step, and
        clearing it here gave Replay (`resume_failed`) nothing to re-run, so a
        replayed fleet run "completed" having run nothing.
        """
        from datetime import datetime, timezone
        try:
            async with self._session_factory() as session:
                from sqlalchemy import update
                from core.scale.models_db import OrchestrationRunDB
                values: dict = {
                    "status": status,
                    "ended_at": datetime.now(timezone.utc),
                    "waiting_for_human": False,
                }
                if not (keep_position or status == "failed"):
                    values["current_step_id"] = None
                if self._worker_id:
                    values["worker_id"] = self._worker_id
                    values["job_id"] = self._job_id

                # Aggregate cost/token totals from usage_tracker for this run
                try:
                    from core.usage_tracker import get_usage_logs
                    usage_records = await get_usage_logs(run_id=self._run_id, limit=100_000)
                    values["total_tokens_used"] = sum(r.get("total_tokens", 0) for r in usage_records)
                    values["total_cost_usd"] = round(sum(r.get("estimated_cost", 0.0) for r in usage_records), 8)
                    values["cache_read_tokens"] = sum(r.get("cache_read_tokens", 0) for r in usage_records)
                    values["cache_write_tokens"] = sum(r.get("cache_write_tokens", 0) for r in usage_records)
                    values["cache_hit_count"] = sum(1 for r in usage_records if r.get("response_cache_hit"))
                    values["estimated_savings_usd"] = round(sum(r.get("estimated_savings", 0.0) for r in usage_records), 8)
                except Exception as e:
                    _log.debug(f"[worker_engine_adapter] cost aggregation error: {e}")

                await session.execute(
                    update(OrchestrationRunDB)
                    .where(OrchestrationRunDB.run_id == self._run_id)
                    .values(**values)
                )
                await session.commit()
        except Exception as e:
            _log.debug(f"[worker_engine_adapter] _sync_final_state error: {e}")

        # Clear any stale cancel signal
        try:
            await clear_cancellation(self._redis, self._run_id)
        except Exception:
            pass
