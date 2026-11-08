"""The features a workspace switches on in Settings › General, and what everything says when one it needs is off
(F1203, BLD1; CAP3's F-i).

Boards and Knowledge are off by default. A build or a card that needed one failed quietly: the planner was told
"no board tools" and planned without the board, a card was refused in words that named no switch. Now one sentence,
here, says what is off and where it is turned on, and `turn_on` is the one door that turns it on for a person who
pressed for it: the Settings route's own save. A host whose switch lives elsewhere (cloud's plan, its own settings)
says where (`set_where`) and turns it on its way (`set_switch`).
"""
from __future__ import annotations

from typing import Any

#: Each feature: its name, its setting, and the pronoun its sentence takes.
FEATURES: dict[str, tuple[str, str, str]] = {
    "boards": ("Boards", "boards_enabled", "them"),
    "knowledge": ("Knowledge", "knowledge_enabled", "it"),
}

_where: dict[str, str] = {}
#: The host's own switch: async `switch(feature) -> reason | None` (a reason when it may not be turned on here).
_switch: Any = None


def set_where(feature: str, words: str | None) -> None:
    """Where the host's switch for `feature` is, in its screen's words. ``None`` puts this engine's back."""
    if words is None:
        _where.pop(feature, None)
    else:
        _where[feature] = words


def set_switch(switch: Any) -> None:
    """Install the host's switch. ``None`` puts this engine's back (the Settings route's save)."""
    global _switch
    _switch = switch


def label(feature: str) -> str:
    return FEATURES[feature][0]


def where(feature: str) -> str:
    return _where.get(feature) or f"Settings › General › {label(feature)}"


def off_sentence(feature: str) -> str:
    """What is off and where it is turned on: "Boards are switched off in this workspace: turn them on in Settings ›
    General › Boards." """
    name, _, them = FEATURES[feature]
    verb = "are" if them == "them" else "is"
    return f"{name} {verb} switched off in this workspace: turn {them} on in {where(feature)}."


async def is_on(feature: str) -> bool:
    # Each feature's own check, the one its product tools already answer by.
    if feature == "knowledge":
        from core.product_tools import knowledge
        return knowledge._on()
    from core.product_tools import boards
    return await boards._host.enabled()


async def turn_on(feature: str) -> str | None:
    """Turn `feature` on, as its switch in Settings does; a reason when the host may not, else None."""
    if _switch is not None:
        return await _switch(feature)
    from core.models import Settings
    from core.routes.settings import update_settings

    await update_settings(Settings(**{FEATURES[feature][1]: True}))
    return None
