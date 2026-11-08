"""
REST-only message senders — one function per platform, no adapter required.

Every platform the messaging layer supports can *send* over a plain HTTPS call:
Telegram's ``sendMessage``, Slack's ``chat.postMessage``, Discord's
``/channels/{id}/messages``. None of that needs the long-lived connection the
adapters hold for *receiving* — which is exactly why this module exists apart
from them:

* The adapters' ``_send_raw`` methods delegate here, so there is one
  implementation of each platform's send rather than one per transport.
* A process that never starts an adapter at all — a scale-mode worker, an
  embedder's job runner — can deliver a message with nothing but the channel's
  credentials and a conversation id. Sending must not require listening.

``httpx`` only. Importing ``python-telegram-bot`` / ``slack-bolt`` /
``discord.py`` here would defeat the point: those are optional extras
(``requirements-messaging.txt``), and a worker image that only ever sends
should not need them installed.

Credentials come in as the channel document's ``credentials`` dict, already
resolved — if an embedder stores references instead of secrets, they were
swapped on the way out of the store (``core/store/collections.py``,
``set_document_resolver``).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from core.messaging.markdown import format_for_platform

logger = logging.getLogger(__name__)

#: One bounded wait per REST call. Sends happen on paths that must not wedge —
#: a scheduler tick, a worker job's tail — so a platform that stops answering
#: costs seconds, not a stuck task.
_TIMEOUT_SECONDS = 30

#: Matches the inter-chunk pause ``MessagingAdapter.send_message`` has always
#: used, for the same reason: platforms rate-limit bursts from one bot.
_CHUNK_PAUSE_SECONDS = 0.3


class SendError(RuntimeError):
    """A platform refused or failed a send. The message did not arrive."""


def _client():
    import httpx

    return httpx.AsyncClient(timeout=_TIMEOUT_SECONDS)


# --------------------------------------------------------------------------- #
# Raw sends — one REST call, platform-native text
# --------------------------------------------------------------------------- #

async def send_telegram_raw(credentials: dict, chat_id: str, text: str) -> None:
    """``POST /bot{token}/sendMessage`` with MarkdownV2, falling back to plain.

    The fallback is deliberate: ``to_telegram``'s escaping is best-effort, and
    Telegram answers a mis-escaped message with 400 "can't parse entities".
    Delivering the text unformatted beats delivering nothing.
    """
    token = credentials.get("bot_token", "")
    if not token:
        raise SendError("telegram: channel has no bot_token")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with _client() as client:
        resp = await client.post(
            url, json={"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2"}
        )
        if resp.status_code == 400 and "parse" in resp.text.lower():
            resp = await client.post(url, json={"chat_id": chat_id, "text": text})
        if resp.status_code != 200:
            raise SendError(f"telegram: {resp.status_code} {resp.text[:200]}")


async def send_slack_raw(
    credentials: dict, channel: str, text: str, *, thread_ts: Optional[str] = None
) -> None:
    """``chat.postMessage``. Slack signals failure in the body, not the status."""
    token = credentials.get("bot_token", "")
    if not token:
        raise SendError("slack: channel has no bot_token")
    payload: dict[str, Any] = {"channel": channel, "text": text, "mrkdwn": True}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    async with _client() as client:
        resp = await client.post(
            "https://slack.com/api/chat.postMessage",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        body = resp.json() if resp.status_code == 200 else {}
        if resp.status_code != 200 or not body.get("ok"):
            raise SendError(
                f"slack: {resp.status_code} {body.get('error') or resp.text[:200]}"
            )


async def send_discord_raw(credentials: dict, channel_id: str, text: str) -> None:
    """``POST /channels/{id}/messages`` with the bot token."""
    token = credentials.get("bot_token", "")
    if not token:
        raise SendError("discord: channel has no bot_token")
    async with _client() as client:
        resp = await client.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            json={"content": text},
            headers={"Authorization": f"Bot {token}"},
        )
        if resp.status_code not in (200, 201):
            raise SendError(f"discord: {resp.status_code} {resp.text[:200]}")


async def send_discord_followup(
    application_id: str, interaction_token: str, text: str
) -> None:
    """Answer a deferred interaction (slash command) within its 15-minute window.

    No bot token: the interaction token *is* the credential. Callers that miss
    the window fall back to ``send_discord_raw`` into the channel.
    """
    async with _client() as client:
        resp = await client.patch(
            f"https://discord.com/api/v10/webhooks/{application_id}"
            f"/{interaction_token}/messages/@original",
            json={"content": text},
        )
        if resp.status_code != 200:
            raise SendError(f"discord followup: {resp.status_code} {resp.text[:200]}")


_RAW_SENDERS = {
    "telegram": send_telegram_raw,
    "slack": send_slack_raw,
    "discord": send_discord_raw,
}


def supports_rest_send(platform: str) -> bool:
    """True when :func:`send` can deliver for this platform.

    Teams needs the Bot Framework connector and WhatsApp's two paths are the
    Cloud API adapter and a Playwright browser — neither sends through here.
    """
    return platform in _RAW_SENDERS


# --------------------------------------------------------------------------- #
# Formatted send — Markdown in, chunked platform-native messages out
# --------------------------------------------------------------------------- #

async def send(platform: str, credentials: dict, conversation_id: str, text: str) -> None:
    """Format ``text`` for ``platform`` and deliver it, chunked.

    The counterpart of ``MessagingAdapter.send_message`` for callers that hold
    no adapter. Unlike that method it **raises** on failure — an adapter's send
    is fire-and-forget inside a chat loop that will answer again, while callers
    here (a worker delivering a run's answer, a human-step prompt) need to know
    the message did not arrive.
    """
    sender = _RAW_SENDERS.get(platform)
    if sender is None:
        raise SendError(f"{platform}: no REST sender for this platform")
    chunks = format_for_platform(platform, text)
    for index, chunk in enumerate(chunks):
        await sender(credentials, conversation_id, chunk)
        if index < len(chunks) - 1:
            await asyncio.sleep(_CHUNK_PAUSE_SECONDS)
