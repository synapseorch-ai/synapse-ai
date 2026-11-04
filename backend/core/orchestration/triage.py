"""Why a run failed, read by code, so Synapse AI states the cause and offers the fix (F1260, REACH1; D340 R1).

Naveen, 10-30: "see logs to triage something". A run's view says *that* it failed and its last error; the planner then
guessed the rest from a sentence. `failure` reads the run once, in bounded reads, and says what a fix needs:

* **the failing step**: its id, name and type, and for an agent step the agent and the model it ran on;
* **its error**, as the run records it (secrets scrubbed, cut), and a model failure's `code` (F275);
* **a class**, typed from what the run recorded, never from a model's reading: a model's failure by its code, the
  run's cap, its turn limit, a time limit, a tool that failed (and its HTTP status, when it said one), a route with
  nowhere to go, a person's stop, else `other`;
* **the fixes**: the change ops that answer that class (`FIXES`), each one Synapse AI's change card; and whether the
  run can resume from the step once fixed (the replay's own rule, `replay.why_not`);
* **what the step last did**: the end of its transcript, within one vault read (`vault.read_cap`), and its tools; a
  saved output (D339 P4) is its excerpt and its file's key, never loaded whole into the answer.

Read-only and plain: the run's checkpoint (`SharedState.restore`), as the run page reads it, on any host.
"""
from __future__ import annotations

import re
from typing import Any

#: The ops that fix each class, Synapse AI's change cards (`change_ops`), in the order to offer them. `resume_run`
#: after a fix runs the run on from the step that failed.
FIXES: dict[str, tuple[str, ...]] = {
    "model_quota": ("set_agent_model",),
    "model_refused": ("set_agent_model",),
    "model_busy": ("resume_run", "set_agent_model"),
    "model_no_answer": ("set_agent_model", "set_agent_instructions"),
    "model_error": ("set_agent_model",),
    "run_cap": ("update_workflow",),
    "turn_limit": ("update_workflow",),
    "time_limit": ("update_workflow",),
    "tool_auth": ("app_reconnect", "set_agent_tools"),
    # A tool that isn't available (F1349): its app reconnected, or another tool, then the run resumed from the step.
    "tool_unavailable": ("app_reconnect", "edit_steps", "set_agent_tools", "resume_run"),
    "tool_input": ("edit_steps", "set_agent_instructions"),
    # A Tool step's own HTTP tool found nothing at its address (F1378): the tool's address changed, then the run on.
    "tool_address": ("update_tool", "resume_run"),
    "tool_down": ("resume_run",),
    "tool": ("edit_steps", "set_agent_tools"),
    "route": ("edit_steps",),
    "stopped": ("resume_run",),
    "other": ("edit_steps",),
}

#: What each class means, said beside it so the cause is read the same every time.
MEANS: dict[str, str] = {
    "model_quota": "the model's provider refused for credit or quota: a retry won't help",
    "model_refused": "the model's provider refused the call: its key, the model's id or what was sent",
    "model_busy": "the model's provider was down, overloaded or rate-limited: it may pass on its own",
    "model_no_answer": "the model answered with nothing usable (empty, blocked or all reasoning)",
    "model_error": "the model call failed",
    "run_cap": "the run reached the spending cap its workflow sets",
    "turn_limit": "the run used every turn its workflow allows",
    "time_limit": "the step or the run ran out of time",
    "tool_auth": "a tool's service refused its sign-in or key",
    "tool_unavailable": "a tool the step names isn't available: its app is disconnected, needs signing in again or is "
                        "turned off, or the tool is gone; it never ran, so the run can resume once it is back",
    "tool_input": "a tool's service refused what was sent to it",
    "tool_address": "the tool's address finds nothing at its service (404 or 410): its URL or path is wrong",
    "tool_down": "a tool's service failed on its side",
    "tool": "a tool reported a failure",
    "route": "a step had nowhere to go next",
    "stopped": "a person stopped the run",
    "other": "the step failed",
}

_MODEL = {"quota_exhausted": "model_quota", "provider_refused": "model_refused", "rate_limited": "model_busy",
          "provider_unavailable": "model_busy", "model_no_answer": "model_no_answer", "model_error": "model_error"}
_STATUS = re.compile(r"\b(?:HTTP |status(?: code)?:? )([45]\d\d)\b", re.IGNORECASE)
#: How much of an error is said.
ERROR_CHARS = 600
#: How many of the step's tools are named.
TOOLS = 12


def classify(error: str, entry: dict | None, *, status: str | None) -> tuple[str, int | None]:
    """The class of a failure, and a tool's HTTP status when its error said one."""
    entry = entry or {}
    code = (entry.get("model_failure") or {}).get("code")
    if code:
        return _MODEL.get(code, "model_error"), None
    text = error or ""
    if text.startswith("Stopped: this run reached its cap"):
        return "run_cap", None
    if text.startswith("Global turn limit"):
        return "turn_limit", None
    if re.search(r"\btimed out\b", text, re.IGNORECASE) and not text.startswith("ToolCallFailed"):
        return "time_limit", None
    if text.startswith("RouteFailed") or "no default" in text or "Max iterations" in text:
        return "route", None
    from core.tool_availability import LEAD

    if text.startswith(LEAD):
        return "tool_unavailable", None
    if text.startswith("ToolCallFailed") or entry.get("step_type") == "tool":
        found = _STATUS.search(text)
        http = int(found.group(1)) if found else None
        if http in (401, 403):
            return "tool_auth", http
        if http is not None and http >= 500:
            return "tool_down", http
        if http is not None:
            return "tool_input", http
        return "tool", None
    if status == "cancelled":
        return "stopped", None
    return "other", None


def _failing_entry(history: list[dict], current: str | None) -> dict | None:
    for entry in reversed(history or []):
        if isinstance(entry, dict) and (entry.get("error") or entry.get("status") == "failed"):
            return entry
    return next((e for e in reversed(history or []) if isinstance(e, dict) and e.get("step_id") == current), None)


def _tail(text: str, cap: int) -> str:
    """The end of a transcript, within `cap`, from a line's start."""
    if len(text) <= cap:
        return text
    cut = text[-cap:]
    return "…" + (cut.split("\n", 1)[1] if "\n" in cut[:200] else cut)


def _clean(text: str) -> str:
    """Neither a secret this run holds nor anything shaped like a key or token: the run reader's own (`run_error`)."""
    from core.orchestration.run_error import clean

    return clean(text)


def _said(value: Any) -> str:
    from core import step_refs

    return step_refs.for_person(value) if step_refs.is_ref(value) else str(value)


async def _agent_of(step: dict) -> dict:
    agent_id = step.get("agent_id")
    if not agent_id:
        return {}
    from core.store.resources import get_agent

    agent = await get_agent(agent_id) or {}
    return {"agent": agent.get("name") or agent_id, "model": agent.get("model") or "(the workspace's default)"}


async def _steps_of(run: Any) -> list[dict]:
    """The steps the run ran: its own definition (F443), else, for a run kept without one, its workflow as saved."""
    steps = (run.definition or {}).get("steps")
    if not steps:
        from core.store.resources import get_orchestration

        steps = ((await get_orchestration(run.orchestration_id)) or {}).get("steps")
    return [s for s in steps or [] if isinstance(s, dict)]


#: The statuses that say an address finds nothing there.
_NOT_THERE = (404, 410)


async def _addressed(kind: str, http: int | None, step: dict, entry: dict) -> str:
    """`tool_address` for a Tool step whose own tool is an HTTP tool of this workspace answering 404 or 410 (F1378):
    its address is what is wrong. An agent's tool (an app's or a server's) or a model answering so keeps its class."""
    if kind != "tool_input" or http not in _NOT_THERE or (entry.get("step_type") or step.get("type")) != "tool":
        return kind
    name = step.get("forced_tool") or step.get("tool")
    if not name:
        return kind
    from core.store.resources import load_tools

    tools = await load_tools()
    return "tool_address" if any(t.get("name") == name and t.get("tool_type") == "http" for t in tools) else kind


async def failure(run_id: str) -> dict | None:
    """The failure block of a failed or stopped run; None for one still going, done, or not here."""
    from core import vault
    from core.orchestration.replay import why_not
    from core.orchestration.run_error import failure_reason
    from core.orchestration.state import SharedState

    try:
        run = (await SharedState.restore(run_id)).run
    except FileNotFoundError:
        return None
    if run.status not in ("failed", "cancelled"):
        return None
    error = failure_reason(run.status, run.step_history, run.step_progress) or ""
    entry = _failing_entry(run.step_history, run.current_step_id) or {}
    kind, http = classify(error, entry, status=run.status)
    step_id = entry.get("step_id") or run.current_step_id
    steps = {s.get("id"): s for s in await _steps_of(run)}
    step = steps.get(step_id) or {}
    kind = await _addressed(kind, http, step, entry)
    progress = (run.step_progress or {}).get(step_id) or {}
    cap = vault.read_cap()
    out: dict[str, Any] = {
        "step": {"id": step_id, "name": entry.get("step_name") or step.get("name"),
                 "type": entry.get("step_type") or step.get("type"), **await _agent_of(step)},
        "error": _clean(error)[:ERROR_CHARS],
        "class": kind, "means": MEANS[kind], "fixes": list(FIXES[kind]),
        **({"http_status": http} if http else {}),
        **({"model_failure": entry["model_failure"]["code"]} if (entry.get("model_failure") or {}).get("code")
           else {}),
    }
    resume = await why_not(run)
    out["resumes"] = resume is None
    if resume:
        out["resume_refused"] = resume
    if isinstance(progress, dict):
        if progress.get("tools_used"):
            out["tools_used"] = list(progress["tools_used"])[-TOOLS:]
        if isinstance(progress.get("context_text"), str) and progress["context_text"].strip():
            out["last_said"] = _clean(_tail(progress["context_text"], cap))
    output_key = step.get("output_key")
    if output_key and output_key in (run.shared_state or {}):
        out["its_output"] = _clean(_said(run.shared_state[output_key]))[:cap]
    return out
