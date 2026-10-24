"""The AI builder's connect card (BWA1, D274): credentials go to a secure screen, never into the chat.

BWA1's Nova Pro baseline saw the builder ask for a portal password, an OpenAI key and GitHub and Linear tokens in
chat. The door (`core/native_builder/door.py`) refuses or masks what arrives anyway; this is the way it should go
instead. `prepare_connection` does not connect anything and never sees a secret. It checks what the builder wants
connected against what this deployment offers, and puts a card in front of the person: an app's Connect button
(its own sign-in, or a token field), a remote MCP server, a model provider's key, a custom tool's header secret, or
a link to the right settings screen. The person's browser sends what they type straight to the product's own
routes (each host's panel has the adapter), and the builder hears only "Connected …" from the conversation.

Nothing connects silently (decision 3): the card is the person's to press.
"""
from __future__ import annotations

import uuid
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from core.builder_tools import register_builder_tool

#: The settings screens a card may link to. Each host's panel maps an id to its own path.
SCREENS: dict[str, str] = {
    "models": "Models and provider keys",
    "apps": "Apps and MCP servers",
    "channels": "Channels (Slack, Telegram, Discord)",
    "phone": "Phone numbers",
    "widgets": "Website widgets",
    "tools": "Custom tools",
    "variables": "Variables and secrets",
    "members": "Members and roles",
    "schedules": "Schedules",
    "billing": "Plan and billing",
}

#: Model providers whose keys a card can take, as each host names them. Cloud's `PROVIDER_KEYS` and the
#: self-hosted settings both name these the same way; a host can narrow or extend the list.
PROVIDERS: dict[str, str] = {
    "openai": "OpenAI", "anthropic": "Anthropic", "gemini": "Google Gemini", "grok": "xAI Grok",
    "deepseek": "DeepSeek", "mistral": "Mistral", "moonshot": "Moonshot (Kimi)", "qwen": "Qwen",
}

Options = dict[str, Any]
_options: Callable[[], Awaitable[Options]] | None = None


def set_connect_options(reader: Callable[[], Awaitable[Options]] | None) -> None:
    """A host's own catalog and lists: `{"catalog": [{"slug", "name", "auth", "url"}], "providers": {id: label},
    "screens": {id: label}}`. Self-hosted has no Apps catalog, so its card offers servers by URL, keys and links."""
    global _options
    _options = reader


async def _read_options() -> Options:
    if _options is None:
        return {"catalog": [], "providers": PROVIDERS, "screens": SCREENS}
    got = await _options()
    return {"catalog": got.get("catalog") or [], "providers": got.get("providers") or PROVIDERS,
            "screens": got.get("screens") or SCREENS}


_KINDS = ("app", "mcp_server", "provider_key", "tool_secret", "settings")

SCHEMA = {
    "type": "function",
    "function": {
        "name": "prepare_connection",
        "description": (
            "Put a Connect card in front of the person, so a credential or a sign-in goes to a secure screen and "
            "never into this chat. Use it whenever something needs a key, a token, a password or a sign-in. Kinds: "
            "`app` (an app from this workspace's Apps catalog, by `catalog_slug`), `mcp_server` (a remote MCP "
            "server at an https `url`), `provider_key` (a model provider's API key, by `provider`), `tool_secret` "
            "(the secret header of an existing custom tool, by `tool_name` and `header`), `settings` (a link to a "
            "settings screen, by `screen`). Nothing connects until the person presses Connect on the card; you then "
            "hear \"Connected …\" from them. Never ask the person to type a secret in the chat."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(_KINDS)},
                "title": {"type": "string", "description": "What to connect, in a few words: 'Connect Notion'."},
                "why": {"type": "string", "description": "One sentence: what the connection is for."},
                "catalog_slug": {"type": "string", "description": "kind=app: the app's slug in the Apps catalog."},
                "url": {"type": "string", "description": "kind=mcp_server: the server's https URL."},
                "server_name": {"type": "string", "description": "kind=mcp_server: a short name for it."},
                "provider": {"type": "string", "description": "kind=provider_key: e.g. openai, anthropic, gemini."},
                "tool_name": {"type": "string", "description": "kind=tool_secret: the custom tool's name."},
                "header": {"type": "string", "description": "kind=tool_secret: the header that carries the key."},
                "screen": {"type": "string", "description": f"kind=settings: one of {', '.join(SCREENS)}."},
            },
            "required": ["kind", "title"],
        },
    },
}


async def _https_outward(url: str) -> str | None:
    """Why `url` is not a remote server the card may offer, or None when it is one."""
    parsed = urlparse(url or "")
    if parsed.scheme != "https" or not parsed.hostname:
        return "A remote MCP server needs an https URL."
    from core.builder_tools import _refuse_inward_url

    refused = await _refuse_inward_url(url)
    return (refused.get("error") or str(refused)) if refused else None


def _slug(text: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in (text or "").lower()).strip("-")
    return "-".join(part for part in out.split("-") if part)[:40] or "server"


async def prepare_connection(args: dict, server_module: Any, events: list[dict]) -> dict:
    kind = (args.get("kind") or "").strip()
    if kind not in _KINDS:
        return {"error": f"kind must be one of {', '.join(_KINDS)}."}
    options = await _read_options()
    card: dict[str, Any] = {
        "type": "connection_needed",
        "id": f"conn_{uuid.uuid4().hex[:10]}",
        "kind": kind,
        "title": (args.get("title") or "").strip()[:80] or "Connect",
        "why": (args.get("why") or "").strip()[:300],
    }

    if kind == "app":
        slug = (args.get("catalog_slug") or "").strip().lower()
        entry = next((e for e in options["catalog"] if str(e.get("slug", "")).lower() == slug), None)
        if entry is None:
            known = ", ".join(sorted(str(e.get("slug")) for e in options["catalog"])[:60]) or "none here"
            return {"error": f"'{slug}' is not in this workspace's Apps catalog (it has: {known}). For a server "
                             "found elsewhere, use kind=mcp_server with its https URL."}
        card["app"] = {k: entry.get(k) for k in ("slug", "name", "auth", "url", "token_hint")}

    elif kind == "mcp_server":
        url = (args.get("url") or "").strip()
        why_not = await _https_outward(url)
        if why_not:
            return {"error": why_not}
        name = _slug(args.get("server_name") or urlparse(url).hostname or "")
        card["server"] = {"name": name, "label": (args.get("server_name") or name).strip()[:60], "url": url}

    elif kind == "provider_key":
        provider = (args.get("provider") or "").strip().lower()
        if provider not in options["providers"]:
            return {"error": f"Unknown provider '{provider}'. Known: {', '.join(options['providers'])}."}
        card["provider"] = {"id": provider, "label": options["providers"][provider]}

    elif kind == "tool_secret":
        from core.store.resources import load_tools

        tool_name = (args.get("tool_name") or "").strip()
        header = (args.get("header") or "").strip()
        tools = {t.get("name"): t for t in await load_tools()}
        if isinstance(args.get("tools"), list):
            # One card for every tool a build made for one provider (F945): its key once, with its scheme and the
            # account part of its host, saved by the server (`/api/tools/connect`), never by the browser alone.
            names = [str(n) for n in args["tools"] if str(n) in tools and tools[str(n)].get("base_lock")]
            if not names or len(names) != len(args["tools"]):
                return {"error": "Each tool on this card must be one the build made, and made already."}
            if not header or any(c.isspace() for c in header):
                return {"error": "Name the header that carries the key, e.g. X-Api-Key or Authorization."}
            card["tool"] = {"id": names[0], "name": (args.get("provider") or names[0]).strip()[:60],
                            "header": header, "scheme": str(args.get("scheme") or ""), "tools": names,
                            "host_params": [{"name": str(p.get("name")), "says": str(p.get("says") or "")[:160]}
                                            for p in args.get("host_params") or [] if isinstance(p, dict)]}
            events.append(card)
            return {"status": "card_shown", "card_id": card["id"]}
        if tool_name not in tools:
            return {"error": f"No custom tool named '{tool_name}'. Create it first, then ask for its key here."}
        if not header or any(c.isspace() for c in header):
            return {"error": "Name the header that carries the key, e.g. X-Api-Key or Authorization."}
        card["tool"] = {"id": tools[tool_name].get("id") or tool_name, "name": tool_name, "header": header}

    elif kind == "settings":
        screen = (args.get("screen") or "").strip().lower()
        if screen not in options["screens"]:
            return {"error": f"Unknown screen '{screen}'. Known: {', '.join(options['screens'])}."}
        card["screen"] = {"id": screen, "label": options["screens"][screen]}

    events.append(card)
    return {"status": "card_shown", "id": card["id"],
            "say": "Tell the person the card above is where the connection happens, and to press Connect there. "
                   "Do not ask for the key, token or password in the chat."}


register_builder_tool(SCHEMA, prepare_connection)
