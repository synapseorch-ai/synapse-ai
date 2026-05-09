"""
Step executors for each orchestration step type.
Each executor is an async generator that yields SSE-compatible events.
"""
import asyncio
import datetime
import json
import re
import subprocess
import sys
import time
from typing import AsyncGenerator, TYPE_CHECKING

import anyio

from core.models_orchestration import StepConfig, StepType, OrchestrationRun

if TYPE_CHECKING:
    from .engine import OrchestrationEngine


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
    """Run a sub-agent's ReAct loop. Reuses the existing engine."""

    async def execute(
        self, step: StepConfig, run: OrchestrationRun, engine: "OrchestrationEngine"
    ) -> AsyncGenerator[dict, None]:
        from core.react_engine import run_agent_step
        from core.agent_logger import AgentLogger
        print(f"DEBUG AGENT EXEC: agent_id={step.agent_id} step={step.id}", flush=True)

        from .context import build_origin_aware_context
        transition = getattr(engine, "current_transition", None)
        if transition is None:
            from .context import TransitionContext
            transition = TransitionContext(origin_type="entry", execution_number=1)
        prompt, system_prompt_extra = build_origin_aware_context(
            step, run, engine, transition
        )

        # Emit prompt for the orchestration logger (filtered out before SSE)
        yield {"type": "_log_prompt", "orch_step_id": step.id, "prompt": prompt, "system_prompt_extra": system_prompt_extra}

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
        agent_log.log_event({"type": "_log_prompt", "prompt": prompt})

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
                model_override=step.model,
            ):
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

        if final_response is None:
            raise RuntimeError(f"Agent step '{step.name}' ended without producing a final response")

        if step.output_key:
            run.shared_state[step.output_key] = final_response

        # Store execution trace for memory across re-invocations
        from .context import build_execution_trace, store_execution_memory
        trace = build_execution_trace(execution_events)
        store_execution_memory(run, step, trace, agent_name)


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
        from core.tools import aggregate_all_tools
        from core.routes.agents import load_user_agents

        if not step.forced_tool:
            yield {"type": "step_warning", "orch_step_id": step.id,
                   "message": f"No tool configured for TOOL step '{step.name}'"}
            return

        from .context import build_origin_aware_context, TransitionContext
        transition = getattr(engine, "current_transition", None)
        if transition is None:
            transition = TransitionContext(origin_type="entry", execution_number=1)
        prompt, system_prompt_extra = build_origin_aware_context(step, run, engine, transition)

        yield {"type": "_log_prompt", "orch_step_id": step.id, "prompt": prompt, "system_prompt_extra": system_prompt_extra}

        # Model resolution — same pattern as EvaluatorStepExecutor and LLMStepExecutor
        settings = load_settings()
        _step_model = step.model if (step.model and step.model.strip().lower() not in ("", "default")) else None
        model = _step_model if _step_model else settings.get("model", "mistral")
        mode = detect_mode_from_model(model)

        # Load only the forced tool's schema — aggregate all tools then filter to one
        agents = load_user_agents()
        active_agent = next((a for a in agents if a.get("id") == step.agent_id), agents[0] if agents else {})
        custom_tools = self._load_custom_tools()
        all_tools, _, _, _ = await aggregate_all_tools(
            engine.server_module.agent_sessions, active_agent, custom_tools
        )

        tool_name = step.forced_tool
        tool_obj = next((t for t in all_tools if t.name == tool_name), None)
        if not tool_obj:
            yield {"type": "step_error", "orch_step_id": step.id,
                   "error": f"Tool '{tool_name}' not found in available tools"}
            return

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

        for turn in range(max_turns):
            turn_prompt = tool_prompt
            if last_error:
                turn_prompt += f"\n\nPrevious attempt failed: {last_error}\nPlease try again with correct arguments."

            print(f"DEBUG TOOL STEP: turn {turn + 1}/{max_turns} model={model} tool={tool_name}", flush=True)
            try:
                response = await llm_generate(
                    prompt_msg=turn_prompt,
                    sys_prompt="You are a tool-calling assistant. Output ONLY valid JSON.",
                    mode=mode,
                    current_model=model,
                    current_settings=settings,
                    session_id=run.session_id,
                    agent_id=step.agent_id or "tool_step",
                    source="orchestration",
                    run_id=run.run_id,
                )
            except Exception as e:
                from core.llm_providers import LLMError
                if isinstance(e, LLMError):
                    raise
                raise RuntimeError(f"Tool step '{step.name}' LLM call failed: {e}") from e

            # Log the LLM response (mirrors _log_evaluator for evaluator steps)
            yield {"type": "_log_tool_step_llm", "orch_step_id": step.id,
                   "prompt": turn_prompt, "llm_response": response}

            tool_call, json_error = parse_tool_call(response)
            if not tool_call:
                last_error = json_error or "LLM did not return a valid tool call JSON"
                print(f"DEBUG TOOL STEP: ⚠ parse failed turn={turn + 1}: {last_error}", flush=True)
                continue

            called_tool = tool_call.get("tool", "")
            tool_args = tool_call.get("arguments", {})

            # tool_execution matches the event type the logger and react_engine emit
            yield {
                "type": "tool_execution",
                "orch_step_id": step.id,
                "step_name": step.name,
                "tool_name": called_tool,
                "args": tool_args,
            }
            print(f"DEBUG TOOL STEP: 🔧 Tool Call: {called_tool}", flush=True)
            print(f"DEBUG TOOL STEP: 📥 Args: {json.dumps(tool_args, indent=2, default=str)[:1000]}", flush=True)

            try:
                result = await self._execute_tool(called_tool, tool_args, engine)
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
                print(f"DEBUG TOOL STEP: 📤 Tool Result ({called_tool}): {preview}", flush=True)
                print(f"DEBUG TOOL STEP: ✅ tool '{called_tool}' succeeded", flush=True)
                break
            except Exception as e:
                last_error = str(e)
                print(f"DEBUG TOOL STEP: ❌ tool execution failed turn={turn + 1}: {last_error}", flush=True)

        yield {
            "type": "final",
            "orch_step_id": step.id,
            "step_name": step.name,
            "response": final_response if final_response is not None
                        else f"Tool step '{step.name}' failed after {max_turns} attempt(s): {last_error}",
        }

        if final_response is None:
            raise RuntimeError(f"Tool step '{step.name}' failed after {max_turns} attempt(s): {last_error}")

    async def _execute_tool(self, tool_name: str, tool_args: dict, engine: "OrchestrationEngine") -> str:
        """Execute a tool via MCP session or Docker sandbox (custom Python tools)."""
        from datetime import timedelta
        server_module = engine.server_module

        # Native builder tools (create_orchestration, list_agents, etc.) —
        # dispatched directly to execute_builder_tool so TOOL steps can drive
        # the builder primitives.
        from core.builder_tools import BUILDER_TOOL_NAMES, execute_builder_tool
        if tool_name in BUILDER_TOOL_NAMES:
            return await execute_builder_tool(tool_name, tool_args, server_module)

        tool_router = getattr(server_module, "tool_router", {})
        if tool_name in tool_router:
            agent_name, actual_tool_name = tool_router[tool_name]
            session = server_module.agent_sessions.get(agent_name)
            if session:
                result = await session.call_tool(
                    actual_tool_name, tool_args, read_timeout_seconds=timedelta(seconds=30)
                )
                return result.content[0].text if result.content else ""
        # Custom Python tools — execute in Docker sandbox
        from core.routes.tools import load_custom_tools
        custom_tools = load_custom_tools()
        target_tool = next((t for t in custom_tools if t["name"] == tool_name), None)
        if target_tool and target_tool.get("tool_type") == "python":
            return await self._execute_python_tool(target_tool, tool_args)
        raise RuntimeError(f"Tool '{tool_name}' not found in tool router")

    async def _execute_python_tool(self, tool: dict, tool_args: dict) -> str:
        """Execute a custom Python tool in the Docker sandbox (sandbox-python:latest)."""
        import shutil
        import tempfile
        from pathlib import Path

        python_code = tool.get("code", "")
        if not python_code.strip():
            raise ValueError("Python tool has no code defined.")
        if not shutil.which("docker"):
            raise RuntimeError("Docker is not available. Cannot execute Python tool.")

        args_json = json.dumps(tool_args)
        escaped = args_json.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
        injected_code = (
            f'import json\n_args = json.loads("""{escaped}""")\n\n'
            + python_code
        )

        DATA_DIR_PATH = Path(__file__).resolve().parent.parent.parent / "data"
        vault_root = DATA_DIR_PATH / "vault"
        docker_image = "sandbox-python:latest"

        tmp_dir = tempfile.mkdtemp(prefix="pytool_")
        script_path = f"{tmp_dir}/script.py"
        try:
            with open(script_path, "w") as f:
                f.write(injected_code)

            docker_cmd = [
                "docker", "run", "--rm",
                "--memory", "512m",
                "--cpus", "1.0",
                "--pids-limit", "64",
                "--read-only",
                "--tmpfs", "/tmp:rw,size=256m",
                "--tmpfs", "/root:rw,size=256m",
                "--network", "none",
                "-v", f"{script_path}:/sandbox/script.py:ro",
            ]
            if vault_root.exists():
                docker_cmd += ["-v", f"{vault_root}:/data:ro"]
            docker_cmd += [docker_image, "python", "/sandbox/script.py"]

            proc = await asyncio.create_subprocess_exec(
                *docker_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=35)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise RuntimeError("Python tool execution timed out after 30s")

            stdout_text = stdout_b.decode("utf-8", errors="replace")[:20000]
            stderr_text = stderr_b.decode("utf-8", errors="replace")[:5000]

            if proc.returncode != 0:
                return json.dumps({
                    "error": f"Python tool exited with code {proc.returncode}",
                    "stderr": stderr_text,
                    "stdout": stdout_text,
                })
            try:
                parsed = json.loads(stdout_text.strip())
                return json.dumps(parsed)
            except Exception:
                return json.dumps({"output": stdout_text})
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _load_custom_tools(self) -> list:
        try:
            from core.routes.tools import load_custom_tools
            return load_custom_tools()
        except Exception:
            return []


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

        prompt = (
            f"{evaluator_instructions}"
            f"{_datetime_context()}\n"
            f"Based on the context below, decide which route to take.\n\n"
            f"CONTEXT:\n{context_block}\n\n"
            f"AVAILABLE ROUTES:\n{routes_text}\n\n"
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
            response = await llm_generate_response(
                prompt_msg=prompt,
                sys_prompt="You are a routing decision maker. Output ONLY valid JSON.",
                mode=eval_mode,
                current_model=eval_model,
                current_settings=settings,
                session_id=run.session_id,
                agent_id=step.agent_id or "evaluator",
                source="orchestration",
                run_id=run.run_id,
            )
            print(f"DEBUG: 🔀 Evaluator LLM response: {response}")

            # Emit evaluator LLM response for the logger
            yield {"type": "_log_evaluator", "orch_step_id": step.id, "prompt": prompt, "llm_response": response}

            from core.react_engine import parse_tool_call
            tool_call, _ = parse_tool_call(response)
            if tool_call:
                tool_name = tool_call.get("tool", "")
                if tool_name in {f"route_{l}" for l in labels}:
                    routing_decision = {
                        "type": "routing_decision",
                        "tool_name": tool_name,
                        "arguments": tool_call.get("arguments", {}),
                    }
        except Exception as e:
            # LLM errors must propagate to stop orchestration
            from core.llm_providers import LLMError
            if isinstance(e, LLMError):
                print(f"DEBUG: ❌ Evaluator LLM failed — stopping orchestration: {e}", flush=True)
                raise
            print(f"DEBUG: Evaluator routing call failed: {e}")

        # Fallback: pick the first route
        if not routing_decision:
            fallback_label = labels[0] if labels else None
            if fallback_label:
                print(f"DEBUG: Evaluator falling back to first route: {fallback_label}")
                routing_decision = {
                    "type": "routing_decision",
                    "tool_name": f"route_{fallback_label}",
                    "arguments": {"reasoning": "Fallback — LLM did not return a valid routing decision"},
                }

        if routing_decision:
            print(f"routing decision")
            yield {**routing_decision, "orch_step_id": step.id}
            tool_name = routing_decision.get("tool_name", "")
            label = tool_name.replace("route_", "", 1) if tool_name.startswith("route_") else tool_name
            run.shared_state[f"_routing_decision_{step.id}"] = label
            run.shared_state[f"_routing_reasoning_{step.id}"] = routing_decision.get("arguments", {}).get("reasoning", "")

            # Store bare route label so downstream evaluators/templates can match it verbatim.
            # Reasoning is already kept in `_routing_reasoning_<step_id>` above.
            if step.output_key:
                run.shared_state[step.output_key] = label


class ParallelStepExecutor:
    """Run multiple branches.

    Each branch entry in ``parallel_branches`` can be:
      - A single entry-point step ID  → executor auto-follows ``next_step_id`` chain
      - Multiple step IDs             → executor runs them in that explicit order

    A branch chain stops when a step has no ``next_step_id``, or its ``next_step_id``
    equals the parallel node's own convergence point (``step.next_step_id``).

    NOTE: Branches run sequentially (not with asyncio.gather) because agents
    share resources that are not concurrency-safe — notably MCP server
    connections and single-instance tools like the Playwright browser.
    True parallel execution would require per-branch tool isolation.
    """

    async def execute(
        self, step: StepConfig, run: OrchestrationRun, engine: "OrchestrationEngine"
    ) -> AsyncGenerator[dict, None]:
        branches = step.parallel_branches
        print(f"DEBUG PARALLEL: ▶ step='{step.id}' branches={len(branches)} convergence='{step.next_step_id}'", flush=True)
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

        # Run branches sequentially to avoid MCP/browser resource contention
        for branch_index, branch_step_ids in enumerate(resolved_branches):
            # Checkpoint between branches — give MCP background tasks time
            await anyio.sleep(0)

            print(f"DEBUG PARALLEL: ├─ branch {branch_index}/{len(resolved_branches)} steps={branch_step_ids}", flush=True)
            yield {"type": "branch_start", "orch_step_id": step.id,
                   "branch_index": branch_index, "branch_count": len(resolved_branches)}

            for sid in branch_step_ids:
                sub_step = engine.step_map.get(sid)
                if not sub_step:
                    yield {"type": "step_warning", "orch_step_id": sid,
                           "message": f"Step {sid} not found in branch {branch_index}"}
                    continue
                executor = engine.executors.get(sub_step.type)
                if not executor:
                    yield {"type": "step_error", "orch_step_id": sid,
                           "error": f"No executor for {sub_step.type}"}
                    continue

                step_start = time.time()
                sub_timeout = sub_step.timeout_seconds or 300
                print(f"DEBUG PARALLEL:   ▶ sub-step '{sub_step.name}' ({sub_step.id}) timeout={sub_timeout}s", flush=True)
                yield {"type": "step_start", "orch_step_id": sub_step.id,
                       "step_name": sub_step.name, "step_type": sub_step.type.value}

                try:
                    with anyio.fail_after(sub_timeout):
                        async for event in executor.execute(sub_step, run, engine):
                            yield event
                except TimeoutError:
                    duration = round(time.time() - step_start, 2)
                    print(f"DEBUG PARALLEL: ⏱ Step '{sub_step.name}' timed out after {sub_timeout}s in branch {branch_index}", flush=True)
                    run.step_history.append({
                        "step_id": sub_step.id, "step_name": sub_step.name,
                        "step_type": sub_step.type.value, "status": "failed",
                        "error": f"Timed out after {sub_timeout}s",
                    })
                    yield {"type": "step_error", "orch_step_id": sub_step.id,
                           "error": f"Step '{sub_step.name}' timed out after {sub_timeout}s"}
                    continue

                duration = round(time.time() - step_start, 2)
                print(f"DEBUG PARALLEL:   ✓ sub-step '{sub_step.name}' done in {duration}s", flush=True)
                run.step_history.append({
                    "step_id": sub_step.id, "step_name": sub_step.name,
                    "step_type": sub_step.type.value, "status": "completed",
                    "duration_seconds": duration, "output_key": sub_step.output_key,
                })
                yield {"type": "step_complete", "orch_step_id": sub_step.id,
                       "step_name": sub_step.name, "duration_seconds": duration}

        print(f"DEBUG PARALLEL: ✅ all {len(resolved_branches)} branches complete", flush=True)
        yield {"type": "parallel_complete", "orch_step_id": step.id, "branch_count": len(resolved_branches)}


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
    """Run body steps N times sequentially, accumulating results."""

    async def execute(
        self, step: StepConfig, run: OrchestrationRun, engine: "OrchestrationEngine"
    ) -> AsyncGenerator[dict, None]:
        loop_count = max(1, step.loop_count)
        body_ids = step.loop_step_ids
        print(f"DEBUG LOOP: ▶ step='{step.id}' body_ids={body_ids} loop_count={loop_count}", flush=True)

        if not body_ids:
            yield {"type": "step_warning", "orch_step_id": step.id, "message": "No loop body steps defined"}
            return

        for iteration in range(1, loop_count + 1):
            yield {
                "type": "loop_iteration", "orch_step_id": step.id,
                "iteration": iteration, "total": loop_count,
            }

            for sid in body_ids:
                sub_step = engine.step_map.get(sid)
                if not sub_step:
                    yield {"type": "step_warning", "orch_step_id": step.id,
                           "message": f"Loop body step {sid} not found"}
                    continue

                executor = engine.executors.get(sub_step.type)
                if not executor:
                    yield {"type": "step_error", "orch_step_id": sid,
                           "error": f"No executor for {sub_step.type}"}
                    continue

                step_start = time.time()
                sub_timeout = sub_step.timeout_seconds or 300
                yield {"type": "step_start", "orch_step_id": sub_step.id,
                       "step_name": sub_step.name, "step_type": sub_step.type.value}

                try:
                    with anyio.fail_after(sub_timeout):
                        async for event in executor.execute(sub_step, run, engine):
                            # Human input within loop body — propagate up
                            if event.get("type") == "human_input_required":
                                yield event
                                return
                            yield event
                except TimeoutError:
                    duration = round(time.time() - step_start, 2)
                    print(f"DEBUG LOOP: ⏱ Step '{sub_step.name}' timed out after {sub_timeout}s (iteration {iteration})", flush=True)
                    run.step_history.append({
                        "step_id": sub_step.id, "step_name": sub_step.name,
                        "step_type": sub_step.type.value, "status": "failed",
                        "error": f"Timed out after {sub_timeout}s",
                    })
                    yield {"type": "step_error", "orch_step_id": sub_step.id,
                           "error": f"Step '{sub_step.name}' timed out after {sub_timeout}s"}
                    continue

                duration = round(time.time() - step_start, 2)
                run.step_history.append({
                    "step_id": sub_step.id, "step_name": sub_step.name,
                    "step_type": sub_step.type.value, "status": "completed",
                    "duration_seconds": duration, "output_key": sub_step.output_key,
                })
                yield {"type": "step_complete", "orch_step_id": sub_step.id,
                       "step_name": sub_step.name, "duration_seconds": duration}

            # After each iteration, accumulate results from body steps
            for sid in body_ids:
                sub_step = engine.step_map.get(sid)
                if not sub_step or not sub_step.output_key:
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

        # After all iterations, promote accumulated results to output keys
        for sid in body_ids:
            sub_step = engine.step_map.get(sid)
            if not sub_step or not sub_step.output_key:
                continue
            acc_key = f"_loop_{sub_step.output_key}"
            if acc_key in run.shared_state:
                run.shared_state[sub_step.output_key] = run.shared_state.pop(acc_key)

        # Store loop's own output if configured
        if step.output_key:
            # Collect all body outputs as summary
            summary = {}
            for sid in body_ids:
                sub_step = engine.step_map.get(sid)
                if sub_step and sub_step.output_key and sub_step.output_key in run.shared_state:
                    summary[sub_step.name] = run.shared_state[sub_step.output_key]
            run.shared_state[step.output_key] = summary

        yield {"type": "loop_complete", "orch_step_id": step.id,
               "iterations_completed": loop_count}


class HumanStepExecutor:
    """Pause execution and request human input.

    If the step has a human_channel_id configured, the prompt is also sent
    to that messaging channel. Whichever responds first — the messaging app
    or the browser UI — wins. The later response is silently discarded.
    """

    async def execute(
        self, step: StepConfig, run: OrchestrationRun, engine: "OrchestrationEngine"
    ) -> AsyncGenerator[dict, None]:
        prompt = step.human_prompt or "Please provide input to continue."
        prompt = re.sub(
            r"\{state\.(\w+)\}",
            lambda m: str(run.shared_state.get(m.group(1), f"{{state.{m.group(1)}}}")),
            prompt,
        )

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

        # If a messaging channel is configured, arm a Future so the messaging
        # adapter can resolve it when the user replies there.
        channel_id = step.human_channel_id
        if channel_id:
            messaging_manager = getattr(
                getattr(engine.server_module, "app", None),
                "state", None,
            )
            if messaging_manager:
                messaging_manager = getattr(messaging_manager, "messaging_manager", None)
            if messaging_manager:
                asyncio.create_task(
                    messaging_manager.wait_for_human_input(
                        run_id=run.run_id,
                        step_id=step.id,
                        channel_id=channel_id,
                        prompt=prompt,
                        timeout=step.human_timeout_seconds,
                    )
                )

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
            result = await self._run_sandboxed(code, run.shared_state, timeout=step.timeout_seconds)

            if step.output_key and result is not None:
                run.shared_state[step.output_key] = result

            yield {
                "type": "transform_result",
                "orch_step_id": step.id,
                "result": str(result) if result is not None else None,
            }
        except Exception as e:
            yield {"type": "step_error", "orch_step_id": step.id, "error": f"Transform error: {e}"}

    async def _run_sandboxed(self, code: str, state: dict, timeout: int = 30):
        """Run Python code in the Docker sandbox (sandbox-python:latest)."""
        import shutil
        import tempfile
        from pathlib import Path

        if not shutil.which("docker"):
            raise RuntimeError("Docker is not available. Cannot execute transform code in sandbox.")

        state_json = json.dumps(state, default=str)
        escaped = state_json.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
        script_content = (
            f'import json\nstate = json.loads("""{escaped}""")\nresult = None\n\n'
            + code
            + '\n\nif result is not None:\n    print(json.dumps({"result": result}, default=str))\nelse:\n    print(json.dumps({"result": None}))\n'
        )

        DATA_DIR_PATH = Path(__file__).resolve().parent.parent.parent / "data"
        vault_root = DATA_DIR_PATH / "vault"
        docker_image = "sandbox-python:latest"

        tmp_dir = tempfile.mkdtemp(prefix="transform_")
        script_path = f"{tmp_dir}/transform.py"
        try:
            with open(script_path, "w") as f:
                f.write(script_content)

            docker_cmd = [
                "docker", "run", "--rm",
                "--memory", "512m",
                "--cpus", "1.0",
                "--pids-limit", "64",
                "--read-only",
                "--tmpfs", "/tmp:rw,size=256m",
                "--tmpfs", "/root:rw,size=256m",
                "--network", "none",
                "-v", f"{script_path}:/sandbox/transform.py:ro",
            ]
            if vault_root.exists():
                docker_cmd += ["-v", f"{vault_root}:/data:ro"]
            docker_cmd += [docker_image, "python", "/sandbox/transform.py"]

            proc = await asyncio.create_subprocess_exec(
                *docker_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=min(timeout, 60) + 5
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise RuntimeError(f"Transform timed out after {timeout}s")

            stdout_text = stdout_b.decode("utf-8", errors="replace")
            stderr_text = stderr_b.decode("utf-8", errors="replace")

            if proc.returncode != 0:
                raise RuntimeError(f"Transform failed (exit {proc.returncode}): {stderr_text[:500]}")

            try:
                output = json.loads(stdout_text)
                return output.get("result")
            except json.JSONDecodeError:
                return stdout_text.strip() if stdout_text.strip() else None
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


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

        from .context import build_origin_aware_context
        transition = getattr(engine, "current_transition", None)
        if transition is None:
            from .context import TransitionContext
            transition = TransitionContext(origin_type="entry", execution_number=1)
        prompt, system_prompt_extra = build_origin_aware_context(step, run, engine, transition)

        yield {"type": "_log_prompt", "orch_step_id": step.id, "prompt": prompt, "system_prompt_extra": system_prompt_extra}
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

        try:
            response = await llm_generate(
                prompt_msg=prompt,
                sys_prompt=f"You are a helpful assistant. Be concise and accurate.\n\n{_datetime_context()}",
                mode=mode,
                current_model=model,
                current_settings=settings,
                session_id=run.session_id,
                agent_id=step.agent_id or "llm_step",
                source="orchestration",
                run_id=run.run_id,
            )
        except Exception as e:
            from core.llm_providers import LLMError
            if isinstance(e, LLMError):
                raise
            raise RuntimeError(f"LLM step '{step.name}' failed: {e}") from e

        if step.output_key:
            run.shared_state[step.output_key] = response

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

        extracted = self._extract_all_json(text)

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

    def _extract_all_json(self, text: str) -> list:
        """Try multiple strategies to extract JSON from text."""
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
        results = self._extract_by_brace_matching(text)
        return results

    def _extract_by_brace_matching(self, text: str) -> list:
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


class _DotNavigableState:
    """Wrapper for shared state that supports dot-notation access.
    Missing keys return None instead of raising errors."""

    def __init__(self, data: dict):
        object.__setattr__(self, '_data', data)

    def __getattr__(self, name: str):
        data = object.__getattribute__(self, '_data')
        if name.startswith('_'):
            raise AttributeError(name)
        val = data.get(name)
        if isinstance(val, dict):
            return _DotNavigableState(val)
        return val

    def __getitem__(self, key):
        data = object.__getattribute__(self, '_data')
        val = data.get(key)
        if isinstance(val, dict):
            return _DotNavigableState(val)
        return val

    def __repr__(self):
        data = object.__getattribute__(self, '_data')
        return repr(data)

    def __str__(self):
        data = object.__getattribute__(self, '_data')
        return str(data)

    def __eq__(self, other):
        if isinstance(other, _DotNavigableState):
            return object.__getattribute__(self, '_data') == object.__getattribute__(other, '_data')
        return object.__getattribute__(self, '_data') == other

    def __bool__(self):
        data = object.__getattribute__(self, '_data')
        return bool(data)

    def __contains__(self, item):
        data = object.__getattribute__(self, '_data')
        return item in data

    def __len__(self):
        data = object.__getattribute__(self, '_data')
        return len(data)


# Restricted namespace for safe eval in IF/Else and Switch steps
_SAFE_EVAL_BUILTINS = {
    '__builtins__': {},
    'None': None, 'True': True, 'False': False,
    'len': len, 'int': int, 'float': float, 'str': str,
    'bool': bool, 'list': list, 'dict': dict, 'abs': abs,
    'min': min, 'max': max, 'round': round,
}


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

        state_wrapper = _DotNavigableState(run.shared_state)
        eval_ns = {**_SAFE_EVAL_BUILTINS, 'state': state_wrapper}

        try:
            result = bool(eval(condition, eval_ns))
        except Exception as e:
            print(f"DEBUG IF_ELSE: ⚠ Condition eval failed: {e}", flush=True)
            result = False
            yield {"type": "step_warning", "orch_step_id": step.id,
                   "message": f"Condition eval error (treating as False): {e}"}

        decision = "true" if result else "false"
        run.shared_state[f"_if_decision_{step.id}"] = decision
        print(f"DEBUG IF_ELSE: condition='{condition}' → {decision}", flush=True)

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

        state_wrapper = _DotNavigableState(run.shared_state)
        eval_ns = {**_SAFE_EVAL_BUILTINS, 'state': state_wrapper}

        try:
            value = eval(expression, eval_ns)
        except Exception as e:
            print(f"DEBUG SWITCH: ⚠ Expression eval failed: {e}", flush=True)
            value = None
            yield {"type": "step_warning", "orch_step_id": step.id,
                   "message": f"Expression eval error: {e}"}

        # Convert to string for case matching
        value_str = str(value) if value is not None else "None"
        matched_case = value_str if value_str in (step.switch_cases or {}) else None
        run.shared_state[f"_switch_decision_{step.id}"] = matched_case
        print(f"DEBUG SWITCH: expression='{expression}' → '{value_str}', matched_case={matched_case}", flush=True)

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

        # Interpolate {state.key} and {state.key.nested.path}
        def _resolve_ref(match: re.Match) -> str:
            path = match.group(1)  # e.g. "result.flag"
            parts = path.split('.')
            val = run.shared_state
            for part in parts:
                if isinstance(val, dict):
                    val = val.get(part)
                else:
                    val = None
                    break
            return str(val) if val is not None else match.group(0)  # keep original if not found

        rendered = re.sub(r'\{state\.([\w.]+)\}', _resolve_ref, content)

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
