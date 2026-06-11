"""
Origin-aware context building for orchestration steps.

Provides:
  - TransitionContext   dataclass that describes how a step was invoked
  - build_workflow_graph_markdown()  compact workflow map for system prompt
  - build_execution_trace()       extract structured trace from SSE events
  - store_execution_memory()      persist trace in shared_state
  - get_execution_memory()        retrieve past traces for a step
  - build_transition_context()    determine origin type from run state
  - build_origin_aware_context()  construct the structured prompt + sys-prompt addition
"""
from __future__ import annotations

import datetime
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.models_orchestration import StepConfig, OrchestrationRun
    from .engine import OrchestrationEngine


def datetime_context() -> str:
    """Return a markdown block with the current date, time, and timezone.

    Shared by every LLM-involving step so the model has consistent temporal context.
    """
    now = datetime.datetime.now().astimezone()
    tz_name = now.strftime("%Z") or str(now.tzinfo)
    return (
        "### CURRENT DATE & TIME CONTEXT\n"
        f"**Current Date:** {now.strftime('%A, %B %d, %Y')}\n"
        f"**Current Time:** {now.strftime('%I:%M %p')}\n"
        f"**Timezone:** {tz_name}\n"
    )


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "..."


# ---------------------------------------------------------------------------
# TransitionContext
# ---------------------------------------------------------------------------

@dataclass
class TransitionContext:
    """Describes how the current step was invoked."""

    origin_type: str               # "entry" | "linear" | "evaluator" | "loop" | "human_response"
    execution_number: int = 1      # 1 = first time this step runs

    # Who handed control here
    from_step_id: str | None = None
    from_step_name: str | None = None
    from_agent_name: str | None = None

    # Evaluator-specific
    routing_decision: str | None = None    # e.g. "needs_improvement"
    routing_reasoning: str | None = None   # evaluator's explanation

    # Loop-specific
    loop_iteration: int | None = None
    loop_total: int | None = None

    # Human-specific
    human_response_key: str | None = None


def build_transition_context(
    step: "StepConfig",
    run: "OrchestrationRun",
    engine: "OrchestrationEngine",
) -> TransitionContext:
    """
    Derive the TransitionContext for `step` from current run state.
    Called by the engine right before executor.execute().
    """
    from core.models_orchestration import StepType

    # How many times has this step already run?
    exec_count = sum(1 for h in run.step_history if h["step_id"] == step.id)
    execution_number = exec_count + 1  # +1 because we're about to run it

    # Find the most recent completed step (the one that handed us control)
    last_completed = None
    for h in reversed(run.step_history):
        if h.get("status") == "completed":
            last_completed = h
            break

    from_step_id = last_completed["step_id"] if last_completed else None
    from_step_name = last_completed["step_name"] if last_completed else None
    from_agent_name = None

    # Determine origin type
    if from_step_id is None:
        origin_type = "entry"

    else:
        prev_step = engine.step_map.get(from_step_id)

        if prev_step and prev_step.type == StepType.EVALUATOR:
            # Check if the evaluator explicitly routed to this step
            decision_key = f"_routing_decision_{from_step_id}"
            if decision_key in run.shared_state:
                decision = run.shared_state[decision_key]
                target = prev_step.route_map.get(decision)
                if target == step.id:
                    routing_reasoning = run.shared_state.get(f"_routing_reasoning_{from_step_id}")
                    if prev_step.agent_id:
                        from_agent_name = engine.agent_names.get(prev_step.agent_id)
                    return TransitionContext(
                        origin_type="evaluator",
                        execution_number=execution_number,
                        from_step_id=from_step_id,
                        from_step_name=from_step_name,
                        from_agent_name=from_agent_name or from_step_name,
                        routing_decision=decision,
                        routing_reasoning=routing_reasoning,
                    )

        elif prev_step and prev_step.type == StepType.HUMAN:
            # Find the output key that holds the human response
            human_key = prev_step.output_key or "human_response"
            return TransitionContext(
                origin_type="human_response",
                execution_number=execution_number,
                from_step_id=from_step_id,
                from_step_name=from_step_name,
                human_response_key=human_key,
            )

        elif prev_step and prev_step.type == StepType.LOOP:
            # Inside a loop body — get iteration metadata from shared_state
            iteration = run.shared_state.get("_loop_current_iteration")
            total = run.shared_state.get("_loop_total")
            return TransitionContext(
                origin_type="loop",
                execution_number=execution_number,
                from_step_id=from_step_id,
                from_step_name=from_step_name,
                loop_iteration=iteration,
                loop_total=total,
            )

        # Default: linear flow from previous step
        if prev_step and prev_step.agent_id:
            from_agent_name = engine.agent_names.get(prev_step.agent_id)
        origin_type = "linear"

    return TransitionContext(
        origin_type=origin_type,
        execution_number=execution_number,
        from_step_id=from_step_id,
        from_step_name=from_step_name,
        from_agent_name=from_agent_name,
    )


# ---------------------------------------------------------------------------
# Execution Memory
# ---------------------------------------------------------------------------

def build_execution_trace(events: list[dict]) -> dict:
    """
    Extract a structured trace from the SSE events emitted by run_agent_step().
    Keeps tool names/args/result-previews and the final response — NOT full outputs.
    """
    tool_calls: list[dict] = []
    tools_used: list[str] = []
    turn_count = 0
    final_output = ""

    for event in events:
        etype = event.get("type", "")

        if etype == "tool_call":
            name = event.get("tool_name") or event.get("tool") or ""
            args = event.get("tool_input") or event.get("arguments") or {}
            if name and name not in tools_used:
                tools_used.append(name)
            tool_calls.append({"name": name, "args": args})

        elif etype == "tool_result":
            # Attach result preview to the last recorded call for this tool
            result_raw = str(event.get("result") or event.get("output") or "")
            preview = result_raw[:300] + ("..." if len(result_raw) > 300 else "")
            if tool_calls:
                tool_calls[-1]["result_preview"] = preview

        elif etype in ("thinking", "agent_thinking"):
            turn_count += 1

        elif etype == "final":
            final_output = event.get("response", "")

    return {
        "tool_calls": tool_calls,
        "tools_used": tools_used,
        "turn_count": turn_count,
        "final_output": final_output,
    }


def snapshot_inputs(
    step: "StepConfig",
    run: "OrchestrationRun",
    engine: "OrchestrationEngine",
    max_chars: int = 400,
) -> dict[str, str]:
    """Capture {key: truncated_value} for every input that the prompt builder
    injects into the CONTEXT section — user_input, human-response keys, and
    declared input_keys. Used to record what the agent saw on each turn so
    `include_full_history` can re-render past inputs accurately.
    """
    snap: dict[str, str] = {}

    if "user_input" in run.shared_state:
        snap["user_input"] = _truncate(str(run.shared_state["user_input"]), max_chars)

    # Human response keys (mirrors logic in build_origin_aware_context)
    human_keys = {"human_response"}
    for s in engine.step_map.values():
        if s.type and s.type.value == "human" and s.output_key:
            human_keys.add(s.output_key)
    for hkey in human_keys:
        if hkey in run.shared_state and hkey not in snap:
            snap[hkey] = _truncate(str(run.shared_state[hkey]), max_chars)

    for key in (step.input_keys or []):
        if key in run.shared_state:
            snap[key] = _truncate(str(run.shared_state[key]), max_chars)

    return snap


def store_execution_memory(
    run: "OrchestrationRun",
    step: "StepConfig",
    trace: dict,
    agent_name: str,
    transition: "TransitionContext | None" = None,
    inputs_snapshot: dict[str, str] | None = None,
) -> None:
    """Append execution trace + origin metadata to shared_state under a namespaced key.

    `transition` records what triggered this execution (evaluator decision, loop
    iteration, etc) — used by full-history rendering to show per-turn provenance.
    `inputs_snapshot` records the inputs the agent saw on this turn.
    """
    key = f"_exec_memory_{step.id}"
    history = run.shared_state.get(key)
    if not isinstance(history, list):
        history = []

    origin = None
    if transition is not None:
        origin = {
            "type": transition.origin_type,
            "routing_decision": transition.routing_decision,
            "routing_reasoning": transition.routing_reasoning,
            "loop_iteration": transition.loop_iteration,
            "loop_total": transition.loop_total,
            "from_step_name": transition.from_step_name,
        }

    history.append({
        "execution": len(history) + 1,
        "agent": agent_name,
        "origin": origin,
        "inputs": inputs_snapshot or {},
        "trace": trace,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    run.shared_state[key] = history


def get_execution_memory(run: "OrchestrationRun", step_id: str) -> list[dict]:
    """Return all recorded execution traces for a step (empty list if none)."""
    val = run.shared_state.get(f"_exec_memory_{step_id}")
    return val if isinstance(val, list) else []


# ---------------------------------------------------------------------------
# Workflow graph renderer
# ---------------------------------------------------------------------------

def build_workflow_graph_markdown(
    orch: "Orchestration",  # type: ignore[name-defined]
    current_step_id: str,
    exec_counts: dict[str, int] | None = None,
) -> str:
    """
    Build a compact markdown map of the orchestration graph.

    Traverses from entry_step_id, rendering top-level steps as numbered items.
    Branch steps inside parallel/loop blocks appear only as sub-bullets under
    their parent — never as top-level numbered steps.  Cycles are shown as
    "(retry)" references.

    `exec_counts` maps step_id → execution count; steps with count > 1 get
    a "(×N)" suffix so the LLM sees how many times each step has run.

    Example output:
        ### WORKFLOW: Research Mid term stock
        Goal: Analyse and research mid term stocks

        #### Steps
        1. **Parallel Step** [parallel]
           ├─ Branch 1: Stock Analyser Alpha → `analysis_1`
           └─ Branch 2: Stock analyser beta → `analysis_2`   ← YOU ARE HERE
        2. **Merge Step** [merge] → `all_resources`
        3. **Final Verdict** [llm] → `final_result`
    """
    from core.models_orchestration import StepType

    step_map = {s.id: s for s in orch.steps}

    def _type_val(step) -> str:
        """Always return the plain string value (e.g. 'parallel'), never the enum repr."""
        t = step.type
        return t.value if hasattr(t, "value") else str(t)

    # ------------------------------------------------------------------
    # Walk the graph to determine top-level display order.
    # Branch steps (inside parallel/loop) are added to `seen` but NOT to
    # `ordered_ids` so they don't appear as numbered top-level steps.
    # ------------------------------------------------------------------
    ordered_ids: list[str] = []
    seen: set[str] = set()

    def _mark_branch_seen(step_id: str, stop_at: str | None) -> None:
        """Recursively mark branch body steps as seen without numbering them."""
        if not step_id or step_id in seen or step_id == stop_at or step_id not in step_map:
            return
        seen.add(step_id)
        _mark_branch_seen(step_map[step_id].next_step_id or "", stop_at)

    def _walk(step_id: str) -> None:
        if not step_id or step_id in seen or step_id not in step_map:
            return
        seen.add(step_id)
        ordered_ids.append(step_id)
        step = step_map[step_id]
        t = _type_val(step)

        if t == StepType.EVALUATOR.value and step.route_map:
            for target in step.route_map.values():
                if target and target not in seen:
                    _walk(target)

        elif t == StepType.PARALLEL.value and step.parallel_branches:
            # Mark every branch step as seen so they won't become top-level
            for branch in step.parallel_branches:
                for bid in branch:
                    _mark_branch_seen(bid, step.next_step_id)
            _walk(step.next_step_id or "")

        elif t == StepType.LOOP.value and step.loop_step_ids:
            for bid in step.loop_step_ids:
                _mark_branch_seen(bid, step.next_step_id)
            _walk(step.next_step_id or "")

        else:
            _walk(step.next_step_id or "")

    _walk(orch.entry_step_id)

    # Append anything not reachable from entry (shouldn't happen in valid graphs)
    for s in orch.steps:
        if s.id not in seen:
            ordered_ids.append(s.id)

    num: dict[str, int] = {sid: i + 1 for i, sid in enumerate(ordered_ids)}

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------
    lines: list[str] = [f"### WORKFLOW: {orch.name}"]
    if orch.description:
        lines.append(f"Goal: {orch.description}")
    lines.append("")
    lines.append("#### Steps")

    counts = exec_counts or {}

    for sid in ordered_ids:
        step = step_map[sid]
        t = _type_val(step)
        here = "   ← YOU ARE HERE" if sid == current_step_id else ""
        out = f" → `{step.output_key}`" if step.output_key else ""
        run_count = counts.get(sid, 0)
        runs = f" (×{run_count})" if run_count > 1 else ""

        if t == StepType.END.value:
            lines.append(f"{num[sid]}. **{step.name}** [END]{runs}{here}")
            continue

        lines.append(f"{num[sid]}. **{step.name}** [{t}]{out}{runs}{here}")

        # Evaluator: named routes with cycle detection
        if t == StepType.EVALUATOR.value and step.route_map:
            items = list(step.route_map.items())
            for idx, (decision, target_id) in enumerate(items):
                connector = "└─" if idx == len(items) - 1 else "├─"
                if target_id is None:
                    lines.append(f'   {connector} "{decision}" → [END]')
                elif target_id in num:
                    target_name = step_map[target_id].name
                    suffix = " (retry)" if num[target_id] <= num[sid] else ""
                    lines.append(f'   {connector} "{decision}" → {target_name}{suffix}')
                else:
                    lines.append(f'   {connector} "{decision}" → {target_id}')

        # Parallel: show each branch step with its output key
        elif t == StepType.PARALLEL.value and step.parallel_branches:
            all_branches = step.parallel_branches
            for b_idx, branch in enumerate(all_branches):
                connector = "└─" if b_idx == len(all_branches) - 1 else "├─"
                branch_parts = []
                for bid in branch:
                    if bid not in step_map:
                        continue
                    bs = step_map[bid]
                    bs_out = f" → `{bs.output_key}`" if bs.output_key else ""
                    bs_here = "   ← YOU ARE HERE" if bid == current_step_id else ""
                    branch_parts.append(f"{bs.name}{bs_out}{bs_here}")
                lines.append(f"   {connector} Branch {b_idx + 1}: {' → '.join(branch_parts)}")

        # Loop: show body with iteration count
        elif t == StepType.LOOP.value and step.loop_step_ids:
            body_names = " → ".join(
                step_map[bid].name for bid in step.loop_step_ids if bid in step_map
            )
            lines.append(f"   └─ body ({step.loop_count}×): {body_names}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def _origin_label(origin: dict | None, execution: int) -> str:
    """Human-readable label for what triggered a memorised execution."""
    if not origin:
        return "initial invocation" if execution == 1 else "re-invocation"
    otype = origin.get("type")
    if otype == "entry" or execution == 1:
        return "initial invocation"
    if otype == "evaluator":
        decision = origin.get("routing_decision")
        if decision:
            return f'evaluator routed back: "{decision}"'
        return "evaluator routed back"
    if otype == "loop":
        it = origin.get("loop_iteration")
        total = origin.get("loop_total")
        if it and total:
            return f"loop iteration {it} of {total}"
        if it:
            return f"loop iteration {it}"
        return "loop iteration"
    if otype == "human_response":
        return "after human input"
    if otype == "linear":
        prev = origin.get("from_step_name")
        return f'after "{prev}" (linear)' if prev else "linear re-entry"
    return otype or "re-invocation"


def _render_full_history(memory: list[dict], skip_last_output: bool = False) -> str:
    """Render every recorded turn as inputs -> tools -> output.

    `skip_last_output`: when the caller is also rendering the most recent
    turn's output as a standalone YOUR PREVIOUS OUTPUT section, set this to
    True so the history doesn't duplicate that body. The last turn still
    shows its tools/inputs/label; only the Output: block is replaced with
    a pointer back to the standalone section.
    """
    lines: list[str] = ["## REVISION HISTORY (all prior turns)"]
    last_idx = len(memory) - 1
    for idx, entry in enumerate(memory):
        execution = entry.get("execution", 0)
        origin = entry.get("origin") or {}
        label = _origin_label(origin, execution)
        lines.append(f"\n### Turn {execution} - {label}")

        # Inputs at the time of this turn
        inputs = entry.get("inputs") or {}
        if inputs:
            lines.append("Inputs:")
            for k, v in inputs.items():
                lines.append(f"  - {k}: {v}")

        # Evaluator feedback that triggered this turn (if any)
        reasoning = origin.get("routing_reasoning")
        if reasoning:
            lines.append(f'Evaluator feedback: "{reasoning}"')

        # Tool calls
        trace = entry.get("trace") or {}
        tool_calls = trace.get("tool_calls") or []
        if tool_calls:
            lines.append(f"Tools used ({len(tool_calls)} calls):")
            lines.append(_format_tool_calls(tool_calls))
        else:
            tools_used = trace.get("tools_used") or []
            if tools_used:
                lines.append(f"Tools used: {', '.join(tools_used)}")
            else:
                lines.append("Tools used: (none)")

        # Final output produced on that turn
        final_out = str(trace.get("final_output") or "")
        if final_out:
            if skip_last_output and idx == last_idx:
                lines.append("Output: (see YOUR PREVIOUS OUTPUT section above)")
            else:
                if len(final_out) > 800:
                    from .summarizer import smart_truncate
                    final_out = smart_truncate(final_out, 800)
                lines.append(f"Output:\n{final_out}")

    return "\n".join(lines)


def _render_last_attempt(last: dict) -> str:
    """Render only the most recent turn (legacy behavior when include_full_history is off)."""
    trace = last.get("trace", {})
    lines = ["## YOUR PREVIOUS WORK ON THIS TASK"]
    tool_calls = trace.get("tool_calls", [])
    if tool_calls:
        lines.append(
            f"Tools used ({len(tool_calls)} calls):\n"
            + _format_tool_calls(tool_calls)
        )
    else:
        tools_used = trace.get("tools_used", [])
        if tools_used:
            lines.append(f"Tools used: {', '.join(tools_used)}")
        else:
            lines.append("No tools were used.")
    return "\n".join(lines)


def _format_tool_calls(tool_calls: list[dict]) -> str:
    """Format tool call list into a compact readable block."""
    lines = []
    for tc in tool_calls:
        name = tc.get("name", "?")
        args = tc.get("args", {})
        # Show first arg value or stringify compactly
        if isinstance(args, dict) and args:
            first_key, first_val = next(iter(args.items()))
            arg_str = f'{first_key}="{str(first_val)[:80]}"'
            if len(args) > 1:
                arg_str += f" (+{len(args)-1} more)"
        else:
            arg_str = str(args)[:80]
        preview = tc.get("result_preview", "")
        line = f"  - {name}({arg_str})"
        if preview:
            line += f" → {preview[:120]}"
        lines.append(line)
    return "\n".join(lines)


def _format_context_value(
    key: str,
    val,
    label: str,
    max_chars: int = 5000,
) -> str:
    """Format a single shared_state value for inclusion in the prompt.

    Format:
        ### <key>
        Source: <producer agent or upstream description>
        <value>

    The bare key (no brackets) is used as the header so the heading cannot be
    visually confused with `{state.<key>}` template placeholders. The Source
    line carries provenance (which agent / step produced this value).
    """
    # List values from loop/parallel accumulation
    if isinstance(val, list) and val and isinstance(val[0], dict) and "result" in val[0]:
        parts = []
        for entry in val:
            iteration = entry.get("iteration", entry.get("run", ""))
            agent = entry.get("agent", "")
            result_str = str(entry.get("result", ""))
            if len(result_str) > max_chars:
                from .summarizer import smart_truncate
                result_str = smart_truncate(result_str, max_chars)
            iter_suffix = f" (iteration {iteration})" if iteration else ""
            source = f"{agent} → {key}" if agent else f"loop accumulator → {key}"
            parts.append(f"### {key}{iter_suffix}\nSource: {source}\n{result_str}")
        return "\n\n".join(parts)

    val_str = str(val)
    if len(val_str) > max_chars:
        from .summarizer import smart_truncate
        val_str = smart_truncate(val_str, max_chars)
    source = label if label and label != key else "shared state"
    return f"### {key}\nSource: {source}\n{val_str}"


def build_origin_aware_context(
    step: "StepConfig",
    run: "OrchestrationRun",
    engine: "OrchestrationEngine",
    transition: TransitionContext,
) -> tuple[str, str, str]:
    """
    Build (prompt, system_prompt_extra, system_prompt_prefix) for an
    agent/tool/llm step using the structured, origin-aware format.

    Returns three strings:
      prompt               full user-turn message to send to the LLM
      system_prompt_extra  short orchestration-awareness block APPENDED to
                           the agent's system prompt (can be empty)
      system_prompt_prefix iteration banner PREPENDED to the system prompt
                           on re-runs (execution_number > 1); empty otherwise
    """
    import re
    sections: list[str] = []
    is_rerun = transition.execution_number > 1

    # ------------------------------------------------------------------
    # Section: ROLE
    # ------------------------------------------------------------------
    orch_name = engine.orch.name
    step_name = step.name
    output_key = step.output_key or "(no output key set)"

    role_lines = [
        f"## YOUR ROLE IN THIS WORKFLOW",
        f"Workflow: \"{orch_name}\"",
        f"Step: \"{step_name}\"",
        f"Your output will be stored as: \"{output_key}\"",
    ]

    if transition.origin_type == "entry":
        role_lines.append("You are the first step — no prior steps have run.")
    elif transition.origin_type == "linear":
        prev = transition.from_step_name or transition.from_step_id or "previous step"
        agent = transition.from_agent_name
        invoked_by = f"\"{prev}\"" + (f" (agent: {agent})" if agent else "")
        role_lines.append(f"Invoked after: {invoked_by} (linear flow).")
    elif transition.origin_type == "evaluator":
        role_lines.append(
            f"This is execution #{transition.execution_number} of this step."
        )
        role_lines.append(
            f"You were sent back by evaluator \"{transition.from_step_name}\"."
        )
    elif transition.origin_type == "loop":
        iter_str = (
            f"Loop iteration {transition.loop_iteration} of {transition.loop_total}"
            if transition.loop_iteration and transition.loop_total
            else "Loop iteration"
        )
        role_lines.append(f"{iter_str}. Invoked by loop step \"{transition.from_step_name}\".")
    elif transition.origin_type == "human_response":
        role_lines.append(
            f"Invoked after human input step \"{transition.from_step_name}\"."
        )

    sections.append("\n".join(role_lines))

    # ------------------------------------------------------------------
    # Section: MAIN GOAL (always present)
    # The user's original request is the root of every step in this
    # workflow. Keep it at the top of every prompt so re-runs, branching,
    # and deep step chains do not lose sight of what we are ultimately
    # solving for. Sourced from run.shared_state["user_input"] which
    # the engine seeds on the first step.
    # ------------------------------------------------------------------
    main_goal = run.shared_state.get("user_input")
    if main_goal:
        goal_text = str(main_goal).strip()
        if len(goal_text) > 2000:
            from .summarizer import smart_truncate
            goal_text = smart_truncate(goal_text, 2000)
        sections.append(
            "## MAIN GOAL (original user request)\n"
            "Everything this workflow does exists to solve this. "
            "Keep it in mind when deciding what to produce:\n\n"
            f"{goal_text}"
        )

    # ------------------------------------------------------------------
    # Section: WHY YOU ARE RUNNING AGAIN (any re-run, any origin)
    # ------------------------------------------------------------------
    if is_rerun:
        why_lines = [
            "## WHY YOU ARE RUNNING AGAIN",
            f"This is execution #{transition.execution_number} of this step.",
        ]
        if transition.origin_type == "evaluator":
            if transition.routing_decision:
                why_lines.append(f"Evaluator decision: \"{transition.routing_decision}\"")
            if transition.routing_reasoning:
                why_lines.append(f"Evaluator reasoning: {transition.routing_reasoning}")
            why_lines.append(
                "An evaluator reviewed your previous output and routed you back. "
                "See HOW TO PROCEED below."
            )
        elif transition.origin_type == "loop":
            iter_str = (
                f"iteration {transition.loop_iteration} of {transition.loop_total}"
                if transition.loop_iteration and transition.loop_total
                else f"iteration #{transition.execution_number}"
            )
            why_lines.append(
                f"You are inside a loop ({iter_str}). See HOW TO PROCEED below."
            )
        else:
            why_lines.append(
                "The workflow has routed control back to this step. See HOW TO PROCEED below."
            )
        sections.append("\n".join(why_lines))

    # ------------------------------------------------------------------
    # Section: YOUR PREVIOUS OUTPUT (any re-run)
    # ------------------------------------------------------------------
    if is_rerun and step.output_key and step.output_key in run.shared_state:
        prev_out = str(run.shared_state[step.output_key])
        if len(prev_out) > 2000:
            from .summarizer import smart_truncate
            prev_out = smart_truncate(prev_out, 2000)
        sections.append(
            "## YOUR PREVIOUS OUTPUT\n"
            "This is what you produced last turn. Read it before responding:\n\n"
            f"{prev_out}"
        )

    # ------------------------------------------------------------------
    # Section: YOUR PREVIOUS WORK / REVISION HISTORY (on any re-invocation)
    # ------------------------------------------------------------------
    if is_rerun:
        memory = get_execution_memory(run, step.id)
        if memory:
            use_full = (
                step.include_full_history
                if step.include_full_history is not None
                else True
            )
            # Detect whether the standalone YOUR PREVIOUS OUTPUT section was
            # rendered above (same condition used there). If so, avoid
            # duplicating the most recent turn's body in the history.
            previous_output_rendered = bool(
                step.output_key and step.output_key in run.shared_state
            )
            if use_full:
                # With only one prior turn, the history would just restate
                # YOUR PREVIOUS OUTPUT under a turn label. Skip it then.
                if len(memory) > 1:
                    sections.append(_render_full_history(
                        memory, skip_last_output=previous_output_rendered
                    ))
            else:
                # Last-attempt mode shows only tools/inputs (no output body),
                # so it never duplicates YOUR PREVIOUS OUTPUT.
                sections.append(_render_last_attempt(memory[-1]))

    # ------------------------------------------------------------------
    # Section: HUMAN INPUT (when invoked after a human step)
    # ------------------------------------------------------------------
    if transition.origin_type == "human_response" and transition.human_response_key:
        hkey = transition.human_response_key
        human_val = run.shared_state.get(hkey)
        if human_val:
            sections.append(
                f"## HUMAN INPUT\n"
                f"The user provided the following response:\n{human_val}"
            )

    # ------------------------------------------------------------------
    # Section: PREVIOUS ITERATIONS (loop context)
    # ------------------------------------------------------------------
    if transition.origin_type == "loop" and step.output_key:
        loop_key = f"_loop_{step.output_key}"
        loop_results = run.shared_state.get(loop_key, [])
        if loop_results:
            iter_lines = ["## PREVIOUS ITERATIONS"]
            for entry in loop_results[-5:]:  # last 5 to keep it manageable
                it = entry.get("iteration", "?")
                res = str(entry.get("result", ""))[:200]
                iter_lines.append(f"Iteration {it}: {res}")
            sections.append("\n".join(iter_lines))

    # ------------------------------------------------------------------
    # Section: CONTEXT FROM PREVIOUS STEPS
    # ------------------------------------------------------------------
    context_parts = []

    # NOTE: user_input is rendered at the top under "## MAIN GOAL" so it is
    # not duplicated here. If a step explicitly lists user_input in its
    # input_keys, the explicit-input loop below will skip it for the same
    # reason.

    # Human response keys (always inject unless already listed)
    human_keys = {"human_response"}
    for s in engine.step_map.values():
        if s.type and s.type.value == "human" and s.output_key:
            human_keys.add(s.output_key)
    for hkey in sorted(human_keys):
        if (
            hkey in run.shared_state
            and hkey not in (step.input_keys or [])
            and hkey != "user_input"
        ):
            val = str(run.shared_state[hkey])
            if len(val) > 3000:
                from .summarizer import smart_truncate
                val = smart_truncate(val, 3000)
            context_parts.append(f"### {hkey}\nSource: human response\n{val}")

    # Explicitly declared input_keys (skip user_input — shown under MAIN GOAL)
    for key in (step.input_keys or []):
        if key not in run.shared_state or key == "user_input":
            continue
        val = run.shared_state[key]
        label = key
        producer = next(
            (s for s in engine.step_map.values() if s.output_key == key), None
        )
        if producer and producer.agent_id and producer.agent_id in engine.agent_names:
            label = f"{engine.agent_names[producer.agent_id]} → {key}"
        context_parts.append(_format_context_value(key, val, label))

    if context_parts:
        sections.append("## CONTEXT FROM PREVIOUS STEPS\n" + "\n\n".join(context_parts))

    # ------------------------------------------------------------------
    # Section: HOW TO PROCEED (any re-run)
    # Loose framing: agent decides between refine / redo / push back. The
    # only firm requirement is that the new output explain the change (or
    # the deliberate non-change) relative to the previous attempt.
    # ------------------------------------------------------------------
    if is_rerun:
        if transition.origin_type == "loop":
            proceed_block = (
                "## HOW TO PROCEED\n"
                "This is another iteration of a loop. Your previous iteration's output and "
                "any sibling iterations are shown above.\n\n"
                "Use your judgement: produce the next item, refine, or take a different angle. "
                "Just don't silently re-emit what you produced before.\n\n"
                "At the top of your output, include a brief note (1-3 lines) explaining what "
                "this iteration adds or changes relative to the previous one."
            )
        else:
            proceed_block = (
                "## HOW TO PROCEED\n"
                "You are running this step again. Your previous output and the reason for re-running "
                "are shown above. Read them before responding.\n\n"
                "You decide how to respond — any of these are valid:\n"
                "  - Refine the previous output (small targeted edits).\n"
                "  - Redo it from scratch if it was fundamentally off.\n"
                "  - Push back if you believe the feedback is mistaken: explain why and either "
                "defend the previous output or propose a different correction.\n\n"
                "Whichever path you take, include a brief reasoning note at the top of your output "
                "(1-3 lines) that explains:\n"
                "  - What changed from your previous output (or why you kept it), and\n"
                "  - Why you believe this is the right response to the feedback."
            )
        sections.append(proceed_block)

    # ------------------------------------------------------------------
    # Section: TASK (first run) / ORIGINAL TASK reference (re-run)
    # ------------------------------------------------------------------
    prompt_template = step.prompt_template or run.shared_state.get("user_input", "")

    # Replace {state.key} references
    def replace_ref(match):
        k = match.group(1)
        return str(run.shared_state.get(k, f"{{state.{k}}}"))

    task_text = re.sub(r"\{state\.(\w+)\}", replace_ref, prompt_template)

    if is_rerun:
        task_header = "## ORIGINAL TASK (reference only)"
        task_suffix = (
            "\n\nThis is the original task statement, included for reference. "
            "Use it together with YOUR PREVIOUS OUTPUT and HOW TO PROCEED above to decide your response."
        )
    else:
        task_header = "## TASK"
        task_suffix = ""
        if transition.origin_type == "human_response":
            task_suffix = "\n\nIncorporate the human's input above."

    sections.append(f"{task_header}\n{task_text}{task_suffix}")

    # ------------------------------------------------------------------
    # Assemble final prompt
    # ------------------------------------------------------------------
    prompt = "\n\n---\n\n".join(sections)

    # ------------------------------------------------------------------
    # System prompt addition — workflow graph + step position
    # (Date/time is injected separately by build_system_prompt for every
    # agent including orchestration steps — do not duplicate it here.)
    # ------------------------------------------------------------------
    # Count completed executions per step for the graph (×N badges).
    exec_counts: dict[str, int] = {}
    for h in run.step_history:
        sid = h.get("step_id")
        if sid:
            exec_counts[sid] = exec_counts.get(sid, 0) + 1
    # Use execution_number for the active step (counts this in-flight run).
    if is_rerun:
        exec_counts[step.id] = transition.execution_number

    graph_md = build_workflow_graph_markdown(engine.orch, step.id, exec_counts)
    sys_lines = [
        graph_md,
        "",
        f"You are currently executing step **\"{step_name}\"** (execution #{transition.execution_number}).",
        f"Your output will be stored as: `{output_key}`",
    ]
    if transition.origin_type == "evaluator":
        sys_lines.append(
            f"You are revising your previous output based on evaluator feedback "
            f"(\"{transition.routing_decision}\")."
        )
    system_prompt_extra = "\n".join(sys_lines)

    # ------------------------------------------------------------------
    # System prompt PREFIX: iteration banner (re-runs only).
    # Prepended to the system prompt so a long agent role description
    # cannot drown out the iteration signal.
    # ------------------------------------------------------------------
    system_prompt_prefix = ""
    if is_rerun:
        why_short = ""
        if transition.origin_type == "evaluator" and transition.routing_decision:
            why_short = f" (evaluator routed back: \"{transition.routing_decision}\")"
        elif transition.origin_type == "loop" and transition.loop_iteration:
            why_short = f" (loop iteration {transition.loop_iteration})"
        system_prompt_prefix = (
            "ITERATION CONTEXT - READ BEFORE RESPONDING\n"
            f"You are on execution #{transition.execution_number} of step "
            f"\"{step_name}\"{why_short}. This is NOT your first attempt.\n"
            "In the user message below, read YOUR PREVIOUS OUTPUT, WHY YOU ARE RUNNING AGAIN, "
            "and HOW TO PROCEED. Then decide how to respond — refine, redo, or push back on the "
            "feedback if you think it is mistaken. Whichever you choose, start your output with a "
            "brief reasoning note explaining what changed (or why you kept the previous output)."
        )

    return prompt, system_prompt_extra, system_prompt_prefix
