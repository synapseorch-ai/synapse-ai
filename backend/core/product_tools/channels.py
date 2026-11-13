"""Chat channels, as product tools (F1263, REACH2): what the Channels screen does, for Synapse AI's cards.

"Connect to channels" (Naveen, 10-30): a channel is connected on a Connect card, where the person types its token
(`builder_connect`'s kind `channel`, F1093: never in chat). Once it is there, these do the rest through the screen's own
code (`core.messaging.service`, the one place its routes call too): turn it on or off, remove it, send one test line,
say which agents answer, and send every run's notice there.

In the app only (`app_only`, TRI1 10-30) and a builder's, as on the screen (cloud's routes ask the same). A host whose
channels live elsewhere (cloud: its tables, webhooks, its audit) reads its own through `set_channel_host`, and does each
press its own way (cloud's `assistant_hooks.ACTS`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from core.messaging import service
from core.product_tools.errors import ToolRefusal
from core.product_tools.registry import ProductTool
from core.product_tools.roles import Role


async def _oss_load() -> list[dict]:
    from core.messaging import store

    out = []
    for ch in await store.list_channels():
        if ch.get("platform") == "twilio_voice":
            continue  # a phone number is the Phone screen's
        out.append({"id": ch["id"], "name": ch.get("name") or ch["id"], "platform": ch.get("platform"),
                    "enabled": bool(ch.get("enabled")), "status": ch.get("status"),
                    "agent_ids": await store.get_agent_ids_for_channel(ch["id"]),
                    "default_agent_id": await store.get_default_agent_id(ch["id"]),
                    "has_conversation": bool(ch.get("default_conversation_id")),
                    "conversation": ch.get("default_conversation_id"),
                    # Whether its senders are visitors, and what they may use (F282): a card says what they can't
                    # reach (F1570).
                    "senders_are_members": ch.get("senders_are_members", True) is not False,
                    "visitor_tools": list(ch.get("visitor_tools") or [])})
    return out


async def _oss_run_notifications() -> str | None:
    from core.config import load_settings

    return str((load_settings() or {}).get("run_notification_channel_id") or "")


@dataclass(frozen=True)
class ChannelHost:
    """What a host reads its own way, for a card: its chat channels as {id, name, platform, enabled, status,
    agent_ids, default_agent_id, has_conversation, senders_are_members, visitor_tools}, and the channel every run's
    notice goes to ("" none; None: this host sends run notices some other way, so a card for it is not offered). A
    host that leaves out senders_are_members has its channels read as members' (no visitor line on a card)."""

    load: Callable[[], Awaitable[list[dict]]]
    run_notifications: Callable[[], Awaitable[str | None]]


_HOST = ChannelHost(load=_oss_load, run_notifications=_oss_run_notifications)
_host = _HOST


def set_channel_host(host: ChannelHost | None) -> None:
    global _host
    _host = host or _HOST


def host() -> ChannelHost:
    return _host


async def _done(call):
    try:
        return await call
    except service.ChannelRefused as exc:
        raise ToolRefusal(exc.detail) from None


def _id(args: dict) -> str:
    return str(args.get("channel_id") or "")


async def _enabled(caller: Any, args: dict) -> dict:
    if args.get("enabled"):
        ch = await _done(service.enable(_id(args)))
        if ch.get("status") == "error":
            raise ToolRefusal(f"It did not start: {service.public(ch).get('last_error') or 'unknown error'}")
        return {"channel_id": ch["id"], "enabled": True}
    left = await _done(service.disable(_id(args)))
    return {"channel_id": _id(args), "enabled": False, **({"left": left} if left else {})}


async def _delete(caller: Any, args: dict) -> dict:
    ch = await _done(service.get(_id(args)))
    left = await _done(service.delete(ch["id"]))
    return {"deleted": ch.get("name") or ch["id"], **({"left": left} if left else {})}


async def _test(caller: Any, args: dict) -> dict:
    sent = await _done(service.send_test(_id(args)))
    return {**sent, "say": f"Sent one test line to the conversation it last answered ({sent['to']})."}


async def _agents(caller: Any, args: dict) -> dict:
    ids = args.get("agent_ids")
    await _done(service.set_agents(_id(args), ids, args.get("default_agent_id") or None))
    return {"channel_id": _id(args), "agent_ids": ids, "default_agent_id": args.get("default_agent_id")}


async def _notices(caller: Any, args: dict) -> dict:
    return {"run_notification_channel_id": await _done(service.set_run_notifications(_id(args)))}


_CHANNEL = {"channel_id": {"type": "string"}}


def entries() -> list[ProductTool]:
    act = {"kind": "act", "area": "messaging", "min_role": Role.BUILDER, "scope": "build", "app_only": True}
    by_id = {"target": lambda a, r: ("messaging_channel", _id(a))}
    return [
        ProductTool(name="set_channel_enabled", description="Turn a chat channel on (its bot starts answering) or off.",
                    input_schema={"type": "object", "properties": {**_CHANNEL, "enabled": {"type": "boolean"}},
                                  "required": ["channel_id", "enabled"]},
                    handler=_enabled, idempotent=True, audit_action="messaging_channel.updated", **by_id, **act),
        ProductTool(name="delete_channel", description="Remove a chat channel: its bot stops answering here.",
                    input_schema={"type": "object", "properties": dict(_CHANNEL), "required": ["channel_id"]},
                    handler=_delete, irreversible=True, audit_action="messaging_channel.deleted", **by_id, **act),
        ProductTool(name="test_channel", description="Send one test line to the conversation the channel last "
                    "answered.",
                    input_schema={"type": "object", "properties": dict(_CHANNEL), "required": ["channel_id"]},
                    handler=_test, open_world=True, audit_action="messaging_channel.tested", **by_id, **act),
        ProductTool(name="set_channel_agents", description="Say which agents answer on a chat channel, and which one "
                    "answers first.",
                    input_schema={"type": "object", "properties": {
                        **_CHANNEL, "agent_ids": {"type": "array", "items": {"type": "string"}},
                        "default_agent_id": {"type": "string"}}, "required": ["channel_id", "agent_ids"]},
                    handler=_agents, idempotent=True, audit_action="messaging_channel.agents_set", **by_id, **act),
        ProductTool(name="set_run_notification_channel", description="Send every run's notice to a chat channel too, "
                    "or to none (an empty channel_id).",
                    input_schema={"type": "object", "properties": dict(_CHANNEL), "required": ["channel_id"]},
                    handler=_notices, idempotent=True, audit_action="settings.updated",
                    target=lambda a, r: ("setting", "run_notification_channel_id"), **act),
    ]
