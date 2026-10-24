"""The click on the assistant's action card (ASSIST1, D291, F683).

The card's button carries the product tool, its exact arguments and a confirmation token minted for the person the
assistant's run works for (`core/builder_actions.py`). This route is the person's own request: it calls the same
product tool through `registry.invoke` with the token, as that person. The role is checked again, a token works
once, and it does nothing for other arguments. Self-hosted has one person, the owner; cloud's route builds the
person from its session (`/assistant/act`).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from core.product_tools import areas, confirm, registry
from core.product_tools.caller import Caller
from core.product_tools.errors import ToolRefusal
from core.product_tools.roles import Role

router = APIRouter()


class Press(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tool: str
    arguments: dict[str, Any] = {}
    token: str
    #: The assistant turn whose card this is: the press is kept on that turn's card for the next turn (F977).
    run_id: str | None = None


def status_for(refusal: str) -> tuple[int, str]:
    """A refused press, as the card says it. The token's own refusals first (`confirm.redeem`)."""
    text = refusal.lower()
    if "already used" in text:
        return 409, "Already done: this card was pressed before."
    if "expired" in text:
        return 410, "This card has expired. Ask again for a new one."
    if "different call" in text or "not valid" in text or "someone else" in text:
        return 403, "This card is not yours to press, or it was changed. Ask again for a new one."
    if " role" in text and "you are a" in text:
        return 403, refusal
    return 409, refusal  # the thing itself refused: the run had already ended, the schedule is gone…


async def press(caller: Caller, body: Press) -> dict:
    areas.ensure_loaded()
    tool = registry.get(body.tool)
    if tool is None or tool.kind != "act":
        raise HTTPException(status_code=404, detail="This card's action is not one this workspace knows.")
    from core.native_builder.turn_facts import keep_press

    try:
        result = await registry.invoke(body.tool, caller, {**body.arguments, confirm.TOKEN_ARG: body.token},
                                       confirm_policy="every_act")
    except ToolRefusal as exc:
        code, detail = status_for(str(exc))
        if code != 403:  # a press that is not this person's to make keeps nothing on the turn
            await keep_press(body.run_id, caller.user_id, body.tool, body.arguments, done=False, say=detail)
        raise HTTPException(status_code=code, detail=detail) from None
    await keep_press(body.run_id, caller.user_id, body.tool, body.arguments, done=True, say=_said(result))
    return {"done": True, "result": result}


def _said(result: Any) -> str:
    """What a press did, in the tool's own words when it has them."""
    if isinstance(result, dict):
        for key in ("say", "message", "status"):
            if isinstance(result.get(key), str) and result[key].strip():
                return result[key].strip()
    return "Done."


@router.post("/api/assistant/act")
async def act(body: Press) -> dict:
    return await press(Caller(role=Role.OWNER, via="assistant"), body)
