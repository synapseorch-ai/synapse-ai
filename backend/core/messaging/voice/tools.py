"""The voice tools every agent holds on a call (V17): `end_call`, and `transfer_call` where the number has a
hand-off number. They reach the engine through `extra_tools`. Each ends the turn as it is called: the goodbye
(the tool's own argument) or the hand-off line is spoken by `VoiceCall`, with no second model call to write it
(it cost ~2 s on every goodbye on the first real calls).
"""
from __future__ import annotations

from typing import Any

END_CALL = "end_call"
TRANSFER_CALL = "transfer_call"


_ENDED = "Ending the call; your goodbye is spoken for you."
_HANDED = "Connecting the caller to a person; the hand-off line is spoken for you."


def _declared(name: str):
    from core.tools import VirtualTool

    if name == END_CALL:
        return VirtualTool(
            name=END_CALL,
            description=("End the phone call, when the caller seems done; a thank-you alone is not goodbye. The first "
                         "time, the call asks the caller whether there is anything else instead of ending; if they say "
                         "no, call it again. Put your goodbye in `goodbye` (one short sentence); it is spoken, then the "
                         "call ends. Write nothing else."),
            inputSchema={"type": "object", "properties": {"goodbye": {
                "type": "string", "description": "Your goodbye, one short sentence."}},
                "required": ["goodbye"], "additionalProperties": False},
        )
    return VirtualTool(
        name=TRANSFER_CALL,
        description=("Connect the caller to a person on the team. Use it when they ask for a person, or when "
                     "you cannot help. Give the reason in a few words; the person sees it. A short hand-off "
                     "line is spoken for you, so write nothing else."),
        inputSchema={"type": "object", "properties": {"reason": {
            "type": "string", "description": "Why the caller needs a person, in a few words."}},
            "required": ["reason"], "additionalProperties": False},
    )


def action_from(event: dict) -> tuple[str, dict] | None:
    """The call's action a call of one of its tools asks for, as its handler here would set it: from the tool's
    `tool_execution` event, which reaches the call whether the turn ran here or was relayed from the fleet (F531)."""
    name, args = event.get("tool_name"), event.get("args") or {}
    if name == END_CALL:
        return ("end", {"goodbye": str(args.get("goodbye") or "").strip()[:300]})
    if name == TRANSFER_CALL:
        return ("transfer", {"reason": str(args.get("reason") or "").strip()[:300]})
    return None


def build(call: Any, *, can_transfer: bool) -> list:
    """`(tool, handler)` pairs for this call's turn."""
    async def end_call(args: dict) -> str:
        call.pending_action = action_from({"tool_name": END_CALL, "args": args})
        return _ENDED

    out = [(_declared(END_CALL), end_call)]
    if can_transfer:
        async def transfer_call(args: dict) -> str:
            call.pending_action = action_from({"tool_name": TRANSFER_CALL, "args": args})
            return _HANDED

        out.append((_declared(TRANSFER_CALL), transfer_call))
    return out


def for_the_fleet(names: list[str] | None) -> list:
    """The call's tools for a turn the fleet runs (F531, a workflow agent's): the same declarations, by name as the
    call offered them, with handlers that act on nothing — the call is in another process, and acts on the relayed
    call of one (`action_from`)."""
    out = []
    for name in dict.fromkeys(names or []):
        if name not in (END_CALL, TRANSFER_CALL):
            continue

        async def handler(args: dict, said=_ENDED if name == END_CALL else _HANDED) -> str:
            return said

        out.append((_declared(name), handler))
    return out
