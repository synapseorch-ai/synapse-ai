"""
Microsoft Teams adapter using Bot Framework SDK.
Requires Azure Bot registration (App ID + App Password) and a public HTTPS URL
for the webhook endpoint. For local dev, use ngrok: `ngrok http 8765`

The webhook is registered at: /api/messaging/teams/webhook/{channel_id}
"""
import logging
from typing import TYPE_CHECKING

from core.messaging.base import MessagingAdapter

if TYPE_CHECKING:
    from core.messaging.manager import MessagingManager

logger = logging.getLogger(__name__)


class TeamsAdapter(MessagingAdapter):
    """
    Teams uses an inbound webhook model. The adapter just validates credentials
    and registers itself with the manager so incoming webhook requests can be
    routed here. The actual HTTP handler lives in routes/messaging.py.
    """

    def __init__(self, channel: dict, manager: "MessagingManager"):
        super().__init__(channel, manager)
        creds = channel["credentials"]
        self.app_id: str = creds["app_id"]
        self.app_password: str = creds["app_password"]
        self._adapter_bot = None

    async def start(self) -> None:
        try:
            from botframework.connector.auth import MicrosoftAppCredentials
            from botbuilder.integration.aiohttp import CloudAdapter, ConfigurationBotFrameworkAuthentication
        except ImportError:
            raise RuntimeError(
                "botframework-connector / botbuilder-integration-aiohttp not installed. "
                "Enable messaging in setup to install them."
            )

        # Build the adapter (used by the webhook route to process requests)
        class _Settings:
            APP_ID = self.app_id
            APP_PASSWORD = self.app_password
            APP_TYPE = "MultiTenant"
            APP_TENANTID = ""

        auth = ConfigurationBotFrameworkAuthentication(_Settings())
        from botbuilder.integration.aiohttp import CloudAdapter
        self._adapter_bot = CloudAdapter(auth)
        # Store on manager so the HTTP route can retrieve it
        self.manager.register_teams_adapter(self.channel_id, self)
        logger.info("[Teams] Adapter ready for channel %s (webhook mode)", self.channel_id)

    async def stop(self) -> None:
        self.manager.unregister_teams_adapter(self.channel_id)
        logger.info("[Teams] Adapter stopped for channel %s", self.channel_id)

    async def _send_raw(self, chat_id: str, text: str) -> None:
        # Sending proactively in Teams requires a conversation reference stored
        # during the first incoming message. This is handled separately via
        # send_message_to_conversation() called by the manager.
        logger.warning("[Teams] Proactive send not implemented yet. "
                       "Teams replies are sent within request context.")

    async def process_request(self, auth_header: str, body: dict):
        """One Bot Framework delivery: its token checked, then `_on_turn`.

        `CloudAdapter.process_activity` authenticates `auth_header` — the JWT
        Microsoft signs for this app id — before running the turn, and raises
        `PermissionError` when it is not one; the webhook route answers 401.
        Returns the SDK's invoke response, None for an ordinary message.
        """
        if not self._adapter_bot:
            raise RuntimeError("Teams adapter is not started.")
        from botbuilder.schema import Activity

        activity = Activity().deserialize(body)
        return await self._adapter_bot.process_activity(auth_header, activity, self._on_turn)

    async def _on_turn(self, turn_context) -> None:
        """One Teams message: a command, else `_dispatch` like every other adapter.

        This used to be a copy of `_dispatch`'s agent half, so it never reached
        the half before it: a reply to a human step's question went to the agent
        as chat and the run stayed paused (F94), and a reply past the question's
        window was never refused (F93). Replies go back in the same request,
        through the turn context — Teams has no proactive send here.
        """
        from botbuilder.core import MessageFactory

        text = turn_context.activity.text or ""
        chat_id = turn_context.activity.conversation.id
        user_id = turn_context.activity.from_property.id if turn_context.activity.from_property else chat_id
        session_id = f"teams_{self.channel_id}_{chat_id}"

        if text.startswith("/"):
            parts = text.split(" ", 1)
            consumed = await self._handle_command(
                chat_id, parts[0], parts[1] if len(parts) > 1 else "", session_id
            )
            if consumed:
                return

        async def reply(_chat_id: str, message: str) -> None:
            for chunk in self._format_chunks(message):
                await turn_context.send_activity(MessageFactory.text(chunk))

        await self._dispatch(chat_id, text, user_id, session_id=session_id, send=reply)

    def _format_chunks(self, text: str) -> list[str]:
        from core.messaging.markdown import to_teams
        return to_teams(text)
