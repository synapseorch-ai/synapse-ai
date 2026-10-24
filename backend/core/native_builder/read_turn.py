"""Code reads each turn before any model (SAI2-P4, F978; DESIGN.md "The new definition").

The definition's first step (`step_turn`, an E1 tool step: no model) calls `read_turn`. It reads the previous turn of
this conversation — that run's state and its own card (F977) — and decides what code can decide alone: a pick from
the last list, a typed button label, "done" after a press, a yes with nothing waiting, a yes to a queued second
request. Anything else goes on to classify. `decide` is the whole decision, pure; `read_turn` only gathers its facts
and puts up the card a pick chose.

F759 is not here: a message typed while a review waits sets the parked run aside in the panel, so a new turn never
finds a review waiting (`builderEvents.typedWhilePending`).
"""
from __future__ import annotations

import logging
import re
from typing import Any

from core.native_builder.objects import Turn
from core.native_builder.review import is_yes
from core.native_builder.target import _ordinal, _words

_log = logging.getLogger(__name__)

#: "Done" after a card: what a person types once they have pressed it.
_ACK = frozenset({"done", "ok done", "pressed", "pressed it", "i pressed it", "i pressed", "clicked", "clicked it",
                  "i clicked it", "i clicked", "did it", "i did it", "i did", "i ve pressed it", "ive pressed it",
                  "i have pressed it", "it s done", "its done"})
#: A yes to a queued second request, beyond the review's own yes words.
_GO_ON = frozenset({"sure", "yes please", "please do", "do that", "yes do that", "go on", "next", "ok do that",
                    "yes go ahead", "go ahead please", "please"})


def _plain(text: str) -> str:
    return _words(text or "").strip()


def _quoted(text: str) -> str:
    return f'"{text}"'


def pick_from(message: str, options: list[dict]) -> dict | None:
    """The one option `message` chooses: by its place ("2", "the second one", "last"), its id, what the list printed
    (`"name" (id)`), or its whole name when that name is one option's alone. Anything more is a new request."""
    from core.builder_actions import listed

    text = (message or "").strip()
    if not text or not options:
        return None
    index = _ordinal(text, len(options))
    if index is not None:
        return options[index]
    by_id = [o for o in options if o.get("id") == text]
    if len(by_id) == 1:
        return by_id[0]
    if (by_listing := listed(options, text)) is not None:
        return by_listing
    named = [o for o in options if _plain(str(o.get("name") or "")) == _plain(text)]
    return named[0] if len(named) == 1 else None


def _list_again(options: list[dict]) -> str:
    return "\n".join(f"{n}. {_quoted(str(o.get('name')))} ({o.get('id')})" for n, o in enumerate(options, 1))


def _first_sentence(text: str) -> str:
    text = " ".join(str(text or "").split())
    if not text:
        return ""
    end = re.search(r"(?<=[.!?])\s", text)
    return (text[: end.start()] if end else text)[:240]


def _pressed_line(press: dict) -> str:
    where = f'{_quoted(press.get("label") or "the button")} on {_quoted(press.get("card") or "the card")}'
    if press.get("done"):
        return f"You pressed {where}: {press.get('say') or 'done.'}"
    return f"You pressed {where}, and it didn't go through: {press.get('say') or 'it was refused.'}"


def decide(message: str, *, previous: dict, facts: dict, change: dict | None = None,
           prior_findings: Any = None) -> Turn:
    """What code makes of `message`, given the previous turn's state (`previous`), its card's facts (`facts`, F977),
    its change card's data (`change`) and the findings it ended on (F766)."""
    said = _plain(message)
    yes = is_yes(message)

    # 1. A yes to the second request the last turn queued.
    also = str(facts.get("also") or "").strip()
    if also and (yes or said in _GO_ON):
        return Turn(route="next_request", then="classify", message=also)

    # 2. A pick from the last "which one?" list: an act's (its card goes up) or a change's (back to the change route).
    act_picks = facts.get("picks") or []
    change_picks = ((change or {}).get("change") or {}).get("picks") or []
    if act_picks:
        pick = act_picks[-1]
        chosen = pick_from(message, pick.get("options") or [])
        if chosen is not None:
            return Turn(route="pick", then="said", picked={
                "action": pick.get("action"), "arguments": {**(pick.get("arguments") or {}),
                                                            pick.get("field") or "id": chosen["id"]},
                "id": chosen["id"], "name": chosen.get("name")})
        if yes:
            return Turn(route="pick", then="said",
                        say="Which one? Say its number or its name:\n" + _list_again(pick.get("options") or []))
    if len(change_picks) == 1:
        pick = change_picks[0]
        chosen = pick_from(message, pick.get("options") or [])
        if chosen is not None:
            return Turn(route="pick", then="change", picked={
                "op": pick.get("op"), "field": pick.get("field"), "kind": pick.get("kind"),
                "id": chosen["id"], "name": chosen.get("name")})

    # 3. A typed button label: the card above is where it is pressed.
    buttons = [(card, b) for card in facts.get("cards") or [] for b in card.get("buttons") or []]
    presses = facts.get("presses") or []
    labelled = [(card, b) for card, b in buttons if said and _plain(b.get("label") or "") == said]
    if labelled:
        card, button = labelled[-1]
        done = [p for p in presses if p.get("label") == button.get("label") and p.get("card") == card.get("title")]
        if done:
            return Turn(route="card_label", then="said", card=card.get("title"),
                        say=f"That button was already pressed. {_pressed_line(done[-1])}")
        return Turn(route="card_label", then="said", card=card.get("title"),
                    say=f"Press {_quoted(button.get('label'))} on the card above ({card.get('title')}): "
                        "nothing happens until you do.")

    # 4. "Done" after a card: what the press did, or that nothing has been pressed yet.
    if said in _ACK and (presses or buttons):
        if presses:
            return Turn(route="pressed", then="said", say=" ".join(_pressed_line(p) for p in presses[-3:]))
        labels = " or ".join(_quoted(b.get("label")) for _, b in buttons[:3])
        return Turn(route="pressed", then="said",
                    say=f"The card above hasn't been pressed yet: press {labels} on it to do it.")

    # 5. A yes with nothing waiting for one. A yes after findings is a build (F766), and a yes to a question the last
    # reply asked answers it: both go on to classify, which reads the conversation.
    if yes:
        findings = prior_findings if isinstance(prior_findings, dict) else {}
        last = str(previous.get("summary") or "").strip()
        if findings.get("options") or last.endswith("?"):
            return Turn(route="classify")
        if last:
            return Turn(route="yes_without_review", then="said",
                        say=f"Nothing is waiting for a yes. The last thing I did: {_first_sentence(last)}")
        return Turn(route="yes_without_review", then="said",
                    say="Nothing is waiting for a yes. What would you like me to do?")

    return Turn(route="classify")


# ── the step ──────────────────────────────────────────────────────────────────────────────────────────────────────

async def _previous(session_id: str | None, run_id: str) -> tuple[str, dict]:
    """The newest earlier turn of this conversation: its run id and state, or nothing."""
    if not session_id:
        return "", {}
    from sqlalchemy import select

    from core.store import session
    from core.store.models import OrchestrationRunDB
    from core.tenancy import get_tenant

    async with session() as s:
        row = (await s.execute(
            select(OrchestrationRunDB.run_id, OrchestrationRunDB.shared_state)
            .where(OrchestrationRunDB.tenant_id == get_tenant(), OrchestrationRunDB.session_id == session_id,
                   OrchestrationRunDB.run_id != run_id)
            .order_by(OrchestrationRunDB.created_at.desc()).limit(1))).first()
    return (row[0], row[1] or {}) if row else ("", {})


async def read_turn(args: dict, server_module: Any, events: list[dict]) -> dict:
    """The turn as code reads it (`Turn`); a pick from an act's list puts that card up here."""
    from core.boards import assistant
    from core.builder_tools import _current_run
    from core.orchestration.engine import live_run

    run_id = _current_run.get() or ""
    run = live_run(run_id) if run_id else None
    state = run.shared_state if run is not None else {}
    message = str(state.get("user_message") or "")
    previous_id, previous = await _previous(getattr(run, "session_id", None), run_id)
    facts, change = {}, None
    if previous_id:
        try:
            facts = await assistant.turn(previous_id)
            card = await assistant.card(previous_id)
            change = card.data if card is not None and card.status in ("open", "claimed") else None
        except Exception as exc:  # noqa: BLE001 — no board yet: the turn is read from its message alone
            _log.warning("turn %s: previous turn %s not read: %s", run_id, previous_id, exc)
    turn = decide(message, previous=previous, facts=facts, change=change,
                  prior_findings=state.get("prior_findings"))

    if turn.route == "pick" and turn.then == "said" and isinstance(turn.picked, dict) and not turn.say:
        from core.builder_actions import prepare_action

        put_up = await prepare_action({"action": turn.picked["action"], **turn.picked["arguments"]}, server_module,
                                      events)
        turn.say = put_up.get("say") or put_up.get("error") or ""
    if turn.route == "next_request" and run is not None:
        # The queued request is this turn's message from here on: classify and every route read it.
        run.shared_state["user_message"] = turn.message
    out = turn.model_dump()
    out["message"] = turn.message or message
    out["previous_run_id"] = previous_id
    return out


SCHEMA = {"type": "function", "function": {
    "name": "read_turn",
    "description": "Synapse AI's own first step: reads this message against the conversation's previous turn, in "
                   "code. Takes no arguments.",
    "parameters": {"type": "object", "properties": {}},
}}


def _register() -> None:
    from core.builder_tools import register_builder_tool

    register_builder_tool(SCHEMA, read_turn)


_register()
