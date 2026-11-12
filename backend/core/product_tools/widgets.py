"""Website widgets, as product tools (F1262, REACH2): what the Widgets screen does, for Synapse AI's cards.

"Create chat widgets" (Naveen, 10-30): a widget is made or changed from a card the person presses, through the screen's
own code (`core.chat_widgets.service`, the one place its routes call too) — its agent, the sites it may sit on, its
look, the tools a visitor's turn may hold, its daily cap, on or off — and deleted, its key replaced or its logo set.
The made widget's snippet is on the card once it is pressed.

In the app only (`app_only`, TRI1 10-30): `visitor_tools` decides what an anonymous visitor's turn may do, so no outside
client widens it in #4. A builder's, as on the screen (cloud's routes ask the same). A host whose widgets live
elsewhere (cloud: its table, plan counts, audit) reads its own through `set_widget_host`, and does each press its own way
(cloud's `assistant_hooks.ACTS`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from core.chat_widgets import service
from core.product_tools.errors import ToolRefusal
from core.product_tools.registry import ProductTool
from core.product_tools.roles import Role


@dataclass(frozen=True)
class WidgetHost:
    """What a host reads its own way, for a card: its widgets, as the self-hosted store's dicts (id, name, agent_id,
    allowed_origins, visitor_tools, the look, enabled, public_key), and the tools a visitor's turn on an agent can
    reach ([{name, category}])."""

    load: Callable[[], Awaitable[list[dict]]]
    choices: Callable[[str], Awaitable[list[dict]]]


_HOST = WidgetHost(load=service.list_widgets, choices=service.visitor_tool_choices)
_host = _HOST


def set_widget_host(host: WidgetHost | None) -> None:
    global _host
    _host = host or _HOST


def host() -> WidgetHost:
    return _host


async def _done(call):
    try:
        return await call
    except service.WidgetRefused as exc:
        raise ToolRefusal(exc.detail) from None


def _settings(args: dict) -> dict:
    return {k: args[k] for k in service.FIELDS if k in args}


def _made(widget: dict) -> dict:
    """What a press answers: never the allow-list's or the tools' detail, which the card already showed."""
    return {"id": widget.get("id"), "name": widget.get("name"), "public_key": widget.get("public_key"),
            "enabled": widget.get("enabled")}


async def _create(caller: Any, args: dict) -> dict:
    return _made(await _done(service.create(_settings(args))))


async def _update(caller: Any, args: dict) -> dict:
    return _made(await _done(service.update(str(args.get("widget_id") or ""), _settings(args))))


async def _delete(caller: Any, args: dict) -> dict:
    widget = await _done(service.get(str(args.get("widget_id") or "")))
    await _done(service.delete(widget["id"]))
    return {"deleted": widget.get("name") or widget["id"]}


async def _rotate(caller: Any, args: dict) -> dict:
    return _made(await _done(service.rotate_key(str(args.get("widget_id") or ""))))


async def _logo(caller: Any, args: dict) -> dict:
    widget_id = str(args.get("widget_id") or "")
    if args.get("remove"):
        await _done(service.remove_logo(widget_id))
        return {"id": widget_id, "logo": "removed"}
    raw = await _done(service.vault_logo(str(args.get("path") or "")))
    await _done(service.set_logo(widget_id, raw))
    return {"id": widget_id, "logo": "set"}


_LOOK = {
    "name": {"type": "string"},
    "agent_id": {"type": "string", "description": "The agent that answers."},
    "allowed_origins": {"type": "array", "items": {"type": "string"},
                        "description": "The sites it may sit on, as https://example.com."},
    "visitor_tools": {"type": "array", "items": {"type": "string"},
                      "description": "The tools a visitor's turn may use; none unless named."},
    "greeting": {"type": "string"}, "title": {"type": "string"}, "launcher_label": {"type": "string"},
    "accent": {"type": "string"}, "theme": {"type": "string"}, "position": {"type": "string"},
    "launcher": {"type": "string"}, "font": {"type": "string"}, "header_style": {"type": "string"},
    "daily_message_cap": {"type": "integer"}, "enabled": {"type": "boolean"},
}
_WIDGET = {"widget_id": {"type": "string"}}


def entries() -> list[ProductTool]:
    act = {"kind": "act", "area": "widgets", "min_role": Role.BUILDER, "scope": "build", "app_only": True}
    by_id = {"target": lambda a, r: ("widget", str(a.get("widget_id") or ""))}
    return [
        ProductTool(name="create_widget", description="Make a website widget: the agent that answers, the sites it "
                    "may sit on, its look and the tools a visitor may use.",
                    input_schema={"type": "object", "properties": dict(_LOOK), "required": ["agent_id"]},
                    handler=_create, audit_action="widget.created",
                    target=lambda a, r: ("widget", str((r or {}).get("id") or "")), **act),
        ProductTool(name="update_widget", description="Change a website widget's settings; the rest are kept.",
                    input_schema={"type": "object", "properties": {**_WIDGET, **_LOOK}, "required": ["widget_id"]},
                    handler=_update, audit_action="widget.updated", **by_id, **act),
        ProductTool(name="delete_widget", description="Delete a website widget: every page carrying its snippet "
                    "stops showing it.",
                    input_schema={"type": "object", "properties": dict(_WIDGET), "required": ["widget_id"]},
                    handler=_delete, irreversible=True, audit_action="widget.deleted", **by_id, **act),
        ProductTool(name="rotate_widget_key", description="Give a website widget a new key: the old snippet stops "
                    "working at once, and the new one must go on the site.",
                    input_schema={"type": "object", "properties": dict(_WIDGET), "required": ["widget_id"]},
                    handler=_rotate, audit_action="widget.key.rotated", **by_id, **act),
        ProductTool(name="set_widget_logo", description="Set a website widget's logo from an image in the Vault, or "
                    "take it off.",
                    input_schema={"type": "object", "properties": {
                        **_WIDGET, "path": {"type": "string", "description": "The image's Vault path."},
                        "remove": {"type": "boolean"}}, "required": ["widget_id"]},
                    handler=_logo, audit_action="widget.logo_set", **by_id, **act),
    ]
