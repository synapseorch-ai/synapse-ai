"""
Async scheduler -- runs agents/orchestrations on interval or cron schedules.
Started during server lifespan. Checks all enabled schedules every 30 seconds.
Restart-proof: next_run_at is persisted so overdue schedules are handled on startup.
"""
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from core.store import schedules as schedule_store

logger = logging.getLogger(__name__)

_TICK_INTERVAL = 30  # seconds between scheduler ticks
_BOARD_SWEEP_INTERVAL = 3600  # seconds between board sweeps (BOARD1a)

#: How long a scheduled run's tool call waits for a person before it expires
#: unrun (F268) — as long as a parked AI-builder run waits (`PARKED_BUILDER_TTL`).
APPROVAL_TTL = timedelta(hours=24)
#: Why an expired one was not run, as its conversation and its log keep it.
APPROVAL_EXPIRED = "nobody answered within 24 hours"


def _session_of(schedule_id: str) -> str:
    """The conversation a schedule's agent turns are kept in."""
    return f"schedule_{schedule_id}"


async def schedule_question_settled(pending: dict, *, how: str, why: str) -> None:
    """A schedule's parked call ended without an answer (F268): its bell item and its run's log say so.

    Called by `chat_approval.settle_moved_past` for any door that settles one —
    the schedule firing again, a message typed into its conversation, or the
    expiry sweep.
    """
    from core.chat_approval import questions_of
    from core.notifications import hub
    from core import schedule_logger

    for q in questions_of(pending):   # each of a team's questions is its own item (TEAM1c)
        hub.resolve_approval(q.get("approval_id"), how, question_id=q.get("question_id"))
    sched = pending.get("schedule") or {}
    if not sched.get("run_id"):
        return
    try:
        log = schedule_logger.ScheduleLogger(
            schedule_id=sched.get("id") or "", schedule_name=sched.get("name") or "", target_type="agent",
            target_id="", prompt="", continues=sched["run_id"],
            note=f"`{pending.get('tool_name')}` was not run — {why}.")
        log.run_end("expired")
        log.close()
    except Exception as exc:  # noqa: BLE001 — a log line is not worth failing the settle
        logger.warning("[Scheduler] could not note a settled approval on %s: %s", sched.get("run_id"), exc)


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
        tz = (schedule.get("timezone") or "").strip()
        if not tz or tz.upper() == "UTC":
            # croniter expects a naive datetime; strip tzinfo for calculation
            naive = from_dt.astimezone(timezone.utc).replace(tzinfo=None) if from_dt.tzinfo else from_dt
            cron = croniter(cron_expr, naive)
            next_naive = cron.get_next(datetime)
            return next_naive.replace(tzinfo=timezone.utc)
        return _next_on_local_clock(cron_expr, ZoneInfo(tz), from_dt)


def _next_on_local_clock(cron_expr: str, zone: "ZoneInfo", from_dt: datetime) -> datetime:
    """The cron read on `zone`'s wall clock, in UTC (F948): 4 PM in New York stays 4 PM through the clocks changing.

    The two hours the clocks make awkward: a wall time that does not exist that day (2:30 when they jump from 2:00 to
    3:00) runs at the first minute that does; a wall time that happens twice (1:30 when they fall back) runs once,
    the first time — unless the cron runs every hour (its hour `*` or `*/n`), which runs through the repeated hour as
    it does any other, so "every hour" leaves no hour out (SAI2's re-check).
    """
    from croniter import croniter

    after = (from_dt if from_dt.tzinfo else from_dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    hour = (cron_expr.split() + ["", ""])[1]
    hourly = hour == "*" or hour.startswith("*/")
    # From an hour before, on the wall clock: the repeated hour's second pass has wall times already behind `after`.
    cron = croniter(cron_expr, (after.astimezone(zone) - timedelta(hours=1)).replace(tzinfo=None))
    best: datetime | None = None
    for _ in range(2000):
        wall = cron.get_next(datetime)
        first = _existing(wall, zone).astimezone(timezone.utc)
        if best is not None and first > best:
            return best
        moments = [first]
        second = wall.replace(tzinfo=zone, fold=1)
        if hourly and second.utcoffset() != wall.replace(tzinfo=zone, fold=0).utcoffset():
            moments.append(second.astimezone(timezone.utc))
        for at in moments:
            if at > after and (best is None or at < best):
                best = at
    if best is not None:
        return best
    raise ValueError(f"'{cron_expr}' has no next run in {zone.key}")


def _existing(wall: datetime, zone: "ZoneInfo") -> datetime:
    """A naive wall time on `zone`'s clock as the moment it names: its first occurrence (`fold=0`), or, if the clocks
    skip it, the first minute after it that exists."""
    for minute in range(0, 24 * 60):
        candidate = wall + timedelta(minutes=minute)
        aware = candidate.replace(tzinfo=zone)
        if aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == candidate:
            return aware
    return wall.replace(tzinfo=zone)


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


class TargetGone(Exception):
    """Run Now on a schedule whose target is gone: it was turned off, and this is why (F518)."""


def gone_target_sentence(what: str, kind: str) -> str:
    """Why a schedule was turned off (F468), as the end of a sentence — cloud's notice too.

    "the workflow it runs was deleted, so this schedule was turned off. Point it
    at another workflow, or delete it"
    """
    return (f"{what} was deleted, so this schedule was turned off. "
            f"Point it at another {kind}, or delete it")


async def gone_target(s: dict) -> str | None:
    """Why this schedule's target can never run again, or None (F468).

    A workflow deleted — outright, or kept as a record (`is_active` false) — an
    agent deleted, or a workflow agent whose workflow was. An agent schedule
    naming no agent runs the active one, as it always has.
    """
    from core.store.resources import get_agent, get_orchestration

    def retired(orch: dict | None) -> bool:
        return orch is None or orch.get("is_active", True) is False

    target_id = s.get("target_id") or ""
    if s.get("target_type") == "agent":
        if not target_id:
            return None
        agent = await get_agent(target_id)
        if agent is None:
            return gone_target_sentence("the agent it runs", "agent")
        if agent.get("type") == "orchestrator" and retired(
                await get_orchestration(agent.get("orchestration_id") or "")):
            name = agent.get("name") or target_id
            return gone_target_sentence(f"the workflow its agent '{name}' runs", "workflow")
        return None
    if retired(await get_orchestration(target_id)):
        return gone_target_sentence("the workflow it runs", "workflow")
    return None


class ScheduleManager:
    """Manages persistent schedule execution with restart-proof state."""

    def __init__(self):
        self._server_module = None
        self._loop_task: asyncio.Task | None = None
        self._running = False
        self._manual_runs: set[asyncio.Task] = set()
        #: When the boards were last swept (monotonic); the first tick sweeps.
        self._boards_swept = float("-inf")

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
        # What it targets is gone: turned off and said, as an occurrence is (F468), and refused in
        # the same words. Run Now used to go on and run the active agent for a deleted one (F518).
        gone = await gone_target(s)
        if gone:
            raise TargetGone(await self._turn_off(s, gone))
        sched_log = await self._open_log(s)
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
        try:
            await self._expire_parked_approvals()
        except Exception as e:  # noqa: BLE001 — a failed sweep is retried next tick
            logger.warning("[Scheduler] approval expiry sweep failed: %s", e)
        try:
            # The OpenAI endpoint's parked calls expire on the same tick (OAI1).
            from core import api_approvals
            await api_approvals.expire_parked()
        except Exception as e:  # noqa: BLE001 — a failed sweep is retried next tick
            logger.warning("[Scheduler] API approval expiry sweep failed: %s", e)
        try:
            await self._sweep_boards()
        except Exception as e:  # noqa: BLE001 — a failed sweep is retried next hour
            logger.warning("[Scheduler] board sweep failed: %s", e)

    async def _sweep_boards(self) -> None:
        """Hourly, while Boards are on: closed cards archive after 90 days, and change history past 30 days
        goes (BOARD1a, C6). No query while they are off."""
        from core.boards.config import boards_enabled
        from core.store import boards as board_store

        now = time.monotonic()
        if now - self._boards_swept < _BOARD_SWEEP_INTERVAL or not boards_enabled():
            return
        self._boards_swept = now
        archived, dropped = await board_store.sweep()
        if archived or dropped:
            logger.info("[Scheduler] boards: %d cards archived, %d old history rows dropped", archived, dropped)

    async def _expire_parked_approvals(self) -> None:
        """A scheduled run's tool call nobody answered in `APPROVAL_TTL` expires unrun (F268).

        No model call: the call is not run, the turn that asked is kept in the
        schedule's conversation with the reason, its log ends `expired`, and its
        bell item stops asking.
        """
        from core.chat_approval import settle_moved_past
        from core.store import sessions as _sessions

        now = _utc_now()
        for session_id, agent_id, pending in await _sessions.parked_approvals("schedule_"):
            expires_at = (pending.get("schedule") or {}).get("expires_at")
            if not expires_at:
                continue
            try:
                if _parse_iso(expires_at) > now:
                    continue
            except ValueError:
                continue
            await settle_moved_past(session_id, agent_id, save_as=agent_id, why=APPROVAL_EXPIRED)

    async def _on_startup(self) -> None:
        """
        On server start, evaluate all enabled schedules.
        - Interval schedules overdue → run immediately
        - Cron schedules overdue → apply missed_run_policy (run_immediately or skip)
        - Never-run schedules → compute next_run_at from now (run immediately for interval)
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
                    # Interval with no history → run immediately then set interval from now
                    asyncio.create_task(self._execute_schedule(s["id"]))
                else:
                    # Cron with no history → compute from now, no immediate run
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

        # What it targets is gone (F468): every occurrence would fail, or — an
        # agent's — run another agent. Skipped, said once, and turned off.
        gone = await gone_target(s)
        if gone:
            await self._turn_off(s, gone)
            return

        now = _utc_now()
        next_dt = compute_next_run(s, now)
        await schedule_store.set_next_run(schedule_id, next_dt)
        s["next_run_at"] = _iso(next_dt)

        logger.info(
            "[Scheduler] Running schedule '%s' (id=%s), next=%s",
            s.get("name"), schedule_id, s["next_run_at"],
        )

        await self._run(s, await self._open_log(s))

        # Record when this occurrence started. One row update — the whole-file
        # rewrite this used to be clobbered any edit made through the API while
        # the run was in flight. Only for an occurrence: Run Now is not one.
        await schedule_store.set_last_run(schedule_id, now)

    async def _turn_off(self, s: dict, why: str) -> str:
        """Turn a schedule off with why: in its log and the bell, once (F468). Returns the sentence."""
        from core.notifications import hub

        name = s.get("name") or s["id"]
        sentence = why[:1].upper() + why[1:] + "."
        await schedule_store.save({**s, "enabled": False})
        logger.warning("[Scheduler] Schedule '%s' turned off: %s", name, sentence)
        sched_log = await self._open_log(s)
        sched_log.log_event({"type": "error", "message": sentence})
        sched_log.run_end("skipped")
        sched_log.close()
        hub.publish("schedule_skipped", f"{name} was turned off", body=sentence,
                    data={"schedule_id": s["id"], "schedule_name": name})
        return sentence

    async def _open_log(self, s: dict, *, continues: str | None = None, note: str = ""):
        """The schedule's log, opened once the tenant's variables are read (F1185): a secret among them is then held
        for this task, so the banner's prompt names it rather than carrying it, as a chat's log does (F1184). A run
        started from here inherits the hold."""
        from core import variables
        await variables.load()
        return self._schedule_logger(s, continues=continues, note=note)

    @staticmethod
    def _schedule_logger(s: dict, *, continues: str | None = None, note: str = ""):
        from core import schedule_logger
        return schedule_logger.ScheduleLogger(
            schedule_id=s["id"],
            schedule_name=s.get("name", "Unknown"),
            target_type=s.get("target_type", "agent"),
            target_id=s.get("target_id", ""),
            prompt=s.get("prompt", ""),
            continues=continues,
            note=note,
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

        # The question the last run left, if nobody answered it: this run asks
        # afresh if it needs to (Naveen, 2026-09-28; F247's rule).
        from core.chat_approval import NEWER_RUN, settle_moved_past
        agent_key = s.get("target_id") or "default"
        await settle_moved_past(session_id, agent_key, save_as=agent_key, why=NEWER_RUN)

        final_response = ""
        # A turn that raises — its model failed (F133) — is logged as failed;
        # `_run` records the schedule run's error and sends no notification.
        outcome = "failed"
        try:
            async for event in self._agent_turn_events(s, sched_log.run_id, message=prompt):
                agent_log.log_event(event)
                sched_log.log_event(event)
                if event.get("type") == "final":
                    final_response = event.get("response", "")
            outcome = "completed"
        finally:
            agent_log.run_end(outcome)
            agent_log.close()

        return final_response

    async def _agent_turn_events(self, s: dict, run_id: str, *, message: str,
                                 resume_state: dict | None = None):
        """One scheduled agent turn's events. A tool call that needs approval is
        parked in the schedule's conversation and asked through the bell (F268):
        a schedule has nobody in a chat to see a card, and the pause used to be a
        dead end — "Status: paused" and nowhere to answer."""
        from core.react_engine import run_agent_step

        latest_progress: dict | None = None
        parked = False
        try:
            async for event in run_agent_step(
                message=message,
                agent_id=s.get("target_id"),
                session_id=_session_of(s["id"]),
                server_module=self._server_module,
                source="schedule",
                run_id=run_id,
                resume_state=resume_state,
                progress_events=True,
                # The schedule's own board, whose cards tell this firing what to do; the firing holds its
                # claims (BOARD1a).
                board_scope={"kind": "schedule", "id": s["id"], "holder": run_id},
            ):
                if event.get("type") == "_step_progress":
                    # Engine state for a resume, never an event.
                    latest_progress = {k: v for k, v in event.items() if k != "type"}
                    continue
                if event.get("type") == "human_input_required":
                    parked = True
                if event.get("type") == "human_input_required" and event.get("kind") == "tool_approval":
                    await self._park(s, run_id, event, message=message, progress=latest_progress)
                yield event
        finally:
            if not parked:
                # The firing is over: what it claimed is open for the next one. A parked firing keeps
                # its claims until they lapse.
                from core.boards import resolve as _board_resolve
                await _board_resolve.release(run_id, settings=None, why="the scheduled run ended")

    async def _park(self, s: dict, run_id: str, event: dict, *, message: str, progress: dict | None) -> None:
        """Keep the call and the turn's snapshot, and ask in the bell."""
        from core import tool_policy
        from core.chat_approval import parked_payload
        from core.notifications import hub
        from core.session import _clip_args
        from core.store import sessions as _sessions

        from core.chat_approval import to_ring

        name = s.get("name") or s["id"]
        expires_at = (_utc_now() + APPROVAL_TTL).isoformat()
        pending = parked_payload(event, message=message, progress=progress)
        pending["schedule"] = {"id": s["id"], "name": name, "run_id": run_id, "expires_at": expires_at}
        await _sessions.set_pending_approval(_session_of(s["id"]), s.get("target_id") or "default", pending)
        # One item per question (TEAM1c): a team's members each ask their own; one still waiting from an
        # earlier pause of this turn keeps the item it has.
        for q in to_ring(pending):
            who = q.get("member_name")
            hub.publish(
                "tool_approval", f"{name}: {who} needs approval" if who else f"{name} needs approval",
                body=(f"{'It' if not who else who} wants to run {q.get('tool_name')} — "
                      f"{tool_policy.explain(q.get('category'))}.") if who else
                     f"The agent wants to run {q.get('tool_name')} — {tool_policy.explain(q.get('category'))}.",
                data={"schedule_id": s["id"], "schedule_run_id": run_id, "approval_id": q.get("approval_id"),
                      "question_id": q.get("question_id"), "member_name": who,
                      "tool_name": q.get("tool_name"), "tool_args": _clip_args(q.get("tool_args") or {}),
                      "category": q.get("category"), "expires_at": expires_at},
            )

    async def answer_approval(self, schedule_id: str, *, approve: bool, reason: str = "",
                              approval_id: str | None = None, question_id: str | None = None):
        """Answer a scheduled run's parked tool call and carry the run on (F268).

        Returns the chat turn driving the resumed turn. Raises `LookupError`
        when the schedule is not waiting on that question, and
        `chat_turns.TurnAlreadyRunning` while another turn holds its conversation.
        """
        from core import chat_turns
        from core.chat_approval import find_question
        from core.store import sessions as _sessions

        session_id = _session_of(schedule_id)
        waiting = [(agent_id, pending) for agent_id, pending in await _sessions.pending_approvals(session_id)
                   if pending.get("schedule")]
        if approval_id is not None or question_id is not None:
            waiting = [(a, p) for a, p in waiting
                       if find_question(p, question=question_id, approval_id=approval_id) is not None]
        if len(waiting) != 1:
            raise LookupError("This schedule is not waiting on that approval.")
        agent_id = waiting[0][0]
        # The conversation is claimed before the question is spent, as chat's
        # own approve does: two answers cannot both resume it.
        with chat_turns.claim(session_id, agent_key=agent_id) as turn:
            pending = await _sessions.take_pending_approval(session_id, agent_id)
            asked = find_question(pending, question=question_id, approval_id=approval_id) if pending else None
            if asked is None:
                if pending:
                    await _sessions.set_pending_approval(session_id, agent_id, pending)
                raise LookupError("This schedule is not waiting on that approval.")
            turn.message = pending.get("message") or ""
            turn.start(self.resumed_turn(pending, agent_id, approve=approve, reason=reason, verdicts=[
                {"question_id": asked["question_id"], "approve": approve, "reason": reason}]))
        return turn

    async def resumed_turn(self, pending: dict, agent_id: str, *, approve: bool, reason: str = "",
                           verdicts: list[dict] | None = None):
        """The scheduled turn picked up from its snapshot with the person's answer.

        Under the schedule's own source and run id — the remember gate, the
        memory trace and Usage's per-schedule rows read them — and in the same
        run's log. SSE frames, for whoever reads its chat turn (the schedule's
        conversation on the chat page).
        """
        from core.agent_logger import AgentLogger
        from core.chat_approval import questions_of, resume_state_for, settle_resumed
        from core.notifications import hub
        from core.store import sessions as _sessions

        sched = pending.get("schedule") or {}
        s = await schedule_store.get(sched.get("id") or "") or {}
        s = {**s, "id": sched.get("id") or s.get("id") or "", "name": sched.get("name") or s.get("name") or "",
             "target_type": "agent", "target_id": agent_id}
        run_id = sched.get("run_id") or ""
        asked = {q["question_id"]: q for q in questions_of(pending)}
        verdicts = verdicts or [{"question_id": next(iter(asked), None), "approve": approve, "reason": reason}]
        for verdict in verdicts:
            q = asked.get(verdict.get("question_id")) or {}
            hub.resolve_approval(q.get("approval_id"), "allowed" if verdict.get("approve") else "refused",
                                 question_id=q.get("question_id"))
        message = pending.get("message") or ""
        said = " ".join(
            (f"A person allowed `{q.get('tool_name')}`." if v.get("approve") else
             f"A person refused `{q.get('tool_name')}`" + (f": {v['reason']}." if v.get("reason") else "."))
            for v in verdicts for q in [asked.get(v.get("question_id")) or {}])
        sched_log = await self._open_log(s, continues=run_id, note=said)
        agent_log = AgentLogger(agent_id=agent_id, agent_name=s["name"], session_id=_session_of(s["id"]),
                                source="schedule", user_message=message)
        status, final_response = "completed", ""
        try:
            async for event in self._agent_turn_events(
                s, run_id, message=message,
                resume_state=resume_state_for(pending, approve=approve, reason=reason, verdicts=verdicts),
            ):
                agent_log.log_event(event)
                sched_log.log_event(event)
                if event.get("type") == "final":
                    final_response = event.get("response", "")
                    event = {"type": "response", "content": final_response, "intent": event.get("intent", "chat")}
                yield f"data: {json.dumps(event, default=str)}\n\n"
            if sched_log.paused:
                status = "paused"
        except Exception as e:  # noqa: BLE001 — recorded as the run's error, as `_run` does
            logger.error("[Scheduler] resumed turn of '%s' failed: %s", s.get("name"), e)
            sched_log.log_event({"type": "error", "message": str(e)})
            status = "error"
            yield f"data: {json.dumps({'type': 'error', 'message': 'The scheduled turn failed.'})}\n\n"
        finally:
            sched_log.run_end(status)
            sched_log.close()
            agent_log.run_end("completed" if status != "error" else "failed")
            agent_log.close()
            # A team's other questions (TEAM1c): asked again if it paused again, else no longer asked.
            again = await _sessions.get_pending_approval(_session_of(s["id"]), agent_id) if status == "paused" else None
            settle_resumed(pending, again, lambda q: hub.resolve_approval(
                q.get("approval_id"), "expired", question_id=q.get("question_id")))
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
        if status == "completed":
            hub.publish("run_completed", f"{s['name']} completed", body=final_response[:500],
                        data={"schedule_id": s["id"], "schedule_run_id": run_id})
            if final_response:
                await self._maybe_notify_messaging(s, final_response)

    async def _run_orchestration_schedule(self, s: dict, sched_log) -> str:
        """Run orchestration schedule and return the final output text."""
        from core.orchestration.engine import OrchestrationEngine
        from core.orchestration.read_shapes import load_to_start
        from core.orchestration.runner import stream_engine_events
        from core.store.resources import get_orchestration

        orch_data = await get_orchestration(s.get("target_id"))
        if not orch_data:
            raise ValueError(f"Orchestration {s.get('target_id')} not found")

        orch = load_to_start(orch_data)
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
