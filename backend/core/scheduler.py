"""
Async scheduler -- runs agents/orchestrations on interval or cron schedules.
Started during server lifespan. Checks all enabled schedules every 30 seconds.
Restart-proof: next_run_at is persisted so overdue schedules are handled on startup.
"""
import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_TICK_INTERVAL = 30  # seconds between scheduler ticks

# Lazy import of JsonStore to avoid circular imports at module load
_DATA_DIR = Path(__file__).parent.parent / "data"
_SCHEDULES_FILE = str(_DATA_DIR / "schedules.json")


def _load_schedules() -> list[dict]:
    from core.json_store import JsonStore
    store = JsonStore(_SCHEDULES_FILE, default_factory=list)
    return store.load()


def _save_schedules(schedules: list[dict]) -> None:
    from core.json_store import JsonStore
    store = JsonStore(_SCHEDULES_FILE, default_factory=list)
    store.save(schedules)


def compute_next_run(schedule: dict, from_dt: datetime) -> datetime:
    """
    Compute the next run datetime for a schedule, measured from from_dt.
    Always returns a timezone-aware UTC datetime.
    """
    if schedule.get("schedule_type") == "interval":
        val = schedule.get("interval_value") or 1
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

    async def trigger_now(self, schedule_id: str) -> str:
        """Manually trigger a schedule immediately. Returns the run_id."""
        # We return the future run_id by peeking at what the logger will generate.
        # The actual run_id is set inside _execute_schedule, so we just fire it.
        task = asyncio.create_task(self._execute_schedule(schedule_id))
        # Give the coroutine a moment to create the logger and set run_id
        # But since we can't easily get the run_id back from a fire-and-forget task,
        # we generate a predictable one for the caller.
        short_id = schedule_id.replace("sched_", "") if schedule_id.startswith("sched_") else schedule_id
        run_id = f"schedulerun_{short_id}_{int(time.time() * 1000)}"
        return run_id

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
        schedules = _load_schedules()
        now = _utc_now()
        for s in schedules:
            if not s.get("enabled"):
                continue
            next_run = s.get("next_run_at")
            if not next_run:
                continue
            try:
                due_dt = _parse_iso(next_run)
            except Exception:
                continue
            if due_dt <= now:
                asyncio.create_task(self._execute_schedule(s["id"]))

    async def _on_startup(self) -> None:
        """
        On server start, evaluate all enabled schedules.
        - Interval schedules overdue ? run immediately
        - Cron schedules overdue ? apply missed_run_policy (run_immediately or skip)
        - Never-run schedules ? compute next_run_at from now (run immediately for interval)
        Saves updated next_run_at values for any schedules that need recalculation.
        """
        schedules = _load_schedules()
        now = _utc_now()
        changed = False

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
                    next_dt = compute_next_run(s, now)
                    s["next_run_at"] = _iso(next_dt)
                    changed = True
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
                    s["next_run_at"] = _iso(next_dt)
                    changed = True
                    logger.info(
                        "[Scheduler] Missed cron schedule '%s' (policy=skip), next run: %s",
                        s.get("name"), s["next_run_at"],
                    )

        if changed:
            _save_schedules(schedules)

    # -- Execution --------------------------------------------------------

    async def _execute_schedule(self, schedule_id: str) -> None:
        """
        Fire-and-forget coroutine: runs one schedule invocation end-to-end.
        Advances next_run_at atomically at the start to prevent double-firing.
        """
        # Step 1: Load schedule and atomically advance next_run_at
        schedules = _load_schedules()
        s = next((x for x in schedules if x["id"] == schedule_id), None)
        if not s or not s.get("enabled"):
            return

        now = _utc_now()
        next_dt = compute_next_run(s, now)
        s["next_run_at"] = _iso(next_dt)
        _save_schedules(schedules)

        logger.info(
            "[Scheduler] Running schedule '%s' (id=%s), next=%s",
            s.get("name"), schedule_id, s["next_run_at"],
        )

        # Step 2: Create logger
        from core.schedule_logger import ScheduleLogger
        sched_log = ScheduleLogger(
            schedule_id=s["id"],
            schedule_name=s.get("name", "Unknown"),
            target_type=s.get("target_type", "agent"),
            target_id=s.get("target_id", ""),
            prompt=s.get("prompt", ""),
        )

        started_at = _iso(now)
        final_response = ""
        status = "completed"

        try:
            if s.get("target_type") == "agent":
                final_response = await self._run_agent_schedule(s, sched_log)
            else:
                final_response = await self._run_orchestration_schedule(s, sched_log)

        except Exception as e:
            logger.error("[Scheduler] Schedule '%s' failed: %s", s.get("name"), e)
            sched_log.log_event({"type": "error", "message": str(e)})
            status = "error"
        finally:
            sched_log.run_end(status)
            sched_log.close()

        # Step 3: Update last_run_at in store
        schedules = _load_schedules()
        for entry in schedules:
            if entry["id"] == schedule_id:
                entry["last_run_at"] = started_at
                break
        _save_schedules(schedules)

        # Step 4: Messaging notification
        if final_response and status == "completed":
            await self._maybe_notify_messaging(s, final_response)

    async def _run_agent_schedule(self, s: dict, sched_log) -> str:
        """Run agent schedule and return the final response text."""
        from core.react_engine import run_agent_step
        from core.agent_logger import AgentLogger

        session_id = f"schedule_{s['id']}"
        agent_log = AgentLogger(
            agent_id=s.get("target_id", ""),
            agent_name=s.get("name", ""),
            session_id=session_id,
            source="schedule",
            user_message=s.get("prompt", ""),
        )

        final_response = ""
        try:
            async for event in run_agent_step(
                message=s.get("prompt", ""),
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
        finally:
            agent_log.run_end("completed")
            agent_log.close()

        return final_response

    async def _run_orchestration_schedule(self, s: dict, sched_log) -> str:
        """Run orchestration schedule and return the final output text."""
        from core.json_store import JsonStore
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine

        orch_store = JsonStore(str(_DATA_DIR / "orchestrations.json"), default_factory=list)
        orchs = orch_store.load()
        orch_data = next((o for o in orchs if o["id"] == s.get("target_id")), None)
        if not orch_data:
            raise ValueError(f"Orchestration {s.get('target_id')} not found")

        orch = Orchestration.model_validate(orch_data)
        engine = OrchestrationEngine(orch, self._server_module)

        session_id = f"schedule_{s['id']}"
        final_response = ""
        async for event in engine.run(s.get("prompt", ""), sched_log.run_id, session_id=session_id):
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
            channels = channel_store.get_channels_for_agent(schedule.get("target_id", ""))
        except Exception:
            return

        for ch in channels:
            if not ch.get("enabled"):
                continue
            adapter = messaging_mgr._adapters.get(ch["id"])
            if not adapter:
                continue

            # Prefer an explicitly configured notification target
            chat_id = ch.get("notify_chat_id") or getattr(adapter, "_last_chat_id", None)
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
