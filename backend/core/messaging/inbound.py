"""
Provider webhook payloads, normalised — pure parsing, no transport, no store.

The adapters receive a platform's shape over whatever transport they hold and
all want the same four facts out of it: where to answer, who spoke, what they
said, and an id to deduplicate redeliveries on. This module is that
translation, kept pure so any receiver can use it — the engine's webhook
routes, an embedder's own HTTP layer, a test with a fixture file.

Parsers return ``None`` for payloads that are structurally valid but carry
nothing to act on (an edited-message event, a bot's own echo, a member-join).
Returning ``None`` is not an error: platforms deliver every event class the
subscription covers, and most of them are noise to an agent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class InboundMessage:
    """One human utterance, wherever it came from."""

    platform: str
    #: Where a reply goes: Telegram chat id, Slack channel id, Discord channel id.
    conversation_id: str
    user_id: str
    text: str
    #: The provider's own delivery id, for deduplicating redeliveries.
    #: Telegram ``update_id``, Slack ``event_id``, Discord interaction ``id``.
    event_id: Optional[str] = None
    #: Slack only — answer in the thread the message started or lives in.
    thread_ts: Optional[str] = None
    #: Slack ``team_id`` / Discord ``guild_id`` — which installation this is,
    #: for receivers that serve one endpoint for many workspaces.
    external_workspace_id: Optional[str] = None
    #: Discord only — what a deferred slash-command follow-up needs.
    #: ``{"application_id": ..., "token": ...}``; the token lives 15 minutes.
    interaction: Optional[dict] = None
    #: Anything platform-specific a caller might want without re-parsing.
    raw: dict = field(default_factory=dict, repr=False)


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

def parse_telegram(update: dict) -> Optional[InboundMessage]:
    """One ``Update`` from ``getUpdates`` or a webhook — same shape either way."""
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    text = message.get("text")
    chat_id = (message.get("chat") or {}).get("id")
    if not text or chat_id is None:
        return None
    sender = message.get("from") or {}
    return InboundMessage(
        platform="telegram",
        conversation_id=str(chat_id),
        user_id=str(sender.get("id", chat_id)),
        text=text,
        event_id=str(update["update_id"]) if "update_id" in update else None,
        raw=update,
    )


# --------------------------------------------------------------------------- #
# Slack
# --------------------------------------------------------------------------- #

_SLACK_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")


def slack_url_verification(payload: dict) -> Optional[str]:
    """The challenge to echo back, when Slack is probing the endpoint.

    Sent once when the Request URL is saved. A receiver answers with the
    challenge string and does nothing else with the payload.
    """
    if payload.get("type") == "url_verification":
        return payload.get("challenge") or ""
    return None


def parse_slack(payload: dict) -> Optional[InboundMessage]:
    """An Events API ``event_callback`` carrying a DM or an @-mention.

    The same two event classes the Socket Mode adapter subscribes to, filtered
    by the same rules: DMs must be ``channel_type == "im"``, bot messages are
    ignored, and a mention has the ``<@BOTID>`` stripped from its text.
    """
    if payload.get("type") != "event_callback":
        return None
    event = payload.get("event") or {}
    etype = event.get("type")

    if etype == "message":
        if event.get("channel_type") != "im" or event.get("bot_id"):
            return None
        # A message *edit* or deletion arrives as a message with a subtype;
        # only the plain utterance is something to answer.
        if event.get("subtype"):
            return None
        text = (event.get("text") or "").strip()
    elif etype == "app_mention":
        text = _SLACK_MENTION_RE.sub("", event.get("text") or "").strip()
    else:
        return None

    channel = event.get("channel")
    if not text or not channel:
        return None
    return InboundMessage(
        platform="slack",
        conversation_id=str(channel),
        user_id=str(event.get("user") or channel),
        text=text,
        event_id=payload.get("event_id"),
        thread_ts=event.get("thread_ts") or event.get("ts"),
        external_workspace_id=payload.get("team_id"),
        raw=payload,
    )


# --------------------------------------------------------------------------- #
# Discord (Interactions webhook — slash commands)
# --------------------------------------------------------------------------- #

#: Interaction types, from Discord's API reference.
DISCORD_PING = 1
DISCORD_APPLICATION_COMMAND = 2

#: Interaction *response* types a receiver answers with.
DISCORD_PONG = {"type": 1}
#: "I heard you, the answer follows" — buys the 15-minute follow-up window.
DISCORD_DEFERRED_REPLY = {"type": 5}


def discord_is_ping(payload: dict) -> bool:
    return payload.get("type") == DISCORD_PING


def parse_discord(payload: dict) -> Optional[InboundMessage]:
    """A slash-command invocation. The text is the first string option's value.

    Discord's data model wraps the utterance in a command: ``/ask message:
    <text>``. Which command it was is in ``raw`` for callers that register
    several; the message an agent should answer is the option value.
    """
    if payload.get("type") != DISCORD_APPLICATION_COMMAND:
        return None
    data = payload.get("data") or {}
    text = next(
        (
            str(opt.get("value"))
            for opt in data.get("options") or []
            if isinstance(opt.get("value"), str) and str(opt.get("value")).strip()
        ),
        "",
    ).strip()
    channel_id = payload.get("channel_id")
    if not text or not channel_id:
        return None
    user = (payload.get("member") or {}).get("user") or payload.get("user") or {}
    interaction = None
    if payload.get("application_id") and payload.get("token"):
        interaction = {
            "application_id": str(payload["application_id"]),
            "token": str(payload["token"]),
        }
    return InboundMessage(
        platform="discord",
        conversation_id=str(channel_id),
        user_id=str(user.get("id", channel_id)),
        text=text,
        event_id=str(payload["id"]) if payload.get("id") else None,
        external_workspace_id=str(payload["guild_id"]) if payload.get("guild_id") else None,
        interaction=interaction,
        raw=payload,
    )


_PARSERS = {
    "telegram": parse_telegram,
    "slack": parse_slack,
    "discord": parse_discord,
}


def parse(platform: str, payload: dict) -> Optional[InboundMessage]:
    parser = _PARSERS.get(platform)
    return parser(payload) if parser else None
