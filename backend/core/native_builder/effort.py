"""Before a heavy build, one offer of a stronger model (F1153, SAI2-P3, D300; pro drafts/sai2/EFFORT-CHECK.md).

Naveen, 10-28: before a build runs, judge whether it likely needs a stronger model; if so, offer one. A press builds
it on that model, for that build only; declining, or any reply that goes on, builds it on the workspace's usual model
with no second ask; a failure is said plainly, before F1018's one retry offer.

Code decides, a model only labels. The quick classify (`classify_request`) says `effort` and its own reasons
(`many_steps`, `team`, `nested`); code adds its reasons from facts it holds (`reasons`) and reads heavy from both
(`is_heavy`), never from the model's word alone. The offer (`effort_check`, after the role door on the build route
only) needs: heavy; a stronger model this workspace can call and pay for (`models_menu.stronger_than`, `can_pay`);
a role that may build; and no earlier answer to it in this build, and no F1018 retry. It ends the turn with the
`effort_offer` event and code's words. Its press is the same ask sent again with `effort_of` (`effort_press`): the
server checks it is this chat's latest turn and still open, picks the model itself, and the build runs on it
(`_model_override`; steps pinned to a role keep their role's model). Any other reply is read by `read_turn`: it goes
on with the ask, and the build records `effort_declined`.
"""
from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger(__name__)

#: The event's type.
KIND = "effort_offer"
#: What a build records about its offer: pressed (the turn it was pressed from) or answered otherwise.
ACCEPTED_KEY, DECLINED_KEY = "effort_accepted", "effort_declined"
#: The press's own turn, and only it: it goes to the build route unclassified (an inherited answer does not).
PRESSED_KEY = "effort_pressed"
#: The step's output: `next` is the build's entry, or "offer".
OUTPUT_KEY = "effort"

#: The reasons the quick classify may give; code reads only these from it.
MODEL_REASONS = ("many_steps", "team", "nested")
#: The reasons code sets from facts.
CODE_REASONS = ("many_providers", "api_only", "long_ask", "retry_history")
#: Every threshold in one place, set from the measure (EFFORT-CHECK.md), not from guesses.
THRESHOLDS = {"providers": 3, "long_ask_chars": 1200, "model_reasons": 1, "code_reasons": 2}
#: Each reason, said on the card.
WORDS = {"many_steps": "many steps", "team": "agents working together", "nested": "a workflow inside another",
         "many_providers": "several outside services", "api_only": "a service with no app here yet",
         "long_ask": "a long request", "retry_history": "an earlier try here failed"}

#: What a refused press says.
NOT_OPEN = "That offer isn't open any more: send your request again."


def reasons(request: dict, message: str, *, connectable: set[str], failed_before: bool) -> list[str]:
    """The reasons this build is heavy: the model's own (its closed list only, and only with its `heavy`), then code's
    from facts."""
    from core.native_builder.classify import _found

    said = [w for w in (request.get("why") or []) if w in MODEL_REASONS] if request.get("effort") == "heavy" else []
    providers = [str(p) for p in request.get("providers") or [] if str(p).strip()]
    code = []
    if len(providers) >= THRESHOLDS["providers"]:
        code.append("many_providers")
    if any(not _found(p, connectable) for p in providers):
        code.append("api_only")
    if len(message) > THRESHOLDS["long_ask_chars"]:
        code.append("long_ask")
    if failed_before:
        code.append("retry_history")
    return list(dict.fromkeys([*said, *code]))


def is_heavy(why: list[str]) -> bool:
    """Heavy: one of the model's reasons, two of code's, or an earlier failure here."""
    return ("retry_history" in why or len([w for w in why if w in MODEL_REASONS]) >= THRESHOLDS["model_reasons"]
            or len([w for w in why if w in CODE_REASONS]) >= THRESHOLDS["code_reasons"])


def failed_line(default_name: str, why: str) -> str:
    """A build that failed, said plainly before F1018's offer (Naveen, 10-28)."""
    return f"This didn't build on {default_name}: {why.strip()}"


async def _turns(session_id: str | None, run_id: str) -> list[tuple[str, str, dict]]:
    """This chat's earlier turns, newest first: their run ids, statuses and states."""
    if not session_id:
        return []
    from sqlalchemy import select

    from core.store import session
    from core.store.models import OrchestrationRunDB
    from core.tenancy import get_tenant

    async with session() as s:
        rows = (await s.execute(
            select(OrchestrationRunDB.run_id, OrchestrationRunDB.status, OrchestrationRunDB.shared_state)
            .where(OrchestrationRunDB.tenant_id == get_tenant(), OrchestrationRunDB.session_id == session_id,
                   OrchestrationRunDB.run_id != run_id)
            .order_by(OrchestrationRunDB.created_at.desc()).limit(20))).all()
    return [(r[0], r[1], r[2] or {}) for r in rows]


def _current_model(state: dict) -> str | None:
    from core.config import load_settings
    from core.orchestration.engine import OrchestrationEngine

    return state.get(OrchestrationEngine.MODEL_OVERRIDE_KEY) or load_settings().get("model") or None


async def _stronger(current: str | None) -> tuple[Any, Any] | None:
    """The model up from `current` this workspace can call and pay for, and `current`'s own entry; None if none."""
    from core.native_builder import models_menu as MM

    menu = await MM.menu()
    if not menu or not current:
        return None
    picked = MM.stronger_than(current, "simple", has_tools=True, menu=menu)
    entry = next((e for e in menu if picked is not None and e.id == picked.id), None)
    if entry is None or not entry.can_pay:
        return None
    return entry, next((e for e in menu if e.id == current), None)


def _price(up: Any, now: Any) -> str:
    if now is not None and now.price_out and up.price_out:
        return f"about {round(up.price_out / now.price_out):g}× the price"
    return f"about ${up.price_out:g} per 1M output tokens"


#: What the offer's turn knew that the build needs back, on its press or on the next message (the review's 1): the
#: workflow the ask was about, the findings it went on from, a pasted export's guide and code, and its routing.
CARRY = ("current_orchestration_id", "current_orchestration_name", "current_orchestration_source",
         "current_orchestration_matches", "prior_findings", "plan_findings", "export_guide", "export_source",
         "request", "intent")


def _answered(state: dict) -> dict:
    """The effort answer a turn's state holds, as state keys to carry on (accepted or declined), or {}."""
    return {k: state[k] for k in (ACCEPTED_KEY, DECLINED_KEY) if state.get(k)}


def _unsaved_build(state: dict) -> bool:
    """A build that has not saved yet: the same build goes on in the next turn (the review's 3)."""
    from core.orchestration import run_writes

    return state.get("intent") in ("create", "update") and not state.get(run_writes.WRITES_KEY)


async def effort_check(args: dict, server_module: Any, events: list[dict]) -> dict:
    """The build's entry, or the offer that ends this turn (see the module's docstring)."""
    from core.builder_tools import _current_run
    from core.native_builder.classify import _connectable
    from core.orchestration.engine import ACTIVE_TIME_KEY, OrchestrationEngine, live_run
    from core.try_stronger import RETRIED_KEY

    run_id = _current_run.get() or ""
    run = live_run(run_id) if run_id else None
    state = run.shared_state if run is not None else {}
    go = {"next": "step_fetch_existing" if state.get("intent") == "update" else "step_plan_room"}
    request = state.get("request") if isinstance(state.get("request"), dict) else {}
    # Only a build classify routed as one: a change route's hand-over to the build route stays on the usual model
    # (the review's 8, the manager's call).
    if request.get("kind") != "build":
        return go
    if state.get(ACCEPTED_KEY) or state.get(DECLINED_KEY) or state.get(RETRIED_KEY) or state.get("may_change") is False:
        return go
    turns = await _turns(getattr(run, "session_id", None), run_id)
    if turns:
        last_status, last = turns[0][1], turns[0][2]
        # A build the person is in the middle of (its review waited, or was set aside): no offer between its rounds.
        if last_status in ("paused", "cancelled") and last.get("plan_checked"):
            return go
        # The same build, already answered and not saved (the review's 3): its answer holds, never asked again.
        if _answered(last) and _unsaved_build(last):
            if run is not None:
                run.shared_state.update(_answered(last))
            return go
    current = _current_model(state)
    # A failed build here on this model (the review's 9: builds only, not an answer that failed).
    failed_before = any(status == "failed" and st.get("intent") in ("create", "update")
                        and (st.get(OrchestrationEngine.MODEL_OVERRIDE_KEY) or current) == current
                        for _, status, st in turns)
    message = str(state.get("user_message") or "")
    why = reasons(request, message, connectable=await _connectable(), failed_before=failed_before)
    if not is_heavy(why):
        return {**go, "why": why}
    found = await _stronger(current)
    if found is None:
        return {**go, "why": why}
    up, now = found
    default_name = now.name if now is not None else str(current)
    said_why = ", ".join(WORDS[w] for w in why)
    price = _price(up, now)
    events.append({"type": KIND, "model": up.id, "name": up.name, "default": current, "default_name": default_name,
                   "price": price, "why": why, "use": f"Use {up.name}", "continue": f"Continue with {default_name}",
                   "effort_of": run_id})
    say = (f"This looks like a bigger build ({said_why}). {up.name} usually does these better, at {price} of "
           f"{default_name}. Use {up.name} for this build, or continue with {default_name}.")
    out = {"next": "offer", "ask": message, "model": up.id, "why": why, "say": say,
           "carry": {k: state[k] for k in CARRY if k in state}}
    # The ask's images, kept past this turn's end (a finished run lets its own go): its press or the next message
    # builds with them (the review's 1).
    refs = ((run.step_progress.get(ACTIVE_TIME_KEY) or {}).get(run_images_key()) if run is not None else None)
    if refs:
        from core.orchestration import run_images

        # A copy under the offer's own name where the host stores images (self-hosted's blobs); a host whose
        # references outlive the run (cloud's attachment keys) keeps nothing and hands on the run's own.
        kept = await run_images.keep(_offer_images(run_id), await run_images.load(run_id, refs)) or list(refs)
        if kept:
            out["images"] = kept
    return out


def run_images_key() -> str:
    from core.orchestration import run_images

    return run_images.REFS_KEY


def _offer_images(run_id: str) -> str:
    """Where an offer keeps its turn's images: a name of the offer's own, which its run's end never clears."""
    return f"{run_id}-offer"


def open_offer(state: dict) -> dict | None:
    """The offer a turn ended with, while nothing has answered it yet; None otherwise."""
    offered = state.get(OUTPUT_KEY) if isinstance(state.get(OUTPUT_KEY), dict) else {}
    if offered.get("next") == "offer" and offered.get("ask") and not offered.get("pressed"):
        return offered
    return None


def hand_on(previous_status: str | None, previous: dict) -> dict:
    """What a new turn takes from the turn before it, for a host to start it with (the review's 1 and 4), in code
    alone: `state` (an open offer's carried context, or an unsaved build's effort answer), `model` (the stronger
    model a set-aside build was pressed onto, else None) and `image_refs` (an open offer's own images)."""
    from core.orchestration.engine import OrchestrationEngine

    offered = open_offer(previous)
    if offered is not None:
        return {"state": dict(offered.get("carry") or {}), "model": None, "image_refs": list(offered.get("images") or [])}
    if previous.get(ACCEPTED_KEY) and _unsaved_build(previous) and previous_status in ("paused", "cancelled", "failed"):
        # A build on the stronger model whose round was set aside or stopped goes on, on it, until it saves.
        return {"state": _answered(previous), "model": previous.get(OrchestrationEngine.MODEL_OVERRIDE_KEY),
                "image_refs": []}
    return {"state": _answered(previous) if _unsaved_build(previous) else {}, "model": None, "image_refs": []}


async def next_turn(previous_id: str, previous_status: str | None, previous: dict) -> dict:
    """`hand_on`, with the open offer's images read back (`images`), for a host that runs the engine in process."""
    from core.orchestration import run_images

    handed = hand_on(previous_status, previous)
    images = await run_images.load(_offer_images(previous_id), handed["image_refs"]) if handed["image_refs"] else []
    return {**handed, "images": images}


async def failure_notice(run_id: str) -> dict | None:
    """A build that failed, said plainly in code's words (Naveen, 10-28), before F1018's one offer: the model it ran
    on and why it stopped. None for a run that did not fail, or is not a build."""
    from core.native_builder import models_menu as MM
    from core.orchestration.run_error import run_error
    from core.orchestration.state import SharedState

    try:
        run = (await SharedState.restore(run_id)).run
    except FileNotFoundError:
        return None
    state = run.shared_state or {}
    if run.status != "failed" or state.get("intent") not in ("create", "update"):
        return None
    current = _current_model(state)
    name = next((e.name for e in await MM.menu() if e.id == current), current or "this model")
    why = (run_error(run) or "it stopped before anything was saved").strip().rstrip(".") + "."
    return {"type": "builder_notice", "reason": "build_failed", "final": False, "message": failed_line(name, why)}


#: One press per chat at a time in this process (the review's 6): the check and the mark happen under it.
_PRESS_LOCKS: dict[str, Any] = {}


async def effort_press(effort_of: str, session_id: str) -> dict | str:
    """The pressed offer's build, or why not: it is this chat's latest turn and its offer is still open, claimed once
    (marked pressed under the chat's lock); the model is the server's own pick now (`_stronger`), never the
    browser's. The build starts on the build route with the offer's context (`state`) and images."""
    import asyncio

    from core.orchestration.state import SharedState
    from core.try_stronger import NOTHING_STRONGER

    lock = _PRESS_LOCKS.setdefault(session_id, asyncio.Lock())
    async with lock:
        turns = await _turns(session_id, "")
        if not turns or turns[0][0] != effort_of:
            return NOT_OPEN
        offered = open_offer(turns[0][2])
        if offered is None:
            return NOT_OPEN
        found = await _stronger(_current_model(turns[0][2]))
        if found is None:
            return NOTHING_STRONGER
        restored = await SharedState.restore(effort_of)
        restored.run.shared_state[OUTPUT_KEY] = {**offered, "pressed": True}
        await restored.checkpoint()
    carried = await next_turn(effort_of, turns[0][1], turns[0][2])
    return {"model": found[0].id, "message": str(offered["ask"]), "images": carried["images"],
            "state": {**carried["state"], ACCEPTED_KEY: effort_of, PRESSED_KEY: True}}


def _register() -> None:
    from core.builder_tools import register_builder_tool

    register_builder_tool({"type": "function", "function": {
        "name": "effort_check",
        "description": "Synapse AI's check before a build (code): whether it looks heavy enough to offer a stronger "
                       "model first.",
        "parameters": {"type": "object", "properties": {}}}}, effort_check)


_register()
