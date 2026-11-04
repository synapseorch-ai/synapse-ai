"""
WhatsApp adapter, through Meta's Cloud API (official, requires a Business account):
  Credentials: phone_number_id, access_token, verify_token
  Incoming: FastAPI webhook at /api/messaging/whatsapp/webhook/{channel_id}
  Outgoing: HTTPX call to Meta Graph API

The second path 1.x offered, WhatsApp Web driven in a headed Chromium after a QR scan (`whatsapp_mode:
"unofficial"`), is removed in 2.0 (F1355): it never read a message after the scan, and it was the one part of
Synapse that needed the full Chromium build, which every install downloaded for it. The manager keeps a 1.x
channel in that mode switched off and never builds this adapter for one; refusing here too is the second guard.
"""
import logging
from typing import TYPE_CHECKING

from core.messaging import store as channel_store
from core.messaging.base import MessagingAdapter

if TYPE_CHECKING:
    from core.messaging.manager import MessagingManager

logger = logging.getLogger(__name__)

META_API_URL = "https://graph.facebook.com/v18.0"


class WhatsAppAdapter(MessagingAdapter):

    def __init__(self, channel: dict, manager: "MessagingManager"):
        super().__init__(channel, manager)
        if channel_store.is_whatsapp_web(channel):
            raise ValueError(channel_store.WHATSAPP_WEB_PAUSED)
        creds = channel["credentials"]
        self.phone_number_id: str = creds.get("phone_number_id", "")
        self.access_token: str = creds.get("access_token", "")
        self.verify_token: str = creds.get("verify_token", "synapse_verify")

    async def start(self) -> None:
        if not self.phone_number_id or not self.access_token:
            raise ValueError(
                "WhatsApp Meta API requires phone_number_id and access_token."
            )
        # Register with manager so the webhook route can dispatch here
        self.manager.register_whatsapp_adapter(self.channel_id, self)
        logger.info(
            "[WhatsApp/Meta] Adapter ready for channel %s (webhook mode)",
            self.channel_id,
        )

    async def stop(self) -> None:
        self.manager.unregister_whatsapp_adapter(self.channel_id)
        logger.info("[WhatsApp] Adapter stopped for channel %s", self.channel_id)

    async def _send_raw(self, chat_id: str, text: str) -> None:
        await self._send_meta(chat_id, text)

    async def _send_meta(self, to: str, text: str) -> None:
        if not text.strip():
            return
        try:
            import httpx
            url = f"{META_API_URL}/{self.phone_number_id}/messages"
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            }
            payload = {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": text},
            }
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
        except Exception as e:
            logger.error("[WhatsApp/Meta] Send failed to %s: %s", to, e)

    async def process_webhook(self, data: dict) -> None:
        """Process an incoming webhook payload from Meta."""
        try:
            for entry in data.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    for msg in value.get("messages", []):
                        if msg.get("type") != "text":
                            continue
                        from_number = msg["from"]
                        text = msg["text"]["body"]
                        session_id = f"whatsapp_{self.channel_id}_{from_number}"
                        await self._dispatch(from_number, text, from_number, session_id)
        except Exception as e:
            logger.error("[WhatsApp/Meta] Webhook processing error: %s", e)

    def verify_webhook(self, mode: str, token: str, challenge: str) -> str | None:
        """Verify the Meta webhook subscription."""
        if mode == "subscribe" and token == self.verify_token:
            return challenge
        return None
