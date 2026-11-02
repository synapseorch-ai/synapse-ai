"""What the Channels screen does, once, for every door that does it (F1263, REACH2; the one-place rule, F1189).

The screen's routes (`core.routes.messaging`'s channel management) and the product tools Synapse AI's cards press
(`core.product_tools.channels`) call these, so each door says the same thing for the same reason. Plain async functions
over dicts: no request, no response type. A refusal is a `ChannelRefused` with the words the screen has always shown and
the HTTP status its route answers with.

A host whose channels live elsewhere (cloud: its tables, webhook delivery, OAuth installs, its audit) does each act its
own way; these stay self-hosted's.
"""
from __future__ import annotations

from typing import Any

from core.messaging import store as channel_store
from core.messaging.store import SECRET_CREDENTIALS, SERVER_ONLY_CREDENTIALS, redact_secrets

#: The one line a test sends.
TEST_TEXT = "Test message from Synapse — this channel is connected."


class ChannelRefused(Exception):
    """Why the channel was not changed, in the screen's words, with the status its route answers."""

    def __init__(self, detail: str, status: int = 400):
        super().__init__(detail)
        self.detail, self.status = detail, status


def manager(app_state: Any = None) -> Any:
    """The running MessagingManager, or the screen's 503 when messaging is off."""
    found = getattr(app_state, "messaging_manager", None) if app_state is not None else None
    if found is None:
        import core.server as _server

        found = _server.messaging_manager
    if found is None:
        raise ChannelRefused("Messaging is not enabled. Set messaging_enabled=true in setup.py and restart.", 503)
    return found


def public(ch: dict) -> dict:
    """A channel as the browser may see it: no secrets (F207, F135's pattern).

    Every bot token and app password used to be served in clear by the list,
    the save and the enable. The screen needs to know which are set, and the
    plain fields (an app id, a phone number id, a channel restriction) as they
    are. A copy: the store's document is not touched.
    """
    creds = ch.get("credentials") or {}
    view = dict(ch)
    view["credentials"] = {
        k: ("" if k in SECRET_CREDENTIALS else v)
        for k, v in creds.items() if k not in SERVER_ONLY_CREDENTIALS
    }
    view["credentials_set"] = {k: bool(v) for k, v in creds.items() if k in SECRET_CREDENTIALS}
    view["last_error"] = redact_secrets(ch.get("last_error"), ch)
    return view


async def get(channel_id: str) -> dict:
    ch = await channel_store.get_channel(channel_id) if channel_id else None
    if not ch:
        raise ChannelRefused("Channel not found", 404)
    return ch


async def save(body: dict) -> dict:
    """Create or update a channel config, as the screen's form sends it. Does NOT start the adapter."""
    from core.messaging.voice import languages as voice_languages

    body = dict(body)
    # Strip runtime-only fields from client payload
    body.pop("status", None)
    body.pop("last_error", None)
    body.pop("credentials_set", None)
    if body.get("platform") == "twilio_voice":
        # A language or provider Twilio can't serve would end every call (VOX1d); say so at the save.
        problems = voice_languages.check(body.get("voice"))
        if problems:
            raise ChannelRefused(" ".join(problems), 422)
    # A save is a full replace, and the screen never has the secrets to send
    # back: one it leaves blank keeps what is stored (F207). Only on the same
    # platform: a secret is its platform's, so a channel switched to another
    # carries none across (F499) — a Telegram token must never reach Discord.
    # Telegram mints its webhook secret again at its start.
    existing = await channel_store.get_channel(body["id"]) if body.get("id") else None
    if existing and existing.get("platform") == body.get("platform"):
        creds = dict(body.get("credentials") or {})
        for k, v in (existing.get("credentials") or {}).items():
            if (k in SECRET_CREDENTIALS or k in SERVER_ONLY_CREDENTIALS) and not creds.get(k):
                creds[k] = v
        body["credentials"] = creds
    return await channel_store.save_channel(body)


async def enable(channel_id: str, app_state: Any = None) -> dict:
    """Start the adapter for a channel; the channel as it now stands (a refused token is `error`, with its reason)."""
    mgr = manager(app_state)
    ch = await get(channel_id)
    # Mark as enabled in store before starting
    ch["enabled"] = True
    await channel_store.save_channel(ch)
    await mgr.start_channel(channel_id)
    return await channel_store.get_channel(channel_id) or ch


async def disable(channel_id: str, app_state: Any = None) -> list[str]:
    """Stop the adapter for a channel; what Twilio kept, said (VOX1c)."""
    mgr = manager(app_state)
    ch = await get(channel_id)
    await mgr.stop_channel(channel_id)
    from core.routes.messaging import _disconnect_number

    left = await _disconnect_number(ch)
    ch = await channel_store.get_channel(channel_id) or ch
    ch["enabled"] = False
    await channel_store.save_channel(ch)
    return left


async def delete(channel_id: str, app_state: Any = None) -> list[str]:
    """Stop and delete a channel; what Twilio kept, said."""
    mgr = manager(app_state)
    await mgr.stop_channel(channel_id)
    from core.routes.messaging import _disconnect_number

    left = await _disconnect_number(await channel_store.get_channel(channel_id))
    if not await channel_store.delete_channel(channel_id):
        raise ChannelRefused("Channel not found", 404)
    return left


async def set_agents(channel_id: str, agent_ids: Any, default_agent_id: str | None = None) -> list[dict]:
    """Replace a channel's agents in one call.

    The whole-list write the settings screen uses: its chips describe a set, so
    the request describes a set. Every id is checked against the agent store
    first — an assignment naming an agent that does not exist is the bug D68
    already paid for once, where a dangling `agent_id` silently ran whichever
    agent happened to be first.
    """
    await get(channel_id)
    if not isinstance(agent_ids, list) or not all(isinstance(a, str) for a in agent_ids):
        raise ChannelRefused("agent_ids must be a list of agent ids", 422)

    from core.routes.agents import load_user_agents

    known = {a["id"] for a in await load_user_agents()}
    unknown = [a for a in agent_ids if a not in known]
    if unknown:
        raise ChannelRefused(f"Unknown agent(s): {', '.join(unknown)}", 422)
    if default_agent_id and default_agent_id not in agent_ids:
        raise ChannelRefused("default_agent_id must be one of agent_ids", 422)
    return await channel_store.set_assignments(channel_id, agent_ids, default_agent_id=default_agent_id)


async def send_test(channel_id: str) -> dict:
    """One line through the real send path, to the conversation the bot last answered, once (TRI1, F1263: never
    retried; cloud's Test does the same). Where it went is said; a platform that sends only through its adapter is
    tested on the Channels screen."""
    from core.messaging import senders

    ch = await get(channel_id)
    platform = str(ch.get("platform") or "")
    if not senders.supports_rest_send(platform):
        raise ChannelRefused(f"A {platform.title()} channel is tested on the Channels screen.", 409)
    conversation = ch.get("default_conversation_id")
    if not conversation:
        raise ChannelRefused("Message the bot once first, so it learns where to answer.", 409)
    try:
        await senders.send(platform, ch.get("credentials") or {}, str(conversation), TEST_TEXT)
    except Exception as exc:  # noqa: BLE001 — the platform's own refusal, its secrets blanked
        raise ChannelRefused(f"It did not send: {redact_secrets(str(exc), ch)}", 502) from None
    return {"status": "sent", "to": str(conversation), "text": TEST_TEXT}


async def set_run_notifications(channel_id: str) -> str:
    """Where every run's notice is also sent (`run_notification_channel_id`, the Channels screen's own setting); ""
    sends none. Saved through the settings route, as the screen saves it."""
    from core.models import Settings
    from core.routes.settings import update_settings

    if channel_id:
        await get(channel_id)
    await update_settings(Settings(run_notification_channel_id=channel_id))
    return channel_id
