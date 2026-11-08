"""Changing workspace settings on Synapse AI's allow-list, as the Settings screens save them (F1264, REACH1; D340 R5).

An owner's or admin's act, in the app only (D292 stands: never MCP, OAuth or the CLI): a person's yes on Synapse AI's
reviewed change (`settings_set`), which an undo reverses like any other. What may be set, and the check each value
passes, is `core.settings_allow`; anything else is refused by name with the screen it is done on.

The save is the Settings route's own (`routes.settings.update_settings`), as Boards' and Knowledge's switch is
(F1203). A host saves its own way (`set_settings_host`: cloud's settings route, its roles and its WRITABLE list), and
its list narrows what Synapse AI may set, never widens it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from core.product_tools.errors import ToolRefusal
from core.product_tools.registry import ProductTool
from core.product_tools.roles import Role


@dataclass(frozen=True)
class SettingsHost:
    #: The workspace's settings as its screen reads them.
    read: Callable[[], Awaitable[dict]]
    #: Save these changes as its screen does; a reason when it may not, else None.
    save: Callable[[Any, dict], Awaitable[str | None]]
    #: The names its settings route writes.
    writable: Callable[[], set[str]]


_host: SettingsHost | None = None


def set_settings_host(host: SettingsHost | None) -> None:
    from core import settings_allow

    global _host
    _host = host
    settings_allow.set_writable(host.writable if host is not None else None)


async def current() -> dict[str, Any]:
    """Each setting Synapse AI may set here, as it reads now."""
    from core import settings_allow

    if _host is not None:
        stored = await _host.read()
    else:
        from core.config import load_settings

        stored = load_settings()
    return {name: stored.get(name) for name in settings_allow.allowed()}


async def issues(changes: Any) -> list[str]:
    """What is wrong with these changes, one line each (empty when nothing is): a name off the list, by why and
    where; a value off its type or bounds; a model this workspace cannot run (F1250's one check)."""
    from core import settings_allow
    from core.native_builder import models_menu

    if not isinstance(changes, dict) or not changes:
        return ["Say which settings change, by name, with their new values."]
    here = settings_allow.allowed()
    found: list[str] = []
    for name, value in changes.items():
        if name not in here:
            found.append(settings_allow.refusal(name))
        elif (why := settings_allow.value_issue(name, value)) is not None:
            found.append(why)
        elif here[name].kind == "model" and str(value or "").strip():
            refused, _ = await models_menu.model_refusal(value, default_ok=False)
            if refused:
                found.append(f"`{name}`: {refused}")
    return found


async def _preview(caller: Any, args: dict[str, Any]) -> dict:
    from core import settings_allow

    changes = args.get("changes")
    if found := await issues(changes):
        raise ToolRefusal(" ".join(found))
    now = await current()
    return {"changes": [{"setting": settings_allow.ALLOWED[n].label, "before": settings_allow.shown(n, now.get(n)),
                         "after": settings_allow.shown(n, v)} for n, v in changes.items()]}


async def _set(caller: Any, args: dict[str, Any]) -> dict:
    if getattr(caller, "via", None) != "assistant":
        raise ToolRefusal("Settings are changed in the app.")
    changes = args.get("changes")
    if found := await issues(changes):
        raise ToolRefusal(" ".join(found))
    if _host is not None:
        if reason := await _host.save(caller, dict(changes)):
            raise ToolRefusal(reason)
    else:
        from core.models import Settings
        from core.routes.settings import update_settings

        await update_settings(Settings(**changes))
    now = await current()
    return {"changed": {name: now.get(name) for name in changes}}


def entries() -> list[ProductTool]:
    return [ProductTool(
        name="set_settings",
        description="Change workspace settings on Synapse AI's list, as the Settings screens save them: models, "
                    "compaction, caching, tool policy, turn limits, features and this install's limits.",
        input_schema={"type": "object", "properties": {
            "changes": {"type": "object", "description": "Each setting's name and its new value."}},
            "required": ["changes"]},
        handler=_set, kind="act", area="settings", min_role=Role.ADMIN, scope="build", app_only=True,
        confirmed=True, preview=_preview, audit_action="settings.updated",
        target=lambda a, r: ("setting", ",".join(sorted((a.get("changes") or {}).keys()))),
    )]
