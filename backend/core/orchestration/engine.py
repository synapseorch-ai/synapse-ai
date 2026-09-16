"""
OrchestrationEngine -- walks a graph of steps, managing shared state,
checkpointing, loop guards, and yielding SSE events.
"""
import asyncio
import calendar
import json
import time
from typing import AsyncGenerator, Awaitable, Callable

import anyio

from core.config import ORCH_GLOBAL_TIMEOUT_MIN, ORCH_STEP_TIMEOUT
from core.models_orchestration import Orchestration, OrchestrationRun, StepConfig, StepType
from core.state_types import matches_declared_type, normalize_state_type
from .state import SharedState
from .steps import STEP_EXECUTORS, extract_all_json
from .logger import OrchestrationLogger

from core.log import get_logger

_log = get_logger(__name__)


def _seconds_since(iso_timestamp: str | None) -> float | None:
    """Wall-clock seconds since an ISO-8601 Z timestamp, or None if unparseable.

    Used for a human step's duration, which is measured across a checkpoint and
    possibly across processes — so the two ends cannot share a monotonic clock
    and the stored timestamp is all there is.
    """
    if not iso_timestamp:
        return None
    try:
        started = calendar.timegm(time.strptime(iso_timestamp, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None
    return round(max(0.0, time.time() - started), 2)


MAX_NESTED_DEPTH = 3


#: Step types with no model to override: they run no LLM at all.
_MODEL_FREE_STEP_TYPES = {"end", "human", "merge", "parallel", "loop", "print", "transform"}


def apply_model_override(orchestration: "Orchestration", model: str | None) -> None:
    """Point every LLM-backed step at `model`, in place.

    A per-run choice expressed through the per-step field the engine already
    reads, so nothing downstream needs to learn about it. A step that names its
    own model is overridden too — the caller picking a model for the run means
    the run, not "the steps that had not decided".
    """
    if not model:
        return
    for step in orchestration.steps:
        if step.type not in _MODEL_FREE_STEP_TYPES:
            step.model = model


class OrchestrationEngine:
    """Runs an orchestration by walking through its step graph."""

    def __init__(
        self,
        orchestration: Orchestration,
        server_module,
        depth: int = 0,
        cancel_hook: Callable[[], Awaitable[bool]] | None = None,
    ):
        self.orch = orchestration
        self.server_module = server_module
        self.depth = depth  # 0 = top-level run; >0 = nested sub-orchestration
        # Optional async callable returning True when the run should be cancelled.
        # Used by distributed workers to check Redis instead of the in-memory set.
        # V1 never sets this — cancel_hook=None uses the existing in-memory check.
        self.cancel_hook = cancel_hook
        self.step_map: dict[str, StepConfig] = {s.id: s for s in orchestration.steps}
        self.executors = STEP_EXECUTORS
        # Populated by the run/resume entry points, not here: __init__ cannot
        # await, and reading the store from a constructor would make every
        # construction site — including thirteen in tests — need a live store
        # just to build an engine. It is display attribution only, so an empty
        # mapping until the run starts costs nothing.
        self.agent_names: dict[str, str] = {}
        #: The tenant's `{{ global.* }}` values, loaded once per run/resume
        #: (core/variables.py); prompt templates render against it.
        self.global_variables: dict = {}
        #: Attachments for this turn, given to steps that set `pass_images`.
        #: Declared here rather than set ad hoc by the caller so `resume` and
        #: the nested-run path have something real to carry, and so a step
        #: reading it never depends on whether a caller happened to set it.
        #:
        #: NOT shared state, deliberately: that is echoed to the browser twice
        #: per run, checkpointed at every step boundary and fed to the memory
        #: summariser. Fine for a model name, not for 20 MB of image.
        self.images: list[str] = []
        #: Agents (and their tool sets) resolved for this run, keyed by agent id
        #: — see core/orchestration/resolved.py. Nothing in it changes while a
        #: run is in flight, so it is never invalidated, only discarded with
        #: the engine.
        self._resolved: dict = {}
        self._custom_tools: list | None = None
        #: The run's `@[path]` reader (core/vault.py), made on first use so an
        #: engine built in a test needs no settings. One per run: a file ten
        #: steps mention is read once; a resume builds a new engine and reads
        #: the file as it is then.
        self._mentions = None
        self.current_transition = None  # set by _execute_loop before each step

    def mention_reader(self):
        """The run's `@[path]` reader — see `context.build_step_prompt`."""
        if self._mentions is None:
            from core.vault import MentionReader
            self._mentions = MentionReader()
        return self._mentions

    async def _load_global_variables(self) -> dict:
        """The tenant's global variables, or `{}` — a missing document is not
        an error, and a store hiccup must not fail the run."""
        try:
            from core import variables
            return await variables.load()
        except Exception:
            return {}

    async def _load_agent_names(self) -> dict[str, str]:
        """Load agent_id -> name mapping for context attribution."""
        try:
            from core.routes.agents import load_user_agents
            agents = await load_user_agents()
            return {a["id"]: a["name"] for a in agents}
        except Exception:
            return {}

    #: Shared-state key naming the model every LLM-backed step should use for
    #: this run. It lives in the state rather than in a run() argument on
    #: purpose: the state is checkpointed, so a run that pauses on a human step
    #: and resumes on a different worker still uses the model the user picked.
    MODEL_OVERRIDE_KEY = "_model_override"

    async def run(
        self,
        initial_input: str,
        run_id: str,
        session_id: str | None = None,
        initial_state: dict | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Execute the orchestration from the entry step. Yields SSE-compatible events.

        `initial_state`, if provided, is merged into shared_state after schema
        defaults and `user_input` are applied — letting callers pre-populate
        context (e.g. current_orchestration_id, selected_agent_ids).
        """
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Agent display names, for step attribution in the event stream. Read
        # here rather than in __init__, which cannot await.
        self.agent_names = await self._load_agent_names()
        self.global_variables = await self._load_global_variables()

        shared_state = self._init_state(initial_input)
        if initial_state:
            shared_state.update(initial_state)
        apply_model_override(self.orch, shared_state.get(self.MODEL_OVERRIDE_KEY))

        run = OrchestrationRun(
            run_id=run_id,
            orchestration_id=self.orch.id,
            session_id=session_id,
            shared_state=shared_state,
            current_step_id=self.orch.entry_step_id,
            started_at=now,
        )
        state = SharedState(run)

        # Initial checkpoint: make the run visible to /runs listings and
        # status polling from second zero. Without it the run file only
        # appears at the first step boundary, so a run inside a long first
        # step is invisible to the active-runs UI (and a crash there leaves
        # no file to resume from). The startup zombie sweep handles these
        # like any other interrupted "running" checkpoint.
        await state.checkpoint()

        self.logger = OrchestrationLogger(
            run_id=run_id,
            orchestration_id=self.orch.id,
            orchestration_name=self.orch.name,
            user_input=initial_input,
            session_id=session_id,
        )

        yield {
            "type": "orchestration_start",
            "run_id": run_id,
            "orchestration_id": self.orch.id,
            "orchestration_name": self.orch.name,
        }

        async for event in self._execute_loop(run, state):
            yield event

    async def _execute_loop(self, run: OrchestrationRun, state: SharedState) -> AsyncGenerator[dict, None]:
        """Core execution loop — shared between run() and resume()."""
        total_turns = self._engine_turns(run)
        logger = getattr(self, "logger", None)
        loop_start_time = time.time()
        timeout_seconds = (self.orch.timeout_minutes or ORCH_GLOBAL_TIMEOUT_MIN) * 60

        while run.current_step_id and run.status == "running":
            # Checkpoint between steps — ensures MCP background tasks
            # (receive_loop, stdio reader/writer) get event-loop time
            # before the next step starts sending new requests.
            await anyio.sleep(0)

            # Check if this run was cancelled via the cancel endpoint.
            # cancel_hook (set by distributed workers) checks Redis; the default
            # path checks the in-memory _cancelled_run_ids set (V1 / standalone).
            from .state import _cancelled_run_ids
            _is_cancelled = False
            if self.cancel_hook:
                try:
                    _is_cancelled = await self.cancel_hook()
                except Exception:
                    pass
            if not _is_cancelled and run.run_id in _cancelled_run_ids:
                _cancelled_run_ids.discard(run.run_id)
                _is_cancelled = True
            if _is_cancelled:
                run.status = "cancelled"
                _log.debug(f"ENGINE: 🛑 run '{run.run_id}' cancelled by request")
                break

            # Global timeout guard
            elapsed = time.time() - loop_start_time
            if elapsed > timeout_seconds:
                run.status = "failed"
                yield {"type": "orchestration_error", "error": f"Global timeout ({self.orch.timeout_minutes} min) exceeded after {round(elapsed / 60, 1)} min"}
                break

            step = self.step_map.get(run.current_step_id)
            _log.debug(f"ENGINE: ▶ step='{run.current_step_id}' type={step.type if step else None} status={run.status}")
            if not step:
                run.status = "failed"
                yield {"type": "orchestration_error", "error": f"Step '{run.current_step_id}' not found"}
                break

            executor = self.executors.get(step.type)
            if not executor:
                run.status = "failed"
                yield {"type": "orchestration_error", "error": f"No executor for step type '{step.type}'"}
                break

            # Global turn guard
            total_turns += 1
            if total_turns > self.orch.max_total_turns:
                run.status = "failed"
                yield {"type": "orchestration_error", "error": f"Global turn limit ({self.orch.max_total_turns}) exceeded"}
                break

            step_start_time = time.time()
            step_timeout = step.timeout_seconds or ORCH_STEP_TIMEOUT  # default 5 min

            # A container node's budget has to cover every step it runs inside
            # itself, and each of those already gets its own fail_after. Charging
            # the container one step's worth means a 3x loop over one agent step
            # is given 300s to do 900s of work, and the run dies mid-iteration.
            # The global timeout checked above is the real ceiling for these.
            if step.type in (StepType.LOOP, StepType.PARALLEL):
                step_timeout = max(1.0, timeout_seconds - (time.time() - loop_start_time))

            # Build and store TransitionContext so executors can access it
            from .context import build_transition_context
            transition = build_transition_context(step, run, self)
            self.current_transition = transition

            # Log step start
            agent_name = self.agent_names.get(step.agent_id, "") if step.agent_id else None
            if logger:
                logger.step_start(step.id, step.name, step.type.value, step.agent_id, agent_name)

            yield {
                "type": "step_start",
                "orch_step_id": step.id,
                "step_name": step.name,
                "step_type": step.type.value,
            }

            # What shared state looked like before this step ran. The diff
            # against it is how `_type_written_state` knows which keys this step
            # wrote — `output_key` alone misses every multi-write step: parallel
            # branches, merge, a nested orchestration, a loop body.
            state_before = dict(run.shared_state)

            try:
                # anyio.fail_after installs a cancel scope on the CURRENT task —
                # unlike asyncio.wait_for which creates a new Task and breaks
                # anyio-based MCP sessions (their _receive_loop wakeup never
                # reaches a waiter in a different Task's context).
                # This properly interrupts a stuck session.call_tool() inside
                # the executor when the step deadline is reached.
                #
                # IMPORTANT: this scope stays open across the yields below, so
                # this generator must be advanced — and closed — from a single
                # task, or anyio raises "cancel scope in a different task".
                # core.orchestration.runner is what guarantees that; every
                # entry point runs us through it (issue #356). Do not drive
                # the engine directly from a task-per-pull consumer.
                #
                # human_input_required is still yielded outside the scope: the
                # generator is abandoned at that point, and Python's GC
                # finalizer runs aclose() on abandoned async generators in a
                # NEW asyncio Task. We break out of the scope cleanly (in the
                # current task) and yield the event below.
                human_input_event: dict | None = None
                try:
                    with anyio.fail_after(step_timeout):
                        async for event in executor.execute(step, run, self):
                            # Feed every event to the logger
                            if logger:
                                logger.log_event(event)

                            # _log_ prefixed events are metadata for the logger only
                            if event.get("type", "").startswith("_log_"):
                                continue

                            if event.get("type") == "human_input_required":
                                run.waiting_for_human = True
                                run.status = "paused"
                                run.current_step_id = step.id
                                # Track the sub-run that needs human input when the
                                # request originated inside a nested orchestration.
                                if event.get("nested_run_id"):
                                    run.nested_run_id = event["nested_run_id"]
                                    run.nested_orch_id = event.get("nested_orch_id")
                                else:
                                    run.nested_run_id = None
                                    run.nested_orch_id = None
                                # A human step is a step. It used to be the one
                                # kind that never reached `step_history`: the
                                # break below skips the "record step completion"
                                # block, and `resume()` moves past it. So an
                                # approval was invisible to the run detail view,
                                # uncounted by billing, and — worse — invisible
                                # to `_apply_loop_guard`, which counts entries
                                # per step id, so a loop containing a human step
                                # could iterate without limit.
                                #
                                # Recorded here as "paused" and completed on
                                # resume, so history means "steps that started"
                                # whether or not the run is still waiting.
                                run.step_history.append({
                                    "step_id": step.id,
                                    "step_name": step.name,
                                    "step_type": step.type.value,
                                    "status": "paused",
                                    "started_at": time.strftime(
                                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(step_start_time)
                                    ),
                                    "output_key": step.output_key,
                                })
                                await state.checkpoint()
                                if logger:
                                    logger.step_end(step.id, "paused")
                                    logger.run_end("paused")
                                    await logger.aclose()
                                # Break exits the async-for and then the
                                # fail_after scope cleanly in this task.
                                # The event is yielded below, outside the scope,
                                # so the generator suspends with no cancel scope
                                # active — safe for cross-task GC finalization.
                                human_input_event = event
                                break

                            if event.get("type") == "orchestration_end":
                                yield event
                                run.status = "completed"
                                if logger:
                                    logger.step_end(step.id, "completed")
                                break

                            yield event
                except TimeoutError:
                    yield {
                        "type": "step_error", "orch_step_id": step.id,
                        "error": f"Step '{step.name}' timed out after {step_timeout}s",
                    }
                    run.step_history.append({
                        "step_id": step.id, "step_name": step.name,
                        "step_type": step.type.value, "status": "failed",
                        "error": f"Timed out after {step_timeout}s",
                    })
                    run.status = "failed"
                    if logger:
                        logger.step_end(step.id, "failed", f"Timed out after {step_timeout}s")

                # Yield human_input_required OUTSIDE the anyio cancel scope.
                # The generator suspends here with no active cancel scope, so
                # Python's GC-triggered aclose() (which runs in a new asyncio
                # Task) won't hit "cancel scope in a different task".
                if human_input_event is not None:
                    yield human_input_event
                    return

                # A step writes whatever it produced — nearly always text — and
                # until now `state_schema.type` was read by nothing at run time.
                # Type it here, before the checkpoint below and before the break
                # on `completed`, so the value that is persisted and the value
                # the next step reads are the declared one. Skipped on `failed`:
                # a step that did not finish has nothing to say, and a run that
                # has already failed does not need a second line of noise.
                unconvertible: list[tuple[str, str]] = []
                if run.status != "failed":
                    typing_warnings, unconvertible = self._type_written_state(
                        run, step, state_before)
                    for warning in typing_warnings:
                        if logger:
                            logger.log_event(warning)
                        yield warning

                # If END step or timeout set status, break out
                if run.status in ("completed", "failed"):
                    break

                # Record step completion
                step_duration = round(time.time() - step_start_time, 2)
                run.step_history.append({
                    "step_id": step.id,
                    "step_name": step.name,
                    "step_type": step.type.value,
                    "status": "completed",
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(step_start_time)),
                    "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "duration_seconds": step_duration,
                    "output_key": step.output_key,
                })

                if logger:
                    logger.step_end(step.id, "completed")

                yield {
                    "type": "step_complete",
                    "orch_step_id": step.id,
                    "step_name": step.name,
                    "duration_seconds": step_duration,
                }

                # `strict_state_types`: text that would not become the type its
                # key declares stops the run — here, after the step is recorded
                # and announced, because the step itself completed. A history
                # that quietly loses it is a worse record than one saying "this
                # ran, and the run stopped because of what it wrote".
                if unconvertible and self.orch.strict_state_types:
                    run.status = "failed"
                    named = ", ".join(f"'{k}' ({t})" for k, t in unconvertible)
                    error = (
                        f"Step '{step.name}' wrote text that will not become the "
                        f"declared type of {named}. Stopped because this "
                        f"orchestration has strict state types on."
                    )
                    if logger:
                        logger.run_end("failed")
                    yield {"type": "orchestration_error", "error": error}
                    try:
                        await state.checkpoint()
                    except Exception as ckpt_err:
                        _log.warning(f"strict-type checkpoint failed: {ckpt_err}")
                    break

                # Resolve next step
                next_step_id, extra_events = self._resolve_next(step, run)
                for extra_event in extra_events:
                    if logger:
                        logger.log_event(extra_event)
                    yield extra_event
                run.current_step_id = next_step_id
                _log.debug(f"ENGINE: 💾 checkpointed → next='{run.current_step_id}'")
                await state.checkpoint()

            except Exception as e:
                import traceback; _log.debug(f"ENGINE: ❌ EXCEPTION in step '{step.id}': {e}\n{traceback.format_exc()}")
                # The stream gets a sanitised message — that choice was
                # deliberate, an exception's text can carry anything. The run
                # record gets the real one, bounded: an operator reading a
                # failed run's history should not have to grep worker stdout
                # to learn whether it was a 401 or a KeyError.
                safe_error = "An internal error occurred while executing this step."
                real_error = f"{type(e).__name__}: {str(e)[:300]}"
                run.step_history.append({
                    "step_id": step.id,
                    "step_name": step.name,
                    "step_type": step.type.value,
                    "status": "failed",
                    "error": real_error,
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(step_start_time)),
                    "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "duration_seconds": round(time.time() - step_start_time, 2),
                })
                run.status = "failed"
                if logger:
                    logger.step_end(step.id, "failed", real_error)
                yield {"type": "step_error", "orch_step_id": step.id, "error": safe_error}
                # Persist the failure here: between this and the finalize
                # checkpoint is exactly where a second crash would lose it.
                try:
                    await state.checkpoint()
                except Exception as ckpt_err:
                    _log.warning(f"failure checkpoint failed: {ckpt_err}")
                break

        # Finalize
        if run.status == "running":
            run.status = "completed"

        run.ended_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await state.checkpoint()

        # Memory-enabled agents keep a summary of what this run did — the
        # crisp end signal chat never has. Never allowed to fail the run.
        try:
            await self._write_memory_summaries(run)
        except Exception as exc:
            _log.warning(f"run memory summaries failed: {exc}")

        # An LLM judge scores the finished run against the rubric bound to this
        # orchestration, if one is. Here rather than in a route or a worker job
        # because `_execute_loop` is the one place every path passes through:
        # `run()` delegates to it and `WorkerEngineAdapter.resume()` enters it
        # directly, so an in-process run, an in-process resume, a fleet run and
        # a fleet resume all reach this line. Anything hung off the runner pump
        # would be invisible to the fleet, which is the mistake this codebase
        # has now made twice. Never allowed to fail the run.
        try:
            await self._score_run(run)
        except Exception as exc:
            _log.warning(f"run scoring failed: {exc}")

        if logger:
            logger.run_end(run.status)
            await logger.aclose()

        final_output = self._build_final_response(run)

        yield {
            "type": "orchestration_complete",
            "run_id": run.run_id,
            "status": run.status,
            "final_state": run.shared_state,
        }

        yield {
            "type": "final",
            "response": final_output,
            "intent": "orchestration",
            "data": {
                "run_id": run.run_id,
                "status": run.status,
                "step_history": run.step_history,
                "shared_state": run.shared_state,
            },
            "tool_name": None,
        }

    @classmethod
    async def resume_failed(
        cls, run_id: str, server_module
    ) -> AsyncGenerator[dict, None]:
        """Resume a failed or cancelled orchestration from where it stopped.

        For failed runs: the failed step's history entry is removed so it re-executes.
        For cancelled runs: resumes from current_step_id (which was never started).
        All state accumulated by prior steps is preserved.

        Mid-node continuity: agent/loop/parallel executors checkpoint private
        progress keys in shared_state (_step_progress_*, _loop_progress_*,
        _parallel_progress_*) as work completes, so the re-executed node
        continues from its last turn/iteration/branch instead of starting
        from scratch. The seeding happens inside the executors — this method
        just replays the node.
        """
        from .state import SharedState as SS, _cancelled_run_ids

        restored = await SS.restore(run_id)
        run = restored.run

        if run.status not in ("failed", "cancelled"):
            yield {"type": "orchestration_error", "error": f"Run is not recoverable (status={run.status})"}
            return

        from core.scale.context import resolve_orchestration
        orch_data = await resolve_orchestration(run.orchestration_id)
        if not orch_data:
            yield {"type": "orchestration_error", "error": f"Orchestration '{run.orchestration_id}' not found"}
            return

        orch = Orchestration.model_validate(orch_data)
        apply_model_override(orch, run.shared_state.get(cls.MODEL_OVERRIDE_KEY))
        engine = cls(orch, server_module)
        engine.global_variables = await engine._load_global_variables()
        engine.agent_names = await engine._load_agent_names()

        engine.logger = OrchestrationLogger(
            run_id=run_id,
            orchestration_id=run.orchestration_id,
            orchestration_name=orch.name,
            user_input=f"(resumed from {run.status})",
            session_id=run.session_id,
        )

        # For failed runs: remove the last failed step so it re-executes
        if run.status == "failed" and run.step_history and run.step_history[-1].get("status") == "failed":
            run.step_history.pop()

        # Clear any stale cancel signal left by cancel_run so _execute_loop
        # doesn't immediately re-cancel on the first iteration.
        _cancelled_run_ids.discard(run_id)

        run.status = "running"

        state = SharedState(run)
        # Persist "running" before the first step so the run reappears in the
        # active-runs listing immediately. Without this the checkpoint still
        # reads failed/cancelled until the first step boundary, and a resumed
        # run looks dead for as long as its first step takes.
        await state.checkpoint()
        async for event in engine._execute_loop(run, state):
            yield event

    @classmethod
    async def resume(
        cls, run_id: str, human_response: dict, server_module, images: list[str] | None = None
    ) -> AsyncGenerator[dict, None]:
        """Resume a paused orchestration after human input.

        `images` are the attachments sent *with the answer*, not the ones the
        run started with — those are not persisted anywhere, by design, so a
        step re-entered after a pause sees only what the person attached to
        their reply.
        """
        from .state import SharedState as SS

        _log.debug(f"[engine.resume] ▶ restoring state from JSON checkpoint for run_id={run_id}")
        restored = await SS.restore(run_id)
        run = restored.run
        _log.debug(f"[engine.resume] 📋 restored run: orch_id={run.orchestration_id} status={run.status} current_step_id={run.current_step_id} waiting_for_human={run.waiting_for_human} step_history_len={len(run.step_history)}")

        # Load the orchestration definition
        from core.scale.context import resolve_orchestration
        orch_data = await resolve_orchestration(run.orchestration_id)
        if not orch_data:
            _log.debug(f"[engine.resume] ❌ orchestration '{run.orchestration_id}' NOT FOUND in the store")
            yield {"type": "orchestration_error", "error": f"Orchestration '{run.orchestration_id}' not found"}
            return

        orch = Orchestration.model_validate(orch_data)
        apply_model_override(orch, run.shared_state.get(cls.MODEL_OVERRIDE_KEY))
        engine = cls(orch, server_module)
        engine.global_variables = await engine._load_global_variables()
        engine.images = list(images or [])
        _log.debug(f"[engine.resume] 🗺  step_map keys: {list(engine.step_map.keys())}")

        # Create logger — appends to existing log file if present
        engine.logger = OrchestrationLogger(
            run_id=run_id,
            orchestration_id=run.orchestration_id,
            orchestration_name=orch.name,
            user_input=f"(resumed) human_response={human_response}",
            session_id=run.session_id,
        )

        # If the parent was paused because a NESTED orchestration hit a human step,
        # resume the sub-run and let it complete before continuing the parent.
        if run.nested_run_id:
            _log.debug(f"[engine.resume] 🔗 nested run detected: nested_run_id={run.nested_run_id}, delegating to _resume_nested_orch")
            async for event in cls._resume_nested_orch(run, engine, human_response, server_module):
                yield event
            return

        # A tool call inside an agent step, not a human step: the answer is an
        # approval for that call, and the step has to be re-entered rather than
        # stepped past — its work is unfinished, and the transcript that got it
        # this far is in its progress record.
        _pending = (run.step_progress.get(run.current_step_id) or {}).get("pending_approval")
        if _pending:
            async for event in cls._resume_tool_approval(run, engine, human_response, _pending):
                yield event
            return

        # Normal path: human step is directly in this orchestration.
        # Move to next step after the HUMAN step
        current_step = engine.step_map.get(run.current_step_id)
        _log.debug(f"[engine.resume] 🔍 current_step lookup: current_step_id={run.current_step_id!r} → found={current_step is not None} type={current_step.type.value if current_step else 'N/A'}")

        # Write human response to shared state under the step's configured output_key.
        # Falling back to "human_response" preserves backward-compat for steps with no output_key.
        output_key = (current_step.output_key if current_step else None) or "human_response"
        run.shared_state[output_key] = human_response
        # Always keep "human_response" as well so evaluators that check it still work.
        if output_key != "human_response":
            run.shared_state["human_response"] = human_response

        run.waiting_for_human = False
        run.status = "running"

        if current_step:
            # Before _resolve_next, so the loop guard counts this iteration.
            engine._complete_human_step(run, current_step)
            next_id, resume_events = engine._resolve_next(current_step, run)
            run.current_step_id = next_id
            # A routing decision, a loop_limit_reached, or both. Dropping them
            # meant a run that hit its iteration cap on the step right after a
            # human pause said nothing about why it moved on.
            for resume_event in resume_events:
                yield resume_event
            _log.debug(f"[engine.resume] ➡  _resolve_next({run.current_step_id!r} was HUMAN) → next_step_id={next_id!r}")
        else:
            _log.debug(f"[engine.resume] ⚠️  current_step not found in step_map — current_step_id stays as {run.current_step_id!r}, loop will likely fail immediately")

        _log.debug(f"[engine.resume] 🚀 entering _execute_loop with current_step_id={run.current_step_id!r} status={run.status}")
        engine.agent_names = await engine._load_agent_names()
        state = SharedState(run)
        # Persist "running" + the cleared human flag before the first step, so
        # the run stops showing as "needs input" the moment it resumes.
        await state.checkpoint()
        async for event in engine._execute_loop(run, state):
            yield event
        _log.debug(f"[engine.resume] 🏁 _execute_loop finished, run.status={run.status} run.current_step_id={run.current_step_id!r}")

    @staticmethod
    def _approved(human_response) -> tuple[bool, str]:
        """Read a yes or a no out of whatever the answer arrived as.

        The approval card sends `{"approve": bool}`. A messaging channel or a
        chat reply sends prose. An unreadable answer is a refusal: the whole
        point of the pause is that nobody wanted this to run unattended.
        """
        if isinstance(human_response, dict):
            for key in ("approve", "approved", "confirm", "response"):
                if key in human_response:
                    value = human_response[key]
                    if isinstance(value, bool):
                        return value, str(human_response.get("reason") or "")
                    text = str(value)
                    break
            else:
                text = str(human_response)
        else:
            text = str(human_response)
        cleaned = text.strip().lower()
        yes = cleaned in ("y", "yes", "true", "1", "ok", "okay", "approve", "approved", "allow", "go")
        return yes, "" if yes else (text.strip() or "the user declined")

    @classmethod
    async def _resume_tool_approval(cls, run, engine, human_response, pending):
        """Continue an agent step that stopped to ask about one tool call."""
        step = engine.step_map.get(run.current_step_id)
        approved, reason = cls._approved(human_response)
        approval_id = pending.get("approval_id")
        _log.debug(f"[engine.resume] 🔐 tool approval for '{pending.get('tool_name')}' → "
                   f"{'approved' if approved else 'refused'}")

        progress = dict(run.step_progress.get(run.current_step_id) or {})
        progress.pop("pending_approval", None)
        # The call itself, so the step can act on the answer without asking the
        # model to make the same choice again — it might not, and then an
        # approval a person gave would quietly do nothing.
        progress["pending_call"] = {
            "approval_id": approval_id,
            "tool": pending.get("tool_name"),
            "arguments": pending.get("tool_args") or {},
            "approved": approved,
            "reason": reason,
        }
        bucket = "approved_calls" if approved else "denied_calls"
        progress[bucket] = sorted(set(progress.get(bucket) or []) | {approval_id})
        if not approved and reason:
            progress["denied_reasons"] = {**(progress.get("denied_reasons") or {}), approval_id: reason}
        run.step_progress[run.current_step_id] = progress

        run.waiting_for_human = False
        run.status = "running"
        # The step is unfinished, so its history entry is reopened rather than
        # closed: it did not complete when it paused.
        for entry in reversed(run.step_history):
            if entry.get("step_id") == run.current_step_id and entry.get("status") == "paused":
                entry["status"] = "running"
                break

        yield {"type": "tool_approval_resolved", "orch_step_id": run.current_step_id,
               "tool_name": pending.get("tool_name"), "approved": approved,
               "step_name": step.name if step else ""}

        state = SharedState(run)
        await state.checkpoint()
        async for event in engine._execute_loop(run, state):
            yield event

    @classmethod
    async def _resume_nested_orch(
        cls,
        run: OrchestrationRun,
        engine: "OrchestrationEngine",
        human_response: dict,
        server_module,
    ) -> AsyncGenerator[dict, None]:
        """Resume a parent run whose nested sub-orchestration was paused at a human step.

        Resumes the sub-run, forwards its events tagged with the parent step's
        context, then writes the sub-orch result to parent shared_state and
        continues the parent execution loop.
        """
        parent_step = engine.step_map.get(run.current_step_id)
        nested_run_id = run.nested_run_id
        nested_orch_id = run.nested_orch_id
        _NESTED_FILTER_TYPES = {"orchestration_start", "orchestration_complete", "orchestration_end"}

        final_response: str | None = None
        sub_events: list[dict] = []

        async for sub_event in cls.resume(nested_run_id, human_response, server_module):
            sub_events.append(sub_event)

            # Sub-orch hit another human step (multi-turn human interaction inside nested orch)
            if sub_event.get("type") == "human_input_required":
                run.nested_run_id = sub_event.get("nested_run_id") or nested_run_id
                run.nested_orch_id = sub_event.get("nested_orch_id") or nested_orch_id
                state = SharedState(run)
                await state.checkpoint()
                yield {
                    **sub_event,
                    "run_id": run.run_id,
                    "orch_step_id": run.current_step_id,
                    "step_name": parent_step.name if parent_step else "",
                    "nested_run_id": nested_run_id,
                    "nested_orch_id": nested_orch_id,
                }
                return

            if sub_event.get("type") == "final" and sub_event.get("intent") == "orchestration":
                final_response = sub_event.get("response", "")

            # Skip sub-orch lifecycle meta-events (same filter as initial execution)
            if sub_event.get("type") in _NESTED_FILTER_TYPES:
                continue

            yield {
                **sub_event,
                "run_id": run.run_id,
                "orch_step_id": run.current_step_id,
                "step_name": parent_step.name if parent_step else "",
                "nested_run_id": nested_run_id,
                "nested_orch_id": nested_orch_id,
            }

        # Fallback: extract result from orchestration_complete if no "final" event was emitted
        if final_response is None:
            from core.models_orchestration import Orchestration
            from core.scale.context import resolve_orchestration
            sub_orch_data = await resolve_orchestration(nested_orch_id)
            if sub_orch_data:
                sub_orch = Orchestration.model_validate(sub_orch_data)
                for ev in reversed(sub_events):
                    if ev.get("type") == "orchestration_complete":
                        state_data = ev.get("final_state") or {}
                        for sub_step in reversed(sub_orch.steps):
                            if sub_step.output_key and sub_step.output_key in state_data:
                                final_response = str(state_data[sub_step.output_key])
                                break
                        break

        if final_response is None:
            run.status = "failed"
            state = SharedState(run)
            await state.checkpoint()
            yield {
                "type": "orchestration_error",
                "error": "Nested orchestration failed to produce a result after human input",
            }
            return

        # Write sub-orch result into parent shared_state
        if parent_step and parent_step.output_key:
            run.shared_state[parent_step.output_key] = final_response

        # Record agent step completion in history
        import time as _time
        run.step_history.append({
            "step_id": run.current_step_id,
            "step_name": parent_step.name if parent_step else "",
            "step_type": "agent",
            "status": "completed",
            "ended_at": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
        })

        # Clear nested context and resume parent from next step after agent step
        run.nested_run_id = None
        run.nested_orch_id = None
        run.waiting_for_human = False
        run.status = "running"

        if parent_step:
            next_id, extra_events = engine._resolve_next(parent_step, run)
            for extra_event in extra_events:
                yield extra_event
            run.current_step_id = next_id

        state = SharedState(run)
        # Same as resume(): make the un-paused state visible before step one.
        await state.checkpoint()
        async for event in engine._execute_loop(run, state):
            yield event

    def _type_written_state(
        self, run: OrchestrationRun, step: StepConfig, before: dict
    ) -> tuple[list[dict], list[tuple[str, str]]]:
        """Give the keys this step wrote the type `state_schema` declares.

        Mutates `run.shared_state` in place and returns
        `(warnings, unconvertible)` — the `step_warning` events the caller
        should yield, and the `(key, declared type)` pairs whose **text** would
        not become the declared structure. Only that second list is what `strict_state_types` stops a
        run for: text that will not parse is a step doing the wrong thing,
        while a value that is simply of another type is a schema that disagrees
        with reality, which is worth saying and not worth stopping for.

        A plain function rather than a generator, so the cancel-scope reasoning
        around the step's `fail_after` stays exactly as it was, and so this is
        testable without an engine.

        The rule, per key this step wrote that the schema declares:

          - a type we cannot read, or none at all: left alone, silently. The
            engine never guesses what a value was meant to be;
            `_validate_orchestration` is what tells the builder its schema is
            wrong.
          - already the declared type: nothing to do.
          - `None`: left alone, silently. "Unset" is not a failed conversion.
            Seventeen of the twenty-three mismatches measured on the dev stack
            were this one shape — the AI builder's own `_model_override`,
            declared `str` and None whenever there is no override.
          - a `str` under `list`/`dict`: the same three strategies
            `extract_json` uses (whole text, fenced block, brace matching). One
            result is the value; several are a list of them, which is the
            reduction that step already makes.
          - a `str` under `int`/`float`/`bool`: whole-text JSON only. Fenced
            blocks and brace matching mean nothing for a scalar.
          - parsed, but into something that still is not the declared type: the
            text is kept. Storing a dict under a key declared `list` only moves
            the type error downstream into whatever reads it, and silently
            replacing a string with a dict is a bigger surprise than leaving
            it. No wrapping a lone value in a list either — too magical.
          - would not parse: kept, with a warning naming the key, the declared
            type and what actually arrived.

        Nothing is emitted when a value is typed successfully: a `step_warning`
        is a warning, and the typed value is visible in the transcript anyway.

        Known gap, deliberate: a container step's inner writes — a loop body, a
        parallel branch, a nested orchestration — are typed when the container
        completes, not between iterations. A body step writing a `list`-declared
        key therefore still reads it as text on the *next* iteration of the same
        loop. And a top-level human step's answer is never typed at all:
        `resume()` writes it before this loop takes its pre-step snapshot, so no
        step ever counts it as written.
        """
        schema = self.orch.state_schema or {}
        if not schema:
            return [], []

        # Identity, not equality: no deep compare of a large structure, and a
        # list mutated in place is already a list — nothing to coerce.
        written = {
            key for key, value in run.shared_state.items()
            if key not in before or before[key] is not value
        }
        if step.output_key:
            written.add(step.output_key)

        warnings: list[dict] = []
        unconvertible: list[tuple[str, str]] = []
        for key in sorted(written & schema.keys()):
            entry = schema.get(key)
            if not isinstance(entry, dict):
                continue
            declared = normalize_state_type(entry.get("type"))
            if declared is None:
                continue

            value = run.shared_state.get(key)
            if value is None or matches_declared_type(declared, value):
                continue

            if not isinstance(value, str):
                warnings.append(self._type_warning(step, key, declared, type(value).__name__))
                continue

            if declared in ("list", "dict"):
                found = extract_all_json(value)
                parsed = found[0] if len(found) == 1 else (found or None)
            else:
                try:
                    parsed = json.loads(value.strip())
                except (json.JSONDecodeError, ValueError):
                    parsed = None

            if parsed is not None and matches_declared_type(declared, parsed):
                run.shared_state[key] = parsed
                continue

            arrived = ("text that is not JSON" if parsed is None
                       else f"text holding {type(parsed).__name__}")
            warnings.append(self._type_warning(step, key, declared, arrived))
            unconvertible.append((key, declared))

        return warnings, unconvertible

    @staticmethod
    def _type_warning(step: StepConfig, key: str, declared: str, arrived: str) -> dict:
        return {
            "type": "step_warning",
            "orch_step_id": step.id,
            "message": (
                f"State key '{key}' is declared {declared} but step '{step.name}' "
                f"wrote {arrived} — kept as is."
            ),
        }

    def _init_state(self, user_input: str) -> dict:
        """Initialize shared state from schema defaults + user input."""
        state = {}
        for key, schema in self.orch.state_schema.items():
            if isinstance(schema, dict) and "default" in schema:
                state[key] = schema["default"]
        state["user_input"] = user_input
        state["user_query"] = user_input
        return state

    def _resolve_next(self, step: StepConfig, run: OrchestrationRun) -> tuple[str | None, list[dict]]:
        """
        Resolve the next step ID based on step type and state.
        Returns (next_step_id, events).

        A list, because routing and the loop guard are two different facts and
        one branch can produce both. This used to return `event or loop_event`,
        and `event` is an unconditional dict for IF and SWITCH — so a branch
        target that had exhausted its iterations routed to its fallback
        correctly and never said why. Same failure class as the `step_warning`
        nothing rendered: the behaviour was right, the explanation was dropped.
        """
        # END steps have no next
        if step.type == StepType.END:
            return None, []

        # EVALUATOR — routing decision from LLM call
        if step.type == StepType.EVALUATOR and step.route_map:
            decision = run.shared_state.get(f"_routing_decision_{step.id}")
            if decision and decision in step.route_map:
                target = step.route_map[decision]
                reasoning = run.shared_state.get(f"_routing_reasoning_{step.id}", "")
                event = {
                    "type": "routing_decision",
                    # The routing step's OWN type, not the target's. All three
                    # routing step types emit this one event; `step_type` is
                    # what tells a consumer which of them spoke.
                    "step_type": step.type.value,
                    "orch_step_id": step.id,
                    "decision": decision,
                    "target_step_id": target,
                    "reasoning": reasoning,
                }
                if target is None:
                    # None = end orchestration
                    return None, [event]
                next_id = target
            else:
                # Fallback to next_step_id if no routing decision
                next_id = step.next_step_id
                event = None

            events = [event] if event else []
            # Apply loop guard on target
            if next_id:
                guarded_id, loop_event = self._apply_loop_guard(next_id, run)
                return guarded_id, events + ([loop_event] if loop_event else [])
            return next_id, events

        # IF_ELSE — deterministic true/false branch
        if step.type == StepType.IF_ELSE:
            decision = run.shared_state.get(f"_if_decision_{step.id}")
            if decision == "true" and step.if_true_step_id:
                target = step.if_true_step_id
            elif step.if_false_step_id:
                target = step.if_false_step_id
            else:
                target = step.next_step_id
            event = {
                # `if_decision` is the step executor's name for its own
                # {condition, result} event. The router borrowing it meant one
                # name carried two payloads sharing no field, so every consumer
                # read one shape and printed nothing for the other. One router
                # event for all three routing step types instead.
                "type": "routing_decision",
                "step_type": step.type.value,
                "orch_step_id": step.id,
                "decision": decision,
                "target_step_id": target,
            }
            if target:
                guarded_id, loop_event = self._apply_loop_guard(target, run)
                return guarded_id, [event] + ([loop_event] if loop_event else [])
            return target, [event]

        # SWITCH — deterministic case matching
        if step.type == StepType.SWITCH and step.switch_cases:
            matched = run.shared_state.get(f"_switch_decision_{step.id}")
            if matched is not None and matched in step.switch_cases:
                target = step.switch_cases[matched]
            else:
                target = step.switch_default_step_id
            event = {
                "type": "routing_decision",
                "step_type": step.type.value,
                "orch_step_id": step.id,
                # The matched case IS this router's decision, so it travels
                # under the one field every consumer reads. `None` means no
                # case matched — the default branch — not "no decision".
                "decision": matched,
                "target_step_id": target,
            }
            if target is None:
                return None, [event]
            if target:
                guarded_id, loop_event = self._apply_loop_guard(target, run)
                return guarded_id, [event] + ([loop_event] if loop_event else [])
            return target, [event]

        # Default linear routing
        next_id = step.next_step_id

        # Apply loop guard
        if next_id:
            guarded_id, loop_event = self._apply_loop_guard(next_id, run)
            return guarded_id, [loop_event] if loop_event else []

        return next_id, []

    def _engine_turns(self, run: OrchestrationRun) -> int:
        """How many turns this run has already spent against `max_total_turns`.

        Only steps the engine drives itself count. A loop or parallel node
        appends every body step it runs to `step_history` for the timeline,
        but the run charged the container as one turn while it was live —
        seeding the counter from the raw history length meant a for-each over
        150 items paused at a human step, then died on resume with
        "Global turn limit (100) exceeded" before running anything.

        The guard exists to stop graph cycles, and a body step's repetition is
        already bounded by its container, so leaving it out is the right count
        rather than a loosening of it. A step that is both a container child
        and reachable at top level is still excluded here; the validator warns
        about that wiring.
        """
        nested: set[str] = set()
        for s in self.orch.steps:
            nested.update(s.loop_step_ids or [])
            for branch in s.parallel_branches or []:
                nested.update(branch)
        return sum(1 for h in run.step_history if h.get("step_id") not in nested)

    def _apply_loop_guard(self, next_id: str, run: OrchestrationRun) -> tuple[str | None, dict | None]:
        """Check if the target step has exceeded its max_iterations."""
        if next_id not in self.step_map:
            return next_id, None

        exec_count = sum(1 for h in run.step_history if h["step_id"] == next_id)
        max_iter = self.step_map[next_id].max_iterations
        if exec_count >= max_iter:
            loop_event = {
                "type": "loop_limit_reached",
                "orch_step_id": next_id,
                "iterations": exec_count,
                "max_iterations": max_iter,
            }
            fallback = self.step_map[next_id].next_step_id
            return fallback, loop_event

        return next_id, None

    @staticmethod
    def _complete_human_step(run: OrchestrationRun, step) -> None:
        """Close out the `paused` entry a human step left in `step_history`.

        Called on resume, *before* `_resolve_next`, so `_apply_loop_guard` counts
        this iteration.

        `duration_seconds` here is wall-clock and therefore mostly the human's
        thinking time. That is what the step actually took; billing does not read
        it — an embedder that meters excludes paused time from a separate pause
        timestamp — but anything rendering a run's timeline wants it.

        A run that paused before this existed has no entry to close, so one is
        appended instead. Without that, an in-flight paused run would resume with
        its approval still missing from history.
        """
        started_at = None
        for entry in reversed(run.step_history):
            if entry.get("step_id") == step.id and entry.get("status") == "paused":
                started_at = entry.get("started_at")
                entry["status"] = "completed"
                entry["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                entry["duration_seconds"] = _seconds_since(started_at)
                return

        run.step_history.append({
            "step_id": step.id,
            "step_name": step.name,
            "step_type": step.type.value,
            "status": "completed",
            "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "output_key": step.output_key,
        })

    async def _score_run(self, run: OrchestrationRun) -> None:
        """Judge this run, if its orchestration asked to be judged.

        Top-level runs only, matching `_write_memory_summaries`: a nested
        sub-orchestration's work is part of its parent's result, and scoring it
        separately would bill twice for one verdict and put a number on
        something no screen shows.

        Cost note, because it is the trade this design accepts: the call
        happens before `orchestration_complete` is yielded, so a scored run
        reports complete a few seconds later than an unscored one. It is
        bounded by `judge.TIMEOUT_SECONDS`, and the alternative — a queued job
        — needs a second admission path and a second gateway credential in
        cloud for a latency saving nobody had yet measured. See D84.
        """
        if self.depth > 0:
            return
        rubric_id = getattr(self.orch, "rubric_id", "") or ""
        if not rubric_id:
            return

        from core.config import load_settings
        from core.evals import scoring

        # Set by the golden-set runner on the engine it builds, so the score
        # this run produces can be found again as part of that suite. It rides
        # on the engine rather than in `initial_state` for the reason D80 gives
        # about attachments: `shared_state` is the run's own data and a private
        # key in it is read by everything that renders a run.
        suite_run_id = getattr(self, "eval_suite_run_id", None)
        await scoring.score_run(run, self.orch, load_settings(), suite_run_id=suite_run_id)

    async def _write_memory_summaries(self, run: OrchestrationRun) -> None:
        """One session summary per memory-enabled agent that took part.

        Top-level runs only — a nested sub-orchestration's work is part of its
        parent's story. LLM-compressed when a model is reachable, truncated
        otherwise (`compress_for_context`'s own fallback).
        """
        if self.depth > 0:
            return
        agent_ids = {s.agent_id for s in self.orch.steps if getattr(s, "agent_id", None)}
        if not agent_ids:
            return

        from core.react_engine import _resolve_agent_by_id
        from core.scale.context import resolve_agent as _ctx_resolve_agent

        enabled: list[str] = []
        for agent_id in agent_ids:
            cached = self._resolved.get(agent_id)
            if cached is not None:
                agent = cached.agent
            else:
                try:
                    agent = await _ctx_resolve_agent(agent_id)
                    if agent is None:
                        agent = await _resolve_agent_by_id(agent_id)
                except Exception:
                    agent = None
            if agent and agent.get("memory"):
                enabled.append(agent_id)
        if not enabled:
            return

        lines = [f"Run {run.run_id} of '{self.orch.name}' — {run.status}."]
        for entry in run.step_history[-40:]:
            line = f"- {entry.get('step_name')} ({entry.get('step_type')}): {entry.get('status')}"
            if entry.get("error"):
                line += f" — {str(entry['error'])[:200]}"
            lines.append(line)
        for key, value in list(run.shared_state.items())[:20]:
            if str(key).startswith("__"):
                continue
            lines.append(f"[{key}] {str(value)[:300]}")

        from core.config import load_settings
        from core.orchestration.summarizer import compress_for_context
        summary = await compress_for_context(
            "\n".join(lines), max_chars=2000, purpose="session memory",
            settings=load_settings(), session_id=run.session_id, run_id=run.run_id,
        )

        from core import agent_memory
        for agent_id in enabled:
            try:
                await agent_memory.write_session_summary(
                    agent_id, run.session_id or run.run_id, summary, run_id=run.run_id,
                )
            except Exception as exc:
                _log.warning(f"memory summary for '{agent_id}' failed: {exc}")

    def _build_final_response(self, run: OrchestrationRun) -> str:
        """Build a human-readable summary from the run."""
        if run.status == "failed":
            last_error = None
            for h in reversed(run.step_history):
                if h.get("error"):
                    last_error = h["error"]
                    break
            return f"Orchestration failed. Error: {last_error or 'Unknown error'}"

        if run.status == "paused":
            return "Orchestration paused, waiting for human input."

        # Try to find the last agent output in shared state
        last_output = None
        for h in reversed(run.step_history):
            output_key = h.get("output_key")
            if output_key and output_key in run.shared_state:
                last_output = run.shared_state[output_key]
                break

        if last_output:
            return str(last_output) if not isinstance(last_output, str) else last_output

        return f"Orchestration completed. {len(run.step_history)} steps executed."
