"""Try again on a stronger model (F1018, SAI2-P3, D300; Naveen 10-24, and 10-27 for the press).

A turn that failed, or ended with no answer, and changed nothing, ends with one offer: the same message once more on
a stronger model, at its price, picked by code from what this workspace can call (`models_menu.stronger_than`), so it
never needs a key the workspace lacks or a wallet that cannot pay. Nothing switches on its own.

* **Which turns**: the turn ended with `no_answer` (repeating, no finish, out of turns), its model step failed, or the
  model errored. Not one that answered (F1015's answer turn is an answer), not one a cap or the wallet stopped (a
  stronger model costs more), and not one that changed something (a re-run would do it again: SAI2's rule).
* **The offer** is an event at the end of the turn, before its error (a page stops reading at an error):
  `{"type": "try_stronger", model, name, price, label, retry_of}`.
* **The press** sends the same message on the same chat's stream with `retry_of` (Naveen, 10-27). The server checks
  that turn again, picks the model itself (the browser never names one), runs that one turn on it, and records
  `retried_from`. The next message is on the usual model; no setting changes. A turn is retried at most once, and an
  approval the failed turn waited on is never carried: the re-run is a new turn that asks again.
* **Where**: the Synapse AI panel (a builder run: checked from its stored run) and any agent's chat (checked from the
  conversation's last turn, which a retry must follow at once: the next turn replaces it).
"""
from __future__ import annotations

from typing import Any

#: The event's type.
KIND = "try_stronger"
#: Why a turn stopped that a stronger model cannot mend: a cap or the wallet stopped it, and a stronger one costs more.
CAPPED = frozenset({"run_cap", "spend_limit"})
#: A builder run's state: the run it tries again, and the model it was tried on.
RETRIED_KEY, RETRIED_ON_KEY = "retried_from", "retried_on"

#: What a refused press says.
NOT_THIS_TURN = "That turn can't be tried again: send your message again."
TRIED = "That turn was already tried again on a stronger model."
NOTHING_STRONGER = "There's no stronger model this workspace can use for that now."


async def offer(current: str | None, *, has_tools: bool, vision: bool = False, retry_of: str) -> dict | None:
    """The offer for a failed turn on `current`, or None when nothing stronger fits and can be paid for."""
    from core.native_builder import models_menu as MM

    menu = await MM.menu()
    if not menu or not current:
        return None
    picked = MM.stronger_than(current, "vision" if vision else "simple", has_tools=has_tools, menu=menu)
    if picked is None:
        return None
    name = next((e.name for e in menu if e.id == picked.id), picked.id)
    return {"type": KIND, "model": picked.id, "name": name, "price": picked.reason,
            "label": f"Try again on {name} ({picked.reason})", "retry_of": retry_of}


async def wrote(tool_names: list[str], server_module: Any) -> bool:
    """Whether a turn's calls changed something: any tool it ran that does not declare it only reads, as the policy
    reads tools (TAG1, `builder_guard.acting_tool`). A tool that cannot be looked up counts as one that wrote."""
    from core.builder_access import READS
    from core.routes.tools import load_custom_tools
    from core.tools import resolve_one_tool, tool_hints

    custom = await load_custom_tools()
    for name in dict.fromkeys(tool_names):
        if name in READS:
            continue
        try:
            tool = await resolve_one_tool(server_module, name, {"tools": [name]}, custom)
        except Exception:  # noqa: BLE001 — unknown is not evidence that it only read
            return True
        if tool is None:
            return True
        hints = tool_hints(tool, trusted=getattr(tool, "trusted", True))
        if not (hints.declared and hints.read_only):
            return True
    return False


# ── an agent's chat ─────────────────────────────────────────────────────────────────────────────────────────────────

def chat_failed(final: dict | None, error: dict | None) -> bool:
    """Whether an agent's chat turn failed in a way a stronger model may mend: no answer for a reason other than a
    cap, or the model erred."""
    if error is not None:
        return error.get("kind") == "model"
    no_answer = (final or {}).get("no_answer")
    return isinstance(no_answer, dict) and no_answer.get("why") not in CAPPED


def model_of(agent: dict) -> str | None:
    """The model an agent's chat turn runs on: its own, else the workspace's (`run_agent_step`'s precedence)."""
    from core.config import load_settings

    own = str(agent.get("model") or "").strip()
    return own if own and own.lower() != "default" else (load_settings().get("model") or None)


def offers_for(agent: dict | None) -> bool:
    """A plain agent's chat only: a team's, an orchestrator's or a builder's turn runs on more than one model."""
    from core import team

    return bool(agent) and agent.get("type") not in ("orchestrator", "builder") and team.config_of(agent) is None


def take_chat_offer(session_id: str | None, retry_of: str, agent_key: str) -> dict | str:
    """The offer a press redeems, taken so it is used once, or why not: the conversation's last turn must be the
    failed one (same chat, same agent), still holding its offer."""
    from core import chat_turns

    held = chat_turns.find(session_id) if session_id else None
    if held is None or held.live or held.id != retry_of or held.agent_key != agent_key:
        return NOT_THIS_TURN
    kept = getattr(held, "retry_offer", None)
    if not kept:
        return TRIED if getattr(held, "retried", False) else NOT_THIS_TURN
    held.retry_offer, held.retried = None, True
    return kept


# ── the Synapse AI panel ────────────────────────────────────────────────────────────────────────────────────────────

async def _failed_step(run) -> tuple[Any, str | None] | None:
    """The model step a failed builder run failed in, and the model it ran on; None for any other failure."""
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine, apply_model_override, definition_of
    from core.orchestration.step_models import calls_a_model

    if run.status != "failed" or not run.current_step_id:
        return None
    data = await definition_of(run)
    if not data:
        return None
    orch = Orchestration.model_validate(data)
    state = run.shared_state or {}
    roles = state.get(OrchestrationEngine.MODEL_ROLES_KEY) or {}
    step = next((s for s in orch.steps if s.id == run.current_step_id), None)
    if step is None or not calls_a_model(step) or (step.model_role and roles.get(step.model_role)):
        # A step on a role's own model (the builder's quick steps) is not the run's model to raise.
        return None
    apply_model_override(orch, state.get(OrchestrationEngine.MODEL_OVERRIDE_KEY))
    from core.config import load_settings

    return step, (step.model or load_settings().get("model") or None)


async def panel_offer(run_id: str) -> dict | None:
    """The offer a failed builder run ends with, or None (`_why_not` says why a press would be refused)."""
    from core.orchestration.state import SharedState

    try:
        run = (await SharedState.restore(run_id)).run
    except FileNotFoundError:
        return None
    if await _why_not(run):
        return None
    failed = await _failed_step(run)
    if failed is None:
        return None
    step, current = failed
    return await offer(current, has_tools=step.type.value == "agent", retry_of=run_id)


async def _why_not(run, session_id: str | None = None) -> str | None:
    from core.orchestration import run_writes
    from core.orchestration.run_cap import RunCap, stop_reason

    if session_id is not None and run.session_id != session_id:
        return NOT_THIS_TURN
    if run.status != "failed":
        return NOT_THIS_TURN
    if (run.shared_state or {}).get(run_writes.WRITES_KEY):
        return NOT_THIS_TURN  # it changed something: a re-run would do it again
    if run.shared_state.get(RETRIED_KEY):
        return TRIED  # a turn tried again is not offered again: once, never a chain up the menu
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import definition_of

    data = await definition_of(run)
    cap = RunCap.of(Orchestration.model_validate(data)) if data else None
    if await stop_reason(run.run_id, "agent", cap):
        return NOT_THIS_TURN  # a cap or the wallet stopped it: a stronger model costs more
    return None



async def _retried_before(run) -> bool:
    """Whether a run of the same chat already tried `run` again."""
    from sqlalchemy import select

    from core.store import session
    from core.store.models import OrchestrationRunDB
    from core.tenancy import get_tenant

    async with session() as s:
        states = (await s.execute(
            select(OrchestrationRunDB.shared_state).where(
                OrchestrationRunDB.tenant_id == get_tenant(),
                OrchestrationRunDB.session_id == run.session_id))).scalars().all()
    return any(isinstance(st, dict) and st.get(RETRIED_KEY) == run.run_id for st in states)


async def panel_retry(retry_of: str, session_id: str) -> dict | str:
    """The model a press re-runs the panel's failed turn on, with what to record, or why not: the run is this chat's,
    it failed in a model step on the run's model, changed nothing, was not stopped by a cap, and was not tried again
    before; and something stronger can be paid for now."""
    from core.orchestration.state import SharedState

    try:
        run = (await SharedState.restore(retry_of)).run
    except FileNotFoundError:
        return NOT_THIS_TURN
    if (why := await _why_not(run, session_id)) is not None:
        return why
    if await _retried_before(run):
        return TRIED
    failed = await _failed_step(run)
    if failed is None:
        return NOT_THIS_TURN
    step, current = failed
    made = await offer(current, has_tools=step.type.value == "agent", retry_of=retry_of)
    if made is None:
        return NOTHING_STRONGER
    return {"model": made["model"], RETRIED_KEY: retry_of, RETRIED_ON_KEY: made["model"]}
