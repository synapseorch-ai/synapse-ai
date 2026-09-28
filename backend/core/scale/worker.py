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
) -> dict:
    """Pull an orchestration definition from the store, run it, publish events.

    `images` are the attachments this run starts with — see
    `WorkerEngineAdapter.run` for why they travel on the engine and never in
    `initial_state`.

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

        # Load orchestration definition from Postgres
        orch = await _load_orchestration(session_factory, orch_id)

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

        orch = await _load_orchestration(session_factory, orch_id)
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

        orch = await _load_orchestration(session_factory, orch_id)

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
    expand_mentions: bool = False,
    visitor: dict | None = None,
) -> dict:
    """Run a single agent chat turn, publish events, persist session.

    No `tenant_id` parameter — see `run_orchestration_job`.

    `expand_mentions` is the enqueuer's word on whose text `message` is: a
    member's (in-app chat, an API key, a schedule — `@[path]` reads the vault)
    or a widget visitor's or a channel's (it never does). Off unless said.

    `visitor` is the enqueuer's word on whose *turn* it is (F280): `None` for a
    member, or `VisitorScope.to_dict()` — `{"surface": "widget", "tools": [...]}`
    — for a stranger on a public surface, whose turn then holds only those
    tools, no memory, and no approval card (`core.visitor`). A widget or a
    channel that enqueues without it runs its visitor as a member.

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

    redis = ctx["redis"]
    session_factory = ctx["session_factory"]
    leased = False
    images = images or []

    # Set before the `try`, so the `except` can tell "we never got far enough to
    # have a publisher" from "the turn failed with one". See below.
    publisher = None
    # Whether this turn's exchange is already in `history`: an error after the
    # answer was recorded (its webhook, say) must not replace it (F133).
    recorded = False

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
        history = await _load_chat_history(session_factory, session_id, agent_id)

        if ctx.get("job_try", 1) > 1:
            # Handed out again: the first try was stopped mid-turn (a deploy, a
            # worker shutting down). A chat turn has no checkpoint, so running
            # it again started from scratch — and any tool it had already called
            # ran twice (F89). It is not rerun: the turn says it was
            # interrupted, and the person decides whether to send it again.
            from core.scale.pubsub import ChatEventPublisher
            publisher = ChatEventPublisher(redis, session_id, ttl=cfg.pubsub_event_ttl)
            await publisher.publish({"type": "error", "message": INTERRUPTED_TURN})
            await publisher.publish_done()
            history.append({"role": "user", "content": message})
            history.append({"role": "assistant", "content": INTERRUPTED_TURN})
            await _upsert_chat_session(session_factory, session_id, agent_id, "failed", history)
            _log.info(f"[worker] chat {session_id}: turn handed out again (try {ctx['job_try']}) — not rerun")
            return {"status": "interrupted", "session_id": session_id}

        await _upsert_chat_session(session_factory, session_id, agent_id, "running", history)

        from core.scale.pubsub import ChatEventPublisher
        publisher = ChatEventPublisher(redis, session_id, ttl=cfg.pubsub_event_ttl)

        # This tenant's MCP session set, from the bounded pool. It merges the
        # process-wide native servers with the tenant's own, so what the engine
        # sees is the shape `ctx["server_module"]` used to be — except that it
        # is this tenant's, not whoever's was current at worker startup.
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
        # The last turn-boundary snapshot, and whether this turn stopped to ask
        # a person something. Both are read after `_consume` returns, which is
        # why they are declared out here.
        latest_progress: dict | None = None
        paused_on: dict | None = None
        # The calls made since that snapshot, from their events (F242).
        events_since_progress: list[dict] = []

        async def _consume():
            """Drive the loop. Separated so the cancel watcher has a task to
            cancel that is not the whole job."""
            nonlocal response_text, total_cost, latest_progress, paused_on
            async for event in _visitor_turn(visitor, run_react_loop(
                chat_request, server_module,
                progress_events=True, resume_state=resume_state,
                expand_mentions=expand_mentions,
            )):
                etype = event.get("type")

                # Engine state, not an event. It is what a resume is rebuilt
                # from, and forwarding it would put a whole turn's context on
                # a Redis stream the browser reads.
                if etype == "_step_progress":
                    latest_progress = {k: v for k, v in event.items() if k != "type"}
                    events_since_progress.clear()
                    continue
                if etype in ("tool_execution", "tool_result"):
                    events_since_progress.append(event)

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
        cancelled = await _run_chat_turn_cancellably(redis, session_id, _consume)

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
            await _sessions.set_pending_approval(
                session_id, agent_id or "default",
                parked_payload(paused_on, message=message, progress=latest_progress),
            )
            await _upsert_chat_session(session_factory, session_id, agent_id, "paused", history)
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
        from core.session import image_notes, turn_calls, tools_from_calls
        question = {"role": "user", "content": message}
        if images:
            question["images"] = image_notes(images)
        history.append(question)
        calls = turn_calls(latest_progress, events_since_progress)
        answer_entry = {"role": "assistant", "content": response_text}
        if calls:
            answer_entry.update(tools=tools_from_calls(calls), calls=calls)
        history.append(answer_entry)
        recorded = True
        await _upsert_chat_session(session_factory, session_id, agent_id,
                                   "cancelled" if cancelled else "completed", history)
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
            if not recorded:
                from core.chat_turns import FAILED_ANSWER
                history.append({"role": "user", "content": message})
                history.append({"role": "assistant", "content": FAILED_ANSWER})
            await _upsert_chat_session(session_factory, session_id, agent_id, "failed", history)
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
    ctx["worker_id"] = _worker_id

    _log.debug(f"[worker] startup complete. MCP disabled: {server_module.mcp_disabled}")


async def worker_shutdown(ctx: dict) -> None:
    """Mark worker offline, cancel heartbeat, close connections."""
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
    job_completion_wait = int(os.getenv("WORKER_JOB_COMPLETION_WAIT", "45"))
    on_startup = worker_startup
    on_shutdown = worker_shutdown
    redis_settings = _build_redis_settings()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _load_orchestration(session_factory, orch_id: str):
    """Load orchestration definition from Postgres."""
    from sqlalchemy import select
    from core.scale.models_db import OrchestrationDB
    from core.models_orchestration import Orchestration

    async with session_factory() as session:
        result = await session.execute(
            select(OrchestrationDB).where(OrchestrationDB.id == orch_id)
        )
        row = result.scalar_one_or_none()

    if row is None:
        raise ValueError(f"Orchestration '{orch_id}' not found in Postgres. Run Sync first.")

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


async def _upsert_chat_session(
    session_factory,
    session_id: str,
    agent_id: str | None,
    status: str,
    messages: list,
) -> None:
    """Create or update a chat session row."""
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


async def _gave_up(ctx: dict, job_function: str, run_id: str, orch_id: str | None) -> bool:
    """On the delivery past the retry policy: fail and dead-letter the run, not run it.

    A job is handed out again only after the previous try was stopped — a worker
    shutting down, a deploy, a kill. Once that has happened as often as the
    policy allows, ARQ would fail the next delivery without calling us, and the
    run stayed `running` with no dead letter until an API restart swept it. The
    worker now takes one more delivery than the policy, and on it the job does
    no work: the run is failed and dead-lettered, and Retry resumes it on purpose.
    """
    attempt = ctx.get("job_try", 1)
    if attempt <= job_retries(ctx):
        return False
    await _dead_job(ctx, job_function, run_id, orch_id, RuntimeError(
        f"Stopped {attempt - 1} times without finishing (the worker running it shut "
        f"down or was killed each time), so it was not run again. Retry resumes it."
    ), tb="")
    return True


async def _dead_job(ctx: dict, job_function: str, run_id: str, orch_id: str | None, exc: Exception,
                    tb: str | None = None) -> None:
    """Record a job that died outside the engine: run failed, stream ended, dead-lettered.

    Every step is best-effort — this runs while an exception is on its way to
    ARQ, and a second failure here must not replace it.

    The run keeps its position (`current_step_id` is not touched), so retrying
    the dead letter resumes it. A run that had already completed is left
    alone: the resume that raised was refused because there was nothing to do.
    """
    session_factory = ctx["session_factory"]
    tb = traceback.format_exc() if tb is None else tb
    _log.warning(f"[worker] {job_function} died for run {run_id}: {exc}")

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
        await publisher.publish({"type": "orchestration_error", "error": str(exc)})
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
    except Exception as e:
        _log.warning(f"[worker] failed to write DLQ for run {run_id}: {e}")
