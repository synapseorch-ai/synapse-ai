"""
Discord adapter. Two receive modes:

* **polling** (default) — discord.py >= 2.3 holds the gateway websocket and
  answers DMs and @-mentions. No public URL required.
* **webhook** — the application's Interactions Endpoint URL points at
  `{base}/api/messaging/discord/webhook/{channel_id}` and slash-command
  invocations arrive through `core/routes/messaging.py` after the Ed25519
  check (`core/messaging/verify.py::verify_discord`). Needs `public_key` and
  `application_id` in the channel credentials; nothing is held open and
  discord.py is never imported. What webhook mode gives up is ambient
  messages — Discord only pushes *interactions* over HTTP, so the bot answers
  `/ask` and nothing else.

Sending is identical in both modes: channel REST via
`core/messaging/senders.py`. A slash command is answered through its
interaction follow-up while the 15-minute token lasts, falling back to a
plain channel message after that.
"""
import asyncio
import logging
from typing import TYPE_CHECKING

from core.messaging.base import MessagingAdapter

if TYPE_CHECKING:
    from core.messaging.manager import MessagingManager

logger = logging.getLogger(__name__)


class DiscordAdapter(MessagingAdapter):

    def __init__(self, channel: dict, manager: "MessagingManager"):
        super().__init__(channel, manager)
        self._client = None
        self._task: asyncio.Task | None = None
        creds = channel["credentials"]
        self.bot_token: str = creds["bot_token"]
        # Optional: restrict to a specific Discord channel ID
        self.restrict_channel_id: str | None = creds.get("channel_id")
        # Webhook mode only
        self.application_id: str = creds.get("application_id", "")

    async def start(self) -> None:
        if self.mode == "webhook":
            await self._start_webhook()
            return
        try:
            import discord
        except ImportError:
            raise RuntimeError(
                "discord.py is not installed. Enable messaging in setup to install it."
            )

        intents = discord.Intents.default()
        intents.message_content = True
        intents.messages = True
        intents.guilds = True

        client = discord.Client(intents=intents)
        self._client = client

        @client.event
        async def on_ready():
            logger.info("[Discord] Logged in as %s for channel %s",
                        client.user, self.channel_id)

        @client.event
        async def on_message(message: discord.Message):
            # Ignore own messages
            if message.author == client.user:
                return
            # Restrict to specific channel if configured
            if self.restrict_channel_id and str(message.channel.id) != self.restrict_channel_id:
                return
            # Only respond to DMs OR @mentions in servers
            is_dm = isinstance(message.channel, discord.DMChannel)
            is_mention = client.user in message.mentions
            if not is_dm and not is_mention:
                return

            chat_id = str(message.channel.id)
            user_id = str(message.author.id)
            session_id = f"discord_{self.channel_id}_{chat_id}_{user_id}"

            # Strip bot mention from text
            text = message.content
            if client.user:
                text = text.replace(f"<@{client.user.id}>", "").replace(f"<@!{client.user.id}>", "").strip()

            # Check for commands
            if text.startswith("/"):
                parts = text.split(" ", 1)
                consumed = await self._handle_command(
                    chat_id, parts[0], parts[1] if len(parts) > 1 else "", session_id
                )
                if consumed:
                    return

            # Show typing indicator
            async with message.channel.typing():
                await self._dispatch(chat_id, text, user_id, session_id)

        async def _run():
            await client.start(self.bot_token)

        self._task = asyncio.create_task(_run())
        logger.info("[Discord] Adapter started for channel %s", self.channel_id)

    async def _start_webhook(self) -> None:
        """Register the `/ask` slash command; Discord pushes interactions from here.

        Registration is an idempotent bulk PUT — running it on every start is
        how the command survives the application being reinstalled.
        """
        import httpx

        creds = self.channel.get("credentials", {})
        if not creds.get("public_key") or not self.application_id:
            raise RuntimeError(
                "Webhook mode needs the application's Public Key and "
                "Application ID — both are on the Discord developer portal's "
                "General Information page."
            )
        commands = [
            {
                "name": "ask",
                "description": "Ask the agent",
                "type": 1,  # CHAT_INPUT
                "options": [
                    {
                        "name": "message",
                        "description": "What to ask",
                        "type": 3,  # STRING
                        "required": True,
                    }
                ],
            }
        ]
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.put(
                f"https://discord.com/api/v10/applications/{self.application_id}/commands",
                json=commands,
                headers={"Authorization": f"Bot {self.bot_token}"},
            )
            if resp.status_code not in (200, 201):
                raise RuntimeError(
                    f"command registration failed: {resp.status_code} {resp.text[:200]}"
                )
        logger.info("[Discord] Webhook mode ready for channel %s", self.channel_id)

    async def handle_inbound(self, payload: dict, send=None) -> None:
        """Answer a slash command through its interaction, not the channel.

        The route has already ACKed the interaction as deferred ("thinking…"),
        so the first chunk of the answer must PATCH that placeholder — a plain
        channel message would leave the placeholder spinning forever. Later
        chunks, and anything after the token's 15-minute window, go to the
        channel like any other send.
        """
        from core.messaging import inbound as inbound_messages

        message = inbound_messages.parse("discord", payload)
        if message is not None and message.interaction and send is None:
            interaction = message.interaction
            replied = False

            async def send_via_interaction(chat_id: str, text: str) -> None:
                nonlocal replied
                from core.messaging.markdown import format_for_platform
                from core.messaging import senders

                chunks = format_for_platform("discord", text)
                for chunk in chunks:
                    if not replied:
                        try:
                            await senders.send_discord_followup(
                                interaction["application_id"],
                                interaction["token"],
                                chunk,
                            )
                            replied = True
                            continue
                        except Exception as e:
                            logger.warning(
                                "[Discord] follow-up failed, using channel: %s", e
                            )
                            replied = True  # don't retry the dead token per chunk
                    await senders.send_discord_raw(
                        {"bot_token": self.bot_token}, chat_id, chunk
                    )

            send = send_via_interaction
        await super().handle_inbound(payload, send=send)

    async def stop(self) -> None:
        if self.mode == "webhook":
            logger.info("[Discord] Adapter stopped for channel %s", self.channel_id)
            return
        if self._client and not self._client.is_closed():
            await self._client.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("[Discord] Adapter stopped for channel %s", self.channel_id)

    async def _send_raw(self, chat_id: str, text: str) -> None:
        if not text.strip():
            return
        from core.messaging import senders

        await senders.send_discord_raw({"bot_token": self.bot_token}, chat_id, text)
