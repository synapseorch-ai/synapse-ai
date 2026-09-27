"""
Step executors for each orchestration step type.
Each executor is an async generator that yields SSE-compatible events.
"""
import asyncio
import datetime
import json
import re
import time
from typing import AsyncGenerator, TYPE_CHECKING

import anyio

from core.config import HTTP_TOOL_TIMEOUT, MCP_TOOL_CALL_TIMEOUT, ORCH_STEP_TIMEOUT
from core import expressions as _expressions
from core.models_orchestration import StepConfig, StepType, OrchestrationRun

from core.log import get_logger
from .pauses import end_step, progress_key as _progress_key

_log = get_logger(__name__)


def _resume_index(record: dict, step_ids: list[str]) -> int:
    """Where a container's pause record says to pick up: the step that asked.

    By position when the step is still there, else by id; from the top if the
    definition was edited while the run waited and the step is gone.
    """
    step_id, index = record.get("step_id"), record.get("index")
    if isinstance(index, int) and 0 <= index < len(step_ids) and step_ids[index] == step_id:
        return index
    return step_ids.index(step_id) if step_id in step_ids else 0


def _mention_warnings(mentions) -> list[str]:
    """A `step_warning` per `@[path]` that was left as typed — see core/vault.py."""
    return [w for w in (m.warning() for m in mentions) if w]

if TYPE_CHECKING:
    from .engine import OrchestrationEngine


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _datetime_context() -> str:
    """Return a markdown block with the current date, time, and timezone."""
    now = datetime.datetime.now().astimezone()
    tz_name = now.strftime("%Z") or str(now.tzinfo)
    return (
        "### CURRENT DATE & TIME CONTEXT\n"
        f"**Current Date:** {now.strftime('%A, %B %d, %Y')}\n"
        f"**Current Time:** {now.strftime('%I:%M %p')}\n"
        f"**Timezone:** {tz_name}\n"
    )


class AgentStepExecutor:
    """Run a sub-agent's ReAct loop. Reuses the existing engine.

    If the configured agent has type=='orchestrator', this delegates to a
    nested OrchestrationEngine instead of run_agent_step — implementing the
    sub-workflow composition primitive.
    """

    async def execute(
        self, step: StepConfig, run: OrchestrationRun, engine: "OrchestrationEngine"
    ) -> AsyncGenerator[dict, None]:
        from core.react_engine import run_agent_step
        from core.agent_logger import AgentLogger
        from .resolved import resolve_for_run
        _log.debug(f"AGENT EXEC: agent_id={step.agent_id} step={step.id}")

        from .context import build_step_prompt, snapshot_inputs
        transition = getattr(engine, "current_transition", None)
        if transition is None:
            from .context import TransitionContext
            transition = TransitionContext(origin_type="entry", execution_number=1)
        prompt, system_prompt_extra, system_prompt_prefix, mentions = await build_step_prompt(
            step, run, engine, transition
        )
        inputs_snapshot = snapshot_inputs(step, run, engine)

        # Emit prompt for the orchestration logger (filtered out before SSE)
        yield {"type": "_log_prompt", "orch_step_id": step.id, "prompt": prompt,
               "system_prompt_extra": system_prompt_extra,
               "system_prompt_prefix": system_prompt_prefix,
               "mentions": [m.summary() for m in mentions]}
        for warning in _mention_warnings(mentions):
            yield {"type": "step_warning", "orch_step_id": step.id, "message": warning}

        # ── Orchestrator-as-agent: delegate to a nested OrchestrationEngine ──
        # Resolved once per run: the agent, the custom tools and the aggregated
        # tool set are the same for every step and delegate that uses it. An
        # orchestrator agent needs no tools, so it is resolved without them.
        resolved = await resolve_for_run(engine, step.agent_id, tools=False)
        target_agent = resolved.agent if resolved else None
        if target_agent and target_agent.get("type") == "orchestrator":
            async for ev in self._execute_nested_orchestration(
                step, run, engine, transition, target_agent, prompt, inputs_snapshot
            ):
                yield ev
            return

        if resolved is not None:
            resolved = await resolve_for_run(engine, step.agent_id, tools=True)

        agent_id = step.agent_id or "default"
        agent_name = engine.agent_names.get(agent_id, agent_id)
        # Group sub-agent logs under the same session as the orchestration
        session_id = run.session_id or f"orch_{run.run_id}"
        agent_log = AgentLogger(
            agent_id=agent_id,
            agent_name=agent_name,
            session_id=session_id,
            source=f"orchestration:{run.run_id}",
            user_message=prompt,
        )
        # Log the prompt in the agent log too
        agent_log.log_event({"type": "_log_prompt", "prompt": prompt,
                             "mentions": [m.summary() for m in mentions]})

        # ── Mid-node crash recovery ──────────────────────────────────────
        # A prior interrupted attempt leaves a turn-boundary transcript in
        # shared_state; seed run_agent_step from it so completed tool calls
        # are not re-executed. After two failed resume attempts the
        # transcript is considered poisoned and we fall back to a fresh
        # rerun (which still sees the prior attempt via _exec_memory).
        from core.orchestration.state import SharedState
        legacy_key = f"_step_progress_{step.id}"
        resume_state = None
        resume_attempt = 1
        saved_progress = run.step_progress.get(step.id)
        if saved_progress is None and legacy_key in run.shared_state:
            # A run checkpointed before progress had its own column. Taken
            # out of shared_state at once so the next full checkpoint drops
            # it: with two copies, a second crash could read the stale one's
            # attempt counter.
            saved_progress = run.shared_state.pop(legacy_key)
        # A transcript to continue from, or an approval decision to apply —
        # a step that paused on its very first tool call has the second and
        # not the first, and dropping it would ask the same question forever.
        _has_transcript = isinstance(saved_progress, dict) and bool(saved_progress.get("context_text"))
        _has_verdict = isinstance(saved_progress, dict) and bool(
            saved_progress.get("approved_calls") or saved_progress.get("denied_calls")
        )
        if _has_transcript or _has_verdict:
            prior_attempts = int(saved_progress.get("attempt") or 1)
            if _has_transcript and prior_attempts >= 3:
                run.step_progress.pop(step.id, None)
                _log.debug(f"AGENT EXEC: ⚠ discarding step progress for '{step.id}' "
                      f"after {prior_attempts} attempts — fresh rerun")
            else:
                resume_state = saved_progress
                resume_attempt = prior_attempts + 1
                _log.debug(f"AGENT EXEC: ⏯ resuming step '{step.id}' mid-node "
                      f"(attempt {resume_attempt}, turn {saved_progress.get('turn')})")

        final_response = None
        _log_status = "completed"
        execution_events: list[dict] = []
        try:
            async for event in run_agent_step(
                message=prompt,
                agent_id=step.agent_id,
                session_id=session_id,
                server_module=engine.server_module,
                max_turns=step.max_turns,
                allowed_tools_override=step.allowed_tools,
                source="orchestration",
                run_id=run.run_id,
                system_prompt_extra=system_prompt_extra,
                system_prompt_prefix=system_prompt_prefix,
                model_override=step.model,
                resume_state=resume_state,
                progress_events=True,
                initial_tasks=[dict(t) for t in (run.tasks or [])],
                preresolved=resolved,
                global_variables=getattr(engine, "global_variables", None),
                # Opt-in per step, and off the engine rather than out of shared
                # state — see `StepConfig.pass_images`. `run_agent_step` applies
                # them on its first turn only, so a step that crash-resumes
                # mid-transcript will not see them again.
                images=(getattr(engine, "images", None) or None) if step.pass_images else None,
                should_stop=getattr(engine, "cancel_hook", None),
                # A step's prompt is built from the run, not typed by a person: an
                # earlier step's scraped page, a webhook's input, a vault file.
                # So a `remember` in a workflow always asks (F279; Naveen,
                # 2026-09-27: "every step asks"), and the run pauses for it.
                untrusted_message=True,
            ):
                if event.get("type") == "tasks_updated":
                    # Task state lives on the run, not in a tool's process
                    # memory: checkpointing here is what makes the plan survive
                    # a resume on any worker. The event still falls through to
                    # the journal (structural) and the live stream.
                    run.tasks = [dict(t) for t in (event.get("tasks") or [])]
                    try:
                        await SharedState(run).checkpoint(columns=("tasks",))
                    except Exception as ckpt_err:
                        _log.warning(f"task-list checkpoint failed: {ckpt_err}")
                if event.get("type") == "human_input_required" and event.get("kind") == "tool_approval":
                    # A tool call is waiting on a person. Record it on the run
                    # so the resume can turn their answer into an approval the
                    # loop understands, then let the engine pause as it does
                    # for any human input.
                    progress = dict(run.step_progress.get(step.id) or {})
                    progress["pending_approval"] = {
                        "approval_id": event.get("approval_id"),
                        "tool_name": event.get("tool_name"),
                        "tool_args": event.get("tool_args") or {},
                        "category": event.get("category"),
                    }
                    run.step_progress[step.id] = progress
                    try:
                        await SharedState(run).checkpoint(columns=("step_progress",))
                    except Exception as ckpt_err:
                        _log.warning(f"tool-approval checkpoint failed: {ckpt_err}")
                    yield {**event, "orch_step_id": step.id, "step_name": step.name}
                    return

                if event.get("type") == "_step_progress":
                    # Turn-boundary transcript checkpoint: persist so a crash
                    # resumes mid-step. Swallowed here — never logged, never
                    # forwarded to SSE/journal. Only the progress column is
                    # written: this fires every tool turn, and it used to
                    # rewrite the whole run each time.
                    run.step_progress[step.id] = {
                        "turn": event.get("turn"),
                        "context_text": event.get("context_text"),
                        "has_compacted": event.get("has_compacted"),
                        "browser_actions": event.get("browser_actions"),
                        "tools_used": event.get("tools_used"),
                        # Whether the step has read content nobody typed (F279) —
                        # a resume must not forget it.
                        "memory_tainted": event.get("memory_tainted"),
                        "attempt": resume_attempt,
                    }
                    try:
                        await SharedState(run).checkpoint(columns=("step_progress",))
                    except Exception as ckpt_err:
                        _log.warning(f"step-progress checkpoint failed: {ckpt_err}")
                    continue
                execution_events.append(event)
                agent_log.log_event(event)
                yield {**event, "orch_step_id": step.id, "step_name": step.name}
                if event.get("type") == "final":
                    final_response = event.get("response", "")
        except Exception:
            _log_status = "error"
            raise
        finally:
            agent_log.run_end(_log_status)
            agent_log.close()

        if final_response is None:
            raise RuntimeError(f"Agent step '{step.name}' ended without producing a final response")

        # Clean completion leaves no transcript residue (deliberate
        # re-invocations — loops, evaluator retries — rely on _exec_memory,
        # not on step progress).
        run.step_progress.pop(step.id, None)

        if step.output_key:
            run.shared_state[step.output_key] = final_response

        # Store execution trace for memory across re-invocations
        from .context import build_execution_trace, store_execution_memory
        trace = build_execution_trace(execution_events)
        store_execution_memory(run, step, trace, agent_name, transition, inputs_snapshot)

    async def _execute_nested_orchestration(
        self,
        step: StepConfig,
        run: OrchestrationRun,
        engine: "OrchestrationEngine",
        transition,
        target_agent: dict,
        prompt: str,
        inputs_snapshot: dict,
    ) -> AsyncGenerator[dict, None]:
        """Run an orchestrator-type agent as a nested sub-orchestration.

        Input mapping:  the resolved prompt becomes the sub-orch's user_input.
        State scoping:  sub-orch gets a FRESH shared_state — parent's private
                        keys (_loop_*, _routing_*, _exec_memory_*) are not shared
                        to avoid collisions.
        Output mapping: sub-orch's terminal `final` event becomes the parent
                        step's final_response → written to step.output_key as
                        usual and recorded in parent execution memory.
        Recursion:      capped at engine.MAX_NESTED_DEPTH; sub-engine constructed
                        with depth = parent_depth + 1.
        Events:         all sub-events tagged with parent orch_step_id +
                        nested_run_id so the UI can attribute them.
        """
        from core.scale.context import resolve_orchestration
        from core.models_orchestration import Orchestration
        from .engine import OrchestrationEngine, MAX_NESTED_DEPTH

        sub_orch_id = target_agent.get("orchestration_id")
        if not sub_orch_id:
            raise RuntimeError(
                f"Orchestrator agent '{target_agent.get('id')}' has no orchestration_id"
            )

        parent_depth = getattr(engine, "depth", 0)
        if parent_depth + 1 > MAX_NESTED_DEPTH:
            raise RuntimeError(
                f"Nested orchestration depth limit exceeded "
                f"(max {MAX_NESTED_DEPTH}). Possible recursive agent loop."
            )

        # By id, through the resource provider — the same path resume takes.
        # This used to load every orchestration in the tenant and scan.
        sub_orch_data = await resolve_orchestration(sub_orch_id)
        if not sub_orch_data:
            raise RuntimeError(
                f"Sub-orchestration '{sub_orch_id}' (from agent "
                f"'{target_agent.get('id')}') not found"
            )

        sub_orch = Orchestration.model_validate(sub_orch_data)
        sub_engine = OrchestrationEngine(sub_orch, engine.server_module, depth=parent_depth + 1)
        sub_run_id = f"{run.run_id}__{step.id}_d{parent_depth + 1}"
        sub_session = run.session_id or f"orch_{run.run_id}"

        agent_name = target_agent.get("name") or target_agent.get("id") or "sub_orchestration"
        yield {
            "type": "thinking",
            "orch_step_id": step.id,
            "step_name": step.name,
            "message": f"Delegating to sub-orchestration '{sub_orch.name}' (depth {parent_depth + 1})...",
        }

        # Lifecycle meta-events that belong to the sub-orch's own run; forwarding
        # them would confuse the parent's frontend state (premature "complete" banners,
        # wrong step-start banners, etc.).
        _NESTED_FILTER_TYPES = {"orchestration_start", "orchestration_complete", "orchestration_end"}

        final_response: str | None = None
        outcome = NestedOutcome()
        sub_events: list[dict] = []
        try:
            async for sub_event in sub_engine.run(
                initial_input=prompt,
                run_id=sub_run_id,
                session_id=sub_session,
            ):
                sub_events.append(sub_event)
                outcome.read(sub_event)
                # Capture the sub-engine's terminal final response
                if sub_event.get("type") == "final" and sub_event.get("intent") == "orchestration":
                    final_response = sub_event.get("response", "")

                # Skip sub-orch lifecycle events — they pollute the parent's UI
                if sub_event.get("type") in _NESTED_FILTER_TYPES:
                    continue

                # Override run_id with the PARENT's run_id so the frontend routes
                # human-input calls to the parent run (not the sub-run directly).
                # The sub-run id is preserved in nested_run_id for backend resume logic.
                yield {
                    **sub_event,
                    "run_id": run.run_id,
                    "orch_step_id": step.id,
                    "step_name": step.name,
                    "nested_run_id": sub_run_id,
                    "nested_orch_id": sub_orch.id,
                    "nested_depth": parent_depth + 1,
                }
        except Exception:
            raise

        # A child that did not complete fails this step, with its reason (F253).
        # Its final text used to be taken as the result whatever its status, so
        # "Orchestration failed. Error: … Traceback …" became the step's output,
        # the next step used it, and the run completed; in a Loop the iteration
        # was dropped and the loop carried on to the next item. Here, after the
        # child's last event — a child that paused never gets this far, its
        # parent stops at the pause.
        failure = outcome.failure(sub_orch.name)
        if failure:
            raise RuntimeError(failure)

        if final_response is None:
            # Sub-orch finished without a final event (failed or empty). Fall back
            # to the orchestration_complete event's last shared_state value.
            for ev in reversed(sub_events):
                if ev.get("type") == "orchestration_complete":
                    state = ev.get("final_state") or {}
                    # last output_key in the sub-orch's steps
                    for sub_step in reversed(sub_orch.steps):
                        if sub_step.output_key and sub_step.output_key in state:
                            final_response = str(state[sub_step.output_key])
                            break
                    break

        if final_response is None:
            raise RuntimeError(
                f"Sub-orchestration '{sub_orch.name}' ended without a final response"
            )

        if step.output_key:
            run.shared_state[step.output_key] = final_response

        # Record memory so re-invocations see what the sub-orch returned.
        from .context import build_execution_trace, store_execution_memory
        synth_events = [
            {"type": "tool_call", "tool_name": "sub_orchestration", "tool_input": {"orchestration": sub_orch.name}},
            {"type": "tool_result", "result": str(final_response)},
            {"type": "final", "response": str(final_response)},
        ]
        trace = build_execution_trace(synth_events)
        store_execution_memory(run, step, trace, agent_name, transition, inputs_snapshot)


class NestedOutcome:
    """How a nested run ended, read from its events as they pass (F253).

    Its `orchestration_complete` says the status; its last `step_error` or
    `orchestration_error` says why. The run's own final text is the fallback
    reason: for a failed run it is "Orchestration failed. Error: …".
    """

    _FINAL_PREFIX = "Orchestration failed. Error: "
    _PREFIX = "Sub-orchestration "

    def __init__(self) -> None:
        self.status: str | None = None
        self.reason: str | None = None
        self.response: str | None = None

    def read(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "orchestration_complete":
            self.status = event.get("status")
        elif etype == "step_error":
            self.reason = event.get("reason") or event.get("error") or self.reason
        elif etype == "orchestration_error":
            self.reason = event.get("error") or self.reason
        elif etype == "final" and event.get("intent") == "orchestration":
            self.response = event.get("response")

    def failure(self, name: str) -> str | None:
        """Why the run did not complete, or None when it did.

        A run that ended with no status at all — its resume gave up before it
        started, say — did not complete either.
        """
        if self.status == "completed":
            return None
        reason = self.reason
        if not reason and self.response:
            reason = self.response.removeprefix(self._FINAL_PREFIX)
        reason = (reason or "").removeprefix("RuntimeError: ")
        if reason.startswith(self._PREFIX):
            # A grandchild's failure, already named: one chain, so the innermost
            # reason survives the engine's 300-character cap at every level.
            return f"{self._PREFIX}'{name}' › {reason[len(self._PREFIX):]}"
        verb = "was cancelled" if self.status == "cancelled" else "failed"
        return f"{self._PREFIX}'{name}' {verb}: {reason or 'it ended without finishing'}"


class ToolCallFailed(RuntimeError):
    """A tool that ran and reported failure: an error, not the step's output (F90).

    A Tool step used to hand a python tool's non-zero exit, an MCP result marked
    `isError` and an HTTP tool's 4xx/5xx on as its result, and the run carried on.
    `retryable` says whether other arguments might fix it — the step's attempts
    are the model's to try again with. A server fault is not the model's to fix,
    and re-sending a request that may already have had its effect could do the
    thing twice.
    """

    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


class ToolStepExecutor:
    """Single forced tool call — lightweight direct LLM call (no full agent/ReAct stack).

    Asks the LLM to generate arguments for exactly one tool as JSON, then executes
    the tool directly via the MCP session. Retries up to max_turns if the LLM output
    cannot be parsed or the tool call fails.

    This mirrors EvaluatorStepExecutor's approach: a direct llm_generate call with
    only the target tool's schema embedded in the prompt, avoiding the 276K-char
    system prompt overhead of run_agent_step.
    """

    async def execute(
        self, step: StepConfig, run: OrchestrationRun, engine: "OrchestrationEngine"
    ) -> AsyncGenerator[dict, None]:
        from core.llm_providers import generate_response as llm_generate, detect_mode_from_model
        from core.config import load_settings
        from core.react_engine import parse_tool_call
        from core.tools import resolve_one_tool
        from .resolved import resolve_for_run, run_custom_tools

        if not step.forced_tool:
            # A step that cannot run is not a step that ran (F90): it used to
            # warn and let the run carry on without it.
            message = f"Tool step '{step.name}' has no tool configured"
            yield {"type": "final", "orch_step_id": step.id, "step_name": step.name, "response": message}
            raise RuntimeError(message)

        from core import visitor as _visitor_mod
        _visitor = _visitor_mod.current()
        if _visitor is not None and not _visitor.allows(step.forced_tool):
            # A workflow on a widget runs in the visitor's turn, and a Tool step
            # is a tool call like any other: the owner's list holds here as it
            # does for an agent (F280). Refused before the model is asked for
            # arguments, so nothing about the tool is spent on a stranger.
            message = (f"Tool step '{step.name}' uses '{step.forced_tool}', which visitors "
                       f"on this {_visitor.surface} may not use")
            yield {"type": "final", "orch_step_id": step.id, "step_name": step.name, "response": message}
            raise RuntimeError(message)

        from .context import build_step_prompt, TransitionContext, snapshot_inputs
        transition = getattr(engine, "current_transition", None)
        if transition is None:
            transition = TransitionContext(origin_type="entry", execution_number=1)
        prompt, system_prompt_extra, system_prompt_prefix, mentions = await build_step_prompt(
            step, run, engine, transition
        )
        inputs_snapshot = snapshot_inputs(step, run, engine)

        yield {"type": "_log_prompt", "orch_step_id": step.id, "prompt": prompt,
               "system_prompt_extra": system_prompt_extra,
               "system_prompt_prefix": system_prompt_prefix,
               "mentions": [m.summary() for m in mentions]}
        for warning in _mention_warnings(mentions):
            yield {"type": "step_warning", "orch_step_id": step.id, "message": warning}

        # Model resolution — same pattern as EvaluatorStepExecutor and LLMStepExecutor
        settings = load_settings()
        _step_model = step.model if (step.model and step.model.strip().lower() not in ("", "default")) else None
        model = _step_model if _step_model else settings.get("model", "mistral")
        mode = detect_mode_from_model(model)

        # The forced tool's own declaration. This used to aggregate every
        # server's tools — list, filter, schema-map, serialise — to find one.
        resolved = await resolve_for_run(engine, step.agent_id, tools=False) if step.agent_id else None
        active_agent = (resolved.agent if resolved else None) or {}
        custom_tools = resolved.custom_tools if resolved else await run_custom_tools(engine)

        tool_name = step.forced_tool
        tool_obj = await resolve_one_tool(engine.server_module, tool_name, active_agent, custom_tools)
        if not tool_obj:
            # Failed the way a tool call that fails is, below: the step's own
            # line says why (the engine's stream message is sanitised), and the
            # run stops here rather than carrying on without it.
            message = f"Tool '{tool_name}' not found in available tools"
            yield {"type": "final", "orch_step_id": step.id, "step_name": step.name, "response": message}
            raise RuntimeError(message)

        # ── Tool policy (F267): the step's agent's, or the workspace's ──────
        # An Agent step's calls went through the policy and a Tool step's did
        # not, so `run_bash` forced in a Tool step ran under a `deny`.
        from core import tool_policy as _tool_policy
        from core.chat_approval import approval_prompt
        from core.orchestration.state import SharedState
        from core.react_engine import _approval_id

        from core.tool_router import ToolRouter

        _router = getattr(engine.server_module, "tool_router", None)
        if not isinstance(_router, ToolRouter):
            _router = ToolRouter(_router or {})

        def _same_tool(named: str, forced: str) -> bool:
            """`server__tool` and a bare `tool` the router sends to it are one tool."""
            key = _router.resolve(named) if isinstance(named, str) else None
            return key is not None and key == _router.resolve(forced)

        _policy = _tool_policy.resolve_policy(active_agent, settings)
        if _visitor is not None:
            _policy = {c: ("deny" if d == "confirm" else d) for c, d in _policy.items()}
        _decision, _category = _tool_policy.decide(tool_obj, _policy)

        # A person's answer to a pause this step made: the call they allowed is
        # the one that runs — the model is not asked again, it might ask for
        # something else — and a refusal fails the step with their reason.
        _answered = (run.step_progress.get(step.id) or {}).get("pending_call")
        _replay: dict | None = None
        if _answered:
            run.step_progress.pop(step.id, None)
            # Spent before the call runs, and saved: a crash mid-call must not
            # find the approval again on recovery and run the call a second time.
            try:
                await SharedState(run).checkpoint(columns=("step_progress",))
            except Exception as ckpt_err:
                _log.warning(f"tool-approval checkpoint failed: {ckpt_err}")
            if not _answered.get("approved"):
                reason = _answered.get("reason") or ""
                message = (f"Tool step '{step.name}' did not run '{tool_name}': a person refused it"
                           + (f": {reason}" if reason else ""))
                yield {"type": "final", "orch_step_id": step.id, "step_name": step.name, "response": message}
                raise RuntimeError(message)
            _replay = {"tool": tool_name, "arguments": _answered.get("arguments") or {}}
        elif _decision == "deny":
            # Refused before the model is asked for arguments.
            message = (f"Tool step '{step.name}' did not run '{tool_name}': the tool policy blocks it "
                       f"because {_tool_policy.explain(_category)}")
            yield {"type": "final", "orch_step_id": step.id, "step_name": step.name, "response": message}
            raise RuntimeError(message)

        # Build evaluator-style prompt: ask LLM to output JSON tool call (no tools= API param)
        tool_schema_str = str(tool_obj.inputSchema)
        tool_prompt = (
            f"{prompt}\n\n"
            f"Call the tool '{tool_obj.name}'.\n"
            f"Tool description: {tool_obj.description}\n"
            f"Tool parameters schema: {tool_schema_str}\n\n"
            f'Respond with ONLY a JSON object: {{"tool": "{tool_obj.name}", "arguments": {{...}}}}'
        )

        yield {
            "type": "thinking", "orch_step_id": step.id,
            "message": f"Tool step '{step.name}' — preparing to call '{tool_name}'...",
        }

        max_turns = max(1, step.max_turns or 3)
        last_error = None
        final_response = None
        called_tool: str = ""
        last_tool_args: dict = {}
        attempts = 0

        for turn in range(max_turns):
            attempts = turn + 1
            turn_prompt = tool_prompt
            if last_error:
                turn_prompt += f"\n\nPrevious attempt failed: {last_error}\nPlease try again with correct arguments."

            _log.debug(f"TOOL STEP: turn {turn + 1}/{max_turns} model={model} tool={tool_name}")
            _replaying = _replay is not None
            if _replaying:
                # The approved call, as it was approved — once.
                response, _replay = json.dumps(_replay), None
            else:
                try:
                    tool_sys_prompt = "You are a tool-calling assistant. Output ONLY valid JSON."
                    if system_prompt_prefix:
                        tool_sys_prompt = system_prompt_prefix + "\n\n" + tool_sys_prompt
                    from core.model_wait import ModelWait

                    _wait = ModelWait(llm_generate(
                        prompt_msg=turn_prompt,
                        sys_prompt=tool_sys_prompt,
                        mode=mode,
                        current_model=model,
                        current_settings=settings,
                        # The workspace's effort, as an agent without its own gets
                        # (F77); passing nothing meant "medium" whatever it said.
                        reasoning_effort=settings.get("reasoning_effort"),
                        session_id=run.session_id,
                        agent_id=step.agent_id or "tool_step",
                        source="orchestration",
                        run_id=run.run_id,
                    ), label=f"Tool step '{step.name}'", extra={"orch_step_id": step.id})
                    async for _wait_event in _wait.events():
                        yield _wait_event
                    response = _wait.result
                except Exception as e:
                    from core.llm_providers import LLMError
                    if isinstance(e, LLMError):
                        raise
                    raise RuntimeError(f"Tool step '{step.name}' LLM call failed: {e}") from e

            if not _replaying:
                # Log the LLM response (mirrors _log_evaluator for evaluator steps)
                yield {"type": "_log_tool_step_llm", "orch_step_id": step.id,
                       "prompt": turn_prompt, "llm_response": response}

            tool_call, json_error = parse_tool_call(response)
            if not tool_call:
                last_error = json_error or "LLM did not return a valid tool call JSON"
                _log.debug(f"TOOL STEP: ⚠ parse failed turn={turn + 1}: {last_error}")
                continue

            called_tool = tool_call.get("tool", "")
            tool_args = tool_call.get("arguments", {})
            if called_tool != tool_obj.name and not _same_tool(called_tool, tool_obj.name):
                # This step's tool is the only one it runs (F281). The name in
                # the model's JSON used to be looked up across every server,
                # custom tool and builder tool, so a reply naming `run_bash` on a
                # step forced to read the clock ran `run_bash`.
                last_error = (f"This step calls only '{tool_obj.name}'; '{called_tool}' is not it. "
                              f'Answer with {{"tool": "{tool_obj.name}", "arguments": {{...}}}}')
                _log.debug(f"TOOL STEP: ⚠ model named '{called_tool}' instead of '{tool_obj.name}'")
                continue
            # The forced tool runs, by its own name — a bare name the model used
            # for a namespaced tool dispatches to the same place.
            called_tool = tool_obj.name
            last_tool_args = tool_args

            if _decision == "confirm" and not _replaying:
                # Held for a person, exactly as an Agent step's gated call is: the
                # run pauses here with the call, and the resume re-enters this
                # step with the answer (`OrchestrationEngine._resume_tool_approval`).
                approval_id = _approval_id(called_tool, tool_args)
                progress = dict(run.step_progress.get(step.id) or {})
                progress["pending_approval"] = {
                    "approval_id": approval_id, "tool_name": called_tool,
                    "tool_args": tool_args, "category": _category,
                }
                run.step_progress[step.id] = progress
                try:
                    await SharedState(run).checkpoint(columns=("step_progress",))
                except Exception as ckpt_err:
                    _log.warning(f"tool-approval checkpoint failed: {ckpt_err}")
                yield {
                    "type": "human_input_required", "kind": "tool_approval",
                    "prompt": approval_prompt(called_tool, _category),
                    "tool_name": called_tool, "tool_args": tool_args, "category": _category,
                    "approval_id": approval_id,
                    "fields": [
                        {"name": "approve", "type": "boolean", "label": f"Run {called_tool}?"},
                        {"name": "reason", "type": "text", "label": "Reason (optional)"},
                    ],
                    "orch_step_id": step.id, "step_name": step.name,
                }
                return

            # tool_execution matches the event type the logger and react_engine emit
            yield {
                "type": "tool_execution",
                "orch_step_id": step.id,
                "step_name": step.name,
                "tool_name": called_tool,
                "args": tool_args,
            }
            _log.debug(f"TOOL STEP: 🔧 Tool Call: {called_tool}")
            _log.debug(f"TOOL STEP: 📥 Args: {json.dumps(tool_args, indent=2, default=str)[:1000]}")

            try:
                resource_events: list[dict] = []
                result = await self._execute_tool(
                    called_tool, tool_args, engine, resource_events, run_id=run.run_id
                )
                final_response = result
                if step.output_key:
                    run.shared_state[step.output_key] = result
                preview = str(result)[:500] if result else ""
                yield {
                    "type": "tool_result",
                    "orch_step_id": step.id,
                    "step_name": step.name,
                    "tool_name": called_tool,
                    "preview": preview,
                }
                for _saved in resource_events:
                    yield {**_saved, "orch_step_id": step.id, "step_name": step.name}
                _log.debug(f"TOOL STEP: 📤 Tool Result ({called_tool}): {preview}")
                _log.debug(f"TOOL STEP: ✅ tool '{called_tool}' succeeded")
                break
            except ToolCallFailed as e:
                last_error = str(e)
                _log.debug(f"TOOL STEP: ❌ tool reported failure turn={turn + 1}: {last_error}")
                if not e.retryable:
                    break
            except Exception as e:
                last_error = str(e)
                _log.debug(f"TOOL STEP: ❌ tool execution failed turn={turn + 1}: {last_error}")

        yield {
            "type": "final",
            "orch_step_id": step.id,
            "step_name": step.name,
            "response": final_response if final_response is not None
                        else f"Tool step '{step.name}' failed after {attempts} attempt(s): {last_error}",
        }

        if final_response is None:
            raise RuntimeError(f"Tool step '{step.name}' failed after {attempts} attempt(s): {last_error}")

        # Record execution memory so re-invocations can see prior turn's args + result.
        from .context import build_execution_trace, store_execution_memory
        synth_events = [
            {"type": "tool_call", "tool_name": called_tool, "tool_input": last_tool_args},
            {"type": "tool_result", "result": str(final_response)},
            {"type": "final", "response": str(final_response)},
        ]
        trace = build_execution_trace(synth_events)
        store_execution_memory(
            run, step, trace,
            agent_name=step.forced_tool or "tool_step",
            transition=transition,
            inputs_snapshot=inputs_snapshot,
        )

    async def _execute_tool(
        self, tool_name: str, tool_args: dict, engine: "OrchestrationEngine",
        resource_events: list | None = None, *, run_id: str | None = None,
    ) -> str:
        """Execute a tool via MCP session or Docker sandbox (custom Python tools).

        `resource_events`, when given, collects what a builder tool changed so
        the caller can yield it after the result — this returns a string, and
        the whole resource does not belong in one.
        """
        from datetime import timedelta
        server_module = engine.server_module

        # Native builder tools (create_orchestration, list_agents, etc.) —
        # dispatched directly to execute_builder_tool so TOOL steps can drive
        # the builder primitives.
        from core.builder_tools import BUILDER_TOOL_NAMES, execute_builder_tool_with_events
        if tool_name in BUILDER_TOOL_NAMES:
            # The run's id lets a builder tool tell its own run's work from
            # another's — `finish_build` removes only what this run left (F156).
            output, saved = await execute_builder_tool_with_events(
                tool_name, tool_args, server_module, run_id=run_id
            )
            if resource_events is not None:
                resource_events.extend(saved)
            return output

        tool_router = getattr(server_module, "tool_router", {})
        if tool_name in tool_router:
            agent_name, actual_tool_name = tool_router[tool_name]
            session = server_module.agent_sessions.get(agent_name)
            if session:
                result = await session.call_tool(
                    actual_tool_name, tool_args, read_timeout_seconds=timedelta(seconds=MCP_TOOL_CALL_TIMEOUT)
                )
                text = result.content[0].text if result.content else ""
                if getattr(result, "isError", False):
                    # MCP's own way of saying the call failed; it was never read.
                    raise ToolCallFailed(
                        f"Tool '{tool_name}' returned an error: {text[:500] or '(no detail)'}",
                        retryable=True)
                return text
        # Custom tools — Python or HTTP. The run's resolved list, the one the
        # schema lookup above already used: the store's raw rows carry a hosted
        # deployment's `synsec://` references, which went to the server as the
        # header's value (F1).
        from .resolved import run_custom_tools
        custom_tools = await run_custom_tools(engine)
        target_tool = next((t for t in custom_tools if t["name"] == tool_name), None)
        if target_tool:
            tool_type = target_tool.get("tool_type", "http")
            if tool_type == "python":
                return await self._execute_python_tool(target_tool, tool_args)
            # HTTP tool — with URL templating and method-aware arg routing
            return await self._execute_http_tool(target_tool, tool_args)
        raise RuntimeError(f"Tool '{tool_name}' not found in tool router")

    async def _execute_python_tool(self, tool: dict, tool_args: dict) -> str:
        """Execute a custom Python tool through the registered code runner.

        The same execution as `react_engine.run_agent_step`'s python branch, on
        purpose: a tool called from a TOOL step and the same tool called by an
        agent run identically. Both are one call to `core.execution`, which is
        what stopped them drifting. What a failure *means* differs: an agent is
        handed the error as the tool's answer and decides what to do with it; a
        TOOL step has nobody to decide, so a non-zero exit is a failed call
        (F90) — retried with the error, then the run fails.
        """
        from core import variables as _variables
        from core.execution import ExecRequest, Network, declared_globals, get_runner, wrap_args
        from core.vault import _vault_root

        python_code = tool.get("code", "")
        if not python_code.strip():
            raise ValueError("Python tool has no code defined.")

        # The arguments go in as written — a model wrote them — and only the
        # globals the tool declares go in beside them (F28, `declared_globals`).
        granted = declared_globals(tool, await _variables.load()) if tool.get("globals") else {}

        result = await get_runner().run(ExecRequest(
            code=wrap_args(python_code, tool_args, granted),
            network=Network.INTERNET,
            timeout_s=30,
            vault_path=_vault_root(),
        ))

        if result.exit_code != 0:
            # The end of the output is where a traceback says what went wrong.
            detail = (result.stderr or result.stdout or "").strip()[-800:]
            raise ToolCallFailed(
                f"Python tool exited with code {result.exit_code}: {detail or '(no output)'}",
                retryable=True)
        try:
            return json.dumps(json.loads(result.stdout.strip()))
        except Exception:
            return json.dumps({"output": result.stdout})

    async def _execute_http_tool(self, tool: dict, tool_args: dict) -> str:
        """Execute a custom HTTP tool.

        The request is built by `core.http_tool`, which the agent's executor
        also uses — so `{{global.key}}`, the request-body template and "an
        argument the URL used is not sent twice" behave the same whether a tool
        is called by an agent or by a TOOL step. They did not before: D69 fixed
        all three in the agent's copy and this one kept none of them, so the
        same tool sent a literal `{{global.api_base}}` to the server from here
        and the real value from there.

        `send` also checks where the request is going before it goes (S4).
        """
        import httpx as _httpx

        from core import http_tool
        from core import variables as _variables

        async with _httpx.AsyncClient() as client:
            resp = await http_tool.send(
                client, tool, tool_args, await _variables.load(),
                timeout=HTTP_TOOL_TIMEOUT,
            )
        if resp.status_code >= 400:
            raise ToolCallFailed(
                f"HTTP {resp.status_code} from '{tool.get('name', 'tool')}': {(resp.text or '')[:500]}",
                retryable=resp.status_code < 500)

        _, text = http_tool.decode(resp, tool)
        return text



class EvaluatorStepExecutor:
    """Pure routing node — no agent. Makes a single LLM call using
    evaluator_prompt + route_descriptions + context from input_keys
    to decide which route to take."""

    async def execute(
        self, step: StepConfig, run: OrchestrationRun, engine: "OrchestrationEngine"
    ) -> AsyncGenerator[dict, None]:
        from core.llm_providers import generate_response as llm_generate_response
        from core.config import load_settings

        if not step.route_map:
            yield {"type": "step_warning", "orch_step_id": step.id, "message": "No routes defined"}
            return

        # Build context from input_keys
        context_parts = []
        for key in (step.input_keys or []):
            if key not in run.shared_state:
                continue
            val = run.shared_state[key]
            # Find producer for attribution
            label = key
            producer = next((s for s in engine.step_map.values() if s.output_key == key), None)
            if producer and producer.agent_id and producer.agent_id in engine.agent_names:
                label = f"{engine.agent_names[producer.agent_id]} → {key}"

            val_str = str(val)
            context_parts.append(f"[{label}]:\n{val_str}")

        context_block = "\n\n".join(context_parts) if context_parts else "(no context available)"

        # Build route descriptions
        route_lines = []
        labels = list(step.route_map.keys())
        for label_name, target_id in step.route_map.items():
            custom_desc = step.route_descriptions.get(label_name, "")
            if custom_desc:
                route_lines.append(f'  - "{label_name}": {custom_desc}')
            elif target_id is None:
                route_lines.append(f'  - "{label_name}": End the orchestration')
            else:
                target_step = engine.step_map.get(target_id)
                target_name = target_step.name if target_step else target_id
                route_lines.append(f'  - "{label_name}": Route to {target_name}')
        routes_text = "\n".join(route_lines)

        evaluator_instructions = ""
        if step.evaluator_prompt:
            evaluator_instructions = f"EVALUATOR INSTRUCTIONS:\n{step.evaluator_prompt}\n\n"

        # Date/time goes last: every provider's prompt cache is a prefix cache,
        # and a minute-granular timestamp in the first 200 chars defeats it for
        # the whole message. The routing instruction stays adjacent to it.
        prompt = (
            f"{evaluator_instructions}"
            f"Based on the context below, decide which route to take.\n\n"
            f"CONTEXT:\n{context_block}\n\n"
            f"AVAILABLE ROUTES:\n{routes_text}\n\n"
            f"{_datetime_context()}\n"
            f"Respond with ONLY a JSON object: {{\"tool\": \"route_<label>\", \"arguments\": {{\"reasoning\": \"your reason\"}}}}\n"
            f"Valid labels: {labels}"
        )

        # Emit evaluator prompt for the logger
        yield {"type": "_log_prompt", "orch_step_id": step.id, "prompt": prompt}

        yield {"type": "thinking", "orch_step_id": step.id, "message": f"Evaluator deciding route..."}

        settings = load_settings()
        routing_decision = None
        # Per-step model override for evaluators.
        # Treat None, empty string, or "default" as "use the global default model".
        _step_model = step.model if (step.model and step.model.strip().lower() not in ("", "default")) else None
        eval_model = _step_model if _step_model else settings.get("model", "mistral")
        from core.llm_providers import detect_mode_from_model
        eval_mode = detect_mode_from_model(eval_model)
        try:
            from core.model_wait import ModelWait

            # Out loud, as the agent loop waits: this step is the AI builder's
            # first, and a hung call here was the full step timeout of silence
            # under "Evaluator deciding route..." before the run failed.
            _wait = ModelWait(llm_generate_response(
                prompt_msg=prompt,
                sys_prompt="You are a routing decision maker. Output ONLY valid JSON.",
                mode=eval_mode,
                current_model=eval_model,
                current_settings=settings,
                reasoning_effort=settings.get("reasoning_effort"),  # F77
                session_id=run.session_id,
                agent_id=step.agent_id or "evaluator",
                source="orchestration",
                run_id=run.run_id,
                cache_response=step.cache_responses_enabled,
                cache_response_semantic=step.cache_semantic_enabled,
                cache_response_ttl=step.cache_response_ttl_seconds,
                cache_response_step_id=step.id,
                cache_response_threshold=step.cache_response_threshold,
            ), label="Evaluator", extra={"orch_step_id": step.id})
            async for _wait_event in _wait.events():
                yield _wait_event
            response = _wait.result
            _log.debug(f"🔀 Evaluator LLM response: {response}")

            # Emit evaluator LLM response for the logger
            yield {"type": "_log_evaluator", "orch_step_id": step.id, "prompt": prompt, "llm_response": response}

            from core.react_engine import parse_tool_call
            tool_call, _ = parse_tool_call(response)
            if tool_call:
                tool_name = tool_call.get("tool", "")
                if tool_name in {f"route_{l}" for l in labels}:
                    routing_decision = {
                        # The step's own event: which route the model picked.
                        # `routing_decision` is the ROUTER's name for the
                        # resolved target, and this used to borrow it — two
                        # payloads sharing no field, so the run log printed
                        # `Evaluator routed → undefined` before every real
                        # decision. Named for its step type now, exactly like
                        # `if_decision` and `switch_decision`.
                        "type": "evaluator_decision",
                        "tool_name": tool_name,
                        "arguments": tool_call.get("arguments", {}),
                    }
        except Exception as e:
            # LLM errors must propagate to stop orchestration
            from core.llm_providers import LLMError
            if isinstance(e, LLMError):
                _log.warning(f"❌ Evaluator LLM failed — stopping orchestration: {e}")
                raise
            _log.debug(f"Evaluator routing call failed: {e}")

        # Fallback when the model's reply did not parse into a route.
        #
        # This used to be "the first key of `route_map`", which is not a thing
        # that exists: `definition` is JSONB on Postgres, and JSONB sorts object
        # keys by (length, bytes) rather than storing them in insertion order.
        # The same orchestration therefore fell back one way on SQLite and
        # another on Postgres — and adding a route could silently move it, which
        # is exactly what happened when the builder gained `answer` (six
        # characters, sorting ahead of `create`).
        #
        # `fallback_route` says which one out loud. `labels[0]` remains the
        # default so every orchestration that predates the field behaves as it
        # did on SQLite.
        if not routing_decision:
            declared = step.fallback_route
            fallback_label = declared if declared in labels else (labels[0] if labels else None)
            if fallback_label:
                _log.debug(f"Evaluator falling back to route: {fallback_label}")
                routing_decision = {
                    "type": "evaluator_decision",
                    "tool_name": f"route_{fallback_label}",
                    "arguments": {"reasoning": "Fallback — LLM did not return a valid routing decision"},
                }

        if routing_decision:
            _log.info(f"routing decision")
            yield {**routing_decision, "orch_step_id": step.id}
            tool_name = routing_decision.get("tool_name", "")
            label = tool_name.replace("route_", "", 1) if tool_name.startswith("route_") else tool_name
            run.shared_state[f"_routing_decision_{step.id}"] = label
            run.shared_state[f"_routing_reasoning_{step.id}"] = routing_decision.get("arguments", {}).get("reasoning", "")

            # Store bare route label so downstream evaluators/templates can match it verbatim.
            # Reasoning is already kept in `_routing_reasoning_<step_id>` above.
            if step.output_key:
                run.shared_state[step.output_key] = label


#: A container's own signal that a step inside it was an End: never yielded to
#: the engine, which would take it as the run ending there and abandon the
#: container mid-flight (F128). The container finishes, then yields the End
#: event it carried — so the engine ends the run, and nothing inside was
#: skipped to get there (F125).
CONTAINER_END = "_container_end"


#: Step types with no shared resource behind them — no MCP session, no
#: browser, no subprocess of the agent's — so branches made only of these can
#: overlap. `agent` and `tool` are excluded: MCP sessions and the single
#: Playwright instance are not concurrency-safe. `human` is excluded because
#: a pause inside a concurrent branch has no resume story.
CONCURRENT_SAFE_TYPES = frozenset({
    StepType.LLM, StepType.TRANSFORM, StepType.PRINT, StepType.EXTRACT_JSON,
    StepType.IF_ELSE, StepType.SWITCH, StepType.MERGE,
})

_BRANCH_DONE = object()


class ParallelStepExecutor:
    """Run multiple branches.

    Each branch entry in ``parallel_branches`` can be:
      - A single entry-point step ID  → executor auto-follows ``next_step_id`` chain
      - Multiple step IDs             → executor runs them in that explicit order

    A branch chain stops when a step has no ``next_step_id``, or its ``next_step_id``
    equals the parallel node's own convergence point (``step.next_step_id``).

    Branches made only of `CONCURRENT_SAFE_TYPES` run at the same time, each in
    its own task; the rest run one after another, afterwards. The common
    fan-out — summarise three documents with three `llm` steps — used to take
    the sum of its branches because agents share resources that are not
    concurrency-safe, and that constraint was applied to every branch whether
    or not it touched one. `SYNAPSE_PARALLEL_BRANCHES=0` restores sequential.

    The executor is an async generator and must stay single-task (the engine
    holds a cancel scope across its yields — see core/orchestration/runner.py).
    So concurrent branches do not yield; they push events onto a queue that
    this generator drains. Each branch task has its own per-step timeout
    scopes, which is fine: a scope is bound to the task that entered it.
    """

    async def execute(
        self, step: StepConfig, run: OrchestrationRun, engine: "OrchestrationEngine"
    ) -> AsyncGenerator[dict, None]:
        branches = step.parallel_branches
        _log.debug(f"PARALLEL: ▶ step='{step.id}' branches={len(branches)} convergence='{step.next_step_id}'")
        if not branches:
            yield {"type": "step_warning", "orch_step_id": step.id, "message": "No branches defined"}
            return

        convergence_id = step.next_step_id  # e.g. the Merge node

        def resolve_branch_chain(branch: list[str]) -> list[str]:
            """If the branch only lists an entry point, walk next_step_id to build the full chain."""
            if len(branch) != 1:
                return branch  # explicit chain — use as-is
            chain = [branch[0]]
            visited = {branch[0]}
            current = branch[0]
            while True:
                sub = engine.step_map.get(current)
                if not sub or not sub.next_step_id:
                    break
                nxt = sub.next_step_id
                if nxt == convergence_id or nxt in visited:
                    break
                chain.append(nxt)
                visited.add(nxt)
                current = nxt
            return chain

        resolved_branches = [resolve_branch_chain(b) for b in branches]

        yield {"type": "parallel_start", "orch_step_id": step.id, "branch_count": len(resolved_branches)}

        # ── Mid-node crash recovery: skip branches/sub-steps that completed ──
        # Their outputs are already in shared_state from the checkpoint; only
        # the interrupted (or never-started) work reruns.
        from core.orchestration.state import SharedState
        progress_key = _progress_key(step)
        saved = run.shared_state.get(progress_key) or {}
        completed_branches: set[int] = set(saved.get("completed_branches") or [])
        completed_steps: set[str] = set(saved.get("completed_steps") or [])
        # A step in a branch stopped to ask (F109). Answered, it is done — its
        # answer is in its key — and its branch carries on after it, then the
        # branches after that one. Not answered, it runs again.
        paused = saved.get("paused")
        if isinstance(paused, dict) and paused.get("answered") and paused.get("step_id"):
            completed_steps.add(paused["step_id"])
        if completed_branches or completed_steps:
            _log.debug(f"PARALLEL: ⏯ resuming — branches done={sorted(completed_branches)} "
                  f"sub-steps done={sorted(completed_steps)}")

        async def _checkpoint_progress():
            run.shared_state[progress_key] = {
                "completed_branches": sorted(completed_branches),
                "completed_steps": sorted(completed_steps),
            }
            try:
                # Serialised per run by SharedState.checkpoint's lock, so two
                # branches finishing together cannot land out of order.
                await SharedState(run).checkpoint()
            except Exception as ckpt_err:
                _log.warning(f"parallel-progress checkpoint failed: {ckpt_err}")

        def _pause_progress(branch_index: int, sid: str) -> None:
            """Keep the node's place at a pause. The engine checkpoints it."""
            run.shared_state[progress_key] = {
                "completed_branches": sorted(completed_branches),
                "completed_steps": sorted(completed_steps),
                "paused": {"branch": branch_index, "step_id": sid, "answered": False},
            }

        def _is_concurrent_safe(chain: list[str]) -> bool:
            for sid in chain:
                sub = engine.step_map.get(sid)
                if sub is None or sub.type not in CONCURRENT_SAFE_TYPES:
                    return False
            return True

        from core import config as _config
        pending = [(i, chain) for i, chain in enumerate(resolved_branches) if i not in completed_branches]
        for i, _ in [(i, c) for i, c in enumerate(resolved_branches) if i in completed_branches]:
            _log.debug(f"PARALLEL: ├─ branch {i} already complete — skipping")
        if _config.PARALLEL_BRANCHES_CONCURRENT:
            concurrent = [(i, c) for i, c in pending if _is_concurrent_safe(c)]
            sequential = [(i, c) for i, c in pending if not _is_concurrent_safe(c)]
        else:
            concurrent, sequential = [], pending

        branch_kwargs = dict(
            step=step, run=run, engine=engine, branch_count=len(resolved_branches),
            completed_steps=completed_steps, completed_branches=completed_branches,
            checkpoint_progress=_checkpoint_progress, pause_progress=_pause_progress,
        )

        # ── Concurrent branches: one task each, events through a queue ──
        # A branch that raises fails the node — and so the run — once its
        # siblings have finished (F88, D112). It used to be that branch's own
        # problem: reported, and the run moved on with the merge working from
        # what arrived, while the same step in a sequential branch failed the
        # run. The siblings are let finish rather than cancelled, so what they
        # did is kept for a Replay, which reruns only the branch that failed.
        failures: list[str] = []
        #: An End reached inside a branch. Held until every branch has run —
        #: F125: branch 0 ending the run used to leave branch 1 unrun, and on
        #: the report's own orchestration that was the Entity Extractor, whose
        #: output stayed `[]`.
        end_event: dict | None = None
        if concurrent:
            _log.debug(f"PARALLEL: ├─ running {len(concurrent)} tool-free branch(es) concurrently")
            queue: asyncio.Queue = asyncio.Queue()

            async def _pump(branch_index: int, chain: list[str]):
                try:
                    async for ev in self._iter_branch(branch_index, chain, **branch_kwargs):
                        await queue.put(ev)
                except Exception as e:  # noqa: BLE001 — recorded, raised once the others finish
                    message = f"Branch {branch_index} failed: {type(e).__name__}: {str(e)[:300]}"
                    failures.append(message)
                    await queue.put({"type": "step_error", "orch_step_id": step.id, "error": message})
                finally:
                    await queue.put(_BRANCH_DONE)

            tasks = [asyncio.create_task(_pump(i, c)) for i, c in concurrent]
            remaining = len(tasks)
            try:
                while remaining:
                    ev = await queue.get()
                    if ev is _BRANCH_DONE:
                        remaining -= 1
                        continue
                    if ev.get("type") == CONTAINER_END:
                        end_event = ev["event"]
                        continue
                    yield ev
            finally:
                # The engine's timeout or a cancel closes this generator; the
                # branch tasks must not outlive it.
                for t in tasks:
                    if not t.done():
                        t.cancel()

        if failures:
            # Before any sequential branch starts: an agent or a tool has no
            # business running for a node that has already failed.
            raise RuntimeError(failures[0] if len(failures) == 1
                               else f"{len(failures)} branches failed; first: {failures[0]}")

        # ── Sequential branches: agents and tools, one at a time as before ──
        for branch_index, chain in sequential:
            # Checkpoint between branches — give MCP background tasks time
            await anyio.sleep(0)
            async for ev in self._iter_branch(branch_index, chain, **branch_kwargs):
                if ev.get("type") == "human_input_required":
                    # The branch kept the node's place (`pause_progress`), so
                    # answering re-enters here and carries on (F109).
                    yield ev
                    return
                if ev.get("type") == CONTAINER_END:
                    end_event = ev["event"]
                    continue
                yield ev

        run.shared_state.pop(progress_key, None)
        _log.debug(f"PARALLEL: ✅ all {len(resolved_branches)} branches complete")
        yield {"type": "parallel_complete", "orch_step_id": step.id, "branch_count": len(resolved_branches)}

        # Last, and outside every scope this node held: the engine breaks its
        # own loop on this event, so nothing here may still be in flight.
        if end_event is not None:
            yield end_event

    async def _iter_branch(
        self, branch_index: int, branch_step_ids: list[str], *, step, run, engine,
        branch_count: int, completed_steps: set, completed_branches: set, checkpoint_progress,
        pause_progress,
    ) -> AsyncGenerator[dict, None]:
        """One branch, start to finish. Yields its events; a
        `human_input_required` is yielded last and ends the branch."""
        _log.debug(f"PARALLEL: ├─ branch {branch_index}/{branch_count} steps={branch_step_ids}")
        yield {"type": "branch_start", "orch_step_id": step.id,
               "branch_index": branch_index, "branch_count": branch_count}

        branch_end_event = None
        for sid in branch_step_ids:
            if sid in completed_steps:
                _log.debug(f"PARALLEL: ↷ sub-step '{sid}' already complete — skipping")
                continue
            sub_step = engine.step_map.get(sid)
            if not sub_step:
                # A step that is not there fails the run, as it does at the top
                # level; it used to be warned about and the branch went on
                # without it (F113). Named on the node, which is on the canvas.
                message = f"Step '{sid}' in branch {branch_index} not found"
                yield {"type": "step_error", "orch_step_id": step.id, "error": message}
                raise RuntimeError(message)
            executor = engine.executors.get(sub_step.type)
            if not executor:
                # A step this engine cannot run fails the run, as it does at the
                # top level (F90); it used to be skipped and the branch went on.
                message = f"No executor for {sub_step.type}"
                yield {"type": "step_error", "orch_step_id": sid, "error": message}
                raise RuntimeError(message)

            step_start = time.time()
            sub_timeout = sub_step.timeout_seconds or ORCH_STEP_TIMEOUT
            _log.debug(f"PARALLEL: ▶ sub-step '{sub_step.name}' ({sub_step.id}) timeout={sub_timeout}s")
            yield {"type": "step_start", "orch_step_id": sub_step.id,
                   "step_name": sub_step.name, "step_type": sub_step.type.value}

            # Human events must be yielded OUTSIDE the fail_after scope —
            # yielding inside suspends this generator with the scope
            # active and breaks the engine's own scope exit (issue #356
            # family). Same pattern as the loop executor.
            #
            # `orchestration_end` is the same trap and was not covered (F128).
            # The engine breaks its own `async for` on that event, which left
            # this generator suspended at the yield below *inside* its
            # fail_after — so the engine's scope exit raised, the run failed
            # with "An internal error occurred", and the GC later closed this
            # generator from another task. Captured and broken out of here, for
            # the same reason and in the same shape.
            pending_human_event = None
            pending_end_event = None
            failed = False
            sub_gen = executor.execute(sub_step, run, engine)
            try:
                with anyio.fail_after(sub_timeout):
                    async for event in sub_gen:
                        if event.get("type") == "human_input_required":
                            pending_human_event = event
                            break
                        if event.get("type") == "orchestration_end":
                            pending_end_event = event
                            break
                        if event.get("type") == "step_error":
                            failed = True
                        yield event
            except TimeoutError:
                with anyio.CancelScope(shield=True):
                    try:
                        await sub_gen.aclose()
                    except Exception as close_err:  # noqa: BLE001
                        _log.warning(f"closing timed-out step '{sub_step.name}' failed: {close_err}")
                duration = round(time.time() - step_start, 2)
                _log.debug(f"PARALLEL: ⏱ Step '{sub_step.name}' timed out after {sub_timeout}s in branch {branch_index}")
                end_step(run.step_history, {
                    "step_id": sub_step.id, "step_name": sub_step.name,
                    "step_type": sub_step.type.value, "status": "failed",
                    "error": f"Timed out after {sub_timeout}s",
                    "started_at": _iso(step_start), "ended_at": _iso(time.time()),
                    "duration_seconds": duration,
                })
                yield {"type": "step_error", "orch_step_id": sub_step.id,
                       "error": f"Step '{sub_step.name}' timed out after {sub_timeout}s"}
                # Fails the branch and so the node, as a raise does (F88) and as
                # a timeout does at the top level (F253); it used to be skipped
                # and the branch went on without it. A RuntimeError, so the
                # engine does not read it as the node's own timeout.
                raise RuntimeError(f"Step '{sub_step.name}' timed out after {sub_timeout}s")

            if pending_human_event is not None:
                pause_progress(branch_index, sid)
                yield pending_human_event
                return

            if pending_end_event is not None:
                # The End executor set the run completed before it yielded.
                # Put that back: the run ends when this node has finished, not
                # part-way through it, and every step between here and there
                # must run normally. The node's own generator ends the run.
                run.status = "running"
                branch_end_event = pending_end_event

            duration = round(time.time() - step_start, 2)
            _log.debug(f"PARALLEL: ✓ sub-step '{sub_step.name}' done in {duration}s")
            # A step that reported an error is recorded as failed, as a loop body
            # records it (F88); it was recorded `completed`.
            end_step(run.step_history, {
                "step_id": sub_step.id, "step_name": sub_step.name,
                "step_type": sub_step.type.value, "status": "failed" if failed else "completed",
                "started_at": _iso(step_start), "ended_at": _iso(time.time()),
                "duration_seconds": duration, "output_key": sub_step.output_key,
            })
            yield {"type": "step_complete", "orch_step_id": sub_step.id,
                   "step_name": sub_step.name, "duration_seconds": duration}
            # Sub-step done (its output is in shared_state) — checkpoint so
            # a crash resumes at the next sub-step, not the whole branch.
            completed_steps.add(sid)
            await checkpoint_progress()
            if branch_end_event is not None:
                break  # an End is the last step of its chain

        completed_branches.add(branch_index)
        await checkpoint_progress()
        if branch_end_event is not None:
            yield {"type": CONTAINER_END, "orch_step_id": step.id, "event": branch_end_event}


class MergeStepExecutor:
    """Combine outputs from parallel branches into a single result."""

    async def execute(
        self, step: StepConfig, run: OrchestrationRun, engine: "OrchestrationEngine"
    ) -> AsyncGenerator[dict, None]:
        # Use an ordered list of (display_label, value) pairs so that two steps
        # sharing the same agent_id (and thus the same agent name) never
        # overwrite each other.  The unique key is always the step ID; the
        # display label is "StepName (step_id)" so it stays human-readable.
        entries: list[tuple[str, object]] = []
        for key in step.input_keys:
            if key not in run.shared_state:
                continue

            # Locate the step that produced this output_key
            producer = next(
                (s for s in engine.orch.steps if s.output_key == key), None
            )

            # Build a human-readable label anchored to the *step*, not the agent.
            # Pattern: "StepName (step_id)"  e.g. "NSE Stock Alpha Data (step_ixjrdrk)"
            if producer:
                step_label = f"{producer.name} ({producer.id})"
            else:
                step_label = key

            entries.append((step_label, run.shared_state[key]))

        if step.merge_strategy == "concat":
            merged = "\n\n".join(f"[{label}]:\n{value}" for label, value in entries)
        elif step.merge_strategy == "dict":
            # Use step_label as key — guaranteed unique because step IDs are unique
            merged = {label: value for label, value in entries}
        else:  # "list" (default)
            merged = [{"source": label, "data": value} for label, value in entries]

        if step.output_key:
            run.shared_state[step.output_key] = merged

        yield {
            "type": "merge_complete", "orch_step_id": step.id,
            "input_count": len(entries), "strategy": step.merge_strategy,
        }


class LoopStepExecutor:
    """Run a body of steps repeatedly, accumulating each iteration's results.

    Three modes, resolved in this order so an existing fixed-count loop keeps
    behaving exactly as it did:

      for-each  ``loop_over`` — an expression yielding a list. Each item is
                written to ``loop_item_key`` before the body runs.
      while     ``loop_while_condition`` — re-evaluated before every iteration.
      count     ``loop_count`` — the original behaviour.

    Every mode writes the iteration's 0-based position to ``loop_index_key``
    before the body runs, so a body — a question to a person above all — can
    say which iteration it is (F212). Only for-each has an item.

    ``loop_break_condition`` stops any of the three after the iteration in
    which it becomes true, and ``loop_max_iterations`` caps all of them.

    Only body steps that actually *completed* contribute to the accumulators.
    A step that reported an error contributes nothing rather than having
    whatever its output key happened to already hold recorded as this
    iteration's result. One that raised or timed out fails the loop, and the
    run (F253).
    """

    async def execute(
        self, step: StepConfig, run: OrchestrationRun, engine: "OrchestrationEngine"
    ) -> AsyncGenerator[dict, None]:
        body_ids = step.loop_step_ids
        if not body_ids:
            yield {"type": "step_warning", "orch_step_id": step.id, "message": "No loop body steps defined"}
            return

        from core.orchestration.state import SharedState
        from core.orchestration.context import TransitionContext
        from .engine import MAX_NESTED_DEPTH

        depth = getattr(engine, "_loop_depth", 0)
        if depth >= MAX_NESTED_DEPTH:
            # Fails the run (F90): it used to be reported and skipped, and the
            # run carried on as if the loop's body had run.
            message = (f"Loop nesting exceeded {MAX_NESTED_DEPTH} levels at "
                       f"'{step.name}'. A loop whose body reaches itself never ends.")
            yield {"type": "step_error", "orch_step_id": step.id, "error": message}
            raise RuntimeError(message)

        progress_key = _progress_key(step)
        cap = max(1, step.loop_max_iterations)

        # Saved before the first iteration and restored on every exit, so a
        # nested loop unwinds without clobbering its parent's markers.
        prev_transition = getattr(engine, "current_transition", None)
        _ABSENT = object()
        prev_iter = run.shared_state.get("_loop_current_iteration", _ABSENT)
        prev_total = run.shared_state.get("_loop_total", _ABSENT)

        def _restore():
            engine._loop_depth = depth
            engine.current_transition = prev_transition
            for key, was in (("_loop_current_iteration", prev_iter), ("_loop_total", prev_total)):
                if was is _ABSENT:
                    run.shared_state.pop(key, None)
                else:
                    run.shared_state[key] = was

        engine._loop_depth = depth + 1
        try:
            # ── Mid-node crash recovery: skip iterations that already completed ──
            # Each finished iteration checkpoints its count below; the accumulated
            # _loop_* results ride along in shared_state, so a resumed run picks up
            # exactly where the crash cut it off.
            saved = run.shared_state.get(progress_key)
            saved = saved if isinstance(saved, dict) else {}
            completed_iterations = max(0, int(saved.get("completed_iterations") or 0))
            # A step in the body stopped to ask, part-way through an iteration
            # (F109). The loop re-enters that iteration where it stopped:
            # answered, it carries on after the step that asked; not answered —
            # a tool call's approval, a replay after a cancel — it runs that step
            # again.
            pending_resume = saved.get("paused")
            if not (isinstance(pending_resume, dict)
                    and pending_resume.get("iteration") == completed_iterations + 1):
                pending_resume = None

            # ── Mode resolution ──
            items: list | None = None
            if step.loop_over:
                mode = "foreach"
                # The resolved collection is snapshotted into the progress record
                # so a resumed run iterates the same items even if a body step
                # has since rewritten the source key.
                items = saved.get("items")
                if items is None:
                    resolved, err = _eval_state_expr(step.loop_over, run.shared_state)
                    if err:
                        yield {"type": "step_warning", "orch_step_id": step.id,
                               "message": f"loop_over '{step.loop_over}' failed to evaluate: {err}"}
                        resolved = []
                    if isinstance(resolved, (list, tuple)):
                        items = list(resolved)
                        # An empty source is a legitimate answer, so the run
                        # carries on — but zero iterations used to be silent,
                        # and a run whose collection came back empty read
                        # exactly like one that did its work (F51). Said once,
                        # here, so a resumed run with an empty snapshot does
                        # not repeat it.
                        if not items:
                            yield {"type": "step_warning", "orch_step_id": step.id,
                                   "message": f"loop_over '{step.loop_over}' is an "
                                              f"empty list — zero iterations"}
                    else:
                        yield {"type": "step_warning", "orch_step_id": step.id,
                               "message": f"loop_over '{step.loop_over}' is "
                                          f"{type(resolved).__name__}, not a list — zero iterations"}
                        items = []
                total = min(len(items), cap)
            elif step.loop_while_condition:
                mode = "while"
                total = None
            else:
                mode = "count"
                total = min(max(1, step.loop_count), cap)

            _log.debug(f"LOOP: ▶ step='{step.id}' mode={mode} body_ids={body_ids} total={total}")
            if completed_iterations:
                _log.debug(f"LOOP: ⏯ resuming from iteration {completed_iterations + 1}")

            iteration = completed_iterations
            iterations_run = completed_iterations
            # A for-each that had nothing to iterate is told apart from one that
            # ran out of items, so the event says which of the two happened.
            exhausted = ("count" if mode == "count"
                         else "empty_collection" if mode == "foreach" and not items
                         else "collection_exhausted")
            stopped_reason = exhausted
            #: An End reached in the body. Held until this iteration has
            #: finished, so nothing inside it is abandoned half-done (F128) —
            #: then the loop stops and the engine ends the run.
            loop_end_event: dict | None = None

            while True:
                # An iteration being re-entered was admitted before its pause:
                # its while condition is not asked again half-way through it.
                resume_at, pending_resume = pending_resume, None
                if resume_at is None:
                    if total is not None and iteration >= total:
                        stopped_reason = exhausted
                        break
                    if iteration >= cap:
                        stopped_reason = "max_iterations"
                        yield {"type": "step_warning", "orch_step_id": step.id,
                               "message": f"Loop hit its {cap}-iteration ceiling"}
                        break
                    if mode == "while":
                        keep_going, err = _eval_state_expr(
                            step.loop_while_condition, run.shared_state, default=False)
                        if err:
                            yield {"type": "step_warning", "orch_step_id": step.id,
                                   "message": f"loop_while_condition failed to evaluate "
                                              f"(stopping): {err}"}
                            stopped_reason = "condition_error"
                            break
                        if not keep_going:
                            stopped_reason = "condition_false"
                            break

                iteration += 1

                if mode == "foreach":
                    run.shared_state[step.loop_item_key] = items[iteration - 1]
                run.shared_state[step.loop_index_key] = iteration - 1
                # The keys context.build_prompt reads to tell a body agent where
                # it is. Popped again by _restore() so they never reach final state.
                run.shared_state["_loop_current_iteration"] = iteration
                run.shared_state["_loop_total"] = total

                yield {
                    "type": "loop_iteration", "orch_step_id": step.id,
                    "iteration": iteration, "total": total,
                    **({"resumed": True} if resume_at else {}),
                }

                completed_this_iteration: set[str] = (
                    set(resume_at.get("completed") or []) if resume_at else set())
                start = _resume_index(resume_at, body_ids) if resume_at else 0

                for idx, sid in enumerate(body_ids):
                    if idx < start:
                        continue  # ran before the pause; its output is in state
                    if resume_at and idx == start and resume_at.get("answered"):
                        # The step that asked, answered: the answer is in its
                        # output key, and its history entry was closed by the
                        # resume. It counts as this iteration's result.
                        completed_this_iteration.add(sid)
                        continue
                    sub_step = engine.step_map.get(sid)
                    if not sub_step:
                        # Fails the run, as a missing step does at the top level;
                        # skipped, the loop could complete having run none of its
                        # body (F113).
                        message = f"Loop body step '{sid}' not found"
                        yield {"type": "step_error", "orch_step_id": step.id, "error": message}
                        raise RuntimeError(message)

                    executor = engine.executors.get(sub_step.type)
                    if not executor:
                        # Fails the run, as at the top level (F90).
                        message = f"No executor for {sub_step.type}"
                        yield {"type": "step_error", "orch_step_id": sid, "error": message}
                        raise RuntimeError(message)

                    # The engine only builds a TransitionContext for the steps it
                    # drives itself, so without this a body step would read the
                    # LOOP node's context and believe it was invoked linearly, once.
                    engine.current_transition = TransitionContext(
                        origin_type="loop",
                        execution_number=sum(
                            1 for h in run.step_history if h["step_id"] == sid) + 1,
                        from_step_id=step.id,
                        from_step_name=step.name,
                        loop_iteration=iteration,
                        loop_total=total,
                    )

                    step_start = time.time()
                    sub_timeout = sub_step.timeout_seconds or ORCH_STEP_TIMEOUT
                    yield {"type": "step_start", "orch_step_id": sub_step.id,
                           "step_name": sub_step.name, "step_type": sub_step.type.value}

                    # Human input within a loop body pauses the whole run, and
                    # answering it re-enters this iteration (F109). The
                    # event must be yielded OUTSIDE the fail_after scope: yielding
                    # inside it suspends this generator with the scope active, and
                    # the engine exiting its own step-timeout scope then blows up
                    # with "exit cancel scope that isn't the current" (issue #356
                    # family). Same pattern as the engine's own human-pause path.
                    #
                    # `orchestration_end` is the same trap, and was not covered
                    # (F128): the engine breaks its own loop on it, leaving this
                    # generator suspended inside the fail_after below, so the
                    # engine's scope exit raised and the run failed with "An
                    # internal error occurred".
                    pending_human_event = None
                    pending_end_event = None
                    failed = False
                    # Held so it can be closed explicitly below. A body step that
                    # times out leaves its executor suspended mid-await, and an
                    # abandoned async generator is only finalised whenever the GC
                    # gets to it — which for a transform means its subprocess
                    # transport is reaped on some later, unrelated event loop.
                    sub_gen = executor.execute(sub_step, run, engine)
                    try:
                        with anyio.fail_after(sub_timeout):
                            async for event in sub_gen:
                                if event.get("type") == "human_input_required":
                                    pending_human_event = event
                                    break
                                if event.get("type") == "orchestration_end":
                                    pending_end_event = event
                                    break
                                if event.get("type") == "step_error":
                                    failed = True
                                yield event
                    except TimeoutError:
                        with anyio.CancelScope(shield=True):
                            try:
                                await sub_gen.aclose()
                            except Exception as close_err:
                                _log.warning(f"closing timed-out step "
                                      f"'{sub_step.name}' failed: {close_err}")
                        _log.debug(f"LOOP: ⏱ Step '{sub_step.name}' timed out after {sub_timeout}s (iteration {iteration})")
                        end_step(run.step_history, {
                            "step_id": sub_step.id, "step_name": sub_step.name,
                            "step_type": sub_step.type.value, "status": "failed",
                            "error": f"Timed out after {sub_timeout}s",
                            "started_at": _iso(step_start), "ended_at": _iso(time.time()),
                            "duration_seconds": round(time.time() - step_start, 2),
                        })
                        yield {"type": "step_error", "orch_step_id": sub_step.id,
                               "error": f"Step '{sub_step.name}' timed out after {sub_timeout}s"}
                        # Fails the loop and the run, as a timeout does at the
                        # top level (F253). It used to be skipped: the next body
                        # step ran on whatever state it found, and the loop went
                        # on to the next iteration. Raised as a RuntimeError, not
                        # a TimeoutError, which the engine would read as the loop
                        # node itself running out of time. The iterations before
                        # this one are checkpointed, so a Replay re-enters here.
                        raise RuntimeError(f"Step '{sub_step.name}' timed out after {sub_timeout}s "
                                           f"(iteration {iteration})")

                    if pending_human_event is not None:
                        # Keep the loop's place, so answering carries it on from
                        # here — the rest of this iteration, then the next — and
                        # not past the loop with the rest dropped (F109). The
                        # markers go back first: the engine checkpoints the
                        # pause, and they belong to this generator, not the run.
                        paused_at = {
                            "completed_iterations": iteration - 1,
                            "paused": {
                                "iteration": iteration, "index": idx, "step_id": sid,
                                "completed": [b for b in body_ids if b in completed_this_iteration],
                                "answered": False,
                            },
                        }
                        if mode == "foreach":
                            paused_at["items"] = items
                        run.shared_state[progress_key] = paused_at
                        _restore()
                        yield pending_human_event
                        return

                    if pending_end_event is not None:
                        # The End executor set the run completed before it
                        # yielded. Put that back so the rest of this iteration
                        # runs normally; this generator ends the run when it is
                        # done. Later iterations do not run — an End is the
                        # author saying stop repeating, and unlike a Parallel's
                        # siblings they are repetitions, not work already
                        # declared alongside it.
                        run.status = "running"
                        loop_end_event = pending_end_event

                    duration = round(time.time() - step_start, 2)
                    end_step(run.step_history, {
                        "step_id": sub_step.id, "step_name": sub_step.name,
                        "step_type": sub_step.type.value,
                        "status": "failed" if failed else "completed",
                        "started_at": _iso(step_start), "ended_at": _iso(time.time()),
                        "duration_seconds": duration, "output_key": sub_step.output_key,
                    })
                    if not failed:
                        completed_this_iteration.add(sid)
                    yield {"type": "step_complete", "orch_step_id": sub_step.id,
                           "step_name": sub_step.name, "duration_seconds": duration}

                # After each iteration, accumulate results — but only from body
                # steps that actually completed. A step that errored leaves its
                # previous value sitting in shared_state, and recording that as
                # this iteration's result invents a success that never happened.
                for sid in body_ids:
                    sub_step = engine.step_map.get(sid)
                    if not sub_step or not sub_step.output_key:
                        continue
                    if sid not in completed_this_iteration:
                        continue
                    if sub_step.output_key not in run.shared_state:
                        continue

                    acc_key = f"_loop_{sub_step.output_key}"
                    agent_name = engine.agent_names.get(sub_step.agent_id, sub_step.name) if sub_step.agent_id else sub_step.name
                    if acc_key not in run.shared_state:
                        run.shared_state[acc_key] = []
                    run.shared_state[acc_key].append({
                        "iteration": iteration,
                        "agent": agent_name,
                        "result": run.shared_state[sub_step.output_key],
                    })

                iterations_run = iteration

                # Iteration finished — checkpoint so a crash resumes at the next one.
                progress = {"completed_iterations": iteration}
                if mode == "foreach":
                    progress["items"] = items
                run.shared_state[progress_key] = progress
                try:
                    await SharedState(run).checkpoint()
                except Exception as ckpt_err:
                    _log.warning(f"loop-progress checkpoint failed: {ckpt_err}")

                if step.loop_break_condition:
                    should_break, err = _eval_state_expr(
                        step.loop_break_condition, run.shared_state, default=False)
                    if err:
                        yield {"type": "step_warning", "orch_step_id": step.id,
                               "message": f"loop_break_condition failed to evaluate "
                                          f"(ignored): {err}"}
                    elif should_break:
                        stopped_reason = "break"
                        break

                if loop_end_event is not None:
                    stopped_reason = "end"
                    break

            run.shared_state.pop(progress_key, None)

            # After all iterations, promote accumulated results to output keys
            promoted: dict[str, str] = {}  # step name -> output key it produced
            for sid in body_ids:
                sub_step = engine.step_map.get(sid)
                if not sub_step or not sub_step.output_key:
                    continue
                acc_key = f"_loop_{sub_step.output_key}"
                if acc_key in run.shared_state:
                    run.shared_state[sub_step.output_key] = run.shared_state.pop(acc_key)
                    promoted[sub_step.name] = sub_step.output_key

            # Store loop's own output if configured — only body steps that
            # actually produced something across the run. A step that failed
            # every iteration has whatever its key held beforehand still sitting
            # in state, and that value is not this loop's output.
            if step.output_key:
                run.shared_state[step.output_key] = {
                    name: run.shared_state[key] for name, key in promoted.items()
                }

            yield {"type": "loop_complete", "orch_step_id": step.id,
                   "iterations_completed": iterations_run,
                   "stopped_reason": stopped_reason}

            # Last, and outside every scope this node held: the engine breaks
            # its own loop on this event.
            if loop_end_event is not None:
                yield loop_end_event
        finally:
            _restore()


def render_state_refs(text: str, state: dict) -> str:
    """`{state.key}` and `{state.key.nested.path}` in text, from shared state.

    A reference that resolves to nothing is left as written, so a typo shows
    rather than vanishing. A human step's prompt and a Print's content read
    through this one function; the prompt used to read only top-level keys.
    """
    def _resolve(match: re.Match) -> str:
        val = state
        for part in match.group(1).split("."):
            val = val.get(part) if isinstance(val, dict) else None
            if val is None:
                break
        return str(val) if val is not None else match.group(0)

    return re.sub(r"\{state\.([\w.]+)\}", _resolve, text)


class HumanStepExecutor:
    """Pause execution and request human input.

    If the step has a human_channel_id configured, the engine posts the prompt
    to that messaging channel when the run pauses, and a reply there answers
    it (`core/orchestration/human_prompts.py`). Whichever answer arrives first
    resumes the run; a second is refused because the run is no longer waiting.
    """

    async def execute(
        self, step: StepConfig, run: OrchestrationRun, engine: "OrchestrationEngine"
    ) -> AsyncGenerator[dict, None]:
        prompt = render_state_refs(
            step.human_prompt or "Please provide input to continue.", run.shared_state)

        run.human_prompt = prompt
        run.human_fields = step.human_fields

        # Gather recent agent output from shared state for display context
        agent_context = None
        for key in (step.input_keys or []):
            if key in run.shared_state and run.shared_state[key]:
                agent_context = str(run.shared_state[key])
                break
        # Fallback: find the last output from step history
        if not agent_context:
            for h in reversed(run.step_history):
                okey = h.get("output_key")
                if okey and okey in run.shared_state:
                    agent_context = str(run.shared_state[okey])
                    break

        channel_id = step.human_channel_id

        yield {
            "type": "human_input_required",
            "orch_step_id": step.id,
            "prompt": prompt,
            "fields": step.human_fields,
            "run_id": run.run_id,
            "agent_context": agent_context,
            "channel_id": channel_id,  # frontend can show which channel was notified
        }


class TransformStepExecutor:
    """Run sandboxed Python code to transform shared state."""

    async def execute(
        self, step: StepConfig, run: OrchestrationRun, engine: "OrchestrationEngine"
    ) -> AsyncGenerator[dict, None]:
        code = step.transform_code
        if not code:
            yield {"type": "step_warning", "orch_step_id": step.id, "message": "No transform code provided"}
            return

        yield {"type": "_log_prompt", "orch_step_id": step.id, "prompt": f"[Transform Code]\n{code}"}

        try:
            result = await self._run(code, run.shared_state, timeout=step.timeout_seconds)

            if step.output_key and result is not None:
                run.shared_state[step.output_key] = result

            yield {
                "type": "transform_result",
                "orch_step_id": step.id,
                "result": str(result) if result is not None else None,
            }
        except Exception as e:
            # Raised so the engine fails the run, as it does for every other
            # step that cannot finish. It used to yield `step_error` and return,
            # and the next step ran on state this one never wrote. The step's
            # own line carries the real error, as a Tool step's failure does —
            # the engine's stream message is sanitised.
            message = f"Transform error: {e}"
            yield {"type": "final", "orch_step_id": step.id, "step_name": step.name, "response": message}
            raise RuntimeError(message) from e

    def _parse_script_output(self, stdout_text: str):
        """Parse the JSON output of the wrapped script. Falls back to plain text."""
        try:
            output = json.loads(stdout_text)
            return output.get("result")
        except json.JSONDecodeError:
            return stdout_text.strip() if stdout_text.strip() else None

    async def _run(self, code: str, state: dict, timeout: int = 30):
        """Run the transform through a code runner and return `result`.

        `transform_runtime` picks the runner: `"host"` means the escape hatch
        that runs the script as a subprocess of this process, anything else
        means whatever the deployment registered — the Docker sandbox in the
        shipped product. The setting keeps its old vocabulary (`"docker"` still
        selects the registered runner) so an existing configuration is not
        invalidated by the fact that the sandbox no longer has to be Docker.

        A deployment that must not offer the host runtime pins the setting;
        `core.execution.HostRunner`'s docstring says why it is a named runner
        rather than a branch hidden in here.
        """
        from core.config import load_settings
        from core.execution import ExecRequest, HostRunner, Network, get_runner, wrap_transform

        runtime = (load_settings().get("transform_runtime") or "docker").strip().lower()
        runner = HostRunner() if runtime == "host" else get_runner()

        # The sandbox is capped at a minute because it is the shared path; the
        # host runtime lifts that itself, because model loading is minutes and
        # a caller only reaches it deliberately.
        capped = int(timeout or 30) if runtime == "host" else min(int(timeout or 30), 60)

        from core.vault import _vault_root

        result = await runner.run(ExecRequest(
            code=wrap_transform(code, state),
            network=Network.INTERNET,
            timeout_s=capped,
            vault_path=_vault_root(),
            max_stdout=1_000_000,
        ))

        if result.timed_out:
            raise RuntimeError(f"Transform timed out after {capped}s")
        if result.exit_code != 0:
            label = "Transform (host)" if runtime == "host" else "Transform"
            raise RuntimeError(f"{label} failed (exit {result.exit_code}): {result.stderr[:500]}")

        return self._parse_script_output(result.stdout)


class LLMStepExecutor:
    """Single direct LLM call — no agent, no tools, no routing.

    Useful for inline summaries, rewrites, or lightweight reasoning
    between heavier agent steps.
    """

    async def execute(
        self, step: StepConfig, run: OrchestrationRun, engine: "OrchestrationEngine"
    ) -> AsyncGenerator[dict, None]:
        from core.llm_providers import generate_response as llm_generate, detect_mode_from_model
        from core.config import load_settings

        from .context import build_step_prompt, snapshot_inputs
        transition = getattr(engine, "current_transition", None)
        if transition is None:
            from .context import TransitionContext
            transition = TransitionContext(origin_type="entry", execution_number=1)
        prompt, system_prompt_extra, system_prompt_prefix, mentions = await build_step_prompt(
            step, run, engine, transition
        )
        inputs_snapshot = snapshot_inputs(step, run, engine)

        yield {"type": "_log_prompt", "orch_step_id": step.id, "prompt": prompt,
               "system_prompt_extra": system_prompt_extra,
               "system_prompt_prefix": system_prompt_prefix,
               "mentions": [m.summary() for m in mentions]}
        for warning in _mention_warnings(mentions):
            yield {"type": "step_warning", "orch_step_id": step.id, "message": warning}
        yield {
            "type": "thinking",
            "orch_step_id": step.id,
            "step_name": step.name,
            "message": f"LLM step '{step.name}' — calling model...",
        }

        settings = load_settings()
        # Treat None, empty string, or "default" as "use the global default model".
        _step_model = step.model if (step.model and step.model.strip().lower() not in ("", "default")) else None
        model = _step_model if _step_model else settings.get("model", "mistral")
        mode = detect_mode_from_model(model)

        # system_prompt_extra already contains datetime + workflow graph + position.
        # system_prompt_prefix is the iteration banner (re-runs only); prepend it
        # so it appears at the top of the system prompt.
        sys_prompt = f"You are a helpful assistant. Be concise and accurate.\n\n{system_prompt_extra}"
        if system_prompt_prefix:
            sys_prompt = system_prompt_prefix + "\n\n" + sys_prompt

        try:
            from core.model_wait import ModelWait

            _wait = ModelWait(llm_generate(
                prompt_msg=prompt,
                sys_prompt=sys_prompt,
                mode=mode,
                current_model=model,
                current_settings=settings,
                reasoning_effort=settings.get("reasoning_effort"),  # F77
                session_id=run.session_id,
                agent_id=step.agent_id or "llm_step",
                source="orchestration",
                run_id=run.run_id,
                cache_response=step.cache_responses_enabled,
                cache_response_semantic=step.cache_semantic_enabled,
                cache_response_ttl=step.cache_response_ttl_seconds,
                cache_response_step_id=step.id,
                cache_response_threshold=step.cache_response_threshold,
            ), label=f"LLM step '{step.name}'", extra={"orch_step_id": step.id, "step_name": step.name})
            async for _wait_event in _wait.events():
                yield _wait_event
            response = _wait.result
        except Exception as e:
            from core.llm_providers import LLMError
            if isinstance(e, LLMError):
                raise
            raise RuntimeError(f"LLM step '{step.name}' failed: {e}") from e

        if step.output_key:
            run.shared_state[step.output_key] = response

        # Record execution memory so re-invocations can see prior turn's output.
        from .context import build_execution_trace, store_execution_memory
        trace = build_execution_trace([{"type": "final", "response": response}])
        store_execution_memory(
            run, step, trace,
            agent_name="llm_step",
            transition=transition,
            inputs_snapshot=inputs_snapshot,
        )

        yield {
            "type": "final",
            "orch_step_id": step.id,
            "step_name": step.name,
            "response": response,
        }

class ExtractJsonStepExecutor:
    """Extract JSON from text input (handles markdown fences, raw JSON, multiple objects).
    No LLM call — pure Python extraction."""

    async def execute(
        self, step: StepConfig, run: OrchestrationRun, engine: "OrchestrationEngine"
    ) -> AsyncGenerator[dict, None]:
        # Gather input text from input_keys
        parts = []
        for key in (step.input_keys or []):
            val = run.shared_state.get(key)
            if val is not None:
                parts.append(str(val))

        text = "\n\n".join(parts) if parts else ""
        if not text.strip():
            yield {"type": "step_warning", "orch_step_id": step.id,
                   "message": "No input text to extract JSON from"}
            if step.output_key:
                run.shared_state[step.output_key] = None
            return

        yield {"type": "_log_prompt", "orch_step_id": step.id,
               "prompt": f"[Extract JSON] input text ({len(text)} chars)"}

        extracted = extract_all_json(text)

        if not extracted:
            result = None
            yield {"type": "step_warning", "orch_step_id": step.id,
                   "message": "No valid JSON found in input"}
        elif len(extracted) == 1:
            result = extracted[0]
        else:
            result = extracted

        if step.output_key:
            run.shared_state[step.output_key] = result

        yield {
            "type": "final",
            "orch_step_id": step.id,
            "step_name": step.name,
            "response": json.dumps(result, default=str) if result is not None else "null",
        }


def extract_all_json(text: str) -> list:
    """Try multiple strategies to extract JSON from text.

    Module level rather than a method on `ExtractJsonStepExecutor` because
    the engine needs the same three strategies to type a step's raw output
    against `state_schema` (F14), and there should be one implementation of
    "find the JSON in this text" rather than two that drift.
    """
    # Strategy 1: Try parsing the entire text as JSON
    try:
        parsed = json.loads(text.strip())
        return [parsed]
    except (json.JSONDecodeError, ValueError):
        pass

    results = []

    # Strategy 2: Extract from markdown fenced code blocks (```json ... ``` or ``` ... ```)
    fence_pattern = re.compile(r'```(?:json)?\s*\n(.*?)\n\s*```', re.DOTALL)
    for match in fence_pattern.finditer(text):
        content = match.group(1).strip()
        try:
            parsed = json.loads(content)
            results.append(parsed)
        except (json.JSONDecodeError, ValueError):
            pass

    if results:
        return results

    # Strategy 3: Brace/bracket counting to find top-level JSON objects/arrays
    results = extract_by_brace_matching(text)
    return results

def extract_by_brace_matching(text: str) -> list:
    """Find top-level {...} and [...] blocks by counting braces."""
    results = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] in ('{', '['):
            open_char = text[i]
            close_char = '}' if open_char == '{' else ']'
            depth = 0
            start = i
            in_string = False
            escape_next = False
            while i < n:
                ch = text[i]
                if escape_next:
                    escape_next = False
                    i += 1
                    continue
                if ch == '\\':
                    escape_next = True
                    i += 1
                    continue
                if ch == '"':
                    in_string = not in_string
                elif not in_string:
                    if ch == open_char:
                        depth += 1
                    elif ch == close_char:
                        depth -= 1
                        if depth == 0:
                            candidate = text[start:i + 1]
                            try:
                                parsed = json.loads(candidate)
                                results.append(parsed)
                            except (json.JSONDecodeError, ValueError):
                                pass
                            i += 1
                            break
                i += 1
            else:
                # Unmatched brace, move on
                i = start + 1
        else:
            i += 1
    return results


def _eval_state_expr(expr: str, state: dict, default=None):
    """Evaluate `expr` against shared state in the restricted dialect.

    Returns ``(value, error_message)``; on failure the value is `default` and
    the caller decides whether that is a warning or a hard stop. IF/Else,
    Switch and Loop all speak this one dialect so an author learns
    `state.foo.bar` once.

    The dialect, and the boundary that makes it one, live in `core.expressions`.
    This was a bare `eval()` with an empty `__builtins__` until 2026-09-12, which
    stopped nothing: the builder model writes `if_condition` directly, so the
    path ran from a chat message through prompt injection to arbitrary code in
    this process (S3).

    A refused expression says so, and then behaves exactly like one that raised
    — every call site below already has a documented answer for that, and a
    self-hosted workflow should not stop running on the day this ships.
    """
    result = _expressions.evaluate_expression(expr, state, default)
    if result.refused:
        return result.value, f"unsafe expression — {result.error}"
    return result.value, result.error


class IfElseStepExecutor:
    """Evaluate a Python condition against shared state and route to true/false path.
    No LLM call — pure deterministic branching."""

    async def execute(
        self, step: StepConfig, run: OrchestrationRun, engine: "OrchestrationEngine"
    ) -> AsyncGenerator[dict, None]:
        condition = step.if_condition
        if not condition:
            yield {"type": "step_warning", "orch_step_id": step.id,
                   "message": "No condition configured for IF/Else step"}
            run.shared_state[f"_if_decision_{step.id}"] = "false"
            return

        yield {"type": "_log_prompt", "orch_step_id": step.id,
               "prompt": f"[IF/Else Condition] {condition}"}

        raw, err = _eval_state_expr(condition, run.shared_state, default=False)
        result = bool(raw)
        if err:
            _log.debug(f"IF_ELSE: ⚠ Condition eval failed: {err}")
            yield {"type": "step_warning", "orch_step_id": step.id,
                   "message": f"Condition eval error (treating as False): {err}"}

        decision = "true" if result else "false"
        run.shared_state[f"_if_decision_{step.id}"] = decision
        _log.debug(f"IF_ELSE: condition='{condition}' → {decision}")

        if step.output_key:
            run.shared_state[step.output_key] = result

        yield {
            "type": "if_decision",
            "orch_step_id": step.id,
            "step_name": step.name,
            "condition": condition,
            "result": decision,
        }


class SwitchStepExecutor:
    """Evaluate a state expression and match against case values to route.
    No LLM call — pure deterministic routing."""

    async def execute(
        self, step: StepConfig, run: OrchestrationRun, engine: "OrchestrationEngine"
    ) -> AsyncGenerator[dict, None]:
        expression = step.switch_expression
        if not expression:
            yield {"type": "step_warning", "orch_step_id": step.id,
                   "message": "No expression configured for Switch step"}
            run.shared_state[f"_switch_decision_{step.id}"] = None
            return

        yield {"type": "_log_prompt", "orch_step_id": step.id,
               "prompt": f"[Switch Expression] {expression}"}

        value, err = _eval_state_expr(expression, run.shared_state, default=None)
        if err:
            _log.debug(f"SWITCH: ⚠ Expression eval failed: {err}")
            yield {"type": "step_warning", "orch_step_id": step.id,
                   "message": f"Expression eval error: {err}"}

        # Convert to string for case matching
        value_str = str(value) if value is not None else "None"
        matched_case = value_str if value_str in (step.switch_cases or {}) else None
        run.shared_state[f"_switch_decision_{step.id}"] = matched_case
        _log.debug(f"SWITCH: expression='{expression}' → '{value_str}', matched_case={matched_case}")

        if step.output_key:
            run.shared_state[step.output_key] = value

        yield {
            "type": "switch_decision",
            "orch_step_id": step.id,
            "step_name": step.name,
            "expression": expression,
            "value": value_str,
            "matched_case": matched_case,
        }



class PrintStepExecutor:
    """Store user-defined text/markdown into shared state.
    Supports {state.key} and {state.key.nested} interpolation.
    No LLM call — pure template rendering."""

    async def execute(
        self, step: StepConfig, run: OrchestrationRun, engine: "OrchestrationEngine"
    ) -> AsyncGenerator[dict, None]:
        content = step.print_content or ""
        if not content.strip():
            yield {"type": "step_warning", "orch_step_id": step.id,
                   "message": "No content configured for Print step"}
            if step.output_key:
                run.shared_state[step.output_key] = ""
            return

        rendered = render_state_refs(content, run.shared_state)

        yield {"type": "_log_prompt", "orch_step_id": step.id,
               "prompt": f"[Print] {rendered[:500]}"}

        if step.output_key:
            run.shared_state[step.output_key] = rendered

        yield {
            "type": "final",
            "orch_step_id": step.id,
            "step_name": step.name,
            "response": rendered,
        }


class EndStepExecutor:
    """Terminate the orchestration."""

    async def execute(
        self, step: StepConfig, run: OrchestrationRun, engine: "OrchestrationEngine"
    ) -> AsyncGenerator[dict, None]:
        run.status = "completed"
        yield {"type": "orchestration_end", "orch_step_id": step.id}


# Registry of all step executors
STEP_EXECUTORS = {
    StepType.AGENT: AgentStepExecutor(),
    StepType.LLM: LLMStepExecutor(),
    StepType.TOOL: ToolStepExecutor(),
    StepType.EVALUATOR: EvaluatorStepExecutor(),
    StepType.PARALLEL: ParallelStepExecutor(),
    StepType.MERGE: MergeStepExecutor(),
    StepType.LOOP: LoopStepExecutor(),
    StepType.HUMAN: HumanStepExecutor(),
    StepType.TRANSFORM: TransformStepExecutor(),
    StepType.EXTRACT_JSON: ExtractJsonStepExecutor(),
    StepType.IF_ELSE: IfElseStepExecutor(),
    StepType.SWITCH: SwitchStepExecutor(),
    StepType.PRINT: PrintStepExecutor(),
    StepType.END: EndStepExecutor(),
}
