"""
OrchestrationEngine -- walks a graph of steps, managing shared state,
checkpointing, loop guards, and yielding SSE events.
"""
import asyncio
import calendar
import json
import time
from contextvars import ContextVar
from typing import AsyncGenerator, Awaitable, Callable

import anyio

from core.config import ORCH_GLOBAL_TIMEOUT_MIN
from core.models_orchestration import Orchestration, OrchestrationRun, StepConfig, StepType
from core.state_types import matches_declared_type, normalize_state_type
from .human_prompts import HUMAN_CHANNEL_KEY, announce as announce_human_prompt
from .run_error import failure_reason, run_error, set_run_error
from .pauses import end_step, innermost_container, pause_chain, progress_key
from .state import SharedState
from .steps import STEP_EXECUTORS, extract_all_json
from .time_limits import step_time_limit, timed_out
from .logger import OrchestrationLogger

from core.log import get_logger
from core.telemetry import spans as _spans

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

#: The run's own entry in `step_progress`: the seconds it has worked so far,
#: which `timeout_minutes` is measured against across resumes (F258). Kept
#: there because the OSS store and cloud's engine table both mirror that
#: column already — a new field on the run would need a migration on each.
ACTIVE_TIME_KEY = "__run__"


class RouteFailed(Exception):
    """A route the run cannot take, so the run fails at the step routing it.

    A step has used its Max iterations and names nowhere to go instead (F248), or a
    Switch matched no case and has no default (F256). Both used to end the run
    `completed`, the only trace an event nothing reads as a failure. `events` are
    the routing events already decided, still worth yielding: they say why.
    """

    def __init__(self, message: str, events: list[dict] | None = None):
        super().__init__(message)
        self.message = message
        self.events = list(events or [])


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


#: A Parallel branch running beside others keeps its own view of where it is
#: (F301): the transition every step's prompt reads, and the loop depth. Two
#: loops in two branches set both for each body step, and one engine-wide pair
#: gave a body step the other loop's iteration. Keyed by engine, so a workflow
#: step's child engine keeps its own; set only inside a branch's task, whose
#: context asyncio copied, so a sibling or the node never sees it.
_BRANCH_VIEW: ContextVar[dict | None] = ContextVar("orchestration_branch_view", default=None)


class OrchestrationEngine:
    """Runs an orchestration by walking through its step graph."""

    def _branch_view(self) -> dict | None:
        view = _BRANCH_VIEW.get()
        return view.get(id(self)) if view else None

    @property
    def current_transition(self):
        own = self._branch_view()
        return own["current_transition"] if own is not None else self.__dict__.get("_transition")

    @current_transition.setter
    def current_transition(self, value) -> None:
        own = self._branch_view()
        if own is not None:
            own["current_transition"] = value
        else:
            self.__dict__["_transition"] = value

    @property
    def _loop_depth(self) -> int:
        own = self._branch_view()
        return own["loop_depth"] if own is not None else self.__dict__.get("_depth_in_loops", 0)

    @_loop_depth.setter
    def _loop_depth(self, value: int) -> None:
        own = self._branch_view()
        if own is not None:
            own["loop_depth"] = value
        else:
            self.__dict__["_depth_in_loops"] = value

    def enter_branch(self) -> None:
        """From here on, in the calling task only, this engine's transition and
        loop depth are the branch's own — starting from what they are now."""
        view = dict(_BRANCH_VIEW.get() or {})
        view[id(self)] = {"current_transition": self.current_transition, "loop_depth": self._loop_depth}
        _BRANCH_VIEW.set(view)

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
        # Each step in a span when telemetry is on; the table itself when off.
        self.executors = _spans.traced_executors(STEP_EXECUTORS)
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
        #: Who called this run when it runs nested — `{workflow, step, goal}` —
        #: for its model steps' system text (F252). Rides on the engine, not in
        #: shared state, for the reason `images` does; the parent hands it over
        #: on the first run and again on every resume (`_resume_nested`).
        self.caller: dict | None = None
        #: The chat turn this run owes its conversation — `{session_id,
        #: agent_id, message}` — when a workflow agent runs it for a person
        #: (F261). Stamped on the run, so whatever finishes it after a pause
        #: saves the turn, in-process or on the fleet.
        self.conversation_turn: dict | None = None
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
        if self.conversation_turn and self.depth == 0:
            # Beside the run's time and failure reason — never a state key,
            # which every run view and prompt would carry (F261).
            run.step_progress[ACTIVE_TIME_KEY] = {"turn": dict(self.conversation_turn)}
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

    @_spans.traced_generator("synapse.run", _spans.run_attributes, on_end=_spans.run_end)
    async def _execute_loop(
        self, run: OrchestrationRun, state: SharedState, started: float | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Core execution loop — shared between run() and resume().

        `started` is when this stretch of work began, if before this call: a
        resume passes its own start, so the sub-run it finished is counted.
        """
        total_turns = self._engine_turns(run)
        logger = getattr(self, "logger", None)
        loop_start_time = started if started is not None else time.time()
        timeout_seconds = (self.orch.timeout_minutes or ORCH_GLOBAL_TIMEOUT_MIN) * 60
        # The budget counts the time this run has worked, across its pauses
        # (F258). Each resume used to start it again, so a run with a person's
        # pause in it could work past its budget without limit. What earlier
        # stretches spent is on the run; waiting for a person is not in it.
        spent_before = self._active_seconds(run)

        def active_seconds() -> float:
            return spent_before + (time.time() - loop_start_time)

        def stamp() -> None:
            """Put the time worked so far on the run, for its next checkpoint.

            Merged, not replaced: the entry also holds the run's own failure
            reason (F257).
            """
            run.step_progress[ACTIVE_TIME_KEY] = {
                **(run.step_progress.get(ACTIVE_TIME_KEY) or {}),
                "active_seconds": round(active_seconds(), 2)}

        def fail_run(error: str) -> dict:
            """Fail the run outside any step: the reason is kept on the run (F257).

            With no history entry to carry it, the reason used to be said once
            on the stream and lost — the run's answer, v1 and the run list read
            "Unknown error".
            """
            run.status = "failed"
            set_run_error(run, error)
            return {"type": "orchestration_error", "error": error}

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
            elapsed = active_seconds()
            if elapsed > timeout_seconds:
                yield fail_run(
                    f"Global timeout ({self.orch.timeout_minutes} min) exceeded after "
                    f"{round(elapsed / 60, 1)} min of work — time waiting for a person is not counted")
                break

            step = self.step_map.get(run.current_step_id)
            _log.debug(f"ENGINE: ▶ step='{run.current_step_id}' type={step.type if step else None} status={run.status}")
            if not step:
                yield fail_run(f"Step '{run.current_step_id}' not found")
                break

            executor = self.executors.get(step.type)
            if not executor:
                yield fail_run(f"No executor for step type '{step.type}'")
                break

            # Global turn guard
            total_turns += 1
            if total_turns > self.orch.max_total_turns:
                yield fail_run(f"Global turn limit ({self.orch.max_total_turns}) exceeded")
                break

            step_start_time = time.time()
            # The step's own timeout (5 min unless set), or — for a step that
            # runs a workflow — that workflow's own budget, capped by what is
            # left of this run's (F291).
            step_timeout, timeout_why = await step_time_limit(
                step, self, remaining=timeout_seconds - active_seconds())

            # A container node's budget has to cover every step it runs inside
            # itself, and each of those already gets its own fail_after. Charging
            # the container one step's worth means a 3x loop over one agent step
            # is given 300s to do 900s of work, and the run dies mid-iteration.
            # The global timeout checked above is the real ceiling for these.
            if step.type in (StepType.LOOP, StepType.PARALLEL):
                step_timeout, timeout_why = max(1.0, timeout_seconds - active_seconds()), None
            timeout_text = timed_out(step_timeout, timeout_why) if timeout_why else f"after {step_timeout}s"

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
                                # The question this run waits on, whichever
                                # step asked it (F288): a human step here sets
                                # its own, but a nested one's or a tool
                                # approval's reached no record, and v1 read a
                                # paused nest with no prompt and no fields.
                                run.human_prompt = event.get("prompt")
                                run.human_fields = event.get("fields") or []
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
                                run.step_history.append(
                                    self._paused_entry(run, step, event, step_start_time))
                                # The channel this pause waits on, so a reply
                                # there can find the run (F54). A nested pause
                                # carries its sub-step's channel up to here.
                                if event.get("channel_id"):
                                    run.shared_state[HUMAN_CHANNEL_KEY] = event["channel_id"]
                                else:
                                    run.shared_state.pop(HUMAN_CHANNEL_KEY, None)
                                stamp()
                                await state.checkpoint()
                                if logger:
                                    logger.step_end(step.id, "paused")
                                    logger.run_end("paused")
                                    await logger.aclose()
                                # Asked once, by the run whose human step it is:
                                # a parent forwarding a nested pause does not
                                # ask again.
                                if event.get("channel_id") and not event.get("nested_run_id"):
                                    await announce_human_prompt(
                                        run.run_id, event["channel_id"], event.get("prompt") or "",
                                    )
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
                        "error": f"Step '{step.name}' timed out {timeout_text}",
                        "reason": f"Timed out {timeout_text}",
                    }
                    end_step(run.step_history, {
                        "step_id": step.id, "step_name": step.name,
                        "step_type": step.type.value, "status": "failed",
                        "error": f"Timed out {timeout_text}",
                    })
                    run.status = "failed"
                    if logger:
                        logger.step_end(step.id, "failed", f"Timed out {timeout_text}")

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
                end_step(run.step_history, {
                    "step_id": step.id,
                    "step_name": step.name,
                    "step_type": step.type.value,
                    "status": "completed",
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(step_start_time)),
                    "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "duration_seconds": step_duration,
                    "output_key": step.output_key,
                })

                # Where the run goes next, decided before the step is announced
                # complete: a route the run cannot take fails it here, at the
                # step that routes (F248, F256). `_resolve_next` only reads, and
                # it runs after the entry above so a step routing to itself is
                # counted. Caught on its own: the handler below would append a
                # second, failed entry for a step that already has one.
                try:
                    next_step_id, extra_events = self._resolve_next(step, run)
                except RouteFailed as stop:
                    async for event in self._fail_route(run, step, stop):
                        yield event
                    try:
                        stamp()
                        await state.checkpoint()
                    except Exception as ckpt_err:
                        _log.warning(f"route-failure checkpoint failed: {ckpt_err}")
                    break

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
                    named = ", ".join(f"'{k}' ({t})" for k, t in unconvertible)
                    error = (
                        f"Step '{step.name}' wrote text that will not become the "
                        f"declared type of {named}. Stopped because this "
                        f"orchestration has strict state types on."
                    )
                    if logger:
                        logger.run_end("failed")
                    yield fail_run(error)
                    try:
                        stamp()
                        await state.checkpoint()
                    except Exception as ckpt_err:
                        _log.warning(f"strict-type checkpoint failed: {ckpt_err}")
                    break

                for extra_event in extra_events:
                    if logger:
                        logger.log_event(extra_event)
                    yield extra_event
                run.current_step_id = next_step_id
                _log.debug(f"ENGINE: 💾 checkpointed → next='{run.current_step_id}'")
                stamp()
                await state.checkpoint()

            except Exception as e:
                import traceback; _log.debug(f"ENGINE: ❌ EXCEPTION in step '{step.id}': {e}\n{traceback.format_exc()}")
                # The stream's `error` is a sanitised sentence — that choice was
                # deliberate, an exception's text can carry anything. The run
                # record gets the real one, bounded: an operator reading a
                # failed run's history should not have to grep worker stdout
                # to learn whether it was a 401 or a KeyError. The same words
                # ride the event as `reason` (F195), which the run log and the
                # step card show: every reader of this stream is a member or an
                # API key, both of which read the record anyway, and the widget
                # drops `step_error` altogether.
                safe_error = "An internal error occurred while executing this step."
                real_error = f"{type(e).__name__}: {str(e)[:300]}"
                end_step(run.step_history, {
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
                yield {"type": "step_error", "orch_step_id": step.id,
                       "error": safe_error, "reason": real_error}
                # Persist the failure here: between this and the finalize
                # checkpoint is exactly where a second crash would lose it.
                try:
                    stamp()
                    await state.checkpoint()
                except Exception as ckpt_err:
                    _log.warning(f"failure checkpoint failed: {ckpt_err}")
                break

        # Finalize
        if run.status == "running":
            run.status = "completed"

        # A workflow agent's turn that paused is saved by the run that finishes
        # it (F261): the chat request that started it ended at the pause, and
        # an answer given in the chat, on the run's page, in a channel or on
        # the fleet resumes it here. Taken off the run before its checkpoint,
        # so it is paid once. A run that never paused was saved by its caller.
        owed_turn = None
        if (run.status in ("completed", "failed", "cancelled") and self.depth == 0
                and getattr(self, "_resumed_at", None) is not None):
            owed_turn = (run.step_progress.get(ACTIVE_TIME_KEY) or {}).pop("turn", None)

        run.ended_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # What the run cost, the runs nested under it included (F251). Only the
        # fleet ever wrote this, from its own run id's calls alone, so every
        # in-process run read $0 and a parent never counted its children.
        try:
            from core.usage_tracker import get_usage_logs
            spent = await get_usage_logs(run_id=run.run_id, limit=100_000)
            run.total_tokens_used = sum(int(r.get("total_tokens") or 0) for r in spent)
            run.total_cost_usd = round(sum(float(r.get("estimated_cost") or 0.0) for r in spent), 8)
        except Exception as exc:  # noqa: BLE001 — a figure on the record, never the run's outcome
            _log.warning(f"run cost for {run.run_id} not recorded: {exc}")
        if run.status == "completed":
            # Nothing resumes a completed run, so it has no budget left to
            # count; a finished run leaves no progress behind. A failed or
            # cancelled one keeps its count for a Replay to judge.
            run.step_progress.pop(ACTIVE_TIME_KEY, None)
        else:
            stamp()
        await state.checkpoint()

        # A run that stopped short stops what it had started under it (F257). A
        # nested child runs inside this task, so a step timeout or a cancel took
        # it down too — but its record kept `running` (or `paused` on a question
        # nobody could answer) for ever. Its own failure is already recorded; a
        # finished child keeps its ending.
        if run.status in ("failed", "cancelled"):
            from .descendants import settle_descendants
            why = failure_reason(run.status, run.step_history, run.step_progress)
            verb = "failed" if run.status == "failed" else "was cancelled"
            await settle_descendants(
                run.run_id, run.status,
                f"Stopped: the run that called it {verb}" + (f" — {why}" if why else ""))

        if owed_turn:
            try:
                await self._save_owed_turn(run, owed_turn)
            except Exception as exc:
                _log.warning(f"the conversation's turn for {run.run_id} was not saved: {exc}")

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

        final_output = self._build_final_response(run, getattr(self.orch, "result_key", "") or "")

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
        cls, run_id: str, server_module, *, interrupted: bool = False,
        images: list[str] | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Resume a failed or cancelled orchestration from where it stopped.

        For failed runs: the failed step's history entry is removed so it re-executes.
        For cancelled runs: resumes from current_step_id (which was never started).
        All state accumulated by prior steps is preserved.

        `interrupted` also accepts a run still marked `running`: one whose job
        was stopped mid-run (a worker shutting down) and handed out again. Its
        in-flight step never reached `step_history`, so it re-executes from
        `current_step_id` like a cancelled run's. Only a caller that knows the
        previous attempt is gone may say so — a live run is `running` too.
        `images` are the attachments the job still carries; nothing persists them.

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

        recoverable = ("failed", "cancelled", "running") if interrupted else ("failed", "cancelled")
        if run.status not in recoverable:
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
        engine.images = list(images or [])
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

        # A person asked to carry on: the run gets its budget again (F258) —
        # a run that failed on its time budget could not be replayed otherwise.
        # A job handed out again after its worker stopped keeps counting. Either
        # way the reason it stopped is not this attempt's (F257).
        if not interrupted:
            run.step_progress.pop(ACTIVE_TIME_KEY, None)
        else:
            set_run_error(run, None)

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
        cls, run_id: str, human_response: dict, server_module, images: list[str] | None = None,
        *, depth: int = 0, caller: dict | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Resume a paused orchestration after human input.

        Loads what this entry point has to load — the checkpoint and the
        definition — and hands the rest to `resume_paused`, which the fleet's
        adapter calls with the definition it already has.

        `images` are the attachments sent *with the answer*, not the ones the
        run started with — those are not persisted anywhere, by design, so a
        step re-entered after a pause sees only what the person attached to
        their reply.

        `depth` is the nesting depth of the run being resumed. A parent resuming
        the sub-orchestration it waits on passes its own plus one, so the child
        is not taken for a top-level run — which writes memory summaries and is
        scored — and its nesting cap counts from where it really is. It passes
        `caller` too, so the child's steps are still told who called them.
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
        engine = cls(orch, server_module, depth=depth)
        engine.images = list(images or [])
        engine.caller = caller
        async for event in engine.resume_paused(run, human_response):
            yield event

    async def resume_paused(self, run: OrchestrationRun, human_response) -> AsyncGenerator[dict, None]:
        """Answer a paused run and carry it on. The one resume.

        Both entry points come here: `resume()` above, and
        `WorkerEngineAdapter.resume`, which every resume on a fleet goes
        through. The adapter used to hold a hand copy of this logic, and the
        copy drifted three times: it never closed the human step's history
        entry, it dropped the routing events after an answer, and it had no
        tool-approval branch at all — so on a fleet an approved tool call was
        written into the step's output key as though it were an answer, and
        stepped past with the call never run (F109). What differs between the
        callers — where the definition comes from, where the events go — stays
        with them; everything that decides what an answer does is here.

        Three kinds of pause, told apart by what the run recorded:
        - a nested orchestration paused inside a step (`nested_run_id`): the
          sub-run is resumed, and its result is the step's;
        - a tool call waiting on approval (`pending_approval` in the step's
          progress): the step is re-entered with the decision, not stepped past;
        - anything else is a human step's answer.
        """
        # Work resumes now; the wait before it is a person's and is not counted
        # against `timeout_minutes` (F258). Set before a nested sub-run is
        # resumed, so the work it does counts for this run too.
        self._resumed_at = time.time()
        # Answered: no longer waiting on a channel. Written at the next checkpoint;
        # a nested step that pauses again records its channel again.
        run.shared_state.pop(HUMAN_CHANNEL_KEY, None)
        # Nor on a question (F288): one already answered is not left for a later
        # read to show. A pause after this records its own.
        run.human_prompt = None
        run.human_fields = []
        self.global_variables = await self._load_global_variables()
        self.agent_names = await self._load_agent_names()
        _log.debug(f"[engine.resume] 🗺  step_map keys: {list(self.step_map.keys())}")

        # Create logger — appends to existing log file if present
        self.logger = OrchestrationLogger(
            run_id=run.run_id,
            orchestration_id=run.orchestration_id,
            orchestration_name=self.orch.name,
            user_input=f"(resumed) human_response={human_response}",
            session_id=run.session_id,
        )

        # Where the run waits. At the top level the step that asked is the
        # position; inside a container the position is the container, and the
        # containers' pause records lead in to the step that asked.
        chain, asker_id, resume_past = self._pause_position(run)
        asker = self.step_map.get(asker_id)
        _log.debug(f"[engine.resume] 🔍 position={run.current_step_id!r} asker={asker_id!r} found={asker is not None} type={asker.type.value if asker else 'N/A'} depth_in={len(chain)}")

        answered = False
        if run.nested_run_id:
            # A nested orchestration hit a human step: resume the sub-run and let
            # it finish before the parent moves on.
            _log.debug(f"[engine.resume] 🔗 nested run detected: nested_run_id={run.nested_run_id}")
            outcome: dict = {}
            async for event in self._resume_nested(run, human_response, asker_id, outcome):
                yield event
            if outcome.get("status") == "paused":
                return
            answered = outcome.get("status") == "done"
        elif (pending := (run.step_progress.get(asker_id) or {}).get("pending_approval")):
            # A tool call inside an agent step, not a human step: the answer is an
            # approval for that call, and the step has to be re-entered rather than
            # stepped past — its work is unfinished, and the transcript that got it
            # this far is in its progress record.
            async for event in self._resume_tool_approval(run, human_response, pending, asker_id):
                yield event
        else:
            # A human step: write the answer under the step's configured output_key.
            # Falling back to "human_response" preserves backward-compat for steps with no output_key.
            output_key = (asker.output_key if asker else None) or "human_response"
            human_response = self._as_answered(human_response, asker)
            run.shared_state[output_key] = human_response
            # Always keep "human_response" as well so evaluators that check it still work.
            if output_key != "human_response":
                run.shared_state["human_response"] = human_response

            run.waiting_for_human = False
            run.status = "running"

            if asker:
                # Before _resolve_next, so the loop guard counts this iteration.
                self._complete_human_step(run, asker)
                answered = True
            else:
                _log.debug(f"[engine.resume] ⚠️  step {asker_id!r} not found in step_map — the loop will likely fail immediately")

        if answered and chain:
            # Asked inside a container: the one holding the step carries on past
            # it — the rest of that iteration, then the next — and every
            # container outside it re-enters its own record (F109). The position
            # stays on the outermost, which is where `_execute_loop` starts.
            chain[-1]["answered"] = True
        elif answered and (done_with := self.step_map.get(resume_past) if resume_past else asker):
            try:
                next_id, resume_events = self._resolve_next(done_with, run)
            except RouteFailed as stop:
                # The answer routes where the run cannot go (F248, F256). Failed
                # here, then on to the checkpoint and `_execute_loop`, which
                # finishes the run as failed: returning early would leave the
                # fleet, which reads the status from `orchestration_complete`,
                # recording it completed.
                async for event in self._fail_route(run, done_with, stop):
                    yield event
            else:
                run.current_step_id = next_id
                # A routing decision, a loop_limit_reached, or both. Dropping them
                # meant a run that hit its iteration cap on the step right after a
                # human pause said nothing about why it moved on.
                for resume_event in resume_events:
                    yield resume_event
                _log.debug(f"[engine.resume] ➡  _resolve_next({asker_id!r}) → next_step_id={next_id!r}")

        _log.debug(f"[engine.resume] 🚀 entering _execute_loop with current_step_id={run.current_step_id!r} status={run.status}")
        state = SharedState(run)
        # Persist "running" + the cleared human flag before the first step, so
        # the run stops showing as "needs input" the moment it resumes. A failed
        # nested resume is persisted here too, and `_execute_loop` then finishes
        # it as failed — so the fleet records `failed`, where it used to find no
        # `orchestration_complete` and record the run `completed`.
        await state.checkpoint()
        async for event in self._execute_loop(run, state, started=self._resumed_at):
            yield event
        _log.debug(f"[engine.resume] 🏁 _execute_loop finished, run.status={run.status} run.current_step_id={run.current_step_id!r}")

    @staticmethod
    def _as_answered(human_response, asker):
        """A step with no fields holds its answer as text, whichever road it took (F106).

        The same step's key held `yes` or `{"response": "yes"}` by where it was
        answered: OSS chat sent the mapping, the app's modal and a channel reply
        the string — which the fleet's enqueue wrapped — and v1's and v2's
        resume always wrapped. So `{state.approval}` rendered differently for
        the same answer. Decided here, once, rather than at every sender. A
        step that declares fields keeps its mapping, and so does any other dict:
        a caller that chose a shape keeps it.
        """
        if (asker is not None and not asker.human_fields and isinstance(human_response, dict)
                and list(human_response) == ["response"]):
            return human_response["response"]
        return human_response

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

    async def _resume_tool_approval(self, run, human_response, pending, step_id: str):
        """Give an agent step the decision on the one tool call it stopped to ask about.

        Nothing is run here: the caller's `_execute_loop` re-enters the step,
        which acts on `pending_call` from its progress record, keyed by its id.
        """
        step = self.step_map.get(step_id)
        approved, reason = self._approved(human_response)
        approval_id = pending.get("approval_id")
        _log.debug(f"[engine.resume] 🔐 tool approval for '{pending.get('tool_name')}' → "
                   f"{'approved' if approved else 'refused'}")

        progress = dict(run.step_progress.get(step_id) or {})
        progress.pop("pending_approval", None)
        # A person's answer is not a crash retry: the counter that discards a
        # transcript after three interrupted attempts starts again (F286 made
        # several answers in one step likely — a delegate asking each time).
        progress.pop("attempt", None)
        # The call itself, so the step can act on the answer without asking the
        # model to make the same choice again — it might not, and then an
        # approval a person gave would quietly do nothing. The one shape chat
        # resumes from, so a delegate's question inside the step is answered as
        # the delegate's (F286).
        from core.chat_approval import resume_state_for
        run.step_progress[step_id] = resume_state_for({
            "approval_id": approval_id,
            "tool_name": pending.get("tool_name"),
            "tool_args": pending.get("tool_args") or {},
            "progress": progress,
        }, approve=approved, reason=reason)

        run.waiting_for_human = False
        run.status = "running"
        # The step is unfinished, so its history entry is reopened rather than
        # closed: it did not complete when it paused.
        for entry in reversed(run.step_history):
            if entry.get("step_id") == step_id and entry.get("status") == "paused":
                entry["status"] = "running"
                break

        yield {"type": "tool_approval_resolved", "orch_step_id": step_id,
               "tool_name": pending.get("tool_name"), "approved": approved,
               "step_name": step.name if step else ""}

    async def _resume_nested(
        self,
        run: OrchestrationRun,
        human_response,
        step_id: str,
        outcome: dict,
    ) -> AsyncGenerator[dict, None]:
        """Resume the sub-orchestration step `step_id` is waiting on.

        Forwards the sub-run's events tagged as the parent step's, and says how
        it ended in `outcome["status"]`:
        - "paused": the sub-run stopped to ask again; the parent is
          re-checkpointed, still waiting on it;
        - "done": it finished; its result is written to the step's output key,
          the step's history recorded, the parent un-paused;
        - "failed": it produced nothing, or could not be resumed at all; the
          parent is failed.
        Moving the parent on is the caller's.
        """
        parent_step = self.step_map.get(step_id)
        nested_run_id = run.nested_run_id
        nested_orch_id = run.nested_orch_id
        from .steps import NESTED_OWN_EVENTS as _NESTED_FILTER_TYPES, NestedOutcome, tag_nested_event

        sub_outcome = NestedOutcome()
        sub_events: list[dict] = []

        # The run is working again from here, whatever depth the question was
        # asked at (F250): the record said `paused` — to every list, v1 and the
        # bell — for as long as the sub-run worked after the answer, until it
        # asked again or ended. Each level records itself before it resumes the
        # level it waits on; a sub-run that asks again puts it back below.
        run.status = "running"
        run.waiting_for_human = False
        await SharedState(run).checkpoint()

        resume_error: str | None = None
        try:
            async for sub_event in type(self).resume(
                nested_run_id, human_response, self.server_module, depth=self.depth + 1,
                caller=self.caller_for(run, parent_step),
            ):
                sub_events.append(sub_event)
                sub_outcome.read(sub_event)

                # Sub-orch hit another human step (multi-turn human interaction inside nested orch)
                if sub_event.get("type") == "human_input_required":
                    # Still waiting on the same sub-run. The event names whatever
                    # *it* waits on — a sub-run of its own, two levels down — and
                    # recording that here would make the next answer skip a level.
                    run.nested_run_id = nested_run_id
                    run.nested_orch_id = nested_orch_id
                    # Waiting again (F250): recorded running before the sub-run
                    # was resumed, so it is put back, not left as it was.
                    run.status = "paused"
                    run.waiting_for_human = True
                    run.human_prompt = sub_event.get("prompt")
                    run.human_fields = sub_event.get("fields") or []
                    # A new question opens an entry of its own (F140). Without one
                    # the parent's newest `paused` entry stayed the first
                    # question's, at the same index — so `pause_ordinal` did not
                    # move, and an answer queued behind the first was applied to
                    # this one. A Loop's second question has always appended an
                    # entry, which is why F111 holds there and failed here.
                    #
                    # Typed `human`, not as the step: the step is executing, and
                    # this is a person's wait inside that one execution. That is
                    # what keeps the delegate to a single priced entry — appending
                    # a second `agent` one is the mistake the done path below
                    # records ("cloud priced both") — and out of active time and
                    # the executed-step count, which already exclude human steps.
                    self._reopen_nested_pause(run, step_id, parent_step)
                    if sub_event.get("channel_id"):
                        run.shared_state[HUMAN_CHANNEL_KEY] = sub_event["channel_id"]
                    else:
                        run.shared_state.pop(HUMAN_CHANNEL_KEY, None)
                    # The sub-run worked since the answer, for this run (F258).
                    resumed_at = getattr(self, "_resumed_at", None)
                    if resumed_at is not None:
                        run.step_progress[ACTIVE_TIME_KEY] = {
                            **(run.step_progress.get(ACTIVE_TIME_KEY) or {}),
                            "active_seconds": round(
                                self._active_seconds(run) + time.time() - resumed_at, 2)}
                    state = SharedState(run)
                    await state.checkpoint()
                    outcome["status"] = "paused"
                    yield tag_nested_event(sub_event, run.run_id, step_id,
                                           parent_step.name if parent_step else "",
                                           nested_run_id, nested_orch_id)
                    return

                # Skip sub-orch lifecycle meta-events (same filter as initial execution)
                if sub_event.get("type") in _NESTED_FILTER_TYPES:
                    continue

                yield tag_nested_event(sub_event, run.run_id, step_id,
                                       parent_step.name if parent_step else "",
                                       nested_run_id, nested_orch_id)
        except Exception as exc:  # noqa: BLE001 — told to the run, not raised past it
            # The sub-run cannot be picked up at all — its record is gone, say.
            # The parent is working by now, so letting this escape would leave
            # it `running` with nothing behind it; before, it stayed paused on a
            # question the answer had already spent.
            resume_error = f"{type(exc).__name__}: {str(exc)[:300]}"

        # A sub-run that did not complete fails the step waiting on it, with its
        # reason — as it does when it fails without a pause (F253). Its final
        # text used to be taken as the result whatever its status.
        from core.scale.context import resolve_orchestration
        from .results import nested_result
        sub_orch_data = await resolve_orchestration(nested_orch_id) if nested_orch_id else None
        sub_name = (sub_orch_data or {}).get("name") or nested_orch_id or "sub-orchestration"
        failure = None
        if resume_error:
            failure = f"Sub-orchestration '{sub_name}' could not be resumed: {resume_error}"
        elif sub_outcome.status != "completed":
            failure = sub_outcome.failure(sub_name)

        # What it made, as the value it is — the rule a child that finished in
        # one go is read by (F249), not its closing question's answer.
        result = None
        if not failure:
            result = nested_result(sub_events, (sub_orch_data or {}).get("result_key") or "")
            if result is None:
                failure = "Nested orchestration failed to produce a result after human input"

        if failure:
            # The step's pause entry is its one entry: closed as failed, with the
            # reason `_build_final_response` and the run's timeline read. The
            # event is the step's, so the step card shows it (F195).
            ended_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            for entry in reversed(run.step_history):
                if entry.get("step_id") == step_id and entry.get("status") == "paused":
                    entry.update({"status": "failed", "error": failure, "ended_at": ended_at})
                    break
            else:
                run.step_history.append({
                    "step_id": step_id,
                    "step_name": parent_step.name if parent_step else "",
                    "step_type": "agent", "status": "failed", "error": failure,
                    "ended_at": ended_at,
                })
            run.status = "failed"
            run.waiting_for_human = False
            run.nested_run_id = None
            run.nested_orch_id = None
            outcome["status"] = "failed"
            yield {
                "type": "step_error",
                "orch_step_id": step_id,
                "step_name": parent_step.name if parent_step else "",
                "error": "An internal error occurred while executing this step.",
                "reason": failure,
            }
            return

        # Write sub-orch result into parent shared_state
        if parent_step and parent_step.output_key:
            run.shared_state[parent_step.output_key] = result
        if parent_step is not None:
            await self._remember_nested_result(run, parent_step, nested_orch_id, result)

        # The step's pause entry is its one entry: closed where it stands, with
        # no duration — the time was a person's, spent in the sub-run. Appending
        # a second left the first `paused` for ever, and cloud priced both.
        ended_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for entry in reversed(run.step_history):
            if entry.get("step_id") == step_id and entry.get("status") == "paused":
                entry["status"] = "completed"
                entry["ended_at"] = ended_at
                break
        else:
            run.step_history.append({
                "step_id": step_id,
                "step_name": parent_step.name if parent_step else "",
                "step_type": "agent",
                "status": "completed",
                "ended_at": ended_at,
            })

        # Clear nested context; the caller moves the parent on.
        run.nested_run_id = None
        run.nested_orch_id = None
        run.waiting_for_human = False
        run.status = "running"
        outcome["status"] = "done"

    def caller_for(self, run: OrchestrationRun, step: StepConfig | None) -> dict:
        """What a child run started by `step` is told about who called it (F252)."""
        return {
            "workflow": self.orch.name,
            "step": step.name if step is not None else "",
            "goal": str(run.shared_state.get("user_input") or ""),
        }

    async def _remember_nested_result(
        self, run: OrchestrationRun, step: StepConfig, sub_orch_id: str | None, result: object,
    ) -> None:
        """Record a nested step finished by an answer, as one finished in one go is.

        `_execute_nested_orchestration` records the sub-orchestration's result
        in the step's execution memory — what a later run of the step reads back
        as its previous work, and what the judge scores. A step finished here
        recorded nothing (F112). What triggered its first run is gone by now, so
        there is no origin; its inputs are read again, which is what it saw —
        the parent has not moved while it waited, the answer went to the sub-run.
        """
        from core.scale.context import resolve_orchestration
        from .context import build_execution_trace, snapshot_inputs, store_execution_memory

        sub_name = sub_orch_id or "sub_orchestration"
        try:
            sub_name = ((await resolve_orchestration(sub_orch_id)) or {}).get("name") or sub_name
        except Exception as exc:  # noqa: BLE001 — a label; the result is already in state
            _log.debug(f"nested step {step.id}: sub-orchestration name unavailable: {exc}")
        trace = build_execution_trace([
            {"type": "tool_call", "tool_name": "sub_orchestration", "tool_input": {"orchestration": sub_name}},
            {"type": "tool_result", "result": str(result)},
            {"type": "final", "response": str(result)},
        ])
        agent_name = (self.agent_names.get(step.agent_id or "") or step.agent_id
                      or "sub_orchestration")
        store_execution_memory(run, step, trace, agent_name, None, snapshot_inputs(step, run, self))

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
                return self._guarded(next_id, run, events)
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
                return self._guarded(target, run, [event])
            return target, [event]

        # SWITCH — deterministic case matching
        if step.type == StepType.SWITCH and step.switch_cases:
            matched = run.shared_state.get(f"_switch_decision_{step.id}")
            if matched is not None and matched in step.switch_cases:
                target = step.switch_cases[matched]  # None: the author's own end
            elif step.switch_default_step_id:
                target = step.switch_default_step_id
            else:
                # Nothing matched and there is nowhere else to go: the run used
                # to end `completed` after the switch alone (F256). The value is
                # evaluated again for the sentence — the expression is pure and
                # reads the state it just read — rather than kept in state.
                from .steps import _eval_state_expr
                value, err = _eval_state_expr(step.switch_expression or "", run.shared_state, default=None)
                shown = f"could not be evaluated ({err})" if err else repr(str(value) if value is not None else None)
                raise RouteFailed(
                    f"No case of '{step.name}' matched {shown} and it has no default path — "
                    f"add a case or a default, then Replay."
                )
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
                return self._guarded(target, run, [event])
            return target, [event]

        # Default linear routing
        next_id = step.next_step_id

        # Apply loop guard
        if next_id:
            return self._guarded(next_id, run, [])

        return next_id, []

    async def _fail_route(
        self, run: OrchestrationRun, step: StepConfig, stop: RouteFailed,
    ) -> AsyncGenerator[dict, None]:
        """Fail the run at `step`, which routes where the run cannot go (F248, F256).

        The step ran and its entry is closed, so that entry is marked failed with
        the reason rather than a second one added: one entry per execution is
        what a timeline shows and what cloud prices. `current_step_id` stays on
        the step, so a Replay (after the definition is fixed) runs it and routes
        again.
        """
        logger = getattr(self, "logger", None)
        for event in stop.events:
            if logger:
                logger.log_event(event)
            yield event
        for entry in reversed(run.step_history):
            if entry.get("step_id") == step.id:
                entry["status"] = "failed"
                entry["error"] = stop.message
                break
        run.status = "failed"
        run.waiting_for_human = False
        if logger:
            logger.step_end(step.id, "failed", stop.message)
        yield {"type": "step_error", "orch_step_id": step.id, "step_name": step.name,
               "error": stop.message, "reason": stop.message}

    @staticmethod
    def _active_seconds(run: OrchestrationRun) -> float:
        """What earlier stretches of this run spent working (F258)."""
        record = run.step_progress.get(ACTIVE_TIME_KEY) or {}
        try:
            return max(0.0, float(record.get("active_seconds") or 0))
        except (TypeError, ValueError):
            return 0.0

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

    def _guarded(self, target: str, run: OrchestrationRun,
                 events: list[dict]) -> tuple[str | None, list[dict]]:
        """`target` through the loop guard, after the routing `events` already decided."""
        try:
            guarded_id, loop_event = self._apply_loop_guard(target, run)
        except RouteFailed as stop:
            stop.events = events + stop.events
            raise
        return guarded_id, events + ([loop_event] if loop_event else [])

    def _executions(self, step_id: str, run: OrchestrationRun) -> int:
        """How many times `step_id` has run in this run.

        Counted per step id, and **typed as the step**: an entry recorded
        against this step under another type is not an execution of it. That is
        a person's wait inside one execution — a nested run asking a second
        question writes one (F140) — and counting those would spend a delegate's
        iterations on how talkative its sub-run was. A human step's own entries
        are typed `human` and so still count, which is what keeps a loop
        containing one from iterating without limit.
        """
        step_type = self.step_map[step_id].type.value
        return sum(
            1 for h in run.step_history
            if h["step_id"] == step_id and h.get("step_type", step_type) == step_type
        )

    def _apply_loop_guard(self, next_id: str, run: OrchestrationRun) -> tuple[str | None, dict | None]:
        """Route to `next_id`, unless it has already run `max_iterations` times.

        Then the run goes to the step's `on_exhausted_step_id`, if it names one
        (itself guarded: one fallback, not a chain), and otherwise fails with
        `RouteFailed` — "'Coder' gave up after 3 attempts" (F248). The guard
        used to fall back to the step's own `next_step_id`: a feedback loop whose
        gate never passed went round once more without the fix, then ended
        `completed` when the gate had nowhere left to go, and a failed quality
        gate read as success everywhere a run's status is read.
        """
        if next_id not in self.step_map:
            return next_id, None

        target = self.step_map[next_id]
        exec_count = self._executions(next_id, run)
        max_iter = target.max_iterations
        if exec_count < max_iter:
            return next_id, None

        loop_event = {
            "type": "loop_limit_reached",
            "orch_step_id": next_id,
            "iterations": exec_count,
            "max_iterations": max_iter,
        }
        gave_up = (f"'{target.name}' gave up after {exec_count} attempts — raise its Max "
                   f"iterations ({max_iter}) or give it a step for when it reaches the "
                   f"limit, then Replay.")
        fallback_id = target.on_exhausted_step_id
        if not fallback_id:
            raise RouteFailed(gave_up, [loop_event])
        loop_event["on_exhausted_step_id"] = fallback_id

        fallback = self.step_map.get(fallback_id)
        if fallback is not None and self._executions(fallback_id, run) >= fallback.max_iterations:
            raise RouteFailed(
                f"'{target.name}' reached its Max iterations ({max_iter}), and '{fallback.name}', "
                f"where it goes then, has used its own ({fallback.max_iterations}) — raise one, "
                f"then Replay.",
                [loop_event],
            )
        return fallback_id, loop_event

    def _pause_position(self, run: OrchestrationRun) -> tuple[list[dict], str | None, str | None]:
        """Where a paused run waits: the containers' pause records, the step
        that asked, and — for a run paused before F109 — a container to resume
        past.

        A pause inside a container is a chain of records leading in from the
        position to the step that asked (`pauses.pause_chain`). A run paused
        before those records existed has none, and its place in the container
        is lost, so it resumes past the container instead of inside it. Its
        shape says which build paused it:
        - in-process, the position and the history entry both name the
          container, which is answered and stepped past as it always was;
        - on a fleet, the adapter had moved the position to the step that
          asked, while the entry names the container. The run used to end at
          that step. Now the entry — which is the wait — becomes the step's
          own, closed typed as the step, and the run goes on past the
          container. Not for a tool call's approval: that step is re-entered
          where it stands, as before.
        """
        chain = pause_chain(self.step_map, run.shared_state, run.current_step_id)
        if chain:
            return chain, chain[-1]["step_id"], None
        # No container holds a pause, so no loop is mid-iteration: markers in
        # the state were left by a pre-F109 pause, checkpointed before the loop
        # could put them back.
        run.shared_state.pop("_loop_current_iteration", None)
        run.shared_state.pop("_loop_total", None)
        here = run.current_step_id
        entry = next((e for e in reversed(run.step_history)
                      if isinstance(e, dict) and e.get("status") == "paused"), None)
        waited_in = entry.get("step_id") if entry else None
        step = self.step_map.get(here)
        if (step is not None and waited_in and waited_in != here
                and progress_key(self.step_map.get(waited_in))
                and not (run.step_progress.get(here) or {}).get("pending_approval")):
            entry.update({"step_id": step.id, "step_name": step.name,
                          "step_type": step.type.value, "output_key": step.output_key,
                          "within": waited_in})
            run.current_step_id = waited_in
            return [], here, waited_in
        return [], here, None

    def _paused_entry(self, run: OrchestrationRun, step: StepConfig, event: dict,
                      step_start_time: float) -> dict:
        """The history entry for a pause: it names the step that asked.

        `step` is the top-level step this loop drives, and the run's position
        stays on it — a question asked inside a Loop, a Parallel branch or a
        nested orchestration is answered by re-entering that container (F109).
        But the entry is the asking step's own, typed as it is (a human step
        stays `human`, which is what keeps a person's wait out of billed time)
        and marked `within` the container holding it, so anything looking for
        the waiting step — the resume, a channel's expiry, cloud's notifier —
        reads it here rather than guessing from the position.
        """
        asker = self.step_map.get(event.get("orch_step_id")) or step
        if asker is step:
            return {
                "step_id": step.id,
                "step_name": step.name,
                "step_type": step.type.value,
                "status": "paused",
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(step_start_time)),
                "output_key": step.output_key,
            }
        chain = pause_chain(self.step_map, run.shared_state, step.id)
        return {
            "step_id": asker.id,
            "step_name": asker.name,
            "step_type": asker.type.value,
            "status": "paused",
            # The pause, not the container's start: this is the wait.
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "output_key": asker.output_key,
            "within": innermost_container(step.id, chain) if chain else step.id,
        }

    @staticmethod
    def _reopen_nested_pause(run: OrchestrationRun, step_id: str, parent_step) -> None:
        """Close the question a nested run just had answered, and open the next.

        The parent waits on the same step throughout, so `step_id` is the same
        for every question — but each question is its own wait, and
        `pauses.pause_ordinal` names a question by the index of the newest
        `paused` entry. One entry for a whole conversation meant one index for
        every question in it (F140).

        The closed entry keeps whatever type it had, so the step's own first
        entry stays the priced one. The new entry is a `human` wait: free,
        outside active time, outside the executed-step count.
        """
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for entry in reversed(run.step_history):
            if entry.get("step_id") == step_id and entry.get("status") == "paused":
                entry["status"] = "completed"
                entry["ended_at"] = now
                break
        run.step_history.append({
            "step_id": step_id,
            "step_name": parent_step.name if parent_step else "",
            "step_type": StepType.HUMAN.value,
            "status": "paused",
            "started_at": now,
            "within": step_id,
        })

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

        Never for a visitor's run (F276): an orchestrator agent on a widget runs
        its workflow in the visitor's turn, and the summary would write their
        words into the owner's memory.
        """
        from core import visitor as _visitor_mod

        if self.depth > 0 or _visitor_mod.current() is not None:
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
        # The run's bookkeeping is not what it made: the tool trace is kept
        # below, verbatim, and the progress keys are engine state.
        _internal = ("_exec_memory_", "_step_progress_", "_loop_progress_", "_parallel_progress_")
        state = [(k, v) for k, v in run.shared_state.items()
                 if not str(k).startswith("__") and not str(k).startswith(_internal)]
        for key, value in state[:20]:
            lines.append(f"[{key}] {str(value)[:300]}")

        from core.config import load_settings
        from core.orchestration.summarizer import compress_for_context
        summary = await compress_for_context(
            "\n".join(lines), max_chars=2000, purpose="session memory",
            settings=load_settings(), session_id=run.session_id, run_id=run.run_id,
        )
        # Every tool call, with its arguments and the start of its result,
        # outside the compression (F272): cut at 300 characters per key, the
        # trace ended inside its first call, and asked later what the run had
        # done the agent invented a command.
        calls = self._tool_trace(run)
        if calls:
            summary = f"{summary}\n\nTool calls (verbatim):\n{calls}"

        from core import agent_memory
        for agent_id in enabled:
            try:
                await agent_memory.write_session_summary(
                    agent_id, run.session_id or run.run_id, summary, run_id=run.run_id,
                )
            except Exception as exc:
                _log.warning(f"memory summary for '{agent_id}' failed: {exc}")

    async def _save_owed_turn(self, run: OrchestrationRun, turn: dict) -> None:
        """Save the conversation's turn a paused workflow agent's run owed (F261)."""
        from core.chat_turns import FAILED_RUN_ANSWER, STOPPED_ANSWER
        from core.session import _save_conversation_turn

        if run.status == "completed":
            answer = self._build_final_response(run, getattr(self.orch, "result_key", "") or "")
        else:
            answer = FAILED_RUN_ANSWER if run.status == "failed" else STOPPED_ANSWER
        await _save_conversation_turn(
            session_id=turn.get("session_id") or "default", agent_id=turn.get("agent_id"),
            user=str(turn.get("message") or ""), assistant=answer,
        )

    def _tool_trace(self, run: OrchestrationRun, budget: int = 6_000) -> str:
        """The run's tool calls, one line each, newest kept within `budget`."""
        names = {s.id: s.name for s in self.orch.steps}
        lines: list[str] = []
        for key, executions in run.shared_state.items():
            if not str(key).startswith("_exec_memory_") or not isinstance(executions, list):
                continue
            step = names.get(str(key)[len("_exec_memory_"):], str(key)[len("_exec_memory_"):])
            for execution in executions:
                trace = (execution or {}).get("trace") or {}
                for call in trace.get("tool_calls") or []:
                    try:
                        args = json.dumps(call.get("args") or {}, default=str, ensure_ascii=False)
                    except (TypeError, ValueError):
                        args = str(call.get("args"))
                    result = " ".join(str(call.get("result_preview") or "").split())
                    lines.append(f"- {step}: {call.get('name')} {args[:200]} → {result[:200]}")
        kept: list[str] = []
        used = 0
        for line in reversed(lines):
            if used + len(line) > budget:
                break
            kept.insert(0, line)
            used += len(line) + 1
        return "\n".join(kept)

    @staticmethod
    def _build_final_response(run: OrchestrationRun, result_key: str = "") -> str:
        """Build a human-readable summary from the run.

        From the run and its orchestration's `result_key` alone, so a run's
        answer can be read back from its checkpoint after the run is over (v1's
        run read, F117). What it made is `results.result_of`'s rule (F249).
        """
        from .results import result_of

        if run.status == "failed":
            # The run's own reason first — one that failed outside any step has
            # no history entry to carry it (F257) — then its newest step error.
            last_error = run_error(run)
            for h in reversed(run.step_history):
                if last_error:
                    break
                if h.get("error"):
                    last_error = h["error"]
            return f"Orchestration failed. Error: {last_error or 'Unknown error'}"

        if run.status == "paused":
            return "Orchestration paused, waiting for human input."

        if run.status == "cancelled":
            # It fell through to "Orchestration completed…" (SP7's note).
            return "Orchestration cancelled."

        last_output = result_of(run.step_history, run.shared_state, result_key)

        if last_output:
            return str(last_output) if not isinstance(last_output, str) else last_output

        # Nothing it ran made an answer — a Human → End workflow, say. It said
        # "Orchestration completed. 1 steps executed.", which a chat showed as
        # the agent's answer and a nested child handed its parent.
        return "The workflow finished without producing an answer."
