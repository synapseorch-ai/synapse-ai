"""
Slack adapter. Two receive modes:

* **polling** (default; historically "Socket Mode") — slack-bolt holds an
  outbound WebSocket. No public URL required. Needs the `app_token`
  (xapp-..., connections:write) beside the bot token.
* **webhook** — the app's Event Subscriptions Request URL points at
  `{base}/api/messaging/slack/webhook/{channel_id}` and deliveries arrive
  through `core/routes/messaging.py` after the signing-secret check
  (`core/messaging/verify.py::verify_slack`). Needs `signing_secret` in the
  channel credentials instead of `app_token`; nothing is held open and
  slack-bolt is never imported.

Required Slack App permissions either way:
  Bot Token Scopes: app_mentions:read, channels:history, chat:write, im:history, im:write
  Event Subscriptions: message.im, app_mention

Sending is identical in both modes: `chat.postMessage` over REST via
`core/messaging/senders.py`.
"""
import asyncio
import logging
from typing import TYPE_CHECKING

from core.messaging.base import MessagingAdapter

if TYPE_CHECKING:
    from core.messaging.manager import MessagingManager

logger = logging.getLogger(__name__)


class SlackAdapter(MessagingAdapter):

    def __init__(self, channel: dict, manager: "MessagingManager"):
        super().__init__(channel, manager)
        self._handler = None
        self._task: asyncio.Task | None = None
        creds = channel["credentials"]
        self.bot_token: str = creds["bot_token"]        # xoxb-...
        self.app_token: str = creds.get("app_token", "")  # xapp-... (socket mode only)
        self._slack_app = None

    async def start(self) -> None:
        if self.mode == "webhook":
            await self._start_webhook()
            return
        if not self.app_token:
            raise RuntimeError(
                "Socket Mode needs an App-Level Token (xapp-...). "
                "Either add one, or switch the channel to webhook mode."
            )
        try:
            from slack_bolt.async_app import AsyncApp
            from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        except ImportError:
            raise RuntimeError(
                "slack-bolt is not installed. Enable messaging in setup to install it."
            )

        app = AsyncApp(token=self.bot_token)

        # ── Event: direct messages ──────────────────────────────────────
        @app.event("message")
        async def handle_dm(event, say, client):
            # Only handle DMs (channel_type == "im")
            if event.get("channel_type") != "im":
                return
            if event.get("bot_id"):  # ignore bot messages
                return

            chat_id = event["channel"]
            user_id = event.get("user", chat_id)
            text = event.get("text", "").strip()
            session_id = f"slack_{self.channel_id}_{chat_id}_{user_id}"

            if text.startswith("/"):
                parts = text.split(" ", 1)
                consumed = await self._handle_command(
                    chat_id, parts[0], parts[1] if len(parts) > 1 else "", session_id
                )
                if consumed:
                    return

            # Show typing indicator (Slack doesn't have a built-in typing indicator
            # via events API, so we just proceed)
            await self._dispatch(chat_id, text, user_id, session_id)

        # ── Event: @mentions in channels ────────────────────────────────
        @app.event("app_mention")
        async def handle_mention(event, say, client):
            chat_id = event["channel"]
            user_id = event.get("user", chat_id)
            # Strip the bot mention
            text = event.get("text", "")
            # Remove <@BOTID> prefix
            import re
            text = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
            session_id = f"slack_{self.channel_id}_{chat_id}_{user_id}"

            if text.startswith("/"):
                parts = text.split(" ", 1)
                consumed = await self._handle_command(
                    chat_id, parts[0], parts[1] if len(parts) > 1 else "", session_id
                )
                if consumed:
                    return

            await self._dispatch(chat_id, text, user_id, session_id)

        # The bot token before the socket: a bad one is the channel's error
        # now, not a socket task that dies unread (F207).
        await self._check_bot_token()

        self._slack_app = app
        handler = AsyncSocketModeHandler(app, self.app_token)
        self._handler = handler

        async def _run():
            logger.info("[Slack] Starting Socket Mode for channel %s", self.channel_id)
            await handler.start_async()

        self._task = self._run_in_background(_run(), name=f"slack-{self.channel_id}")
        logger.info("[Slack] Adapter started for channel %s", self.channel_id)

    async def _start_webhook(self) -> None:
        """Prove the bot token works; Slack pushes to the route from here on.

        `auth.test` is the cheapest authenticated call Slack has, and failing
        now surfaces a bad token as the channel's error instead of as silence
        when the first event arrives.
        """
        if not self.channel.get("credentials", {}).get("signing_secret"):
            raise RuntimeError(
                "Webhook mode needs the app's Signing Secret in the channel "
                "credentials — it is what authenticates each delivery."
            )
        body = await self._check_bot_token()
        logger.info(
            "[Slack] Webhook mode ready for channel %s (bot %s)",
            self.channel_id,
            body.get("user"),
        )

    async def _check_bot_token(self) -> dict:
        """`auth.test` — the cheapest authenticated call Slack has."""
        import httpx

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://slack.com/api/auth.test",
                headers={"Authorization": f"Bearer {self.bot_token}"},
            )
            body = resp.json() if resp.status_code == 200 else {}
            if not body.get("ok"):
                raise RuntimeError(
                    f"auth.test failed: {body.get('error') or resp.status_code}"
                )
        return body

    async def stop(self) -> None:
        if self.mode == "webhook":
            logger.info("[Slack] Adapter stopped for channel %s", self.channel_id)
            return
        if self._handler:
            try:
                await self._handler.close_async()
            except Exception as e:
                logger.warning("[Slack] Stop error: %s", e)
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("[Slack] Adapter stopped for channel %s", self.channel_id)

    async def _send_raw(self, chat_id: str, text: str) -> None:
        if not text.strip():
            return
        from core.messaging import senders

        await senders.send_slack_raw({"bot_token": self.bot_token}, chat_id, text)
