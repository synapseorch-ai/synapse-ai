"""
OrchestrationEngine -- walks a graph of steps, managing shared state,
checkpointing, loop guards, and yielding SSE events.
"""
import asyncio
import time
from typing import AsyncGenerator

import anyio

from core.models_orchestration import Orchestration, OrchestrationRun, StepConfig, StepType
from .state import SharedState
from .steps import STEP_EXECUTORS
from .logger import OrchestrationLogger


class OrchestrationEngine:
    """Runs an orchestration by walking through its step graph."""

    def __init__(self, orchestration: Orchestration, server_module):
        self.orch = orchestration
        self.server_module = server_module
        self.step_map: dict[str, StepConfig] = {s.id: s for s in orchestration.steps}
        self.executors = STEP_EXECUTORS
        self.agent_names: dict[str, str] = self._load_agent_names()
        self.current_transition = None  # set by _execute_loop before each step

    def _load_agent_names(self) -> dict[str, str]:
        """Load agent_id -> name mapping for context attribution."""
        try:
            from core.routes.agents import load_user_agents
            agents = load_user_agents()
            return {a["id"]: a["name"] for a in agents}
        except Exception:
            return {}

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

        shared_state = self._init_state(initial_input)
        if initial_state:
            shared_state.update(initial_state)

        run = OrchestrationRun(
            run_id=run_id,
            orchestration_id=self.orch.id,
            session_id=session_id,
            shared_state=shared_state,
            current_step_id=self.orch.entry_step_id,
            started_at=now,
        )
        state = SharedState(run)

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
        total_turns = len(run.step_history)
        logger = getattr(self, "logger", None)
        loop_start_time = time.time()
        timeout_seconds = (self.orch.timeout_minutes or 30) * 60

        while run.current_step_id and run.status == "running":
            # Checkpoint between steps — ensures MCP background tasks
            # (receive_loop, stdio reader/writer) get event-loop time
            # before the next step starts sending new requests.
            await anyio.sleep(0)

            # Check if this run was cancelled via the cancel endpoint
            from .state import _cancelled_run_ids
            if run.run_id in _cancelled_run_ids:
                _cancelled_run_ids.discard(run.run_id)
                run.status = "cancelled"
                print(f"DEBUG ENGINE: 🛑 run '{run.run_id}' cancelled by request", flush=True)
                break

            # Global timeout guard
            elapsed = time.time() - loop_start_time
            if elapsed > timeout_seconds:
                run.status = "failed"
                yield {"type": "orchestration_error", "error": f"Global timeout ({self.orch.timeout_minutes} min) exceeded after {round(elapsed / 60, 1)} min"}
                break

            step = self.step_map.get(run.current_step_id)
            print(f"DEBUG ENGINE: ▶ step='{run.current_step_id}' type={step.type if step else None} status={run.status}", flush=True)
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
            step_timeout = step.timeout_seconds or 300  # default 5 min

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

            try:
                # anyio.fail_after installs a cancel scope on the CURRENT task —
                # unlike asyncio.wait_for which creates a new Task and breaks
                # anyio-based MCP sessions (their _receive_loop wakeup never
                # reaches a waiter in a different Task's context).
                # This properly interrupts a stuck session.call_tool() inside
                # the executor when the step deadline is reached.
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
                                state.checkpoint()
                                if logger:
                                    logger.step_end(step.id, "paused")
                                    logger.run_end("paused")
                                yield event
                                return

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

                # Resolve next step
                next_step_id, extra_event = self._resolve_next(step, run)
                if extra_event:
                    if logger:
                        logger.log_event(extra_event)
                    yield extra_event
                run.current_step_id = next_step_id
                print(f"DEBUG ENGINE: 💾 checkpointed → next='{run.current_step_id}'", flush=True)
                state.checkpoint()

            except Exception as e:
                import traceback; print(f"DEBUG ENGINE: ❌ EXCEPTION in step '{step.id}': {e}\n{traceback.format_exc()}", flush=True)
                safe_error = "An internal error occurred while executing this step."
                run.step_history.append({
                    "step_id": step.id,
                    "step_name": step.name,
                    "step_type": step.type.value,
                    "status": "failed",
                    "error": safe_error,
                })
                run.status = "failed"
                if logger:
                    logger.step_end(step.id, "failed", safe_error)
                yield {"type": "step_error", "orch_step_id": step.id, "error": safe_error}
                break

        # Finalize
        if run.status == "running":
            run.status = "completed"

        run.ended_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        state.checkpoint()

        if logger:
            logger.run_end(run.status)

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
        """
        from .state import SharedState as SS, _cancelled_run_ids

        restored = SS.restore(run_id)
        run = restored.run

        if run.status not in ("failed", "cancelled"):
            yield {"type": "orchestration_error", "error": f"Run is not recoverable (status={run.status})"}
            return

        from core.routes.orchestrations import load_orchestrations
        orchestrations = load_orchestrations()
        orch_data = next((o for o in orchestrations if o["id"] == run.orchestration_id), None)
        if not orch_data:
            yield {"type": "orchestration_error", "error": f"Orchestration '{run.orchestration_id}' not found"}
            return

        orch = Orchestration.model_validate(orch_data)
        engine = cls(orch, server_module)

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
        async for event in engine._execute_loop(run, state):
            yield event

    @classmethod
    async def resume(
        cls, run_id: str, human_response: dict, server_module
    ) -> AsyncGenerator[dict, None]:
        """Resume a paused orchestration after human input."""
        from .state import SharedState as SS

        restored = SS.restore(run_id)
        run = restored.run

        # Load the orchestration definition
        from core.routes.orchestrations import load_orchestrations
        orchestrations = load_orchestrations()
        orch_data = next((o for o in orchestrations if o["id"] == run.orchestration_id), None)
        if not orch_data:
            yield {"type": "orchestration_error", "error": f"Orchestration '{run.orchestration_id}' not found"}
            return

        orch = Orchestration.model_validate(orch_data)
        engine = cls(orch, server_module)

        # Create logger — appends to existing log file if present
        engine.logger = OrchestrationLogger(
            run_id=run_id,
            orchestration_id=run.orchestration_id,
            orchestration_name=orch.name,
            user_input=f"(resumed) human_response={human_response}",
            session_id=run.session_id,
        )

        # Move to next step after the HUMAN step
        current_step = engine.step_map.get(run.current_step_id)

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
            next_id, _ = engine._resolve_next(current_step, run)
            run.current_step_id = next_id

        state = SharedState(run)
        async for event in engine._execute_loop(run, state):
            yield event

    def _init_state(self, user_input: str) -> dict:
        """Initialize shared state from schema defaults + user input."""
        state = {}
        for key, schema in self.orch.state_schema.items():
            if isinstance(schema, dict) and "default" in schema:
                state[key] = schema["default"]
        state["user_input"] = user_input
        state["user_query"] = user_input
        return state

    def _resolve_next(self, step: StepConfig, run: OrchestrationRun) -> tuple[str | None, dict | None]:
        """
        Resolve the next step ID based on step type and state.
        Returns (next_step_id, optional_event).
        """
        # END steps have no next
        if step.type == StepType.END:
            return None, None

        # EVALUATOR — routing decision from LLM call
        if step.type == StepType.EVALUATOR and step.route_map:
            decision = run.shared_state.get(f"_routing_decision_{step.id}")
            if decision and decision in step.route_map:
                target = step.route_map[decision]
                reasoning = run.shared_state.get(f"_routing_reasoning_{step.id}", "")
                event = {
                    "type": "routing_decision",
                    "orch_step_id": step.id,
                    "decision": decision,
                    "target_step_id": target,
                    "reasoning": reasoning,
                }
                if target is None:
                    # None = end orchestration
                    return None, event
                next_id = target
            else:
                # Fallback to next_step_id if no routing decision
                next_id = step.next_step_id
                event = None

            # Apply loop guard on target
            if next_id:
                guarded_id, loop_event = self._apply_loop_guard(next_id, run)
                return guarded_id, event or loop_event
            return next_id, event

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
                "type": "if_decision",
                "orch_step_id": step.id,
                "decision": decision,
                "target_step_id": target,
            }
            if target:
                guarded_id, loop_event = self._apply_loop_guard(target, run)
                return guarded_id, event or loop_event
            return target, event

        # SWITCH — deterministic case matching
        if step.type == StepType.SWITCH and step.switch_cases:
            matched = run.shared_state.get(f"_switch_decision_{step.id}")
            if matched is not None and matched in step.switch_cases:
                target = step.switch_cases[matched]
            else:
                target = step.switch_default_step_id
            event = {
                "type": "switch_decision",
                "orch_step_id": step.id,
                "matched_case": matched,
                "target_step_id": target,
            }
            if target is None:
                return None, event
            if target:
                guarded_id, loop_event = self._apply_loop_guard(target, run)
                return guarded_id, event or loop_event
            return target, event

        # Default linear routing
        next_id = step.next_step_id

        # Apply loop guard
        if next_id:
            return self._apply_loop_guard(next_id, run)

        return next_id, None

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
