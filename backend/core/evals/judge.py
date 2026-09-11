"""
The judge: one model call that scores one finished run against one rubric.

Deliberately **one** call, not one per criterion and not one per step. The
model is asked for an overall score, a score per criterion and a score per
step in a single structured answer. Three reasons, in the order they mattered:
cost is the whole argument for this product over an autonomous framework and N
calls per run is the easiest way to lose it; a judge that sees the whole run at
once can say "step 2 is where this went wrong", which a per-step call cannot;
and one call is one place for a parse to fail.

What the judge is allowed to read
---------------------------------
Only what is **durable in both products**, which is a narrower set than it
first appears:

* `shared_state` — every step's output, keyed. Durable, it is a column.
* `step_history` — the timeline: name, type, status, duration, error.
* `_exec_memory_<step_id>` inside `shared_state` — per execution, the agent,
  what triggered it, the inputs it saw, every tool call with its arguments and
  a result preview, the turn count, and the final output. This is the
  turn-level evidence, and it is durable because it rides in a column.

Explicitly **not** the run event journal. That is scratch (`scratch_dir`),
capped at 20 000 events, and truncates exactly the keys a judge wants; on a
fleet its counterpart is a Redis stream that expires in an hour. A judge built
on it would work on a laptop and quietly score nothing in production, which is
the same class of mistake `worker/notify.py` documents in cloud.

Nothing here is instrumentation. Every input above already existed.
"""
from __future__ import annotations

import asyncio
import json
import time

from core.log import get_logger

_log = get_logger(__name__)

#: A judge that has not answered by now is not going to. The run is already
#: finished and is waiting on this, so the ceiling is a promise to the person
#: watching, not a tuning knob — see `core/evals/__init__` and D84.
TIMEOUT_SECONDS = 30.0

#: Bounds on what reaches the prompt. Generous enough that a judge is reading
#: the work rather than a summary of it, tight enough that a runaway state does
#: not turn one call into a bill.
MAX_OUTPUT_CHARS = 6_000
MAX_STATE_VALUE_CHARS = 1_500
MAX_STATE_KEYS = 25
MAX_STEPS = 40
MAX_TOOL_CALLS_PER_STEP = 12

SYSTEM_PROMPT = (
    "You are a strict, consistent evaluator of automated workflow runs. "
    "You judge only what you are shown. Output ONLY valid JSON."
)


def _truncate(text: str, limit: int) -> str:
    from core.orchestration.summarizer import smart_truncate
    text = text if isinstance(text, str) else str(text)
    return smart_truncate(text, limit) if len(text) > limit else text


def final_output_of(run) -> str:
    """What the run produced, re-derived from durable state.

    The engine computes this at finalize and *emits* it; it is never stored.
    Rather than duplicate the walk, this mirrors `_build_final_response`'s rule
    — the last step whose `output_key` is present in `shared_state` — so the
    judge scores the same text a person was shown.
    """
    for entry in reversed(run.step_history or []):
        key = entry.get("output_key")
        if key and key in (run.shared_state or {}):
            value = run.shared_state[key]
            return value if isinstance(value, str) else str(value)
    if run.status == "failed":
        for entry in reversed(run.step_history or []):
            if entry.get("error"):
                return f"Run failed: {entry['error']}"
        return "Run failed with no recorded error."
    return ""


def _evidence(run) -> str:
    """The run, rendered for a model to read.

    Ordered so the most important thing is first and the bulk is last: a judge
    that runs out of attention should have already seen the answer.
    """
    parts: list[str] = []

    parts.append(f"RUN STATUS: {run.status}")

    output = final_output_of(run)
    if output:
        parts.append("FINAL OUTPUT:\n" + _truncate(output, MAX_OUTPUT_CHARS))

    history = (run.step_history or [])[:MAX_STEPS]
    if history:
        lines = []
        for entry in history:
            line = (
                f"- [{entry.get('step_id')}] {entry.get('step_name')} "
                f"({entry.get('step_type')}): {entry.get('status')}"
            )
            if entry.get("duration_seconds") is not None:
                line += f", {entry['duration_seconds']}s"
            if entry.get("error"):
                line += f" — error: {str(entry['error'])[:300]}"
            lines.append(line)
        parts.append("STEPS:\n" + "\n".join(lines))

    state = run.shared_state or {}
    visible = [
        (key, value) for key, value in state.items()
        if not str(key).startswith("_")
    ][:MAX_STATE_KEYS]
    if visible:
        lines = [
            f"[{key}] {_truncate(str(value), MAX_STATE_VALUE_CHARS)}"
            for key, value in visible
        ]
        parts.append("STATE:\n" + "\n\n".join(lines))

    traces = _step_traces(run)
    if traces:
        parts.append("WHAT EACH STEP ACTUALLY DID:\n" + "\n\n".join(traces))

    return "\n\n".join(parts)


def _step_traces(run) -> list[str]:
    """Per-step execution detail from `_exec_memory_*`.

    This is the turn-level evidence, and the reason step subscores are possible
    at all. `get_execution_memory` returns the last few executions of a step;
    the most recent is the one that produced the output being judged, so only
    that one is rendered — earlier executions are the loop or the evaluator
    retry that led to it, and including them invites the judge to score work
    that was deliberately discarded.
    """
    from core.orchestration.context import get_execution_memory

    out: list[str] = []
    for entry in (run.step_history or [])[:MAX_STEPS]:
        step_id = entry.get("step_id")
        if not step_id:
            continue
        try:
            history = get_execution_memory(run, step_id)
        except Exception:
            continue
        if not history:
            continue
        latest = history[-1]
        trace = latest.get("trace") or {}
        lines = [f"- [{step_id}] {entry.get('step_name')} (agent: {latest.get('agent') or 'n/a'})"]
        if latest.get("execution", 1) > 1:
            lines.append(f"  attempt {latest['execution']} — earlier attempts were re-run")
        turns = trace.get("turn_count")
        if turns:
            lines.append(f"  turns: {turns}")
        for call in (trace.get("tool_calls") or [])[:MAX_TOOL_CALLS_PER_STEP]:
            args = json.dumps(call.get("args") or {}, default=str)[:300]
            line = f"  tool {call.get('name')}({args})"
            preview = call.get("result_preview")
            if preview:
                line += f" -> {str(preview)[:300]}"
            lines.append(line)
        step_output = trace.get("final_output")
        if step_output:
            lines.append("  output: " + _truncate(str(step_output), MAX_STATE_VALUE_CHARS))
        out.append("\n".join(lines))
    return out


def build_prompt(run, rubric: dict) -> str:
    """The judge's instructions. Ends with the output contract, not the data."""
    scale = rubric.get("scale") or {"min": 0.0, "max": 10.0}
    lo, hi = scale.get("min", 0.0), scale.get("max", 10.0)

    criteria = rubric.get("criteria") or []
    if criteria:
        criteria_text = "\n".join(
            f'  - id "{c["id"]}" — {c["name"]}'
            + (f" (weight {c.get('weight', 1)})" if c.get("weight") not in (None, 1) else "")
            + (f": {c['guidance']}" if c.get("guidance") else "")
            for c in criteria
        )
    else:
        criteria_text = "  - id \"overall\" — Overall quality of this run."

    step_ids = [
        entry.get("step_id") for entry in (run.step_history or [])[:MAX_STEPS]
        if entry.get("step_id")
    ]
    steps_line = ", ".join(f'"{s}"' for s in step_ids) if step_ids else "(none)"

    guidance = rubric.get("description") or ""
    guidance_block = f"EVALUATION GUIDANCE:\n{guidance}\n\n" if guidance else ""

    return (
        f"{guidance_block}"
        f"Score the workflow run below against each criterion, on a scale from "
        f"{lo} to {hi}.\n\n"
        f"CRITERIA:\n{criteria_text}\n\n"
        f"{_evidence(run)}\n\n"
        f"Also give each step its own score on the same scale, so the weakest "
        f"part of the run can be identified. Step ids: {steps_line}\n\n"
        f"Respond with ONLY a JSON object of this exact shape:\n"
        f'{{"score": <overall {lo}-{hi}>, "reasoning": "<two sentences>", '
        f'"criteria": {{"<criterion id>": {{"score": <{lo}-{hi}>, "reasoning": "<one sentence>"}}}}, '
        f'"steps": {{"<step id>": {{"score": <{lo}-{hi}>, "reasoning": "<one sentence>"}}}}}}'
    )


def _clamp(value, lo: float, hi: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, number))


def _extract_json_objects(text: str) -> list[dict]:
    """Every well-formed top-level JSON object in a model's reply.

    Three strategies, cheapest first: the whole reply, the reply with a
    markdown fence stripped, then a brace scan that tolerates a preamble. The
    brace scan tracks string state so a `}` inside a quoted reasoning string
    does not close the object early — which is the failure the naive
    `find('{')`/`rfind('}')` pair hits the moment a judge explains itself.

    All of them, not the first: a model that demonstrates the format before
    answering ("for example {...}, so my verdict is {...}") puts a parseable
    object in front of the real one. The caller takes the **last** object that
    looks like a verdict, because in every phrasing where a reply contains two,
    the conclusion is the one at the end.

    Deliberately not `core.react_engine.parse_tool_call`: that scans for
    objects carrying a `tool` key, and a verdict has none. Reusing it here
    would return nothing, always, and look like a model problem.
    """
    raw = (text or "").strip()
    if not raw:
        return []

    found: list[dict] = []
    candidates = [raw]
    if raw.startswith("```"):
        candidates.append(
            raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        )
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return [parsed]

    depth = 0
    start = -1
    in_string = False
    escape = False
    for index, char in enumerate(raw):
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth:
                depth -= 1
                if depth == 0 and start != -1:
                    try:
                        parsed = json.loads(raw[start:index + 1])
                    except (json.JSONDecodeError, ValueError):
                        start = -1
                        continue
                    if isinstance(parsed, dict):
                        found.append(parsed)
                    start = -1
    return found


def parse_verdict(
    text: str,
    rubric: dict,
    step_ids: set[str] | None = None,
) -> dict | None:
    """Read the judge's answer, or return None if it did not give one.

    Returning None rather than a zero matters: a score of zero is a verdict,
    and recording one because a model wrapped its JSON oddly would flag a
    regression that never happened and poison the baseline for every run after
    it.

    **Subscore keys are checked against what actually exists.** A model is free
    to invent a criterion or a step id — mangle one, echo the output contract
    back, or score a step that never ran — and an unchecked key is stored
    verbatim and then rendered as a subscore for something the run does not
    have. Seen for real: a judge whose reply produced `per_step` entries called
    `"<two sentences>"` and `"score"`. The overall score stays usable when this
    happens, so the unknown keys are dropped rather than the verdict refused.
    """
    scale = rubric.get("scale") or {"min": 0.0, "max": 10.0}
    lo, hi = float(scale.get("min", 0.0)), float(scale.get("max", 10.0))

    scored = [obj for obj in _extract_json_objects(text) if "score" in obj]
    if not scored:
        return None
    payload = scored[-1]

    def _section(raw, allowed: set[str] | None) -> dict:
        out: dict[str, dict] = {}
        if not isinstance(raw, dict):
            return out
        for key, value in raw.items():
            name = str(key).strip()
            if allowed is not None and name not in allowed:
                continue
            if isinstance(value, dict):
                out[name] = {
                    "score": _clamp(value.get("score"), lo, hi),
                    "reasoning": str(value.get("reasoning") or "")[:1000],
                }
            else:
                out[name] = {"score": _clamp(value, lo, hi), "reasoning": ""}
        return out

    criterion_ids = {c["id"] for c in (rubric.get("criteria") or []) if c.get("id")}

    return {
        "score": _clamp(payload.get("score"), lo, hi),
        "reasoning": str(payload.get("reasoning") or "")[:2000],
        "per_criterion": _section(payload.get("criteria"), criterion_ids or None),
        "per_step": _section(payload.get("steps"), step_ids),
    }


async def judge_run(run, rubric: dict, settings: dict) -> dict | None:
    """Score one run. Returns the verdict, or None if the judge did not answer.

    The call is attributed with `source="orchestration:judge"` and the run's own
    id, which is what makes its cost land on the scored run in the usage
    summary — `get_usage_summary` buckets any source beginning `orchestration:`
    by `run_id`. So "what did this eval cost" is answerable without a second
    accounting path, and in cloud the spend settles on the org's MODEL wallet
    through the gateway token the job already holds.
    """
    from core.llm_providers import detect_mode_from_model, generate_response

    model = (
        (rubric.get("judge_model") or "").strip()
        or (settings.get("judge_model") or "").strip()
        or settings.get("model", "mistral")
    )
    if model.strip().lower() == "default":
        model = settings.get("model", "mistral")

    prompt = build_prompt(run, rubric)
    started = time.time()
    try:
        text = await asyncio.wait_for(
            generate_response(
                prompt_msg=prompt,
                sys_prompt=SYSTEM_PROMPT,
                mode=detect_mode_from_model(model),
                current_model=model,
                current_settings=settings,
                session_id=run.session_id,
                agent_id="judge",
                source="orchestration:judge",
                run_id=run.run_id,
            ),
            timeout=TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        _log.warning(f"judge timed out after {TIMEOUT_SECONDS}s for run '{run.run_id}'")
        return None
    except Exception as exc:
        _log.warning(f"judge call failed for run '{run.run_id}': {exc}")
        return None

    step_ids = {
        entry["step_id"] for entry in (run.step_history or []) if entry.get("step_id")
    }
    verdict = parse_verdict(text if isinstance(text, str) else "", rubric, step_ids)
    if verdict is None:
        _log.warning(f"judge returned no usable verdict for run '{run.run_id}'")
        return None

    verdict["latency_seconds"] = round(time.time() - started, 2)
    verdict["judge_model"] = model
    return verdict
