"""
ARQ worker definition.
Defines the job functions and WorkerSettings class consumed by `arq run_worker`.
Entry point: backend/worker_main.py
"""
import asyncio
import contextlib
import functools
import json
import os
import socket
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any

from arq import ArqRedis
from arq.connections import RedisSettings

from core import settings_runtime
from core.scale import mcp_pool
from core.scale.pubsub import clear_chat_cancellation, is_chat_cancelled
from core.scale.config import QUEUE_NAME, get_scale_config
from core.tenancy import get_tenant, tenant_scope
from core.telemetry import queue as _tqueue

from core.log import get_logger

_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Shared worker context (populated in on_startup, used in job functions)
# ---------------------------------------------------------------------------

_worker_id: str = ""
_worker_address: str = ""
_active_jobs: int = 0        # approximate counter updated by jobs


#: What a chat turn handed out again says instead of running again (F89).
INTERRUPTED_TURN = (
    "This reply was interrupted by a restart before it finished, and was not run again: "
    "anything it had already started may have happened. Send your message again if you "
    "still want it."
)

#: Why a workflow a chat turn was running stopped with it, when a stopping
#: worker cut the turn (F424): a chat turn is not run again (F89), so neither is
#: its run.
TURN_RUN_STOPPED = (
    "Stopped: the worker running this chat turn shut down before the workflow finished, and a "
    "chat turn is not run again. Send the message again if you still want it."
)

#: How long a stopping worker spends telling a chat turn it cut (F424). ARQ
#: drops the job's claim only after this, and the grace left after ARQ's wait
#: is short — fifteen seconds on ECS and compose.
CUT_TELL_SECONDS = 8.0

#: A chat turn told at its cut (F424), so its next delivery says and saves
#: nothing more. Kept as long as ARQ keeps its count of the job's deliveries.
CHAT_TOLD_KEY = "synapse:chat-told:{job_id}"
CHAT_TOLD_TTL = 88_400


def job_retries(ctx: dict) -> int:
    """How many deliveries a job gets to do its work: `WORKER_MAX_RETRIES` (3).

    An embedder with its own setting puts it in the worker's `ctx` as
    `job_retries` at startup. The worker's ARQ `max_tries` is one more than
    this: ARQ fails a job past `max_tries` without calling it, so the extra
    delivery is the one on which the job records that it gave up (F89).
    """
    configured = ctx.get("job_retries")
    return int(configured) if configured else int(os.getenv("WORKER_MAX_RETRIES", "3"))


def _get_active_jobs() -> int:
    return _active_jobs


def _for_the_jobs_tenant(fn):
    """Establish the job's tenant around the handler, if an embedder said how.

    The tenant has to be in the context before the body runs: `_load_job_settings`
    reads it, `mcp_pool.acquire()` keys on it, and the run row is written with
    it. A decorator rather than a line inside each handler so there is one place
    it can be got wrong, and so `functools.wraps` keeps the signature the four
    handlers advertise — `tests/unit/test_tenancy.py` inspects it to assert none
    of them takes a `tenant_id`, and that has to keep meaning something.

    In the shipped single-tenant product no resolver is registered, this returns
    None, and the handler runs exactly as before.
    """
    @functools.wraps(fn)
    async def wrapper(ctx: dict, *args, **kwargs):
        from core.scale.context import resolve_job_tenant

        # The job's span, from the trace its envelope carried (a no-op when
        # telemetry is off, or when an embedder's own wrapper started one).
        with _tqueue.job_span(ctx, fn.__name__, kwargs):
            tenant = await resolve_job_tenant(ctx, fn.__name__, args, kwargs)
            if tenant is None:
                return await fn(ctx, *args, **kwargs)
            with tenant_scope(tenant):
                return await fn(ctx, *args, **kwargs)

    return wrapper


async def _load_job_settings() -> None:
    """Read the current tenant's settings into the job's context.

    Called at the top of every job function, after the tenant is established.
    Once per job rather than per step: a long run would otherwise re-read the
    database at every `load_settings()` call, of which there are ~16 per step —
    hence `force=True`, which skips the staleness check the HTTP path wants and
    always reads.

    The machinery lives in `core.settings_runtime`; this is just where a job
    happens to be the boundary. `load_settings()` stays synchronous either way.
    """
    from core import settings_runtime

    await settings_runtime.bind(force=True)


# ---------------------------------------------------------------------------
# Job: run an orchestration
# ---------------------------------------------------------------------------

@_for_the_jobs_tenant
async def run_orchestration_job(
    ctx: dict,
    run_id: str,
    orch_id: str,
    initial_input: str,
    session_id: str | None = None,
    initial_state: dict | None = None,
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
    api_key_id: str | None = None,
    images: list | None = None,
    image_refs: list | None = None,
) -> dict:
    """Pull an orchestration definition from the store, run it, publish events.

    `images` are the attachments this run starts with — see
    `WorkerEngineAdapter.run` for why they travel on the engine and never in
    `initial_state`. `image_refs` are the enqueuer's own names for them (cloud:
    its attachment keys), kept on the run in their place for after a pause
    (F395); without them the run keeps the images in the blob store.

    There is deliberately no `tenant_id` parameter. It used to be taken from
    the job payload and written straight onto the run row, which meant any
    caller that could enqueue a job could label its run with any tenant it
    liked. The tenant now comes from the execution context — see
    core/tenancy.py — which an embedder establishes around the call and which
    nothing in the shipped single-tenant product can change.
    """
    global _active_jobs
    _active_jobs += 1
    cfg = get_scale_config()
    await _load_job_settings()

    redis = ctx["redis"]
    session_factory = ctx["session_factory"]
    leased = False

    try:
        if await _gave_up(ctx, "run_orchestration_job", run_id, orch_id):
            return {"status": "failed", "run_id": run_id}

        # This tenant's MCP session set, from the bounded pool. It merges the
        # process-wide native servers with the tenant's own, so what the engine
        # sees is the shape `ctx["server_module"]` used to be — except that it
        # is this tenant's, not whoever's was current at worker startup.
        server_module = await mcp_pool.acquire()
        leased = True

        # A second try means the first was stopped mid-run (see `_redelivered`).
        # Decided before the upsert below, which would mark a finished run
        # `running` again.
        redelivery = "fresh"
        if ctx.get("job_try", 1) > 1:
            prior = await _load_run_row(session_factory, run_id)
            redelivery = _redelivered(prior)
            _log.info(
                f"[worker] run {run_id}: job handed out again (try {ctx['job_try']}) — "
                f"{redelivery}" + (f" from step {prior.current_step_id}" if redelivery == "resume" else "")
            )
            if redelivery == "done":
                if webhook_url and prior.status != "paused":
                    await _deliver_webhook_for_run(
                        run_id=run_id,
                        session_factory=session_factory,
                        webhook_url=webhook_url,
                        webhook_secret=webhook_secret,
                    )
                return {"status": prior.status, "run_id": run_id}

        # Mark run as picked up in Postgres (UPSERT so DLQ retries work even
        # without a pre-existing row created by the V2 API enqueue path).
        async with session_factory() as session:
            from core.store import upsert
            from core.store.models import OrchestrationRunDB
            now = datetime.now(timezone.utc)
            await upsert(
                session,
                OrchestrationRunDB,
                values={
                    "run_id": run_id,
                    "orchestration_id": orch_id,
                    "status": "running",
                    "worker_id": _worker_id,
                    "job_id": ctx.get("job_id", ""),
                    "tenant_id": get_tenant(),
                    "started_at": now,
                },
                index_elements=["run_id"],
                # A retry must not rewrite the tenant or the start time.
                update=["worker_id", "job_id", "status"],
            )
            await session.commit()

        # Load orchestration definition from Postgres. A run the first try
        # started carries on with its own copy of it (F443).
        orch = await _load_orchestration(session_factory, orch_id, prior if redelivery == "resume" else None)

        # Publish worker_picked_up event so SSE clients know execution started
        from core.scale.pubsub import RunEventPublisher
        publisher = RunEventPublisher(redis, run_id, ttl=cfg.pubsub_event_ttl)
        await publisher.publish({
            "type": "worker_picked_up",
            "worker_id": _worker_id,
            "run_id": run_id,
        })

        # Run the orchestration
        from core.scale.worker_engine_adapter import WorkerEngineAdapter
        adapter = WorkerEngineAdapter(
            orch=orch,
            run_id=run_id,
            worker_server_module=server_module,
            publisher=publisher,
            session_factory=session_factory,
            redis_client=redis,
            worker_id=_worker_id,
            job_id=ctx.get("job_id", ""),
        )
        if redelivery == "resume":
            final_status = await adapter.resume_failed(interrupted=True, images=images)
        else:
            final_status = await adapter.run(
                initial_input=initial_input,
                session_id=session_id,
                initial_state=initial_state,
                images=images,
                image_refs=image_refs,
            )

        # Deliver webhook if requested
        if webhook_url and final_status != "paused":
            await _deliver_webhook_for_run(
                run_id=run_id,
                session_factory=session_factory,
                webhook_url=webhook_url,
                webhook_secret=webhook_secret,
            )

        return {"status": final_status, "run_id": run_id}

    except Exception as exc:
        # The engine records a step's failure itself and the job ends normally,
        # so reaching here means the job died outside it: the orchestration is
        # gone, the store or Redis failed. ARQ does not retry an exception —
        # only `Retry` or a cancellation — so this is the job's last try.
        await _dead_job(ctx, "run_orchestration_job", run_id, orch_id, exc)
        raise

    finally:
        if leased:
            await mcp_pool.release()
        _active_jobs = max(0, _active_jobs - 1)


# ---------------------------------------------------------------------------
# Job: resume after human input
# ---------------------------------------------------------------------------

@_for_the_jobs_tenant
async def resume_orchestration_job(
    ctx: dict,
    run_id: str,
    human_response: dict | str,
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
    images: list | None = None,
    pause_ordinal: int | None = None,
) -> dict:
    """Resume a paused orchestration run after human input is submitted.

    `images` are the attachments sent with the answer, not the ones the run
    started with — see `WorkerEngineAdapter.resume`. `pause_ordinal` is the
    question the answer was given for (`pauses.pause_ordinal`); a job without
    one was enqueued before answers carried it and is applied as it was.
    """
    global _active_jobs
    _active_jobs += 1
    cfg = get_scale_config()
    await _load_job_settings()

    redis = ctx["redis"]
    session_factory = ctx["session_factory"]
    leased = False
    orch_id: str | None = None

    try:
        if await _gave_up(ctx, "resume_orchestration_job", run_id, None):
            return {"status": "failed", "run_id": run_id}

        # This tenant's MCP session set, from the bounded pool. It merges the
        # process-wide native servers with the tenant's own, so what the engine
        # sees is the shape `ctx["server_module"]` used to be — except that it
        # is this tenant's, not whoever's was current at worker startup.
        server_module = await mcp_pool.acquire()
        leased = True
        _log.debug(f"[resume] ▶ job picked up run_id={run_id} human_response_keys={list(human_response.keys()) if isinstance(human_response, dict) else type(human_response).__name__}")

        # Load run to find orchestration_id
        async with session_factory() as session:
            from core.scale.models_db import OrchestrationRunDB
            from sqlalchemy import select
            result = await session.execute(
                select(OrchestrationRunDB).where(OrchestrationRunDB.run_id == run_id)
            )
            row = result.scalar_one_or_none()
            if not row:
                raise ValueError(f"Run {run_id} not found in Postgres")
            orch_id = row.orchestration_id
            _log.debug(f"[resume] 📋 loaded run from DB: orch_id={orch_id} status={row.status} current_step_id={row.current_step_id} waiting_for_human={row.waiting_for_human}")

        # A second try whose first got as far as checkpointing past the human
        # step must not apply the answer again: `adapter.resume` writes it into
        # whatever step the run is on and skips that step. While the answer has
        # not reached a checkpoint the run still looks paused, and the answer
        # is applied as the first try would have.
        redelivery = "answer"
        if ctx.get("job_try", 1) > 1 and not _awaits_answer(row):
            redelivery = _redelivered(row)
            _log.info(
                f"[worker] run {run_id}: answer job handed out again (try {ctx['job_try']}) — "
                f"{redelivery}" + (f" from step {row.current_step_id}" if redelivery == "resume" else "")
            )
            if redelivery == "done":
                return {"status": row.status, "run_id": run_id}
        elif _answers_another_question(row, pause_ordinal):
            # The question this answer was given for has been answered: by
            # this job's own first try, which then reached the next question
            # (F111), or by a second answer to it — the app and a channel can
            # both answer before either job runs. The run waits for its own.
            _log.info(
                f"[worker] run {run_id}: answer for question {pause_ordinal} not applied — "
                f"the run is {row.status}, asking {_asking_ordinal(row)}"
            )
            return {"status": row.status, "run_id": run_id, "applied": False}

        # The workflow as the run started it, not as it is saved now (F443): an
        # edit made while the run waited on this answer is for the runs after it.
        orch = await _load_orchestration(session_factory, orch_id, row)
        _log.debug(f"[resume] 📦 orchestration loaded from Postgres: id={orch.id} name={orch.name} steps={len(orch.steps)}")

        from core.scale.pubsub import RunEventPublisher
        publisher = RunEventPublisher(redis, run_id, ttl=cfg.pubsub_event_ttl)

        if redelivery == "resume":
            # No answer to apply — the first try's already reached a checkpoint
            # — so nothing to claim: this try carries the run on from there.
            async with session_factory() as session:
                from sqlalchemy import update
                from core.scale.models_db import OrchestrationRunDB
                await session.execute(
                    update(OrchestrationRunDB)
                    .where(OrchestrationRunDB.run_id == run_id)
                    .values(
                        status="running",
                        waiting_for_human=False,
                        worker_id=_worker_id,
                        job_id=ctx.get("job_id", ""),
                    )
                )
                await session.commit()
        else:
            # The run is this answer's only if this UPDATE is the one that takes
            # it off the question (F136): two answers picked up in one poll both
            # got this far, and both used to drive the engine.
            from core.orchestration.state_store import claim_answer
            if not await claim_answer(
                session_factory, run_id,
                pause_ordinal=pause_ordinal,
                job_id=ctx.get("job_id", ""),
                worker_id=_worker_id,
                redelivery=ctx.get("job_try", 1) > 1,
            ):
                status = await _status_of_run(session_factory, run_id)
                _log.info(
                    f"[worker] run {run_id}: answer for question {pause_ordinal} not applied — "
                    f"another answer to it is being applied (the run is {status})"
                )
                return {"status": status, "run_id": run_id, "applied": False}
        _log.debug(f"[resume] ✅ DB status set to 'running', waiting_for_human=False")
        await _answer_claimed(ctx, run_id)

        await publisher.publish({"type": "worker_picked_up", "worker_id": _worker_id, "run_id": run_id})

        from core.scale.worker_engine_adapter import WorkerEngineAdapter
        adapter = WorkerEngineAdapter(
            orch=orch,
            run_id=run_id,
            worker_server_module=server_module,
            publisher=publisher,
            session_factory=session_factory,
            redis_client=redis,
            worker_id=_worker_id,
            job_id=ctx.get("job_id", ""),
        )
        if redelivery == "resume":
            final_status = await adapter.resume_failed(interrupted=True, images=images)
        else:
            _log.debug(f"[resume] 🔄 calling adapter.resume() ...")
            final_status = await adapter.resume(human_response=human_response, images=images)
        _log.debug(f"[resume] 🏁 adapter.resume() returned final_status={final_status}")

        if webhook_url and final_status != "paused":
            await _deliver_webhook_for_run(
                run_id=run_id,
                session_factory=session_factory,
                webhook_url=webhook_url,
                webhook_secret=webhook_secret,
            )

        return {"status": final_status, "run_id": run_id}

    except Exception as exc:
        await _dead_job(ctx, "resume_orchestration_job", run_id, orch_id, exc)
        raise

    finally:
        if leased:
            await mcp_pool.release()
        _active_jobs = max(0, _active_jobs - 1)


# ---------------------------------------------------------------------------
# Job: resume a failed run
# ---------------------------------------------------------------------------

@_for_the_jobs_tenant
async def resume_failed_job(ctx: dict, run_id: str) -> dict:
    """Retry a failed or cancelled orchestration run from its last checkpoint."""
    global _active_jobs
    _active_jobs += 1
    cfg = get_scale_config()
    await _load_job_settings()

    redis = ctx["redis"]
    session_factory = ctx["session_factory"]
    leased = False

    orch_id: str | None = None

    try:
        if await _gave_up(ctx, "resume_failed_job", run_id, None):
            return {"status": "failed", "run_id": run_id}

        # This tenant's MCP session set, from the bounded pool. It merges the
        # process-wide native servers with the tenant's own, so what the engine
        # sees is the shape `ctx["server_module"]` used to be — except that it
        # is this tenant's, not whoever's was current at worker startup.
        server_module = await mcp_pool.acquire()
        leased = True
        async with session_factory() as session:
            from core.scale.models_db import OrchestrationRunDB
            from sqlalchemy import select
            result = await session.execute(
                select(OrchestrationRunDB).where(OrchestrationRunDB.run_id == run_id)
            )
            row = result.scalar_one_or_none()
            if not row:
                raise ValueError(f"Run {run_id} not found in Postgres")
            orch_id = row.orchestration_id

            # Name this worker as the run's owner now, as the other two jobs do at
            # pickup. The adapter writes it again when the run ends; until then the
            # row named the worker of the attempt that failed, and the API's boot
            # sweep would fail a replay in progress under the worker running it (F85).
            from sqlalchemy import update
            await session.execute(
                update(OrchestrationRunDB)
                .where(OrchestrationRunDB.run_id == run_id)
                .values(worker_id=_worker_id, job_id=ctx.get("job_id", ""))
            )
            await session.commit()

        # A second try: the first set the run `running` before its first step,
        # which the engine only resumes when told the attempt that did so is
        # gone. A run that got as far as finishing or pausing stays that way.
        interrupted = ctx.get("job_try", 1) > 1
        if interrupted:
            _log.info(f"[worker] run {run_id}: resume job handed out again (try {ctx['job_try']}), status {row.status}")
            if row.status in ("completed", "paused"):
                return {"status": row.status, "run_id": run_id}

        # A person's Replay runs the workflow as saved now; a try handed out
        # again carries on with the run's copy — the engine's own rule (F443).
        orch = await _load_orchestration(session_factory, orch_id, row if interrupted else None)

        from core.scale.pubsub import RunEventPublisher
        publisher = RunEventPublisher(redis, run_id, ttl=cfg.pubsub_event_ttl)

        from core.scale.worker_engine_adapter import WorkerEngineAdapter
        adapter = WorkerEngineAdapter(
            orch=orch,
            run_id=run_id,
            worker_server_module=server_module,
            publisher=publisher,
            session_factory=session_factory,
            redis_client=redis,
            worker_id=_worker_id,
            job_id=ctx.get("job_id", ""),
        )
        final_status = await adapter.resume_failed(interrupted=interrupted)
        return {"status": final_status, "run_id": run_id}

    except Exception as exc:
        await _dead_job(ctx, "resume_failed_job", run_id, orch_id, exc)
        raise

    finally:
        if leased:
            await mcp_pool.release()
        _active_jobs = max(0, _active_jobs - 1)


#: How often a running chat turn looks for its stop signal. Matches the run
#: watcher's interval — the person is waiting either way.
CHAT_CANCEL_POLL_SECONDS = 2.0


async def _run_chat_turn_cancellably(redis, session_id: str, consume) -> bool:
    """Run `consume()` under a cancel watcher. True if it was stopped.

    Lifted from `WorkerEngineAdapter._cancel_guarded`, which does this for
    orchestration runs, and kept deliberately close to it: the polling
    interval, the `uncancel()` and the "was this cancel ours?" question are the
    parts that are easy to get subtly wrong.

    `task.uncancel()` is the subtle one. Without it the outstanding
    cancellation count stays raised, and every `await` in the caller's cleanup
    — persisting the partial turn, publishing the terminal event — is cancelled
    again the moment it starts, so a stopped turn would leave exactly the silent
    dead stream that stopping it is meant to avoid.

    A cancel that is not ours (an ARQ timeout, a worker shutting down) is not a
    request: it propagates, and the job is retried or abandoned as it always was.
    """
    driver = asyncio.create_task(consume())
    stop = asyncio.Event()

    async def watch():
        while not stop.is_set():
            try:
                if await is_chat_cancelled(redis, session_id):
                    driver.cancel()
                    return
                await _stop_asked_helpers(redis, session_id)
            except Exception:  # noqa: BLE001 — a Redis hiccup is not a cancel
                pass
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=CHAT_CANCEL_POLL_SECONDS)

    watcher = asyncio.create_task(watch())
    try:
        await driver
        return False
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None and hasattr(task, "uncancel"):
            task.uncancel()
        if not await is_chat_cancelled(redis, session_id):
            raise
        return True
    finally:
        stop.set()
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


async def _stop_asked_helpers(redis, session_id: str) -> None:
    """Stop the helpers a person stopped from another process, that this turn waits for (SUB1b).

    Their parent's job runs them, in whichever worker has it, so a Stop is a
    Redis set the job's watcher reads — only while a helper works here.
    """
    from core import helpers

    if not helpers.live_in(session_id):
        return
    from core.scale.pubsub import take_helper_stops

    for helper_id in await take_helper_stops(redis, session_id):
        found = helpers.find(helper_id, session_id)
        if found is not None:
            found.stop_requested = True


#: What a background helper a restart cut short says, in place of the result it never had (SUB1a's words).
HELPER_CUT_SHORT = "It stopped when the server restarted, before it finished."


async def _run_background_helper(ctx: dict, session_id: str, spec: dict, *, chat_turn: str | None) -> dict:
    """A background helper's own job (SUB1b): `run_agent_chat_job(helper=…)`.

    It runs on its own conversation (`helperc_…`, whose row the host wrote when it
    started it) and its own stream, which its lane reads; Stop is that
    conversation's cancel. However it ends, its row says so, and its result goes
    back once to the agent that sent it (`helpers.report`: the host's follow-up) —
    never one a person stopped. Handed out again after a cut, it does not run
    again: it says it stopped, as the in-process boot sweep does.
    """
    from core import helpers
    from core.scale import drain, mcp_needs
    from core.scale.pubsub import ChatEventPublisher

    cfg = get_scale_config()
    redis = ctx["redis"]
    helper = helpers.Helper.from_spec(spec)
    publisher = ChatEventPublisher(redis, session_id, ttl=cfg.pubsub_event_ttl, chat_turn=chat_turn)

    async def cut_short() -> None:
        helper.status, helper.error = "failed", HELPER_CUT_SHORT
        helper.ended_at = helper.ended_at or time.time()
        await helpers.save_ending(helper)
        with contextlib.suppress(Exception):
            await publisher.publish(helper.finished_event())
            await publisher.publish_done()
        await helpers.report(helper, said=helpers.RESTARTED.format(name=helper.name))

    if ctx.get("job_try", 1) > 1:
        if await _told_at_the_cut(redis, ctx.get("job_id")):
            return {"status": "interrupted", "session_id": session_id, "told_at_the_cut": True}
        await cut_short()
        _log.info(f"[worker] helper {helper.id}: handed out again (try {ctx['job_try']}) — not rerun")
        return {"status": "interrupted", "session_id": session_id}

    leased = False
    try:
        needs = await mcp_needs.for_agent(helper.agent)
        with mcp_pool.needing(needs):
            server_module = await mcp_pool.acquire()
        leased = True

        async def consume():
            async for event in helpers.run_detached(helper, server_module=server_module):
                await publisher.publish(event)

        cancelled = await _run_chat_turn_cancellably(redis, session_id, consume)
        if cancelled:
            # Stopped from outside: the run recorded it and said nothing more, so its lane is told here.
            helper.status = "cancelled"
            await clear_chat_cancellation(redis, session_id)
            await publisher.publish(helper.finished_event())
        await helpers.save_ending(helper)
        await publisher.publish_done()
        await helpers.report(helper)
        return {"status": helper.status, "session_id": session_id}

    except asyncio.CancelledError:
        # A stopping worker's cut (F424): said now, and the next delivery finds it told.
        if not drain.draining():
            raise
        task = asyncio.current_task()
        if task is not None and hasattr(task, "uncancel"):
            task.uncancel()

        async def tell() -> None:
            await cut_short()
            await _mark_told_at_the_cut(redis, ctx.get("job_id"))

        try:
            await asyncio.wait_for(asyncio.shield(tell()), timeout=CUT_TELL_SECONDS)
        except BaseException as e:  # noqa: BLE001 — the next delivery says it if this could not
            _log.warning(f"[worker] helper {helper.id}: could not tell the cut: {e!r}")
        raise

    except Exception as exc:  # noqa: BLE001 — it failed before it could run: its agent is told, once
        _log.warning(f"[worker] helper {helper.id} failed: {exc}")
        helper.status, helper.error = "failed", str(exc) or type(exc).__name__
        helper.ended_at = helper.ended_at or time.time()
        await helpers.save_ending(helper)
        with contextlib.suppress(Exception):
            await publisher.publish(helper.finished_event())
            await publisher.publish_done()
        await helpers.report(helper)
        return {"status": "failed", "session_id": session_id}

    finally:
        if leased:
            await mcp_pool.release()


async def _visitor_turn(visitor: dict | None, events):
    """Drive `events` in `visitor`'s scope (`None` is a member's turn).

    The scope is set around each step of the generator rather than around the
    loop: the generator's body runs in the task that advances it, and this keeps
    the job's own context untouched between steps.
    """
    from core import visitor as _visitor_mod

    scope = _visitor_mod.VisitorScope.from_dict(visitor)
    iterator = events.__aiter__()
    try:
        while True:
            with _visitor_mod.scope(scope):
                try:
                    event = await iterator.__anext__()
                except StopAsyncIteration:
                    return
            yield event
    finally:
        # Closed with this wrapper, as the loop was when it was iterated directly.
        with _visitor_mod.scope(scope):
            await iterator.aclose()


# ---------------------------------------------------------------------------
# Job: run an agent chat turn
# ---------------------------------------------------------------------------

@_for_the_jobs_tenant
async def run_agent_chat_job(
    ctx: dict,
    session_id: str,
    agent_id: str | None,
    message: str,
    images: list | None = None,
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
    resume_state: dict | None = None,
    image_refs: list | None = None,
    expand_mentions: bool = False,
    visitor: dict | None = None,
    workflow_run_id: str | None = None,
    stream_answer: bool = True,
    history_override: list | None = None,
    caller_system: str | None = None,
    save_turn: bool = True,
    log_source: str = "chat",
    report_usage: bool = False,
    keep_parked: bool = False,
    background_ok: bool = False,
    automated: dict | None = None,
    inbox_seed: list | None = None,
    helper: dict | None = None,
    chat_turn: str | None = None,
) -> dict:
    """Run a single agent chat turn, publish events, persist session.

    Helpers (SUB1b, `core.helpers`). Every one defaults to "as before", for a job
    enqueued before they existed (a parked voice answer's stored kwargs, VOX1f):

    * `background_ok` is the enqueuer's word that this is the app's chat or a
      member's own channel, where an agent may send a helper to the background
      (S4) — never a visitor's turn, which the engine refuses whatever is said. It
      opens the turn's inbox, as the app's chat route does in-process.
    * `automated` makes the turn a follow-up: its question is a helper's result,
      not the person's words. It is read as automated, untrusted, offers no
      background, never expands `@[…]`, never moves past a parked question, and
      every save of it keeps the mark.
    * `inbox_seed` is results a host took off a question the person moved past,
      for this turn to read first.
    * `helper` is a background helper's spec (`Helper.spec`): this job *is* that
      helper, on its own conversation, and reports back when it ends
      (`_run_background_helper`).
    * `chat_turn` tags every event this job publishes, so two turns on one
      conversation's stream never read as one (`ChatEventPublisher`).

    `keep_parked` is a phone call's (F486): the question the call parked waits while the caller talks on — it
    is answered from the bell, and said on the call (F474) — so a new turn does not move past it, and a second
    question never takes its place in the conversation's one slot.

    `stream_answer` publishes the answer as it is written (U72): `answer_delta`
    and `answer_reset` events, merged by the publisher so a long answer is a
    few hundred stream entries, not thousands. `history_override`,
    `caller_system`, `save_turn` and `log_source` are the OpenAI-compatible
    endpoint's (OAI1, `run_react_loop`): the request carries the conversation,
    and with `save_turn=False` the turn writes no Chat row. `report_usage`
    publishes the turn's tokens, summed over its model calls, as one
    `turn_usage` event just before `done` (the endpoint answers with `usage`).

    No `tenant_id` parameter — see `run_orchestration_job`.

    `expand_mentions` is the enqueuer's word on whose text `message` is: a
    member's (in-app chat, an API key, a schedule — `@[path]` reads the vault)
    or a widget visitor's or a channel's (it never does). Off unless said.

    `visitor` is the enqueuer's word on whose *turn* it is (F280): `None` for a
    member, or `VisitorScope.to_dict()` — `{"surface": "widget", "tools": [...]}`
    — for a stranger on a public surface, whose turn then holds only those
    tools, no memory, and no approval card (`core.visitor`). A widget or a
    channel that enqueues without it runs its visitor as a member.

    `workflow_run_id` is the enqueuer's id for the run a workflow agent's turn
    starts (F417): cloud admits that run before the turn is queued — its row,
    its meter and its hold — so the run is charged as a run, not as the turn's
    minutes. Unset, the run mints its own id, as it always has.

    `resume_state` picks a turn up from where a tool-approval pause left it,
    and is the queued twin of what `core/routes/chat.py` does in-process. Until
    it existed, a gated call on a worker was a dead end that the person could
    not even see was a dead end: the loop yielded its question and returned, the
    job appended an empty assistant message and marked the conversation
    `completed`. So the answer arrived as silence, and the transcript recorded
    that the agent had replied with nothing.
    """
    global _active_jobs
    _active_jobs += 1
    cfg = get_scale_config()
    await _load_job_settings()
    if helper is not None:
        try:
            return await _run_background_helper(ctx, session_id, helper, chat_turn=chat_turn)
        finally:
            _active_jobs = max(0, _active_jobs - 1)

    redis = ctx["redis"]
    session_factory = ctx["session_factory"]
    leased = False
    images = images or []
    # A helper's result posted back is nobody's text: never read as the person's `@[…]` (SUB1b).
    expand_mentions = expand_mentions and not automated
    # What the turn's helpers did, from their events: kept by a turn saved from outside the loop's answer.
    helper_events: list[dict] = []
    # From the loop's `final`, when it answers: the records, and the results the answer read.
    turn_helpers: list[dict] | None = None
    turn_inbox_read: list[str] = []
    # What the turn's inbox held when it parked (never published).
    parked_inbox: dict | None = None

    # Set before the `try`, so the `except` can tell "we never got far enough to
    # have a publisher" from "the turn failed with one". See below.
    publisher = None

    async def _save_row(status: str, rows: list) -> None:
        # A turn that keeps no conversation (the OpenAI endpoint's) writes no Chat row (OAI1).
        if save_turn:
            await _upsert_chat_session(session_factory, session_id, agent_id, status, rows)

    # Whether this turn's exchange is already in `history`: an error after the
    # answer was recorded (its webhook, say) must not replace it (F133).
    recorded = False
    # What the turn thought, from its events (U60): out here so a failed turn
    # keeps it too.
    thinking_events: list[dict] = []
    # The last turn-boundary snapshot, and the calls made since it from their
    # events (F242): out here so a turn that fails after its tools ran keeps
    # them (F426), even one that failed before the loop started.
    latest_progress: dict | None = None
    events_since_progress: list[dict] = []
    # The answer's Sources: the root turn's `knowledge_citations` (F333).
    turn_citations: dict | None = None
    # The conversation as loaded, and the runs this turn started (a workflow
    # agent's): out here so a turn a stopping worker cuts can be told and saved,
    # and its runs stopped, at the cut (F424).
    history: list | None = None
    turn_runs: list[str] = []

    def _unanswered(content: str) -> list[dict]:
        """The question, and `content` in place of its answer, with what the turn did.

        With the calls that ran before it stopped (F426), what it thought, and the
        question's images, as a completed turn is saved: saved with no calls, the
        next turn's model said it had never run them — and a tool that acts could
        be run twice.
        """
        from core.session import (helpers_from_events, image_notes, thinking_from_events, tools_from_calls,
                                  turn_calls)
        question = {"role": "user", "content": message}
        if images:
            question["images"] = image_notes(images)
        if automated:
            question["automated"] = dict(automated)
        answer = {"role": "assistant", "content": content}
        calls = turn_calls(latest_progress, events_since_progress, resumed=resume_state)
        if calls:
            answer.update(tools=tools_from_calls(calls), calls=calls)
        reasoning, thoughts = thinking_from_events(thinking_events, resumed=resume_state)
        if reasoning:
            answer["reasoning"] = reasoning
        if thoughts:
            answer["thoughts"] = thoughts
        # Its helpers' lanes, as far as they got (SUB1b): a background one works on in its own conversation.
        if kept := helpers_from_events(helper_events, resume_state):
            answer["helpers"] = kept
        return [question, answer]

    try:
        # Everything that makes this turn *visible* happens before the pool is
        # acquired, and the ordering is the point.
        #
        # `mcp_pool.acquire()` can block for a long time — it builds this
        # tenant's MCP sessions, and a remote server that accepts a connection
        # and then goes quiet holds it. With the acquire first, a turn wedged
        # there wrote no session row and published no event: no trace anywhere
        # except `j_ongoing` in a health line, for up to `job_timeout` (an
        # hour). Observed for real on 2026-08-25 — acme's job `25698d0f` sat
        # in-progress for forty minutes with zero `chat_%` rows for the org.
        #
        # These three reads and the publisher need only Postgres and Redis, so
        # there is nothing to trade away by doing them first. A wedged turn is
        # now a `running` row a staff console or a support reply can see.
        history = TurnRows(await _load_chat_history(session_factory, session_id, agent_id))

        if ctx.get("job_try", 1) > 1:
            # Handed out again: the first try was stopped mid-turn (a deploy, a
            # worker shutting down). A chat turn has no checkpoint, so running
            # it again started from scratch — and any tool it had already called
            # ran twice (F89). It is not rerun: the turn says it was
            # interrupted, and the person decides whether to send it again.
            #
            # A worker that stopped gracefully said so at the cut, and saved the
            # turn (F424): nothing is left to say or save.
            if await _told_at_the_cut(redis, ctx.get("job_id")):
                _log.info(f"[worker] chat {session_id}: turn handed out again (try {ctx['job_try']}) — "
                          f"already told at the cut")
                return {"status": "interrupted", "session_id": session_id, "told_at_the_cut": True}
            from core.scale.pubsub import ChatEventPublisher
            publisher = ChatEventPublisher(redis, session_id, ttl=cfg.pubsub_event_ttl, chat_turn=chat_turn)
            await publisher.publish({"type": "error", "message": INTERRUPTED_TURN})
            await publisher.publish_done()
            history.append({"role": "user", "content": message,
                            **({"automated": dict(automated)} if automated else {})})
            history.append({"role": "assistant", "content": INTERRUPTED_TURN})
            await _save_row("failed", history)
            _log.info(f"[worker] chat {session_id}: turn handed out again (try {ctx['job_try']}) — not rerun")
            return {"status": "interrupted", "session_id": session_id}

        seed = [dict(i) for i in inbox_seed or [] if isinstance(i, dict)]
        if resume_state is None and not keep_parked and not automated:
            # A new message refuses the call this conversation waits on (F247).
            # The turn that asked goes into `history` in its own place, which
            # the row below writes before the loop reads the conversation.
            # A follow-up never does: the person did not move on (SUB1a).
            from core.chat_approval import settle_moved_past
            # What that turn kept from its helpers, this one reads first (SUB1b).
            carried: list = []
            moved = await settle_moved_past(session_id, agent_id or "default", save_as=None, redis=redis,
                                            carried=carried)
            if moved:
                history.extend(moved)
            seed.extend(carried)

        await _save_row("running", history)

        from core.scale.pubsub import ChatEventPublisher
        publisher = ChatEventPublisher(redis, session_id, ttl=cfg.pubsub_event_ttl, chat_turn=chat_turn)

        # This tenant's MCP session set, from the bounded pool. It merges the
        # process-wide native servers with the tenant's own, so what the engine
        # sees is the shape `ctx["server_module"]` used to be — except that it
        # is this tenant's, not whoever's was current at worker startup.
        #
        # Only the tenant's servers this turn can call are connected for it
        # (VOX-MCP, D262): a phone call waited 12.9 s for three servers its
        # agent never used. `for_turn` never raises; in doubt, every server.
        from core.scale import mcp_needs
        needs = await mcp_needs.for_turn(agent_id, visitor)
        with mcp_pool.needing(needs):
            server_module = await mcp_pool.acquire()
        leased = True

        # No local materialisation: core/session.py reads the same rows this
        # job just loaded. The shim that used to live here wrote a session file
        # onto this worker's own disk, so a conversation's history followed
        # whichever worker won the job — and it wrote an empty cli_session_ids,
        # discarding any CLI session this worker was resuming.

        # Run the ReAct loop for this chat turn
        response_text = ""
        tool_calls_summary = []
        total_cost = 0.0

        from core.models import ChatRequest
        from core.react_engine import run_react_loop
        chat_request = ChatRequest(
            message=message,
            session_id=session_id,
            agent_id=agent_id,
            images=images or [],
        )
        # The enqueuer's names for the images (F395): a workflow agent's run
        # keeps them, not the images, for after a pause. Never a request field.
        chat_request._image_refs = list(image_refs or []) or None
        # The run a workflow agent starts, under the enqueuer's id (F417).
        chat_request._workflow_run_id = workflow_run_id or None
        # Helpers (SUB1b): the app's chat or a member's channel, a follow-up's mark, results to read first.
        chat_request._app_chat = bool(background_ok)
        chat_request._automated = dict(automated) if automated else None
        chat_request._inbox_seed = seed or None
        # Whether this turn stopped to ask a person something: read after
        # `_consume` returns, which is why it is declared out here (the last
        # snapshot and the calls since it are declared above the `try`).
        paused_on: dict | None = None
        # A workflow agent's run waiting on a person (a Human step): its turn is
        # saved where the run ends (F261), so this job writes none (F294).
        workflow_waiting = False

        async def _consume():
            """Drive the loop. Separated so the cancel watcher has a task to
            cancel that is not the whole job."""
            nonlocal response_text, total_cost, latest_progress, paused_on, workflow_waiting, turn_citations, \
                turn_helpers, turn_inbox_read, parked_inbox
            async for event in _visitor_turn(visitor, run_react_loop(
                chat_request, server_module,
                progress_events=True, resume_state=resume_state,
                expand_mentions=expand_mentions, stream_answer=stream_answer,
                history_override=history_override, caller_system=caller_system,
                # This job saves the turn (`_save_row`), every ending of it: the loop's own copy
                # was written over by the job's whole list, and is a second pair now that a save
                # appends (F475).
                save_turn=save_turn, log_source=log_source, save_here=False,
            )):
                etype = event.get("type")

                # A run this turn started (a workflow agent's), pumped in a task
                # of its own: a stopping worker stops it with the turn (F424).
                if (etype == "orchestration_start" and event.get("run_id")
                        and not event.get("nested_run_id") and event["run_id"] not in turn_runs):
                    turn_runs.append(event["run_id"])

                # Engine state, not an event. It is what a resume is rebuilt
                # from, and forwarding it would put a whole turn's context on
                # a Redis stream the browser reads.
                if etype == "_step_progress":
                    latest_progress = {k: v for k, v in event.items() if k != "type"}
                    events_since_progress.clear()
                    continue
                # What the turn's helpers sent and it had not answered, as it parked (SUB1b): engine state,
                # kept with the question, never on the stream.
                if etype == "_inbox_parked":
                    parked_inbox = event.get("state") or None
                    continue
                if etype in ("helper_started", "helper_finished"):
                    helper_events.append(event)
                elif etype == "final" and ("helpers" in event or "inbox_read" in event):
                    # This job saves the answer: what to keep with it comes on `final`, and stays off the stream.
                    turn_helpers = list(event.get("helpers") or [])
                    turn_inbox_read = list(event.get("inbox_read") or [])
                    event = {k: v for k, v in event.items() if k not in ("helpers", "inbox_read")}
                if etype in ("tool_execution", "tool_result"):
                    events_since_progress.append(event)
                elif etype in ("llm_reasoning", "llm_thought"):
                    thinking_events.append(event)
                elif (etype == "knowledge_citations" and not event.get("delegate_from")
                      and not event.get("orch_step_id")):
                    # The root turn's Sources; a delegate's or a step's labels
                    # are another turn's (F333).
                    turn_citations = event

                await publisher.publish(event)

                # Capture the final response text and cost
                if etype in ("response", "final"):
                    response_text = event.get("response", response_text)
                elif etype == "tool_execution":
                    tool_calls_summary.append({
                        "tool": event.get("tool_name"),
                        # `args` is what the loop sends; `tool_args` was
                        # never set, so every summary carried null (F81).
                        "args": event.get("args"),
                    })
                elif etype == "done":
                    total_cost = event.get("total_cost_usd", 0.0)
                elif (etype == "human_input_required" and event.get("kind") != "tool_approval"
                      and event.get("run_id")):
                    workflow_waiting = True
                elif etype == "human_input_required" and event.get("kind") == "tool_approval":
                    # The loop has already returned by the time we see this —
                    # it yields the question and stops. Remember it; the parking
                    # itself happens below, outside the task the cancel watcher
                    # may be about to cancel.
                    paused_on = event

        # A chat turn can now be stopped, the same way a run can.
        #
        # The engine checks a stop flag between turns, and between turns is not
        # where a turn spends its time: it is inside one LLM call, or a
        # twenty-minute tool, or one of the continuation retries the loop makes
        # when a response comes back truncated. So the watcher cancels the task
        # driving the engine rather than asking it to notice — the same
        # `_cancel_guarded` shape `WorkerEngineAdapter` uses for runs, keyed on
        # the session because a chat turn has no run id.
        from core import turn_usage
        meter = turn_usage.start() if report_usage else None
        try:
            cancelled = await _run_chat_turn_cancellably(redis, session_id, _consume)
        finally:
            if meter is not None:
                turn_usage.stop(meter)
        if meter is not None:
            await publisher.publish({"type": "turn_usage", **meter.totals()})

        if cancelled:
            # Say so on the stream before closing it. A client that hangs when
            # the server stops talking is a client bug; a server that stops
            # talking without saying why is a server bug, and this is that half
            # — the same rule the `except` below already follows.
            await publisher.publish({"type": "cancelled", "message": "Stopped."})
        await publisher.publish_done()

        # A turn that stopped to ask permission is not a turn that finished.
        #
        # Before this, it fell through to the append below and was written as
        # `completed` with an empty assistant message — so the conversation
        # recorded an answer nobody gave, and the next turn read that silence
        # as context. Park the call instead and leave the history alone: the
        # user's question is still unanswered, and `resume_state` is how it
        # gets answered.
        #
        # Not inside `_consume`: that task is the one the cancel watcher
        # cancels, and a write racing a cancellation is how you get a pending
        # approval for a turn that no longer exists.
        if paused_on is not None and not cancelled:
            from core.chat_approval import parked_payload
            from core.store import sessions as _sessions
            waiting = await _sessions.get_pending_approval(session_id, agent_id or "default") if keep_parked else None
            if waiting is None:
                progress = latest_progress
                if parked_inbox and (parked_inbox.get("pending") or parked_inbox.get("in_flight")):
                    # Read when the question is answered, or by the next turn if it is moved past (SUB1a).
                    progress = {**(latest_progress or {}), "inbox": parked_inbox}
                await _sessions.set_pending_approval(
                    session_id, agent_id or "default",
                    parked_payload(paused_on, message=message, progress=progress, automated=automated),
                )
            # Else a call's first question still waits (F486): this one is not asked; the call says so.
            await _save_row("paused", history)
            return {"status": "paused", "session_id": session_id}

        # A workflow waiting on a person has not answered, and its run saves
        # the turn when it ends (F261). Appended here it was the question and
        # an empty answer, which went back to the model as history (F294).
        if workflow_waiting and not response_text and not cancelled:
            await _save_row("paused", history)
            return {"status": "paused", "session_id": session_id}

        # Append to message history and persist.
        #
        # A cancelled turn is persisted too, with whatever the agent had
        # already said. Dropping it would mean the Stop button destroyed the
        # exchange rather than ending it — and Retry would have nothing to
        # retry from. Stopped before it said anything, it is saved as the
        # in-process path saves it, never as silence: an empty answer goes
        # back to the model as history, and the next turn answered the stopped
        # question instead of the new one (F116).
        if cancelled and not response_text:
            from core.chat_turns import STOPPED_ANSWER
            response_text = STOPPED_ANSWER
        # With what the question carried (F245) and what the turn's tools did
        # (F242), as the in-process path saves them: a bare pair left the next
        # turn blind to both.
        from core.session import (citation_fields, helpers_from_events, image_notes, thinking_from_events,
                                  turn_calls, tools_from_calls)
        question = {"role": "user", "content": message}
        if images:
            question["images"] = image_notes(images)
        if automated:
            question["automated"] = dict(automated)
        history.append(question)
        calls = turn_calls(latest_progress, events_since_progress, resumed=resume_state)
        answer_entry = {"role": "assistant", "content": response_text}
        if chat_turn:
            # Which turn saved it: a phone call that cut this turn marks exactly this answer as heard (F486).
            answer_entry["chat_turn"] = chat_turn
        if calls:
            answer_entry.update(tools=tools_from_calls(calls), calls=calls)
        reasoning, thoughts = thinking_from_events(thinking_events, resumed=resume_state)
        if reasoning:
            answer_entry["reasoning"] = reasoning
        if thoughts:
            answer_entry["thoughts"] = thoughts
        # Its Sources, as the in-process save keeps them — never for a visitor:
        # they name the owner's files (F333).
        if visitor is None:
            answer_entry.update(citation_fields(turn_citations))
        # Its helpers' lanes (SUB1a's S7, on the fleet): the loop's own records when it answered, else what
        # their events said (a Stop). And the results it read, so each is known to have arrived.
        kept_helpers = turn_helpers if turn_helpers is not None else helpers_from_events(helper_events, resume_state)
        if kept_helpers:
            answer_entry["helpers"] = [dict(h) for h in kept_helpers]
        if turn_inbox_read:
            answer_entry["inbox_read"] = list(turn_inbox_read)
        history.append(answer_entry)
        recorded = True
        await _save_row("cancelled" if cancelled else "completed", history)
        if cancelled:
            await clear_chat_cancellation(redis, session_id)
            return {"status": "cancelled", "session_id": session_id}

        # Deliver webhook if requested
        if webhook_url:
            from core.scale.webhook import deliver_webhook
            await deliver_webhook(
                webhook_url=webhook_url,
                payload={
                    "session_id": session_id,
                    "agent_id": agent_id,
                    "status": "completed",
                    "message": {"role": "assistant", "content": response_text},
                    "tool_calls": tool_calls_summary,
                    "total_cost_usd": total_cost,
                },
                secret=webhook_secret,
            )

        return {"status": "completed", "session_id": session_id}

    except asyncio.CancelledError:
        # A stopping worker's cut (F424). ARQ gave the turn its 45 s and is now
        # cutting it; it hands the job out again, and the next delivery used to
        # be the first to say anything — the person watched "Analyzing your
        # request…" through the shutdown and the restart. They are told now,
        # the turn is saved as the next delivery would have saved it, and the
        # runs it started stop with it. Then the cancel goes on to ARQ, as the
        # knowledge worker's crawl does (F353), and the next delivery finds the
        # turn told. Any other cancel (a job timeout) is not a stop: as before.
        from core.scale import drain
        if not drain.draining():
            raise
        task = asyncio.current_task()
        if task is not None and hasattr(task, "uncancel"):
            # The cancel is handled here: left counted, the timeouts inside the
            # writes below would read it as theirs.
            task.uncancel()

        async def _tell() -> None:
            nonlocal publisher
            for run_id in turn_runs:
                await _stop_turn_run(run_id)
            if not recorded:
                if history is None:
                    return  # cut before the conversation was read: the next delivery says it, as before
                if publisher is None:
                    from core.scale.pubsub import ChatEventPublisher
                    publisher = ChatEventPublisher(redis, session_id, ttl=cfg.pubsub_event_ttl, chat_turn=chat_turn)
                with contextlib.suppress(Exception):
                    await publisher.publish({"type": "error", "message": INTERRUPTED_TURN})
                    await publisher.publish_done()
                history.extend(_unanswered(INTERRUPTED_TURN))
                await _save_row("failed", history)
            # Also for a turn whose answer was saved before the cut (its webhook
            # was going): the next delivery must not add an interruption after it.
            await _mark_told_at_the_cut(redis, ctx.get("job_id"))
            _log.info(f"[worker] chat {session_id}: a stop cut this turn — told at the cut (F424)")

        try:
            # Bounded: the worker's grace runs out after ARQ's wait, and the
            # job's claim is dropped only once this returns.
            await asyncio.wait_for(asyncio.shield(_tell()), timeout=CUT_TELL_SECONDS)
        except BaseException as e:  # noqa: BLE001 — the next delivery says it if this could not
            _log.warning(f"[worker] chat {session_id}: could not tell the turn at the cut: {e!r}")
        raise

    except Exception as exc:
        tb = traceback.format_exc()
        _log.debug(f"[worker] run_agent_chat_job ERROR {session_id}: {exc}\n{tb}")

        # Say so on the stream before failing.
        #
        # This used to mark the row `failed` and re-raise without publishing
        # anything, so a turn that died outside the ReAct loop — a recycled
        # worker, an MCP pool failure, anything before the loop starts — left
        # the Redis stream open with nothing further to say. The API went on
        # sending keepalives because it was still waiting for `done`, and the
        # browser spun forever. A client that hangs when the server stops
        # talking is a client bug; a server that stops talking without saying
        # why is a server bug, and this is that half.
        #
        # Best-effort: a Redis that is already unreachable must not replace the
        # real error with a publishing one, and the `failed` row below is the
        # durable record either way.
        if publisher is not None:
            try:
                # What kind of failure, beside the words: the v2 routes hand an
                # API caller that and not these, which are a member's (F295).
                await publisher.publish({
                    "type": "error", "message": str(exc),
                    **({"code": exc.code, "retryable": exc.retryable} if getattr(exc, "code", None) else {}),
                    **({"run_id": exc.run_id} if getattr(exc, "run_id", None) else {}),
                })
                await publisher.publish_done()
            except Exception:
                pass

        try:
            # `history` — never `[]`. This wrote an empty list into the
            # `messages` column, and `_upsert_chat_session` lists `messages`
            # among the columns it updates, so **every failed turn erased the
            # whole conversation**. The row was described here as "the durable
            # record"; it was the opposite. `history` holds the turns loaded at
            # the top of this job, so the exchange survives its own failure and
            # there is something left to retry from.
            #
            # And the question with it (F133): a turn that failed before it
            # answered keeps what was asked, answered by a marker, as a stopped
            # or an interrupted turn does — a reopened conversation used to
            # lose it. The provider's words are not the answer; they went out
            # on the stream above, to the person in the app.
            #
            # With the calls that ran before it failed (F426), and the
            # question's images, as the completed save and the in-process
            # failed save keep them: saved with no calls, the next turn's model
            # said it had never run them — and a tool that acts could be run
            # twice. This whole-column write replaces the engine's own save.
            if not recorded:
                from core.chat_turns import FAILED_ANSWER
                history.extend(_unanswered(FAILED_ANSWER))
            await _save_row("failed", history)
        except Exception:
            pass
        if webhook_url and not recorded:
            # A caller waiting on its webhook is told the turn failed. It used
            # to hear nothing — or, when the failure was the model's, a
            # `completed` carrying the provider's error as the answer.
            try:
                from core.scale.webhook import deliver_webhook
                await deliver_webhook(
                    webhook_url=webhook_url,
                    payload={
                        "session_id": session_id,
                        "agent_id": agent_id,
                        "status": "failed",
                        "error": "The agent encountered an error processing your request.",
                    },
                    secret=webhook_secret,
                )
            except Exception:
                pass
        raise

    finally:
        if leased:
            await mcp_pool.release()
        _active_jobs = max(0, _active_jobs - 1)


# ---------------------------------------------------------------------------
# Worker lifecycle hooks
# ---------------------------------------------------------------------------

async def worker_startup(ctx: dict) -> None:
    """Initialize Postgres, Redis, register worker, load LLM keys, start heartbeat."""
    global _worker_id, _worker_address

    cfg = get_scale_config()

    # Unique worker ID
    _worker_id = os.getenv("WORKER_ID", f"worker-{socket.gethostname()}-{uuid.uuid4().hex[:8]}")
    _worker_address = f"http://{socket.gethostname()}:{os.getenv('WORKER_HEALTH_PORT', '9000')}"

    _log.debug(f"[worker] starting up as {_worker_id} @ {_worker_address}")

    # Hear the stop ARQ hears, so a run is handed back between steps and a chat
    # turn is told at the cut (F424). After ARQ's own handler, which it installed
    # when it built the Worker; see `core/scale/drain.py`.
    from core.scale import drain
    drain.install()

    # Build the store's engine and session factory
    from core.store import build_engine, build_session_factory, init_db, set_store
    pg_engine = build_engine(cfg.postgres_url, pgbouncer_mode=cfg.pgbouncer_mode)
    await init_db(pg_engine)
    session_factory = build_session_factory(pg_engine)
    ctx["session_factory"] = session_factory
    ctx["pg_engine"] = pg_engine

    # Point the store at this engine, so the resolvers (agents, tools, MCP
    # servers) and the state backend reach the same Postgres the worker uses
    # rather than opening a second, SQLite one.
    set_store(session_factory)

    # Telemetry: off unless a Synapse switch turns it on (core/telemetry). After
    # set_store, so the Scale tab's saved collector is readable; the engine just
    # built is instrumented when it is on.
    try:
        from core import telemetry
        telemetry.setup("synapse-worker", saved_endpoint=get_scale_config().otlp_endpoint)
    except Exception as exc:
        _log.warning("[worker] telemetry setup failed: %s", type(exc).__name__)

    # The vault, run logs and notifications on the bucket when one is configured
    # — the store every API server and worker must share (F57).
    from core.storage import install_configured_blob_store
    install_configured_blob_store()

    # A human step that names a channel asks its question there when the run
    # pauses on this worker (F54). REST only: a worker holds no adapters.
    from core.messaging.manager import send_human_prompt
    from core.orchestration.human_prompts import set_human_prompt_sender
    set_human_prompt_sender(send_human_prompt)

    # Redis client (already provided by ARQ as ctx["redis"])
    redis = ctx["redis"]

    # A run this worker pumps in a task of its own (a workflow agent's turn, a
    # delegated workflow) holds a lease: the sweeps see it alive, and its
    # Cancel reaches it (F470).
    from core.scale import run_lease
    run_lease.install(redis, holder=_worker_id)

    # This worker's notifications (an eval's regression, a resolved approval) join the one ring every API
    # server and worker shares, rather than a copy of it written over theirs (F361).
    from core.notifications import hub as notification_hub
    notification_hub.share(redis, listen=False)

    # Settings come from the store, per job, for the tenant in context.
    # Nothing is written to the process environment: a provider key there is
    # readable in /proc/<pid>/environ and inherited by every stdio MCP server
    # and sandbox the worker spawns.
    settings_runtime.install_provider()

    # The native tool servers, which are the same for every tenant. A tenant's
    # own MCP — and the Filesystem server, which is rooted at its vault — is
    # built per job by core.scale.mcp_pool and cached there.
    from core.scale.worker_server_module import WorkerServerModule
    server_module = await WorkerServerModule.build_shared()
    mcp_pool.set_shared(server_module)
    ctx["server_module"] = server_module

    # Register worker in Postgres
    from core.scale.models_db import WorkerDB
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    async with session_factory() as session:
        stmt = pg_insert(WorkerDB).values(
            worker_id=_worker_id,
            hostname=socket.gethostname(),
            address=_worker_address,
            status="online",
            active_jobs=0,
            max_jobs=cfg.worker_concurrency,
            last_heartbeat=datetime.now(timezone.utc),
            mcp_disabled=server_module.mcp_disabled,
        ).on_conflict_do_update(
            index_elements=["worker_id"],
            set_={
                "hostname": socket.gethostname(),
                "address": _worker_address,
                "status": "online",
                "max_jobs": cfg.worker_concurrency,
                "last_heartbeat": datetime.now(timezone.utc),
                "mcp_disabled": server_module.mcp_disabled,
            },
        )
        await session.execute(stmt)
        await session.commit()

    # Start heartbeat background task
    from core.scale.heartbeat import run_heartbeat
    heartbeat_task = asyncio.create_task(
        run_heartbeat(
            worker_id=_worker_id,
            address=_worker_address,
            hostname=socket.gethostname(),
            session_factory=session_factory,
            get_active_jobs_fn=_get_active_jobs,
            max_jobs=cfg.worker_concurrency,
            mcp_disabled=server_module.mcp_disabled,
        )
    )
    ctx["heartbeat_task"] = heartbeat_task
    # The queue's depth for `synapse.queue.depth`, when metrics are on.
    ctx["queue_depth_task"] = _tqueue.watch_depth(redis, QUEUE_NAME)
    ctx["worker_id"] = _worker_id

    _log.debug(f"[worker] startup complete. MCP disabled: {server_module.mcp_disabled}")


async def worker_shutdown(ctx: dict) -> None:
    """Mark worker offline, cancel heartbeat, close connections."""
    _tqueue.stop_watching(ctx.get("queue_depth_task"))
    from core.notifications import hub as notification_hub
    await notification_hub.unshare()  # what this worker announced is written before Redis closes (F361)
    heartbeat_task = ctx.get("heartbeat_task")
    if heartbeat_task and not heartbeat_task.done():
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

    session_factory = ctx.get("session_factory")
    if session_factory and _worker_id:
        from core.scale.heartbeat import mark_worker_offline
        await mark_worker_offline(_worker_id, session_factory)

    # Every tenant's sessions first, then the shared ones they borrow from.
    # `drain()` waits, because each entry closes on its own task and the process
    # exiting underneath one leaves an orphaned MCP subprocess behind.
    await mcp_pool.drain()

    server_module = ctx.get("server_module")
    if server_module:
        await server_module.close()

    pg_engine = ctx.get("pg_engine")
    if pg_engine:
        await pg_engine.dispose()

    _log.debug(f"[worker] shutdown complete")


# ---------------------------------------------------------------------------
# ARQ WorkerSettings
# ---------------------------------------------------------------------------

_cfg = get_scale_config()


def _build_redis_settings() -> RedisSettings:
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    # ARQ doesn't support Redis Cluster natively; use single-node URL for ARQ
    # and RedisCluster separately for event streaming.
    if redis_url.startswith("redis+cluster://"):
        stripped = redis_url.replace("redis+cluster://", "")
        first = stripped.split(",")[0]
        host, port = first.rsplit(":", 1)
        return RedisSettings(host=host, port=int(port))
    return RedisSettings.from_dsn(redis_url)


class WorkerSettings:
    functions = [
        run_orchestration_job,
        resume_orchestration_job,
        resume_failed_job,
        run_agent_chat_job,
    ]
    queue_name = QUEUE_NAME
    max_jobs = int(os.getenv("WORKER_CONCURRENCY", "10"))
    job_timeout = int(os.getenv("WORKER_JOB_TIMEOUT", "3600"))
    # One more than the retry policy: ARQ fails a job past `max_tries` without
    # calling it, so the extra delivery is the one on which a job records that
    # it gave up (`_gave_up`, F89).
    max_tries = job_retries({}) + 1
    # On SIGTERM, stop taking jobs and give running ones this long to finish
    # before cancelling them. Unset, every job was cancelled at once. A job
    # still running at the end is cancelled, handed out again, and resumes
    # from its checkpoint (`_redelivered`). Keep it under the orchestrator's
    # grace period — k8s `terminationGracePeriodSeconds`, compose's
    # `stop_grace_period` — or SIGKILL lands first, and a killed job is not
    # handed out again until its in-progress key expires, about an hour.
    # Within the wait a run is handed back at its next step boundary, and a chat
    # turn the wait cuts is told so at once (F424, `core/scale/drain.py`).
    job_completion_wait = int(os.getenv("WORKER_JOB_COMPLETION_WAIT", "45"))
    on_startup = worker_startup
    on_shutdown = worker_shutdown
    redis_settings = _build_redis_settings()
    # Pickle plus the trace context, in a key an older worker never reads
    # (core/telemetry/queue.py). Plain pickle when telemetry is off.
    job_serializer = _tqueue.serialize
    job_deserializer = _tqueue.deserialize


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _load_orchestration(session_factory, orch_id: str, run=None):
    """The definition a job runs: a run's own copy (F443), else the one stored now.

    `run` is the run's row, given where a job carries a run on — an answer, a
    try handed out again — so it goes on with the workflow it started with, not
    an edit made while it waited; `engine.definition_of` decides, as it does
    for the in-process resume. Without a row (a fresh run, a person's Replay),
    or for a run from before the copy was kept, the orchestration as stored.
    """
    from sqlalchemy import select
    from core.scale.models_db import OrchestrationDB
    from core.models_orchestration import Orchestration

    if run is not None and getattr(run, "definition", None):
        from core.orchestration.engine import definition_of
        data = await definition_of(run)
        if data:
            return Orchestration.model_validate(data)

    async with session_factory() as session:
        result = await session.execute(
            select(OrchestrationDB).where(OrchestrationDB.id == orch_id)
        )
        row = result.scalar_one_or_none()

    if row is None:
        # The engine's own words (a RuntimeError keeps no class name, D267): a
        # job that dies here leaves this on its run as the reason (F430).
        raise RuntimeError(f"The workflow this run belongs to ('{orch_id}') no longer exists, so it could not run.")

    return Orchestration.model_validate(row.definition)


async def _load_run_row(session_factory, run_id: str):
    """The run row as the last attempt left it, or None."""
    from sqlalchemy import select
    from core.scale.models_db import OrchestrationRunDB

    async with session_factory() as session:
        result = await session.execute(
            select(OrchestrationRunDB).where(OrchestrationRunDB.run_id == run_id)
        )
        return result.scalar_one_or_none()


def _redelivered(row) -> str:
    """What a job handed out a second time does with the run: fresh, resume or done.

    ARQ runs a job again only when the previous attempt was cancelled (a worker
    shutting down) or killed, so the run is wherever that attempt's last
    checkpoint left it:

    * **done** — it finished, paused for a person, was cancelled by one, or
      failed at a step. Running it again would repeat what it did; a failed
      step is retried by a person (Replay), not by a restart.
    * **resume** — it holds a position (`current_step_id`) and is `running`,
      or `failed` by the API's startup sweep, which adds no history entry.
    * **fresh** — no row, or no position: nothing ran.
    """
    if row is None:
        return "fresh"
    history = row.step_history or []
    if row.status in ("completed", "cancelled", "paused") or row.waiting_for_human:
        return "done"
    if row.status == "failed" and history and history[-1].get("status") == "failed":
        return "done"
    return "resume" if row.current_step_id else "fresh"


def _awaits_answer(row) -> bool:
    """True while a paused run's answer has not reached a checkpoint.

    The human step's `paused` history entry is closed by the resume that
    applies the answer, and first persisted at the next step boundary.
    """
    history = row.step_history or []
    return bool(row.waiting_for_human or (history and history[-1].get("status") == "paused"))


async def _status_of_run(session_factory, run_id: str) -> str | None:
    row = await _load_run_row(session_factory, run_id)
    return row.status if row is not None else None


async def _answer_claimed(ctx: dict, run_id: str) -> None:
    """Tell an embedder the run is this job's to resume — `ctx["on_answer_claimed"]`.

    Cloud closes its meter's pause here rather than before the job, so an answer
    that does not apply (F136) leaves the pause exactly as it found it. Awaited
    after the claim and never allowed to fail the resume: an error in someone
    else's bookkeeping is not a reason to dead-letter a run that is fine.
    """
    hook = ctx.get("on_answer_claimed")
    if hook is None:
        return
    try:
        await hook()
    except Exception as exc:  # noqa: BLE001
        _log.warning(f"[worker] run {run_id}: on_answer_claimed failed: {exc}")


def _asking_ordinal(row) -> int | None:
    from core.orchestration.pauses import pause_ordinal
    return pause_ordinal(row.step_history)


def _answers_another_question(row, pause_ordinal: int | None) -> bool:
    """True when an answer's question is not the one the run is asking (F111).

    `_awaits_answer` says only that the run is waiting, not on what — and a
    loop asks once per iteration under one step id, so an old answer looked
    like an answer to the new question. A run no longer waiting was answered
    already. An answer that says nothing about its question (enqueued before
    answers did), or a run whose pause has no entry to compare, is applied.
    """
    if pause_ordinal is None:
        return False
    if not _awaits_answer(row):
        return True
    asking = _asking_ordinal(row)
    return asking is not None and asking != pause_ordinal


async def _load_chat_history(session_factory, session_id: str, agent_id: str | None) -> list:
    """Load message history for this conversation.

    Scoped by agent as well as session: the same session id under a different
    agent is a different conversation, and this is the read that decides what
    the model is shown.
    """
    from sqlalchemy import select
    from core.store.models import ChatSessionDB

    async with session_factory() as session:
        result = await session.execute(
            select(ChatSessionDB).where(
                ChatSessionDB.tenant_id == get_tenant(),
                ChatSessionDB.session_id == session_id,
                ChatSessionDB.agent_id == (agent_id or "default"),
            )
        )
        row = result.scalar_one_or_none()

    if row and row.messages:
        return list(row.messages)
    return []


class TurnRows(list):
    """A chat turn's conversation as it holds it: what it loaded, then what it added (F475).

    `stored` is how much of it is in the conversation's row already: what was
    loaded, then what each save wrote. A save appends only the rest, to the row as
    it stands then, so a turn another writer saved meanwhile (a phone call's own,
    beside the fleet resuming its approval, VOX1e) is kept.
    """

    def __init__(self, messages=()):
        super().__init__(messages)
        self.stored = len(self)


async def _upsert_chat_session(
    session_factory,
    session_id: str,
    agent_id: str | None,
    status: str,
    messages: list,
) -> None:
    """Create or update a chat session row.

    `messages` is the conversation as the turn holds it. A `TurnRows` adds what is
    past its `stored` to the row as it stands now, holding the row from read to
    commit (F475): writing the whole list back erased every turn another writer
    appended while this one ran. A plain list is written whole, as it always was.
    """
    if isinstance(messages, TurnRows):
        await _append_chat_session(session_factory, session_id, agent_id, status, messages)
        return
    from core.store import upsert
    from core.store.models import ChatSessionDB

    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        await upsert(
            session,
            ChatSessionDB,
            values={
                "session_id": session_id,
                "agent_id": agent_id or "default",
                "tenant_id": get_tenant(),
                "status": status,
                "messages": messages,
                "last_message_at": now,
                "worker_id": _worker_id,
            },
            index_elements=["tenant_id", "session_id", "agent_id"],
            update=["status", "messages", "last_message_at", "worker_id"],
        )
        await session.commit()


async def _append_chat_session(session_factory, session_id: str, agent_id: str | None, status: str,
                               turn: TurnRows) -> None:
    """`turn`'s unsaved messages appended to the row under its lock, its status set (F475)."""
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from core.store.models import ChatSessionDB

    new = list(turn[turn.stored:])
    agent = agent_id or "default"

    async def write() -> None:
        now = datetime.now(timezone.utc)
        async with session_factory() as session:
            row = (await session.execute(
                select(ChatSessionDB).where(
                    ChatSessionDB.tenant_id == get_tenant(),
                    ChatSessionDB.session_id == session_id,
                    ChatSessionDB.agent_id == agent,
                ).with_for_update()
            )).scalar_one_or_none()
            if row is None:
                # No row (a first turn, or one deleted while it ran): the turn's whole view, as before.
                session.add(ChatSessionDB(
                    tenant_id=get_tenant(), session_id=session_id, agent_id=agent, status=status,
                    messages=list(turn), last_message_at=now, worker_id=_worker_id,
                ))
            else:
                if new:
                    row.messages = list(row.messages or []) + new
                row.status = status
                row.last_message_at = now
                row.worker_id = _worker_id
            await session.commit()

    try:
        await write()
    except IntegrityError:
        await write()  # another writer created the row first: append to it
    turn.stored = len(turn)


async def _deliver_webhook_for_run(
    run_id: str,
    session_factory,
    webhook_url: str,
    webhook_secret: str | None,
) -> None:
    """Load run result from Postgres and deliver to webhook."""
    try:
        from sqlalchemy import select
        from core.scale.models_db import OrchestrationRunDB
        from core.scale.webhook import deliver_webhook

        async with session_factory() as session:
            result = await session.execute(
                select(OrchestrationRunDB).where(OrchestrationRunDB.run_id == run_id)
            )
            row = result.scalar_one_or_none()

        if not row:
            return

        await deliver_webhook(
            webhook_url=webhook_url,
            payload={
                "run_id": run_id,
                "orchestration_id": row.orchestration_id,
                "status": row.status,
                "started_at": str(row.started_at) if row.started_at else None,
                "ended_at": str(row.ended_at) if row.ended_at else None,
                "total_cost_usd": float(row.total_cost_usd or 0),
                "shared_state": row.shared_state or {},
                "error": None if row.status == "completed" else "Run did not complete",
            },
            secret=webhook_secret,
        )
    except Exception as e:
        _log.debug(f"[worker] webhook delivery failed for {run_id}: {e}")


async def _told_at_the_cut(redis, job_id: str | None) -> bool:
    """Whether this chat job's earlier delivery told its turn at the cut (F424)."""
    if redis is None or not job_id:
        return False
    try:
        return bool(await redis.get(CHAT_TOLD_KEY.format(job_id=job_id)))
    except Exception as e:  # noqa: BLE001 — in doubt, the turn is told again
        _log.warning(f"[worker] chat job {job_id}: could not read whether it was told at the cut: {e}")
        return False


async def _mark_told_at_the_cut(redis, job_id: str | None) -> None:
    if redis is not None and job_id:
        await redis.set(CHAT_TOLD_KEY.format(job_id=job_id), 1, ex=CHAT_TOLD_TTL)


async def _stop_turn_run(run_id: str) -> None:
    """Stop a run a cut chat turn started, and fail it with the reason (F424).

    A workflow agent's run is pumped in a task of its own (`spawn_engine_run`),
    which nothing stops when its turn is cut: the process exits under it, the
    row stays `running` at its step with nothing to resume it, and on a host
    that meters runs it is never settled and keeps its slot. Its pump is
    stopped first, so nothing writes after the row is failed. The steps it
    finished keep their entries — a host prices those — and the step it was in
    has none.
    """
    from core.orchestration.runner import _active_tasks

    task = _active_tasks.get(run_id)
    if task is not None and not task.done():
        task.cancel()
        await asyncio.wait({task}, timeout=3)
    try:
        if await _fail_outside_a_step(run_id, TURN_RUN_STOPPED, statuses=("running",)):
            _log.info(f"[worker] run {run_id}: stopped with the chat turn a stop cut (F424)")
    except Exception as e:  # noqa: BLE001 — the turn is told either way
        _log.warning(f"[worker] run {run_id}: could not be failed with its cut turn: {e}")


#: Every run status but `completed`: what a dead job fails (F430), as its UPDATE always did.
_NOT_COMPLETED = ("queued", "running", "paused", "failed", "cancelled")


async def _fail_outside_a_step(run_id: str, reason: str, *, statuses: tuple[str, ...]) -> bool:
    """Fail a run that stopped outside any step, with `reason` on it. False if it was not in `statuses`.

    The boot sweep's shape (`core/scale/heartbeat.py`): the run has no failed
    step to say why, so the reason is the run's own (F257), and what it was
    running nested stops with it.
    """
    from core.orchestration.descendants import settle_descendants
    from core.orchestration.run_error import set_run_error
    from core.orchestration.state import SharedState

    try:
        restored = await SharedState.restore(run_id)
    except FileNotFoundError:
        return False
    run = restored.run
    if run.status not in statuses:
        return False
    run.status = "failed"
    run.ended_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    set_run_error(run, reason)
    await restored.checkpoint()
    await settle_descendants(run_id, "failed", reason)
    return True


async def _gave_up(ctx: dict, job_function: str, run_id: str, orch_id: str | None) -> bool:
    """On the delivery past the retry policy: fail and dead-letter the run, not run it.

    A job is handed out again only after the previous try was stopped — a worker
    shutting down, a deploy, a kill. Once that has happened as often as the
    policy allows, ARQ would fail the next delivery without calling us, and the
    run stayed `running` with no dead letter until an API restart swept it. The
    worker now takes one more delivery than the policy, and on it the job does
    no work: the run is failed and dead-lettered, and Retry resumes it on purpose.

    Only deliveries in a row that made no progress count (F376): see
    `_deliveries_without_progress`.
    """
    attempt = await _deliveries_without_progress(ctx, run_id)
    if attempt <= job_retries(ctx):
        return False
    await _dead_job(ctx, job_function, run_id, orch_id, RuntimeError(
        f"Stopped {attempt - 1} times in a row without finishing or getting any further (the "
        f"worker running it shut down or was killed each time), so it was not run again. "
        f"Retry resumes it."
    ), tb="")
    return True


#: How far a run job's run had got when its last delivery began (F376), kept as long as ARQ keeps its own
#: count of the job's deliveries (`arq:retry:<job_id>`, 88,400 s).
RUN_PROGRESS_KEY = "synapse:run-progress:{job_id}"
RUN_PROGRESS_TTL = 88_400


async def _deliveries_without_progress(ctx: dict, run_id: str) -> int:
    """Which delivery in a row without progress this one is: ARQ's `job_try`, unless the run got further.

    ARQ counts every delivery of a job, so a long run that each deploy stopped and
    the next delivery resumed used its tries up while it was working: a 6-hour run
    met its end at its third restart (F376). A delivery that finds the run further
    on than the previous delivery found it is the first again, and ARQ's own count
    is set back to match. A run killed on the same step every time still gives up.

    Further on: more steps ended, or the same step on a later agent turn — never
    fewer entries (a resume drops a failed one) or an earlier turn (a step's turns
    start again after three attempts). `ctx["job_try"]` keeps ARQ's value: the job
    reads it to know it is resuming. Without Redis or a job id, ARQ's count stands.
    """
    attempt = int(ctx.get("job_try", 1) or 1)
    redis, job_id = ctx.get("redis"), ctx.get("job_id")
    if redis is None or not job_id:
        return attempt
    from arq.constants import retry_key_prefix

    key = RUN_PROGRESS_KEY.format(job_id=job_id)
    try:
        now = _progress_of(await _load_run_row(ctx["session_factory"], run_id))
        before = await redis.get(key)
        await redis.set(key, json.dumps(now), ex=RUN_PROGRESS_TTL)
        if attempt > 1 and before is not None and _further(now, json.loads(before)):
            await redis.set(retry_key_prefix + job_id, 1, ex=RUN_PROGRESS_TTL)
            _log.info(f"[worker] run {run_id}: further on than at its last delivery, so try {attempt} counts as 1")
            return 1
    except Exception as e:  # noqa: BLE001 — ARQ's count is the policy without this
        _log.warning(f"[worker] run {run_id}: could not compare its progress across deliveries: {e}")
    return attempt


def _progress_of(row) -> list:
    """[steps ended, the current step, its agent turn] — how far a run has got."""
    if row is None:
        return [0, None, 0]
    step = row.current_step_id
    turn = ((row.step_progress or {}).get(step) or {}).get("turn") if step else None
    return [len(row.step_history or []), step,
            int(turn) if isinstance(turn, (int, float)) and not isinstance(turn, bool) else 0]


def _further(now: list, before: list) -> bool:
    steps, step, turn = now
    steps_before, step_before, turn_before = before
    return steps > steps_before or (steps == steps_before and step == step_before and turn > turn_before)


async def _dead_job(ctx: dict, job_function: str, run_id: str, orch_id: str | None, exc: Exception,
                    tb: str | None = None) -> None:
    """Record a job that died outside the engine: run failed with why, stream ended, dead-lettered.

    Every step is best-effort — this runs while an exception is on its way to
    ARQ, and a second failure here must not replace it.

    The run keeps its position (`current_step_id` is not touched), so retrying
    the dead letter resumes it. A run that had already completed is left
    alone: the resume that raised was refused because there was nothing to do.

    Why it stopped is the run's own reason (F430), which Retry clears: a run
    given up after its worker stopped too often, or one whose job died before
    or between its steps, has no failed step to say so, and read `failed` with
    no reason anywhere but in the dead letter — on the run page, in v1 and v2.
    In the words a failed step's are kept in (`reason_of`, D267).
    """
    from core.orchestration.run_error import reason_of

    session_factory = ctx["session_factory"]
    tb = traceback.format_exc() if tb is None else tb
    reason = reason_of(exc)
    _log.warning(f"[worker] {job_function} died for run {run_id}: {exc}")

    try:
        await _fail_outside_a_step(run_id, reason, statuses=_NOT_COMPLETED)
    except Exception:
        # The row alone, without its reason, as before: the run is not left `running`.
        try:
            async with session_factory() as session:
                from sqlalchemy import update
                from core.scale.models_db import OrchestrationRunDB
                await session.execute(
                    update(OrchestrationRunDB)
                    .where(OrchestrationRunDB.run_id == run_id)
                    .where(OrchestrationRunDB.status != "completed")
                    .values(status="failed", ended_at=datetime.now(timezone.utc))
                )
                await session.commit()
        except Exception:
            pass

    try:
        from core.scale.pubsub import RunEventPublisher
        publisher = RunEventPublisher(ctx["redis"], run_id)
        await publisher.publish({"type": "orchestration_error", "error": reason})
        await publisher.publish_done()
    except Exception:
        pass

    await _write_dlq(
        session_factory=session_factory,
        run_id=run_id,
        orch_id=orch_id,
        job_function=job_function,
        error_message=str(exc),
        error_traceback=tb,
        attempt_count=ctx.get("job_try", 1),
    )


async def _write_dlq(
    session_factory,
    run_id: str,
    orch_id: str | None,
    job_function: str,
    error_message: str,
    error_traceback: str,
    attempt_count: int,
) -> None:
    """Write a dead job to the dead-letter queue table, under the job's tenant.

    The payload is the run and nothing else: retrying resumes the run
    (`resume_failed_job`), whose input is already in its row — so no input, and
    no webhook secret, sits in this table.
    """
    from sqlalchemy import insert
    from core.scale.models_db import DeadLetterQueueDB

    now = datetime.now(timezone.utc)
    try:
        async with session_factory() as session:
            await session.execute(
                insert(DeadLetterQueueDB).values(
                    id=uuid.uuid4(),
                    tenant_id=get_tenant(),
                    run_id=run_id,
                    orchestration_id=orch_id,
                    job_function=job_function,
                    job_payload={"run_id": run_id, "orch_id": orch_id},
                    error_message=error_message,
                    error_traceback=error_traceback,
                    attempt_count=attempt_count,
                    first_failed_at=now,
                    last_failed_at=now,
                )
            )
            await session.commit()
        from core.telemetry import metrics as _tmetrics
        _tmetrics.job_dead_lettered(job_function)
    except Exception as e:
        _log.warning(f"[worker] failed to write DLQ for run {run_id}: {e}")
