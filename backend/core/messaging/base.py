"""
Abstract base class for all messaging platform adapters.
Every adapter must implement start(), stop(), and send_message().
The shared dispatch logic lives here.
"""
import asyncio
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from core.messaging.markdown import format_for_platform

if TYPE_CHECKING:
    from core.messaging.manager import MessagingManager

logger = logging.getLogger(__name__)

# Built-in commands handled before routing to agent
_BUILTIN_COMMANDS = {"/start", "/help", "/reset", "/agent", "/agents"}

HELP_TEXT = """? *Synapse AI Bot*

Available commands:
/start -- Show this welcome message
/help -- Show available commands
/reset -- Clear your conversation memory
/agent <name> -- Switch to a different agent (if multi-agent mode is on)
/agents -- List available agents (if multi-agent mode is on)

Just send any message to chat with the agent!"""


class MessagingAdapter(ABC):
    """
    Base adapter. Subclasses implement platform-specific connection logic.
    Shared state (active agent per chat, human-step futures) lives in manager.
    """

    def __init__(self, channel: dict, manager: "MessagingManager"):
        self.channel = channel
        self.manager = manager
        self.channel_id: str = channel["id"]
        self.platform: str = channel["platform"]
        self.agent_id: str = channel.get("agent_id", "default")
        self.multi_agent_mode: bool = channel.get("multi_agent_mode", False)
        # "polling" holds a connection to the platform; "webhook" holds nothing
        # and receives through core/routes/messaging.py. Sending is identical
        # in both modes (REST, via core/messaging/senders.py).
        self.mode: str = channel.get("mode", "polling")
        # Tracks the most recent chat_id that sent a message -- used for proactive
        # schedule completion notifications when no notify_chat_id is configured.
        self._last_chat_id: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Abstract interface
    # ------------------------------------------------------------------ #

    @abstractmethod
    async def start(self) -> None:
        """Begin listening (polling loop or webhook registration)."""

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully stop the listener."""

    @abstractmethod
    async def _send_raw(self, chat_id: str, text: str) -> None:
        """Platform-specific send. Called by send_message() after formatting."""

    # ------------------------------------------------------------------ #
    # Public send -- formats and chunks automatically
    # ------------------------------------------------------------------ #

    async def send_message(self, chat_id: str, text: str) -> None:
        """Format for platform and send (chunked if needed)."""
        chunks = format_for_platform(self.platform, text)
        for chunk in chunks:
            try:
                await self._send_raw(chat_id, chunk)
                if len(chunks) > 1:
                    await asyncio.sleep(0.3)  # avoid rate limits when chunking
            except Exception as e:
                logger.error("[%s] send_message failed for chat %s: %s",
                             self.platform, chat_id, e)

    # ------------------------------------------------------------------ #
    # Command handling
    # ------------------------------------------------------------------ #

    async def _handle_command(
        self,
        chat_id: str,
        command: str,
        args: str,
        session_id: str,
    ) -> bool:
        """
        Handle built-in commands. Returns True if the command was consumed,
        False if it should fall through to the agent.
        """
        cmd = command.lower().split("@")[0]  # strip bot-name suffix (Telegram groups)

        if cmd == "/start":
            await self.send_message(chat_id, HELP_TEXT)
            return True

        if cmd == "/help":
            await self.send_message(chat_id, HELP_TEXT)
            return True

        if cmd == "/reset":
            # Clearing session memory: just notify -- the session_id change on next
            # message achieves a soft reset. For now, tell the user.
            await self.send_message(
                chat_id,
                "? Conversation reset. Your next message will start fresh."
            )
            # Signal manager to clear per-chat active agent selection
            self.manager.reset_chat_agent(self.channel_id, chat_id)
            return True

        if cmd == "/agents":
            if not self.multi_agent_mode:
                await self.send_message(
                    chat_id,
                    "?? Multi-agent mode is disabled for this bot."
                )
                return True
            agent_list = await self.manager.list_agent_names()
            if not agent_list:
                await self.send_message(chat_id, "No agents available.")
            else:
                lines = ["Available agents:"] + [f"? {name}" for name in agent_list]
                await self.send_message(chat_id, "\n".join(lines))
            return True

        if cmd == "/agent":
            if not self.multi_agent_mode:
                await self.send_message(
                    chat_id,
                    "?? Multi-agent mode is disabled for this bot."
                )
                return True
            agent_name = args.strip()
            if not agent_name:
                await self.send_message(chat_id, "Usage: /agent <name>")
                return True
            ok = await self.manager.switch_agent(self.channel_id, chat_id, agent_name)
            if ok:
                await self.send_message(
                    chat_id, f"? Switched to agent: *{agent_name}*"
                )
            else:
                await self.send_message(
                    chat_id,
                    f"? Agent '{agent_name}' not found. Use /agents to list available agents."
                )
            return True

        return False  # unrecognised command -- let agent handle it

    # ------------------------------------------------------------------ #
    # Core dispatch
    # ------------------------------------------------------------------ #

    async def _dispatch(
        self,
        chat_id: str,
        user_text: str,
        user_id: str,
        session_id: Optional[str] = None,
        send: Optional[Callable[[str, str], Awaitable[None]]] = None,
    ) -> None:
        """
        Route an incoming message through the agent's ReAct loop.
        Also checks if this message resolves a pending human-step Future.

        `send` replaces `send_message` for this one exchange — a webhook
        transport that must answer through a delivery-specific route (Discord's
        interaction follow-up) passes its own; everything else answers into the
        conversation.
        """
        if session_id is None:
            session_id = f"{self.platform}_{self.channel_id}_{chat_id}"
        if send is None:
            send = self.send_message

        # Track most recent chat_id for proactive schedule notifications
        self._last_chat_id = chat_id

        # -- 1. Check for human-step future waiting on this channel ------
        # If an orchestration is paused waiting for human input on this channel,
        # resolve it with the user's message (first-wins -- manager handles dedup).
        resolved = await self.manager.try_resolve_human_input(
            channel_id=self.channel_id,
            response=user_text,
        )
        if resolved:
            await send(
                chat_id,
                "? Response received. Resuming the workflow..."
            )
            return

        # -- 2. Determine active agent ------------------------------------
        active_agent_id = self.manager.get_chat_agent(self.channel_id, chat_id) or self.agent_id

        # -- 3. Run agent -------------------------------------------------
        try:
            response = await self.manager.run_agent(
                message=user_text,
                agent_id=active_agent_id,
                session_id=session_id,
            )
        except Exception as e:
            logger.error("[%s] Agent run failed: %s", self.platform, e)
            response = "?? An error occurred while processing your message. Please try again."

        # -- 4. Send response ---------------------------------------------
        if response:
            await send(chat_id, response)

    # ------------------------------------------------------------------ #
    # Webhook-mode entry point
    # ------------------------------------------------------------------ #

    async def handle_inbound(
        self,
        payload: dict,
        send: Optional[Callable[[str, str], Awaitable[None]]] = None,
    ) -> None:
        """One provider webhook delivery, already signature-verified upstream.

        The webhook counterpart of the platform callbacks the polling adapters
        register: parse the payload (`core/messaging/inbound.py`), remember the
        conversation for proactive sends, run the shared command handling, and
        dispatch. Subclasses override only when a delivery needs an answer the
        conversation cannot carry (Discord's interactions).
        """
        from core.messaging import inbound as inbound_messages

        message = inbound_messages.parse(self.platform, payload)
        if message is None:
            return

        # Durable sibling of `_last_chat_id`: proactive sends (schedule
        # results, run notifications, human-step prompts) need somewhere to go
        # even after a restart, and for most platforms the only way to learn a
        # conversation id is to be spoken to in it.
        try:
            from core.messaging import store as channel_store
            await channel_store.set_default_conversation(
                self.channel_id, message.conversation_id
            )
        except Exception:
            logger.debug("[%s] could not persist default conversation", self.platform)

        session_id = f"{self.platform}_{self.channel_id}_{message.conversation_id}"

        if message.text.startswith("/"):
            parts = message.text.split(" ", 1)
            consumed = await self._handle_command(
                message.conversation_id,
                parts[0],
                parts[1] if len(parts) > 1 else "",
                session_id,
            )
            if consumed:
                return

        await self._dispatch(
            message.conversation_id,
            message.text,
            message.user_id,
            session_id,
            send=send,
        )
