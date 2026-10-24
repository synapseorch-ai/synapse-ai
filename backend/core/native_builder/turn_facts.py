"""What a turn put up and what was pressed, kept on the turn's own card (SAI2-P4, F977; Naveen 10-24).

Code writes these, at the one door every builder tool call passes (`builder_tools.execute_builder_tool_with_events`)
and at the act route (`core.routes.assistant.press`); `read_turn` reads them the next turn. No model is told to keep
them (BOARD2's lesson). A failure to keep a fact never stops the tool or the press: the card is the record, not the
act.
"""
from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger(__name__)


def card_fact(card: dict) -> dict:
    """An action card as it was shown, without its tokens: a record of a card is never a way to press it."""
    return {"id": card.get("id"), "title": card.get("title") or "",
            "buttons": [{"label": b.get("label") or "", "tool": b.get("tool") or "",
                         "arguments": b.get("arguments") or {}} for b in card.get("buttons") or []]}


def connect_what(card: dict) -> str:
    """What a Connect card connects, one key per thing: an app's slug, a server's URL, a provider, a tool, a screen."""
    for part, field in (("app", "slug"), ("server", "url"), ("provider", "id"), ("tool", "name"), ("screen", "id")):
        if isinstance(card.get(part), dict) and card[part].get(field):
            return str(card[part][field]).lower()
    return str(card.get("title") or "").lower()


def facts_of(events: list[dict]) -> dict[str, list]:
    """The facts a tool call's events add to its turn: the action cards and the Connect cards it put up."""
    cards = [card_fact(e) for e in events if e.get("type") == "action_needed"]
    connects = [{"kind": e.get("kind") or "", "what": connect_what(e), "title": e.get("title") or ""}
                for e in events if e.get("type") == "connection_needed"]
    return {name: value for name, value in (("cards", cards), ("connects", connects)) if value}


async def keep(run_id: str | None, owner_id: str | None, **facts: Any) -> None:
    """Add `facts` to turn `run_id`'s card; nothing without a run (an MCP call is no turn)."""
    if not run_id or not any(facts.values()):
        return
    from core.boards import assistant

    try:
        await assistant.note(run_id, owner_id=owner_id, **facts)
    except Exception as exc:  # noqa: BLE001 — the card is the record, not the act
        _log.warning("turn %s: facts not kept: %s", run_id, exc)


async def keep_press(run_id: str | None, user_id: str | None, tool: str, arguments: dict, *, done: bool,
                     say: str) -> None:
    """Keep a press on the card of the turn that showed it, when it is that turn's: its person pressed it, and it is a
    button that turn put up with these exact arguments (SAI2's review). Anything else keeps nothing."""
    if not run_id:
        return
    from core.boards import assistant

    try:
        facts = await assistant.turn(run_id)
        if not facts or (facts.get("owner_id") or None) != (user_id or None):
            return
        shown = [(card, b) for card in facts.get("cards") or [] for b in card.get("buttons") or []
                 if b.get("tool") == tool and b.get("arguments") == arguments]
        if not shown:
            return
        card, button = shown[-1]
        await assistant.note(run_id, owner_id=facts.get("owner_id"), presses=[
            {"card": card.get("title") or "", "label": button.get("label") or "", "tool": tool, "done": done,
             "say": say[:300]}])
    except Exception as exc:  # noqa: BLE001
        _log.warning("turn %s: press not kept: %s", run_id, exc)
