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
    "screens": {id: label}, "channels": {platform: {"label", "fields", "signin"}}}`. Self-hosted has no Apps catalog,
    so its card offers servers by URL, keys and links; its channels are `core.messaging.fields`'."""
    global _options
    _options = reader


async def _read_options() -> Options:
    if _options is None:
        return {"catalog": [], "providers": PROVIDERS, "screens": SCREENS, "channels": None}
    got = await _options()
    return {"catalog": got.get("catalog") or [], "providers": got.get("providers") or PROVIDERS,
            "screens": got.get("screens") or SCREENS, "channels": got.get("channels")}


_KINDS = ("app", "mcp_server", "provider_key", "tool_secret", "settings", "channel")

SCHEMA = {
    "type": "function",
    "function": {
        "name": "prepare_connection",
        "description": (
            "Put a Connect card in front of the person, so a credential or a sign-in goes to a secure screen and "
            "never into this chat. Use it whenever something needs a key, a token, a password or a sign-in. Kinds: "
            "`app` (an app from this workspace's Apps catalog, by `catalog_slug`), `mcp_server` (a remote MCP "
            "server at its http or https `url`), `provider_key` (a model provider's API key, by `provider`), `tool_secret` "
            "(the secret header of an existing custom tool, by `tool_name` and `header`), `settings` (a link to a "
            "settings screen, by `screen`), `channel` (a chat channel: `platform`, its `name`, the `agent` that answers; the person "
            "types its token into the card). Nothing connects until the person presses Connect on the card; you then "
            "hear \"Connected …\" from them. Never ask the person to type a secret in the chat."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(_KINDS)},
                "title": {"type": "string", "description": "What to connect, in a few words: 'Connect Notion'."},
                "why": {"type": "string", "description": "One sentence: what the connection is for."},
                "catalog_slug": {"type": "string", "description": "kind=app: the app's slug in the Apps catalog."},
                "url": {"type": "string", "description": "kind=mcp_server: the server's URL (http or https)."},
                "server_name": {"type": "string", "description": "kind=mcp_server: a short name for it."},
                "provider": {"type": "string", "description": "kind=provider_key: e.g. openai, anthropic, gemini."},
                "tool_name": {"type": "string", "description": "kind=tool_secret: the custom tool's name."},
                "header": {"type": "string", "description": "kind=tool_secret: the header that carries the key."},
                "screen": {"type": "string", "description": f"kind=settings: one of {', '.join(SCREENS)}."},
                "platform": {"type": "string", "description": "kind=channel: telegram, slack, discord, teams or "
                                                              "whatsapp."},
                "name": {"type": "string", "description": "kind=channel: what the channel is called here."},
                "agent": {"type": "string", "description": "kind=channel: the agent that answers on it, by name."},
            },
            "required": ["kind", "title"],
        },
    },
}


#: Said after the private-host refusal: the card stops there, the Apps screen does not (F1201, Naveen 10-29).
_ADD_IT_YOURSELF = "A server on your own machine or network is added on the Apps screen, by you."


async def _outward(url: str) -> str | None:
    """Why `url` is not a remote server the card may offer, or None when it is one: the Apps screen's rule
    (`mcp_client.remote_url_problem`), and, as a model wrote the URL, never a host inside this network unless the
    deployment lets it be reached (`EGRESS_ALLOW_HOSTS`)."""
    from core.mcp_client import remote_url_refusal

    if (problem := await remote_url_refusal(url)) is not None:
        return problem
    from core.builder_tools import _refuse_inward_url

    refused = await _refuse_inward_url(url)
    return f"{refused.get('error') or refused} {_ADD_IT_YOURSELF}" if refused else None


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
                             "found elsewhere, use kind=mcp_server with its URL."}
        card["app"] = {k: entry.get(k) for k in ("slug", "name", "auth", "url", "token_hint")}

    elif kind == "mcp_server":
        url = (args.get("url") or "").strip()
        why_not = await _outward(url)
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
            # Where the key goes, said before it is entered, and a host that doesn't look like the provider's own
            # warned on (F1244: the research names its domains; code's name rule is a warning there, not a gate).
            from core.builder_research import _providers_own_page

            sends_to = sorted({_key_host(tools[n].get("base_lock")) for n in names} - {""})
            provider = (args.get("provider") or "").strip()
            card["tool"] = {"id": names[0], "name": (provider or names[0])[:60],
                            "header": header, "scheme": str(args.get("scheme") or ""), "tools": names,
                            "sends_to": sends_to,
                            "unsure": [h for h in sends_to if provider and not _providers_own_page(provider, h)],
                            "host_params": [{"name": str(p.get("name")), "says": str(p.get("says") or "")[:160]}
                                            for p in args.get("host_params") or [] if isinstance(p, dict)]}
            events.append(card)
            return {"status": "card_shown", "card_id": card["id"]}
        if tool_name not in tools:
            return {"error": f"No custom tool named '{tool_name}'. Create it first, then ask for its key here."}
        if not header or any(c.isspace() for c in header):
            return {"error": "Name the header that carries the key, e.g. X-Api-Key or Authorization."}
        card["tool"] = {"id": tools[tool_name].get("id") or tool_name, "name": tool_name, "header": header,
                        "sends_to": [h for h in [_key_host(tools[tool_name].get("url"))] if h], "unsure": []}

    elif kind == "channel":
        found = await _channel(args, options)
        if "error" in found:
            return found
        card["channel"] = found

    elif kind == "settings":
        screen = (args.get("screen") or "").strip().lower()
        if screen not in options["screens"]:
            return {"error": f"Unknown screen '{screen}'. Known: {', '.join(options['screens'])}."}
        card["screen"] = {"id": screen, "label": options["screens"][screen]}
        if waits_for := str(args.get("waits_for") or "").strip():
            # A build waits on this screen (F1104): the card's own press says "Connected X.", and the build's check of
            # what is here decides, never the press.
            card["waits_for"] = waits_for[:80]

    events.append(card)
    return {"status": "card_shown", "id": card["id"],
            "say": "Tell the person the card above is where the connection happens, and to press Connect there. "
                   "Do not ask for the key, token or password in the chat."}


async def _channel(args: dict, options: Options) -> dict:
    """A chat channel's card (F1263): the platform's fields for the person to type (never the model's), the agent that
    answers, and for a webhook platform this install's address it will be called at. A phone number is the Phone
    screen's."""
    from core.messaging import fields

    platform = (args.get("platform") or "").strip().lower()
    # Connecting one is the Channels screen's, a builder's there and on cloud's routes (F1313, 79): one who may not
    # gets no card they cannot use, and is told who can.
    from core.builder_actions import _caller
    from core.product_tools.roles import Role

    role = _caller().role
    if not role.can_act_as(Role.BUILDER):
        article = "an" if role.label[:1] in "aeiou" else "a"
        return {"error": f"Connecting a chat channel needs the builder role, and they are {article} {role.label} here: "
                         "put up no card, and say someone with the builder role can connect it on the Channels "
                         "screen.", "role": role.label}
    hosted = options.get("channels")
    offered = hosted if hosted is not None else fields.PLATFORMS
    if platform in ("twilio_voice", "phone", "voice"):
        return {"error": "A phone number is set up on the Phone screen, not from here: say so, and offer a settings "
                         "card for screen `phone`."}
    if platform not in offered:
        return {"error": f"Unknown platform '{platform}'. This workspace connects: {', '.join(offered)}."}
    entry = offered[platform]
    out: dict[str, Any] = {"platform": platform, "label": entry.get("label") or platform.title(),
                           "name": (args.get("name") or "").strip()[:100] or entry.get("label") or platform}
    if wanted := (args.get("agent") or "").strip():
        from core.builder_actions import _NotOffered, _pick
        from core.store.resources import load_agents

        agents = [a for a in await load_agents() if not a.get("system") and a.get("type") != "builder"]
        try:
            agent = _pick(agents, wanted, "agent", "Topic `agents` lists them.")
        except _NotOffered as why:
            return {"error": str(why)}
        out["agent"] = {"id": agent["id"], "name": agent.get("name") or agent["id"]}
    if hosted is not None:  # the host's own way: its fields, or its sign-in
        return {**out, "fields": list(entry.get("fields") or []), "signin": bool(entry.get("signin"))}
    from core.config import load_settings
    from core.messaging import service

    try:
        service.manager()
    except service.ChannelRefused as why:
        return {"error": why.detail}
    out.update(fields=fields.card_fields(platform), mode=entry["mode"], signin=False)
    if entry["mode"] == "webhook":
        base = str((load_settings() or {}).get("messaging_webhook_base_url") or "").strip()
        if not base:
            return {"error": f"{entry['label']} calls this app at a public address, and none is set. It is set on the "
                             "Channels screen (Public webhook address): offer a settings card for screen `channels`."}
        out.update(webhook_base=base.rstrip("/"), paste_into=entry["paste_into"])
    return out


def _key_host(url: Any) -> str:
    """The host a tool's key is sent to, as the card says it: its URL's host, without a placeholder label it starts
    with (`{dc}.api.mailchimp.com`, or `__dc__.` as a made tool keeps it, → `api.mailchimp.com`), which the person's
    own value fills."""
    host = (urlparse(str(url or "")).hostname or "") if "//" in str(url or "") else ""
    labels = host.split(".")
    placeholder = bool(labels) and (labels[0].startswith("{") or labels[0].startswith("__") and labels[0].endswith("__"))
    return ".".join(labels[1:] if placeholder else labels)


register_builder_tool(SCHEMA, prepare_connection)
