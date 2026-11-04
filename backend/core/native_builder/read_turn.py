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

from core.native_builder.classify import OFFER_TAKEN_KEY, OFFERED_KEY
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
#: A yes with more after it ("yes, at 7am by email"), said to the offer for a live fact (F1382). "ok thanks" is not one.
_YES_FIRST = re.compile(r"^(?:yes|yeah|yep|sure)\b(?! thanks?\b| no\b)")
#: Go on as it is, said to the effort offer (F1153).
_JUST = frozenset({"just do it", "just build it", "do it", "build it", "no thanks", "no", "nope", "keep going",
                   "carry on", "proceed", "no just do it", "use the default"})


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


#: An option a reply lists, on a line of its own: "A) …", "A. …", "Option A: …", "1. …", "1) …" (F1347).
_OFFERED = re.compile(r"^\s*(?:\*\*)?(?:option\s+)?([A-Za-z]|\d{1,2})[.):](?:\*\*)?\s+(.+?)\s*$", re.IGNORECASE)
#: A choice said by its letter or number: "A", "option a", "plan B", "choice 2" (after `_words`).
_LETTERED = re.compile(r"^(?:[a-z]|\d{1,2})$|\b(?:option|plan|choice|number)\s+([a-z]|\d{1,2})\b")


def options_offered(previous: dict) -> list[dict] | None:
    """The options the last turn's reply offered, as a pick list: only a reply the planner ended on (`say`, a plan
    outcome `said`) that asks among two or more options listed on lines of their own (F1347). Read from the state that
    turn kept; None otherwise."""
    draft = previous.get("plan_draft") if isinstance(previous.get("plan_draft"), dict) else {}
    say = str(draft.get("say") or "")
    if draft.get("outcome") != "said" or "?" not in say:
        return None
    options = [{"id": m.group(1).upper(), "name": m.group(2)}
               for m in (_OFFERED.match(line) for line in say.splitlines()) if m]
    return options if len(options) >= 2 and len({o["id"] for o in options}) == len(options) else None


def _offer_picked(message: str, options: list[dict]) -> dict | None:
    """The one offered option `message` chooses: by its place or name (`pick_from`), or by its letter or number."""
    if (chosen := pick_from(message, options)) is not None:
        return chosen
    said = _plain(message)
    found = _LETTERED.search(said)
    if found is None:
        return None
    letter = (found.group(1) or said).upper()
    return next((o for o in options if o["id"] == letter), None)


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


#: Code's own replies that point at a card and put up none ("Press Run it on the card above", "done"): past them, the
#: card above is still the one an earlier turn put up (F985).
_POINTING = frozenset({"card_label", "pressed"})
#: How many of those replies in a row the card above is looked for past.
LOOK_BACK = 5


def card_above(turns: list[tuple[dict, dict]]) -> dict:
    """The previous turn's facts with the card above filled in. `turns` are this conversation's earlier turns, newest
    first, as (state, facts). When the newest put up no card and was one of code's own pointing replies, the cards and
    their presses are the earlier turn's that put the card up: a press is kept on its card's own turn, so card, typed
    "Run it", the press, "done" read only the pointing turn and said nothing of the press (F985)."""
    if not turns:
        return {}
    facts = dict(turns[0][1])
    for state, earlier in turns[:LOOK_BACK + 1]:
        if earlier.get("cards"):
            facts["cards"], facts["presses"] = earlier["cards"], earlier.get("presses") or []
            return facts
        if (state.get("turn") or {}).get("route") not in _POINTING:
            return facts
    return facts


def decide(message: str, *, previous: dict, facts: dict, change: dict | None = None,
           prior_findings: Any = None, pressed: bool = False) -> Turn:
    """What code makes of `message`, given the previous turn's state (`previous`), its card's facts (`facts`, F977),
    its change card's data (`change`) and the findings it ended on (F766). `pressed`: this turn is the effort offer's
    press (F1153), whose message is already the ask."""
    said = _plain(message)
    yes = is_yes(message)

    # 0. A reply to the effort offer that is not its press (F1153, Naveen 10-28): the build goes on, on the usual
    # model, never asked again. A go-ahead (or the card's "Continue with …") is the ask itself; anything else is the
    # ask with what was typed after it.
    # The press itself (the review's 7): its message is the ask and its context the offer's; it goes to the build
    # route through the role door, never classified again, so it cannot become an answer on the stronger model.
    if pressed:
        return Turn(route="effort_build", then="build")
    offered = previous.get("effort") if isinstance(previous.get("effort"), dict) else {}
    if offered.get("next") == "offer" and offered.get("ask"):
        ask = str(offered["ask"])
        going_on = yes or said in _GO_ON or said in _ACK or said.startswith("continue") or said in _JUST
        return Turn(route="effort_continue", then="classify", message=ask if going_on else f"{ask}\n\n{message}")

    # 0b. A yes to the offer to build the agent for a live fact (F1382): its ask, with what they added. Only a
    # builder's turn keeps an offer, so a viewer's yes is read as below.
    offer = previous.get(OFFERED_KEY) if isinstance(previous.get(OFFERED_KEY), dict) else {}
    if ask := str(offer.get("ask") or ""):
        if yes or said in _GO_ON:
            return Turn(route="offer_taken", then="classify", message=ask)
        if _YES_FIRST.match(said):
            return Turn(route="offer_taken", then="classify", message=f"{ask}\n\n{message}")

    # 1. A yes to the second request the last turn queued.
    also = str(facts.get("also") or "").strip()
    asked_back = str(previous.get("summary") or "").strip().endswith("?")
    # A yes to a question the last reply asked is that question's, not the queue's (SAI2's review).
    if also and (yes or said in _GO_ON) and not asked_back:
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

    # 4b. A pick from the options the last reply offered (F1347, CAP3's prod-13): the planner listed them in its reply
    # and the run ended there. The pick goes back on the original ask with the choice, through classify, so the build or
    # change still comes to its review and its yes; a reply that picks none is read as before.
    if options := options_offered(previous):
        if (chosen := _offer_picked(message, options)) is not None:
            ask = str(previous.get("user_message") or "").strip()
            return Turn(route="offer_pick", then="classify",
                        message=f"{ask}\n\nOf the options you offered, use {chosen['id']}: {chosen['name']}".strip())

    # 5. A yes with nothing waiting for one. A yes after findings is a build (F766), and a yes to a question the last
    # reply asked answers it: both go on to classify, which reads the conversation.
    if yes and buttons and not presses:
        # A card not yet pressed is what waits: the yes points at it (SAI2's review 2).
        card, button = buttons[-1]
        return Turn(route="card_label", then="said", card=card.get("title"),
                    say=f"Press {_quoted(button.get('label'))} on the card above ({card.get('title')}): "
                        "nothing happens until you do.")
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

async def _earlier(session_id: str | None, run_id: str, limit: int) -> list[tuple[str, dict]]:
    """This conversation's earlier turns, newest first: their run ids and states."""
    if not session_id:
        return []
    from sqlalchemy import select

    from core.store import session
    from core.store.models import OrchestrationRunDB
    from core.tenancy import get_tenant

    async with session() as s:
        rows = (await s.execute(
            select(OrchestrationRunDB.run_id, OrchestrationRunDB.shared_state)
            .where(OrchestrationRunDB.tenant_id == get_tenant(), OrchestrationRunDB.session_id == session_id,
                   OrchestrationRunDB.run_id != run_id)
            .order_by(OrchestrationRunDB.created_at.desc()).limit(limit))).all()
    return [(row[0], row[1] or {}) for row in rows]


async def previous_turns(session_id: str | None, run_id: str, limit: int = 20) -> list[str]:
    """This conversation's earlier turns, newest first, by run id."""
    if not session_id:
        return []
    from sqlalchemy import select

    from core.store import session
    from core.store.models import OrchestrationRunDB
    from core.tenancy import get_tenant

    async with session() as s:
        rows = (await s.execute(
            select(OrchestrationRunDB.run_id)
            .where(OrchestrationRunDB.tenant_id == get_tenant(), OrchestrationRunDB.session_id == session_id,
                   OrchestrationRunDB.run_id != run_id)
            .order_by(OrchestrationRunDB.created_at.desc()).limit(limit))).scalars().all()
    return list(rows)


async def read_turn(args: dict, server_module: Any, events: list[dict]) -> dict:
    """The turn as code reads it (`Turn`); a pick from an act's list puts that card up here."""
    from core.boards import assistant
    from core.builder_tools import _current_run
    from core.orchestration.engine import live_run

    run_id = _current_run.get() or ""
    run = live_run(run_id) if run_id else None
    state = run.shared_state if run is not None else {}
    message = str(state.get("user_message") or "")
    earlier = await _earlier(getattr(run, "session_id", None), run_id, LOOK_BACK + 1)
    previous_id, previous = earlier[0] if earlier else ("", {})
    facts, change = {}, None
    if previous_id:
        try:
            facts = card_above([(state_, await assistant.turn(rid)) for rid, state_ in earlier])
            card = await assistant.card(previous_id)
            change = card.data if card is not None and card.status in ("open", "claimed") else None
        except Exception as exc:  # noqa: BLE001 — no board yet: the turn is read from its message alone
            _log.warning("turn %s: previous turn %s not read: %s", run_id, previous_id, exc)
    from core.native_builder.effort import DECLINED_KEY, PRESSED_KEY

    turn = decide(message, previous=previous, facts=facts, change=change,
                  prior_findings=state.get("prior_findings"), pressed=bool(state.get(PRESSED_KEY)))
    if turn.route == "effort_continue" and run is not None:
        # The ask goes on, on the usual model; the build records the answer, and is not asked again (F1153).
        run.shared_state["user_message"] = turn.message
        run.shared_state[DECLINED_KEY] = previous_id

    if turn.route == "pick" and turn.then == "said" and isinstance(turn.picked, dict) and not turn.say:
        from core.builder_actions import prepare_action

        put_up = await prepare_action({"action": turn.picked["action"], **turn.picked["arguments"]}, server_module,
                                      events)
        turn.say = put_up.get("say") or put_up.get("error") or ""
    if turn.route in ("next_request", "offer_pick", "offer_taken") and run is not None:
        # The queued request, the ask with the offered option chosen (F1347), or the offer's ask (F1382), is this
        # turn's message from here on: classify and every route read it.
        run.shared_state["user_message"] = turn.message
    if turn.route == "offer_taken" and run is not None:
        # Classify sends it to the build route, through the role door (F1382).
        run.shared_state[OFFER_TAKEN_KEY] = True
    if turn.then == "change" and run is not None:
        # A change's pick goes back through the role door to the change route (`step_may_change` reads it).
        run.shared_state["intent"] = "manage"
    out = turn.model_dump()
    out["message"] = turn.message or message
    out["previous_run_id"] = previous_id
    # What the last turn did, for the answer's pack: its reply's first sentence.
    out["last"] = _first_sentence(str(previous.get("summary") or ""))
    # The workflows the last few turns built or changed, newest first: the pack says what each does, from its parts
    # (F1228), so an answer about one never says what it does from memory.
    out["built"] = built_in(earlier)
    # A plan waiting at its review (F1241): its title sends this turn's classify through the call that must say
    # whether the message changes it (`step_classify_door`; F1241's follow-up). "" when none waits.
    from core.native_builder.change_tools import waiting_plan_title

    out["plan_waiting"] = waiting_plan_title(state)
    return out


def built_in(earlier: list[tuple[str, dict]], most: int = 3) -> list[str]:
    """The workflows these turns' own writes created or changed (`run_writes`), newest first, once each."""
    from core.orchestration.run_writes import WRITES_KEY

    ids: list[str] = []
    for _, state_ in earlier:
        writes = state_.get(WRITES_KEY) if isinstance(state_, dict) else None
        for w in reversed(writes if isinstance(writes, list) else []):
            if isinstance(w, dict) and w.get("kind") == "orchestration" and w.get("action") in ("created", "updated") \
                    and w.get("id") and w["id"] not in ids:
                ids.append(str(w["id"]))
    return ids[:most]


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
