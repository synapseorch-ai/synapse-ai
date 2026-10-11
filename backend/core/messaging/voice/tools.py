"""The voice tools every agent holds on a call (V17): `end_call`, and `transfer_call` where the number has a
hand-off number. They reach the engine through `extra_tools`. Each ends the turn as it is called: the goodbye
(the tool's own argument) or the hand-off line is spoken by `VoiceCall`, with no second model call to write it
(it cost ~2 s on every goodbye on the first real calls).
"""
from __future__ import annotations

from typing import Any

END_CALL = "end_call"
TRANSFER_CALL = "transfer_call"


def build(call: Any, *, can_transfer: bool) -> list:
    """`(tool, handler)` pairs for this call's turn."""
    from core.tools import VirtualTool

    async def end_call(args: dict) -> str:
        goodbye = str((args or {}).get("goodbye") or "").strip()[:300]
        call.pending_action = ("end", {"goodbye": goodbye})
        return "Ending the call; your goodbye is spoken for you."

    out = [(VirtualTool(
        name=END_CALL,
        description=("End the phone call, when the caller seems done; a thank-you alone is not goodbye. The first "
                     "time, the call asks the caller whether there is anything else instead of ending; if they say "
                     "no, call it again. Put your goodbye in `goodbye` (one short sentence); it is spoken, then the "
                     "call ends. Write nothing else."),
        inputSchema={"type": "object", "properties": {"goodbye": {
            "type": "string", "description": "Your goodbye, one short sentence."}},
            "required": ["goodbye"], "additionalProperties": False},
    ), end_call)]

    if can_transfer:
        async def transfer_call(args: dict) -> str:
            reason = str((args or {}).get("reason") or "").strip()[:300]
            call.pending_action = ("transfer", {"reason": reason})
            return "Connecting the caller to a person; the hand-off line is spoken for you."

        out.append((VirtualTool(
            name=TRANSFER_CALL,
            description=("Connect the caller to a person on the team. Use it when they ask for a person, or when "
                         "you cannot help. Give the reason in a few words; the person sees it. A short hand-off "
                         "line is spoken for you, so write nothing else."),
            inputSchema={"type": "object", "properties": {"reason": {
                "type": "string", "description": "Why the caller needs a person, in a few words."}},
                "required": ["reason"], "additionalProperties": False},
        ), transfer_call))
    return out
