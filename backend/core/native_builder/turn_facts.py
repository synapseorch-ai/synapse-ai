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


def connect_what_args(args: dict) -> str:
    """What a `prepare_connection` call would connect, as `connect_what` keys the card it would put up."""
    for field in ("catalog_slug", "url", "provider", "tool_name", "screen"):
        if args.get(field):
            return str(args[field]).strip().lower()
    return str(args.get("title") or "").strip().lower()


#: Words that ask for a connection again, so a card shown before may go up once more.
_AGAIN = ("connect", "reconnect", "link", "hook", "sign in", "again")


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


async def answer_card_refusal(run_id: str | None, tool: str, args: dict) -> str | None:
    """Why the answer route may not put this card up, or None (SAI2's review, after Naveen saw cards pile up and
    Firecrawl offered on prod):

    * one card per turn: a second waits for the person's next message;
    * a Connect card for an app or a server only when the person named it: the product's own web search, page reader
      and scraper do what an unnamed service would.
    """
    if tool not in ("prepare_action", "prepare_connection") or not run_id:
        return None
    from core.orchestration.engine import live_run

    run = live_run(run_id)
    request = (run.shared_state.get("request") if run is not None else None) or {}
    if not isinstance(request, dict) or request.get("route") != "step_answer":
        return None
    from core.boards import assistant

    facts = await assistant.turn(run_id)
    if facts.get("cards") or facts.get("connects"):
        return ("One card per turn: a card is already up above. Say the next step in words; the person asks for it "
                "when they are ready.")
    if tool == "prepare_connection":
        from core.native_builder.read_turn import previous_turns

        message = str(run.shared_state.get("user_message") or "").lower()
        what = connect_what_args(args)
        if what and not any(w in message for w in _AGAIN):
            for previous in await previous_turns(getattr(run, "session_id", None), run_id):
                if any(c.get("what") == what for c in (await assistant.turn(previous)).get("connects") or []):
                    return ("That Connect card was shown earlier in this conversation: say it is above, and put up "
                            "no other; it goes up again when the person asks to connect it again.")
    if tool == "prepare_connection" and args.get("kind") in ("app", "mcp_server"):
        from core.native_builder.target import _words

        from urllib.parse import urlparse

        message = f" {_words(str(run.shared_state.get('user_message') or ''))} "
        host = (urlparse(str(args.get("url") or "")).hostname or "").split(".")
        # What the card connects, never its title (the model writes that): the app, the server, its host's name.
        names = [str(args.get("catalog_slug") or ""), str(args.get("server_name") or ""),
                 host[-2] if len(host) >= 2 else ""]
        if not any(w and f" {w} " in message for n in names for w in [_words(n), *_words(n).split()]
                   if len(w) >= 4):
            return ("The person did not name this service. This product's own tools search the web, read a page and "
                    "scrape a site: answer with those, and offer a service only when the person names it.")
    return None
