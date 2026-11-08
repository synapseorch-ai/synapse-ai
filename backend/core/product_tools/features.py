"""Turning on a feature a build or a card needs, as its switch in Settings does (F1203, BLD1).

An admin's act, in the app only: a person's press on Synapse AI's card ("Turn on Boards") or on a build's review.
Never over MCP or the API, where acts stop at a builder (D292).
"""
from __future__ import annotations

from typing import Any

from core.product_tools.errors import ToolRefusal
from core.product_tools.registry import ProductTool
from core.product_tools.roles import Role


async def _turn_on(caller: Any, args: dict[str, Any]) -> dict:
    from core import features

    feature = str(args.get("feature") or "").strip().lower()
    if feature not in features.FEATURES:
        raise ToolRefusal(f"feature is one of {', '.join(features.FEATURES)}.")
    name = features.label(feature)
    if await features.is_on(feature):
        return {"feature": name, "on": True, "note": f"{name} was already on."}
    if reason := await features.turn_on(feature):
        raise ToolRefusal(reason)
    return {"feature": name, "on": True}


def entries() -> list[ProductTool]:
    from core import features

    return [ProductTool(
        name="turn_on_feature",
        description="Turn on Boards or Knowledge for this workspace, as its switch in Settings › General does.",
        input_schema={"type": "object", "properties": {
            "feature": {"type": "string", "enum": list(features.FEATURES)}}, "required": ["feature"]},
        handler=_turn_on, kind="act", area="settings", min_role=Role.ADMIN, scope="build", app_only=True,
        idempotent=True, audit_action="settings.updated",
        target=lambda a, r: ("setting", features.FEATURES.get(str(a.get("feature")), ("", "", ""))[1]),
    )]
