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

    async def process_request(self, request, response):
        """Process an aiohttp request from the Teams webhook endpoint."""
        if not self._adapter_bot:
            return

        async def _turn_handler(turn_context):
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

            agent_id = await self.resolve_agent_id(chat_id)
            if not agent_id:
                from botbuilder.core import MessageFactory
                from core.messaging.base import NO_AGENT_MESSAGE
                await turn_context.send_activity(MessageFactory.text(NO_AGENT_MESSAGE))
                return

            response_text = await self.manager.run_agent(
                message=text,
                agent_id=agent_id,
                session_id=session_id,
            )
            if response_text:
                from botbuilder.core import MessageFactory
                chunks = self._format_chunks(response_text)
                for chunk in chunks:
                    await turn_context.send_activity(MessageFactory.text(chunk))

        await self._adapter_bot.process_activity(request, response, _turn_handler)

    def _format_chunks(self, text: str) -> list[str]:
        from core.messaging.markdown import to_teams
        return to_teams(text)
