"""
Telegram adapter. Two receive modes:

* **polling** (default) — python-telegram-bot >= 20 long-polls `getUpdates`.
  No public URL required; the right mode for a laptop or a LAN install.
* **webhook** — `setWebhook` points Telegram at
  `{messaging_webhook_base_url}/api/messaging/telegram/webhook/{channel_id}`,
  and deliveries arrive through `core/routes/messaging.py` after the
  `X-Telegram-Bot-Api-Secret-Token` check. No connection is held and the
  optional python-telegram-bot dependency is never imported. A bot can only be
  in one mode at a time — Telegram itself refuses `getUpdates` while a webhook
  is set, so a token shared with another install will fight over this.

Sending is identical in both modes: plain REST via `core/messaging/senders.py`.
"""
import asyncio
import logging
from typing import TYPE_CHECKING

from core.messaging.base import MessagingAdapter

if TYPE_CHECKING:
    from core.messaging.manager import MessagingManager

logger = logging.getLogger(__name__)


class TelegramAdapter(MessagingAdapter):

    def __init__(self, channel: dict, manager: "MessagingManager"):
        super().__init__(channel, manager)
        self._app = None
        self._task: asyncio.Task | None = None
        self.bot_token: str = channel["credentials"]["bot_token"]

    async def start(self) -> None:
        if self.mode == "webhook":
            await self._start_webhook()
            return
        try:
            from telegram.ext import Application, MessageHandler, CommandHandler, filters
        except ImportError:
            raise RuntimeError(
                "python-telegram-bot is not installed. "
                "Enable messaging in setup to install it."
            )

        app = (
            Application.builder()
            .token(self.bot_token)
            .build()
        )
        self._app = app

        # Register command handlers
        for cmd in ["start", "help", "reset", "agents"]:
            app.add_handler(CommandHandler(cmd, self._on_command))
        app.add_handler(CommandHandler("agent", self._on_command))

        # Register plain text handler
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message))

        async def _run():
            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            logger.info("[Telegram] Polling started for channel %s", self.channel_id)
            # Keep running until stopped
            while True:
                await asyncio.sleep(3600)

        self._task = asyncio.create_task(_run())
        logger.info("[Telegram] Adapter started for channel %s", self.channel_id)

    async def _start_webhook(self) -> None:
        """Register the webhook with Telegram. Holds nothing afterwards.

        The secret token is minted here on first start and persisted on the
        channel, because it has to match what the webhook route later compares
        against (`core/messaging/verify.py::verify_telegram`).
        """
        import secrets as _secrets

        import httpx

        from core.config import load_settings
        from core.messaging import store as channel_store

        base = (load_settings().get("messaging_webhook_base_url") or "").rstrip("/")
        if not base:
            raise RuntimeError(
                "Webhook mode needs messaging_webhook_base_url in Settings — "
                "a public HTTPS address Telegram can reach."
            )

        secret = self.channel.get("credentials", {}).get("webhook_secret")
        if not secret:
            secret = _secrets.token_urlsafe(32)
            self.channel.setdefault("credentials", {})["webhook_secret"] = secret
            await channel_store.save_channel(self.channel)

        url = f"{base}/api/messaging/telegram/webhook/{self.channel_id}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{self.bot_token}/setWebhook",
                json={
                    "url": url,
                    "secret_token": secret,
                    "drop_pending_updates": True,
                },
            )
            body = resp.json() if resp.status_code == 200 else {}
            if not body.get("ok"):
                raise RuntimeError(
                    f"setWebhook failed: {resp.status_code} "
                    f"{body.get('description') or resp.text[:200]}"
                )
        logger.info("[Telegram] Webhook registered for channel %s", self.channel_id)

    async def stop(self) -> None:
        if self.mode == "webhook":
            import httpx

            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    await client.post(
                        f"https://api.telegram.org/bot{self.bot_token}/deleteWebhook"
                    )
            except Exception as e:
                logger.warning("[Telegram] deleteWebhook failed: %s", e)
            logger.info("[Telegram] Webhook removed for channel %s", self.channel_id)
            return
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._app:
            try:
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
            except Exception as e:
                logger.warning("[Telegram] Stop error: %s", e)
        logger.info("[Telegram] Adapter stopped for channel %s", self.channel_id)

    async def _send_raw(self, chat_id: str, text: str) -> None:
        if not text.strip():
            return
        from core.messaging import senders

        await senders.send_telegram_raw(
            {"bot_token": self.bot_token}, chat_id, text
        )

    # ------------------------------------------------------------------ #
    # Handlers
    # ------------------------------------------------------------------ #

    async def _on_command(self, update, context) -> None:
        if not update.message:
            return
        msg = update.message
        chat_id = str(msg.chat_id)
        # Full text like "/agent MyBot" → command="/agent", args="MyBot"
        text = msg.text or ""
        parts = text.split(" ", 1)
        command = parts[0]
        args = parts[1] if len(parts) > 1 else ""
        session_id = f"telegram_{self.channel_id}_{chat_id}"
        await self._handle_command(chat_id, command, args, session_id)

    async def _on_message(self, update, context) -> None:
        if not update.message or not update.message.text:
            return
        msg = update.message
        chat_id = str(msg.chat_id)
        user_id = str(msg.from_user.id) if msg.from_user else chat_id
        session_id = f"telegram_{self.channel_id}_{chat_id}"

        # Show typing indicator
        try:
            await self._app.bot.send_chat_action(chat_id=int(chat_id), action="typing")
        except Exception:
            pass

        await self._dispatch(chat_id, msg.text, user_id, session_id)
