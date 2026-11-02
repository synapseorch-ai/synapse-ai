"""What each chat platform needs to connect, for Synapse AI's Connect card (F1263, REACH2).

One table, read by the card (`builder_connect`'s kind `channel`): the person types these into the card, and their
browser sends them to the Channels screen's own route. The model names the platform; it never sees a value. A secret
is a password input on the card and is never served back (`store.SECRET_CREDENTIALS`).

Telegram, Discord and Slack connect by polling, so the card alone connects them. Teams and WhatsApp are webhook-only:
the card saves them, then shows the address to paste into the platform (TRI1, 10-30), and the channel answers once the
platform calls it. Phone numbers are not here: they are set up on the Phone screen.
"""
from __future__ import annotations

from core.messaging.store import SECRET_CREDENTIALS

#: platform → (its name, its delivery, where its webhook address is pasted or "", its fields in the card's order).
PLATFORMS: dict[str, dict] = {
    "telegram": {"label": "Telegram", "mode": "polling", "paste_into": "", "fields": [
        {"key": "bot_token", "label": "Bot token", "placeholder": "123456:ABC-DEF...",
         "note": "From @BotFather on Telegram: send /newbot."},
    ]},
    "discord": {"label": "Discord", "mode": "polling", "paste_into": "", "fields": [
        {"key": "bot_token", "label": "Bot token", "placeholder": "MTE...",
         "note": "Discord Developer Portal → your app → Bot → Token."},
        {"key": "channel_id", "label": "Only in this channel (optional)", "placeholder": "123456789012345678",
         "optional": True, "note": "Leave it empty to answer wherever the bot is mentioned."},
    ]},
    "slack": {"label": "Slack", "mode": "polling", "paste_into": "", "fields": [
        {"key": "bot_token", "label": "Bot token", "placeholder": "xoxb-...",
         "note": "OAuth & Permissions → Bot User OAuth Token."},
        {"key": "app_token", "label": "App-level token", "placeholder": "xapp-...",
         "note": "Socket Mode → an app-level token with connections:write."},
    ]},
    "teams": {"label": "Microsoft Teams", "mode": "webhook", "paste_into": "the Azure Bot's Messaging endpoint",
              "fields": [
        {"key": "app_id", "label": "Azure app ID", "placeholder": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
         "note": "Azure Bot → Configuration → Microsoft App ID."},
        {"key": "app_password", "label": "Client secret", "placeholder": "",
         "note": "Azure Bot → Certificates & secrets."},
    ]},
    "whatsapp": {"label": "WhatsApp", "mode": "webhook", "paste_into": "Meta's WhatsApp Configuration → Webhook URL",
                 "fields": [
        {"key": "phone_number_id", "label": "Phone number ID", "placeholder": "123456789012345",
         "note": "Meta for Developers → WhatsApp → API Setup."},
        {"key": "access_token", "label": "Access token", "placeholder": "",
         "note": "A permanent token from a system user, or the temporary one on API Setup."},
        {"key": "app_secret", "label": "App secret", "placeholder": "",
         "note": "App settings → Basic. Synapse refuses a delivery it does not sign."},
        {"key": "verify_token", "label": "Verify token (your choice)", "placeholder": "synapse_verify",
         "note": "Any word: type the same one into Meta's Verify token."},
    ]},
}

#: Set on a WhatsApp channel the card makes: the Cloud API, never the browser path (whatsapp_mode on the screen).
WHATSAPP_MODE = "meta_api"


def card_fields(platform: str) -> list[dict]:
    """The card's inputs for `platform`, each marked secret or not (the store's own list decides)."""
    return [{**f, "secret": f["key"] in SECRET_CREDENTIALS, "optional": bool(f.get("optional"))}
            for f in PLATFORMS[platform]["fields"]]


def webhook_url(base: str, platform: str, channel_id: str) -> str:
    """Where a webhook platform delivers (`routes/messaging.py`'s own paths), on this install's public address."""
    return f"{base.rstrip('/')}/api/messaging/{platform}/webhook/{channel_id}"
