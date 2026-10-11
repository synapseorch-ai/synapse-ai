"""Connecting a number (V4, V5, V7): check the key, point the number at us, put it back on disconnect.

The channel document carries:

* ``credentials``: ``account_sid``, ``api_key_sid``, ``api_key_secret`` (a secret), ``route_token`` (minted
  here, server-only: it names the channel in every URL Twilio is given) and, when the key may create one,
  ``signing_secret`` (the number's SharedKey, a secret);
* ``voice``: ``number`` (E.164), ``number_sid``, ``previous`` (where the number pointed before us, restored on
  disconnect), ``notice``, ``filler``, ``filler_late``, ``transfer_number``, ``recording``, ``language``,
  ``voice``, ``tts_provider``, ``max_minutes``; ``signing`` (the SharedKey's, Setting's and Rule's ids) or
  ``signing_error`` (why webhooks are not signed: a Restricted key); and, when the key cannot change the
  number, ``manual_url`` and ``point_error`` (V7: degrade, don't fail).

What admits a call (V6, as measured in VOX1a): the route token in every URL, the one-time socket token, and a
SharedKey signature on every webhook when the number has one. Twilio's REST API could not confirm a call
(`GET /Calls/{sid}` was a 404 for every real call), so that check was dropped.
"""
from __future__ import annotations

import logging

from core.messaging.voice import PLATFORM, hooks
from core.messaging.voice.provider import CallRefused
from core.messaging.voice.tokens import new_route_token

log = logging.getLogger("voice.connect")

#: Where a channel's webhooks live, under the public base.
WEBHOOK = "/api/messaging/" + PLATFORM + "/webhook/{route_token}"


def provider_for(credentials: dict):
    from core.messaging.voice.twilio import TwilioProvider
    return TwilioProvider.from_credentials(credentials)


def urls(base: str, route_token: str) -> dict:
    root = base.rstrip("/") + WEBHOOK.format(route_token=route_token)
    wss = "wss://" + root.split("://", 1)[1] if "://" in root else root
    return {"root": root, "incoming": root + "/incoming", "end": root + "/end", "dialed": root + "/dialed",
            "recorded": root + "/recorded", "status": root + "/status", "ws": wss + "/ws"}


def _same(a, b) -> bool:
    def digits(n):
        return "".join(ch for ch in str(n or "") if ch.isdigit())
    return bool(digits(a)) and digits(a) == digits(b)


async def connect(channel: dict) -> dict:
    """Check the key and the number, then point the number at this server. Returns the saved channel.

    Raises `RuntimeError` with the reason a person can act on (the channel's error). A key that may read but
    not change numbers connects anyway, with the URL to paste into Twilio's console (V7).
    """
    from core.messaging import store as channel_store

    host = hooks.current()
    base = host.public_base()
    if not base.startswith("https://"):
        raise RuntimeError("Set the public URL (messaging_webhook_base_url) to an https address Twilio can "
                           "reach, for example a cloudflared tunnel, then turn the number on again.")
    creds = await host.secrets(channel)
    if not (creds.get("account_sid") and creds.get("api_key_sid") and creds.get("api_key_secret")):
        raise RuntimeError("Add the Twilio Account SID, API key SID and API key secret.")
    provider = provider_for(creds)
    voice = dict(channel.get("voice") or {})
    try:
        numbers = await provider.list_numbers()
    except CallRefused as exc:
        raise RuntimeError(exc.reason) from exc
    match = next((n for n in numbers if (voice.get("number_sid") and n.sid == voice.get("number_sid"))
                  or _same(n.number, voice.get("number"))), None)
    if match is None:
        raise RuntimeError("That number is not on this Twilio account.")
    voice["number"], voice["number_sid"] = match.number, match.sid

    stored = await channel_store.get_channel(channel["id"]) or channel
    credentials = dict(stored.get("credentials") or {})
    if not credentials.get("route_token"):
        credentials["route_token"] = new_route_token()
    to = urls(base, credentials["route_token"])
    voice.pop("manual_url", None)
    voice.pop("point_error", None)
    if match.voice_url != to["incoming"]:
        try:
            previous = await provider.point_number(match.sid, voice_url=to["incoming"], status_url=to["status"])
            # Only the first time: pointing again must not record ourselves as "before".
            voice.setdefault("previous", previous)
        except CallRefused as exc:
            voice["manual_url"] = to["incoming"]
            voice["point_error"] = exc.reason
    if not (voice.get("signing") and credentials.get("signing_secret")):
        voice.pop("signing_error", None)
        try:
            made = await provider.start_signing(urls(base, credentials["route_token"])["root"] + "/",
                                                name=f"synapse {channel['id']}")
        except CallRefused as exc:
            made = None
            voice["signing_error"] = (f"{exc.reason} Calls still work, verified by the tokens in their URLs; "
                                      "a Standard API key turns signing on.")
        if made:
            credentials["signing_secret"] = made.pop("secret", "")
            voice["signing"] = made
    saved = dict(stored)
    saved["credentials"] = credentials
    saved["voice"] = voice
    saved["platform"] = PLATFORM
    return await channel_store.save_channel(saved)


async def disconnect(channel: dict) -> list[str]:
    """Put the number back where it pointed before us (V5). Never raises: a disconnect always completes.

    Returns what was left behind: the signing ids Twilio would not remove (a trial account cannot delete them),
    and ``"number"`` when the number could not be put back.
    """
    from core.messaging import store as channel_store

    voice = dict(channel.get("voice") or {})
    previous = voice.get("previous")
    creds = await hooks.current().secrets(channel)
    provider = provider_for(creds)
    cleared = []
    left: list[str] = []
    if voice.get("signing"):
        left += await provider.stop_signing(voice["signing"])
        cleared.append("signing")
    if voice.get("number_sid") and previous is not None:
        try:
            await provider.restore_number(voice["number_sid"], previous)
            cleared.append("previous")
        except Exception as exc:  # noqa: BLE001 — the person is told in the channel's error; the number keeps our URL
            log.warning("could not restore a number on disconnect: %s", exc)
            left.append("number")
    if not cleared:
        return left
    stored = await channel_store.get_channel(channel["id"])
    if stored is not None:
        voice = dict(stored.get("voice") or {})
        for key in cleared:
            voice.pop(key, None)
        stored["voice"] = voice
        if "signing" in cleared:
            stored["credentials"] = {k: v for k, v in (stored.get("credentials") or {}).items()
                                     if k != "signing_secret"}
        await channel_store.save_channel(stored)
    return left
