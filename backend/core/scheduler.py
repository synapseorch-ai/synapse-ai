"""
Async scheduler -- runs agents/orchestrations on interval or cron schedules.
Started during server lifespan. Checks all enabled schedules every 30 seconds.
Restart-proof: next_run_at is persisted so overdue schedules are handled on startup.
"""
import asyncio
import logging
import uuid
from datetime import datetime, timezone, timedelta

from core.store import schedules as schedule_store

logger = logging.getLogger(__name__)

_TICK_INTERVAL = 30  # seconds between scheduler ticks


def compute_next_run(schedule: dict, from_dt: datetime) -> datetime:
    """
    Compute the next run datetime for a schedule, measured from from_dt.
    Always returns a timezone-aware UTC datetime.
    """
    if schedule.get("schedule_type") == "interval":
        # At least 1: a row saved before F197 checked it could hold -5, a next
        # run in the past, and fire on every tick.
        val = max(schedule.get("interval_value") or 1, 1)
        unit = schedule.get("interval_unit") or "minutes"
        if unit == "minutes":
            delta = timedelta(minutes=val)
        elif unit == "hours":
            delta = timedelta(hours=val)
        else:  # days
            delta = timedelta(days=val)
        return from_dt + delta
    else:
        # cron
        from croniter import croniter
        cron_expr = schedule.get("cron_expression") or "0 * * * *"
        # croniter expects a naive datetime; strip tzinfo for calculation
        naive = from_dt.replace(tzinfo=None)
        cron = croniter(cron_expr, naive)
        next_naive = cron.get_next(datetime)
        return next_naive.replace(tzinfo=timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    """Parse an ISO 8601 string to a timezone-aware UTC datetime."""
    s = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class ScheduleManager:
    """Manages persistent schedule execution with restart-proof state."""

    def __init__(self):
        self._server_module = None
        self._loop_task: asyncio.Task | None = None
        self._running = False
        self._manual_runs: set[asyncio.Task] = set()

    async def start(self, server_module) -> None:
        """Called from server lifespan. Evaluates overdue schedules and starts the loop."""
        self._server_module = server_module
        self._running = True
        # Handle any schedules that were overdue while the server was offline
        try:
            await self._on_startup()
        except Exception as e:
            logger.warning("[Scheduler] Startup check failed: %s", e)
        self._loop_task = asyncio.create_task(self._loop())
        logger.info("[Scheduler] Started (tick every %ds)", _TICK_INTERVAL)

    async def stop(self) -> None:
        """Called from server lifespan shutdown."""
        self._running = False
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        logger.info("[Scheduler] Stopped")

    async def trigger_now(self, schedule_id: str) -> str | None:
        """Run a schedule now, as a test. Returns the run id, or None if there is no such schedule.

        Runs whether or not the schedule is enabled, and leaves `next_run_at` and
        `last_run_at` alone — pressing Run Now is not an occurrence of the
        schedule. It used to fire the tick's path, which returned at once for a
        disabled schedule and moved an enabled one's next run, and it returned a
        run id guessed from the clock rather than the run's (F72). Cloud's Run
        Now behaves this way already.
        """
        s = await schedule_store.get(schedule_id)
        if not s:
            return None
        sched_log = self._schedule_logger(s)
        task = asyncio.create_task(self._run(s, sched_log))
        # A task nobody references can be collected mid-run.
        self._manual_runs.add(task)
        task.add_done_callback(self._manual_runs.discard)
        return sched_log.run_id

    # -- Internal loop ----------------------------------------------------

    async def _loop(self) -> None:
        while self._running:
            await asyncio.sleep(_TICK_INTERVAL)
            try:
                await self._tick()
            except Exception as e:
                logger.warning("[Scheduler] Tick error: %s", e)

    async def _tick(self) -> None:
        """Fire any schedules whose next_run_at has arrived."""
        for s in await schedule_store.due(_utc_now()):
            asyncio.create_task(self._execute_schedule(s["id"]))

    async def _on_startup(self) -> None:
        """
        On server start, evaluate all enabled schedules.
        - Interval schedules overdue ? run immediately
        - Cron schedules overdue ? apply missed_run_policy (run_immediately or skip)
        - Never-run schedules ? compute next_run_at from now (run immediately for interval)
        Writes an updated next_run_at for any schedule that needs recalculation.
        """
        schedules = await schedule_store.load()
        now = _utc_now()

        for s in schedules:
            if not s.get("enabled"):
                continue

            next_run_str = s.get("next_run_at")

            if not next_run_str:
                # Never scheduled before -- compute next_run_at
                if s.get("schedule_type") == "interval":
                    # Interval with no history ? run immediately then set interval from now
                    asyncio.create_task(self._execute_schedule(s["id"]))
                else:
                    # Cron with no history ? compute from now, no immediate run
                    await schedule_store.set_next_run(s["id"], compute_next_run(s, now))
                continue

            try:
                due_dt = _parse_iso(next_run_str)
            except Exception:
                continue

            if due_dt > now:
                # Not yet due -- nothing to do
                continue

            # Overdue
            stype = s.get("schedule_type", "interval")
            if stype == "interval":
                # Always catch up overdue interval schedules
                asyncio.create_task(self._execute_schedule(s["id"]))
            else:
                # Cron -- apply policy
                policy = s.get("missed_run_policy", "skip")
                if policy == "run_immediately":
                    asyncio.create_task(self._execute_schedule(s["id"]))
                else:
                    # Skip: just recalculate next_run_at from now
                    next_dt = compute_next_run(s, now)
                    await schedule_store.set_next_run(s["id"], next_dt)
                    logger.info(
                        "[Scheduler] Missed cron schedule '%s' (policy=skip), next run: %s",
                        s.get("name"), _iso(next_dt),
                    )

    # -- Execution --------------------------------------------------------

    async def _execute_schedule(self, schedule_id: str) -> None:
        """
        Fire-and-forget coroutine: runs one schedule invocation end-to-end.
        Advances next_run_at atomically at the start to prevent double-firing.
        """
        # Step 1: Load schedule and advance next_run_at before running, so a
        # tick arriving mid-run does not fire the same schedule again.
        s = await schedule_store.get(schedule_id)
        if not s or not s.get("enabled"):
            return

        now = _utc_now()
        next_dt = compute_next_run(s, now)
        await schedule_store.set_next_run(schedule_id, next_dt)
        s["next_run_at"] = _iso(next_dt)

        logger.info(
            "[Scheduler] Running schedule '%s' (id=%s), next=%s",
            s.get("name"), schedule_id, s["next_run_at"],
        )

        await self._run(s, self._schedule_logger(s))

        # Record when this occurrence started. One row update — the whole-file
        # rewrite this used to be clobbered any edit made through the API while
        # the run was in flight. Only for an occurrence: Run Now is not one.
        await schedule_store.set_last_run(schedule_id, now)

    @staticmethod
    def _schedule_logger(s: dict):
        from core import schedule_logger
        return schedule_logger.ScheduleLogger(
            schedule_id=s["id"],
            schedule_name=s.get("name", "Unknown"),
            target_type=s.get("target_type", "agent"),
            target_id=s.get("target_id", ""),
            prompt=s.get("prompt", ""),
        )

    async def _run(self, s: dict, sched_log) -> None:
        """Run one schedule invocation end-to-end under `sched_log`'s run id."""
        final_response = ""
        status = "completed"

        try:
            if s.get("target_type") == "agent":
                final_response = await self._run_agent_schedule(s, sched_log)
            else:
                final_response = await self._run_orchestration_schedule(s, sched_log)
            # A run that stopped on a human step is waiting, not finished.
            if getattr(sched_log, "paused", False):
                status = "paused"

        except Exception as e:
            logger.error("[Scheduler] Schedule '%s' failed: %s", s.get("name"), e)
            sched_log.log_event({"type": "error", "message": str(e)})
            status = "error"
        finally:
            sched_log.run_end(status)
            sched_log.close()

        # Messaging notification
        if final_response and status == "completed":
            await self._maybe_notify_messaging(s, final_response)

    async def _run_agent_schedule(self, s: dict, sched_log) -> str:
        """Run agent schedule and return the final response text."""
        from core.react_engine import run_agent_step
        from core.agent_logger import AgentLogger

        session_id = f"schedule_{s['id']}"
        # A schedule's prompt is a member's text: its `@[path]` mentions read
        # the vault, as they do in chat (core/vault.py).
        from core.vault import MentionReader
        prompt, mentions = await MentionReader().expand(s.get("prompt", ""))
        for mention in mentions:
            if mention.warning():
                logger.warning("[Scheduler] %s: vault mention %s", s.get("id"), mention.warning())
        # An Orchestrator agent runs its orchestration, under this schedule run's
        # id so the run id Run Now returns is the run's (F71).
        from core.react_engine import resolve_agent_for_run, run_orchestrator_agent
        try:
            agent = await resolve_agent_for_run(s.get("target_id"))
        except Exception:
            agent = None
        if agent and agent.get("type") == "orchestrator":
            final_response = ""
            error = None
            async for event in run_orchestrator_agent(
                agent, prompt, self._server_module, session_id=session_id, run_id=sched_log.run_id,
            ):
                sched_log.log_event(event)
                if event.get("type") == "final":
                    final_response = event.get("response", "")
                elif event.get("type") in ("orchestration_error", "error"):
                    error = event.get("error") or event.get("message") or "orchestration failed"
                elif event.get("type") == "orchestration_complete" and event.get("status") in ("failed", "cancelled"):
                    error = error or f"orchestration {event.get('status')}"
            if error:
                raise RuntimeError(error)
            return final_response

        agent_log = AgentLogger(
            agent_id=s.get("target_id", ""),
            agent_name=s.get("name", ""),
            session_id=session_id,
            source="schedule",
            user_message=prompt,
        )


        final_response = ""
        # A turn that raises — its model failed (F133) — is logged as failed;
        # `_run` records the schedule run's error and sends no notification.
        outcome = "failed"
        try:
            async for event in run_agent_step(
                message=prompt,
                agent_id=s.get("target_id"),
                session_id=session_id,
                server_module=self._server_module,
                source="schedule",
                run_id=sched_log.run_id,
            ):
                agent_log.log_event(event)
                sched_log.log_event(event)
                if event.get("type") == "final":
                    final_response = event.get("response", "")
            outcome = "completed"
        finally:
            agent_log.run_end(outcome)
            agent_log.close()

        return final_response

    async def _run_orchestration_schedule(self, s: dict, sched_log) -> str:
        """Run orchestration schedule and return the final output text."""
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        from core.orchestration.runner import stream_engine_events
        from core.store.resources import get_orchestration

        orch_data = await get_orchestration(s.get("target_id"))
        if not orch_data:
            raise ValueError(f"Orchestration {s.get('target_id')} not found")

        orch = Orchestration.model_validate(orch_data)
        engine = OrchestrationEngine(orch, self._server_module)

        session_id = f"schedule_{s['id']}"
        final_response = ""
        # Through the runner pump: the run is journaled and visible to the
        # reattach UI / notifications like any UI-started run (issue #356
        # single-task discipline included).
        async for event in stream_engine_events(
            engine.run(s.get("prompt", ""), sched_log.run_id, session_id=session_id),
            sched_log.run_id,
        ):
            sched_log.log_event(event)
            if event.get("type") == "final":
                final_response = event.get("response", "")
            elif event.get("type") == "orchestration_complete":
                # Orchestration final output may come via different event keys
                out = event.get("output") or event.get("result") or event.get("response", "")
                if out and not final_response:
                    final_response = str(out)

        return final_response

    async def _maybe_notify_messaging(self, schedule: dict, response: str) -> None:
        """
        Send schedule completion message to the agent's connected messaging channel.
        Uses notify_chat_id from channel config, or falls back to adapter._last_chat_id.
        Only applies to agent schedules (orchestrations don't have a single bound agent).
        """
        if schedule.get("target_type") != "agent":
            return

        messaging_mgr = getattr(self._server_module, "messaging_manager", None)
        if not messaging_mgr:
            return

        try:
            from core.messaging import store as channel_store
            channels = await channel_store.get_channels_for_agent(schedule.get("target_id", ""))
        except Exception:
            return

        for ch in channels:
            if not ch.get("enabled"):
                continue
            adapter = messaging_mgr._adapters.get(ch["id"])
            if not adapter:
                continue

            # Prefer an explicitly configured notification target; the stored
            # default conversation covers webhook-mode channels and restarts,
            # where the in-memory _last_chat_id is empty.
            chat_id = (
                ch.get("notify_chat_id")
                or ch.get("default_conversation_id")
                or getattr(adapter, "_last_chat_id", None)
            )
            if not chat_id:
                logger.debug(
                    "[Scheduler] No chat_id for channel %s -- skipping notification", ch["id"]
                )
                continue

            try:
                msg = f"[Schedule: {schedule.get('name', 'Unknown')}] Run completed.\n\n{response}"
                await adapter.send_message(chat_id, msg)
                logger.info(
                    "[Scheduler] Sent completion notification to channel %s chat %s",
                    ch["id"], chat_id,
                )
            except Exception as e:
                logger.warning("[Scheduler] Notification failed for channel %s: %s", ch["id"], e)
            break  # notify only first active channel
