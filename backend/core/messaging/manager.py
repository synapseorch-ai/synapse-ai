"""
Messaging Manager — central lifecycle controller.

Responsibilities:
- Load all channels from the store on startup
- Start/stop adapters for enabled channels
- Provide the adapter registry to HTTP routes (Teams/WhatsApp webhooks)
- Manage per-chat active agent selection (multi-agent mode)
- Handle human-step Future resolution (first-response-wins)
- Provide agent runner function used by all adapters
"""
import asyncio
import logging
from typing import Any, Optional

from core.messaging import store as channel_store

logger = logging.getLogger(__name__)

# Map platform → adapter class (lazy import in _build_adapter to avoid
# ImportError when libraries are not installed)
PLATFORM_ADAPTER_MAP = {
    "telegram": "core.messaging.adapters.telegram.TelegramAdapter",
    "discord": "core.messaging.adapters.discord.DiscordAdapter",
    "slack": "core.messaging.adapters.slack.SlackAdapter",
    "teams": "core.messaging.adapters.teams.TeamsAdapter",
    "whatsapp": "core.messaging.adapters.whatsapp.WhatsAppAdapter",
}


def _import_adapter(platform: str):
    import importlib
    dotted = PLATFORM_ADAPTER_MAP.get(platform)
    if not dotted:
        raise ValueError(f"Unknown platform: {platform}")
    module_path, class_name = dotted.rsplit(".", 1)
    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)


class MessagingManager:

    def __init__(self, server_module: Any):
        self.server_module = server_module

        # channel_id → adapter instance
        self._adapters: dict[str, Any] = {}

        # channel_id → {chat_id → agent_id}  (active agent per conversation)
        self._active_agents: dict[str, dict[str, str]] = {}

        # Human-step futures: f"{run_id}:{step_id}" → asyncio.Future
        self._human_futures: dict[str, asyncio.Future] = {}
        # Reverse map: channel_id → future_key (so adapter can resolve by channel)
        self._channel_to_future: dict[str, str] = {}

        # WebHook-mode adapter registries
        self._teams_adapters: dict[str, Any] = {}       # channel_id → TeamsAdapter
        self._whatsapp_adapters: dict[str, Any] = {}   # channel_id → WhatsAppAdapter

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start_all(self) -> None:
        """Start all enabled channels from the store."""
        channels = await channel_store.list_channels()
        for ch in channels:
            if ch.get("enabled", True):
                await self.start_channel(ch["id"])

    async def stop_all(self) -> None:
        """Stop all running adapters."""
        for channel_id in list(self._adapters.keys()):
            await self.stop_channel(channel_id)

    async def start_channel(self, channel_id: str) -> None:
        """Load channel config, build adapter, and start it."""
        ch = await channel_store.get_channel(channel_id)
        if not ch:
            logger.warning("[Manager] Channel %s not found in store", channel_id)
            return

        # Stop existing adapter first if any
        if channel_id in self._adapters:
            await self.stop_channel(channel_id)

        platform = ch.get("platform", "")

        # The adapter's default agent comes from the assignment join, with the
        # legacy scalar as fallback (core/messaging/store.py). Injected into the
        # runtime copy only — never saved back — so the adapter keeps its
        # one-`agent_id` view of the world while the store holds many.
        default_agent = await channel_store.get_default_agent_id(channel_id)
        if default_agent:
            ch["agent_id"] = default_agent

        try:
            AdapterClass = _import_adapter(platform)
            adapter = AdapterClass(ch, self)
            await adapter.start()
            self._adapters[channel_id] = adapter
            await channel_store.update_channel_status(channel_id, "running")
            logger.info("[Manager] Channel %s (%s) started", channel_id, platform)
        except Exception as e:
            logger.error("[Manager] Failed to start channel %s: %s", channel_id, e)
            await channel_store.update_channel_status(channel_id, "error", str(e))

    async def stop_channel(self, channel_id: str) -> None:
        """Stop and remove an adapter."""
        adapter = self._adapters.pop(channel_id, None)
        if adapter:
            try:
                await adapter.stop()
            except Exception as e:
                logger.warning("[Manager] Error stopping channel %s: %s", channel_id, e)
        await channel_store.update_channel_status(channel_id, "stopped")
        logger.info("[Manager] Channel %s stopped", channel_id)

    async def restart_channel(self, channel_id: str) -> None:
        await self.stop_channel(channel_id)
        await self.start_channel(channel_id)

    def get_adapter(self, channel_id: str, platform: str | None = None):
        """The running adapter for a channel, or None.

        The generic webhook route uses this instead of the per-platform
        registries below; `platform` guards against a delivery addressed to
        the right channel id on the wrong platform's URL.
        """
        adapter = self._adapters.get(channel_id)
        if adapter is None:
            return None
        if platform is not None and adapter.platform != platform:
            return None
        return adapter

    # ------------------------------------------------------------------ #
    # Status
    # ------------------------------------------------------------------ #

    async def get_status(self) -> list[dict]:
        """Return live status for all channels (merges store + runtime info)."""
        channels = await channel_store.list_channels()
        running_ids = set(self._adapters.keys())
        for ch in channels:
            if ch["id"] in running_ids:
                ch["status"] = "running"
            # last_error and status are kept in the store (persisted by update_channel_status)
        return channels

    # ------------------------------------------------------------------ #
    # Multi-Agent Mode
    # ------------------------------------------------------------------ #

    def get_chat_agent(self, channel_id: str, chat_id: str) -> Optional[str]:
        """Get the currently active agent for a chat, or None (use default)."""
        return self._active_agents.get(channel_id, {}).get(chat_id)

    def reset_chat_agent(self, channel_id: str, chat_id: str) -> None:
        """Clear the per-chat agent selection (reset to channel default)."""
        self._active_agents.get(channel_id, {}).pop(chat_id, None)

    async def switch_agent(self, channel_id: str, chat_id: str, agent_name: str) -> bool:
        """
        Switch the active agent for a chat by name.
        Returns True if the agent was found, False otherwise.
        """
        agents = await self.list_agents()
        match = next(
            (a for a in agents if a.get("name", "").lower() == agent_name.lower()),
            None,
        )
        if not match:
            return False
        if channel_id not in self._active_agents:
            self._active_agents[channel_id] = {}
        self._active_agents[channel_id][chat_id] = match["id"]
        return True

    async def list_agents(self) -> list[dict]:
        """Return all configured agents from the agent store."""
        try:
            from core.routes.agents import load_user_agents
            return await load_user_agents()
        except Exception:
            return []

    async def list_agent_names(self) -> list[str]:
        agents = await self.list_agents()
        return [a.get("name", a.get("id", "")) for a in agents]

    # ------------------------------------------------------------------ #
    # Agent Runner
    # ------------------------------------------------------------------ #

    async def run_agent(self, message: str, agent_id: str, session_id: str) -> str:
        """
        Run an agent's ReAct loop and return the final text response.
        """
        from core.react_engine import run_agent_step
        final_response = ""
        try:
            async for event in run_agent_step(
                message=message,
                agent_id=agent_id,
                session_id=session_id,
                server_module=self.server_module,
                max_turns=15,
                allowed_tools_override=None,
            ):
                if event.get("type") == "final":
                    final_response = event.get("response", "")
        except Exception as e:
            logger.error("[Manager] run_agent error (agent=%s): %s", agent_id, e)
            final_response = f"⚠️ Error: {e}"
        return final_response

    # ------------------------------------------------------------------ #
    # Human-Step Integration (first-response-wins)
    # ------------------------------------------------------------------ #

    async def wait_for_human_input(
        self,
        run_id: str,
        step_id: str,
        channel_id: str,
        prompt: str,
        timeout: int = 3600,
    ) -> str:
        """
        Register a Future for this (run_id, step_id) pair, send the prompt
        to the messaging channel, and wait for the first response.
        Raises asyncio.TimeoutError if no response arrives within `timeout` seconds.
        """
        key = f"{run_id}:{step_id}"
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._human_futures[key] = future
        self._channel_to_future[channel_id] = key

        # Send the prompt to the channel
        adapter = self._adapters.get(channel_id)
        if adapter:
            try:
                # Find a chat_id to send to — for now broadcast to the most recent.
                # In practice, the orchestration should store the initiating chat_id.
                # Simplification: send to the channel's default agent's last known chat.
                # For Teams/WhatsApp webhook channels we can't proactively send without
                # a stored conversation reference; log a warning.
                logger.info(
                    "[Manager] Human step paused, waiting for input on channel %s",
                    channel_id,
                )
            except Exception as e:
                logger.warning("[Manager] Could not notify channel %s: %s", channel_id, e)

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._human_futures.pop(key, None)
            self._channel_to_future.pop(channel_id, None)

    async def try_resolve_human_input(
        self,
        channel_id: str,
        response: str,
    ) -> bool:
        """
        Try to resolve a pending human-step Future for this channel.
        Returns True if a future was found and resolved (this call wins).
        Returns False if no future was pending (treat as normal chat message).
        """
        key = self._channel_to_future.get(channel_id)
        if not key:
            return False
        return self.resolve_human_input_by_key(key, response)

    def resolve_human_input_by_key(self, key: str, response: str) -> bool:
        """
        Resolve a future by its f"{run_id}:{step_id}" key.
        First call wins — subsequent calls are no-ops.
        Returns True if this call won the race.
        """
        future = self._human_futures.get(key)
        if future is None or future.done():
            return False
        try:
            future.set_result(response)
            return True
        except asyncio.InvalidStateError:
            return False

    # ------------------------------------------------------------------ #
    # Webhook-mode adapter registries (Teams, WhatsApp)
    # ------------------------------------------------------------------ #

    def register_teams_adapter(self, channel_id: str, adapter) -> None:
        self._teams_adapters[channel_id] = adapter

    def unregister_teams_adapter(self, channel_id: str) -> None:
        self._teams_adapters.pop(channel_id, None)

    def get_teams_adapter(self, channel_id: str):
        return self._teams_adapters.get(channel_id)

    def register_whatsapp_adapter(self, channel_id: str, adapter) -> None:
        self._whatsapp_adapters[channel_id] = adapter

    def unregister_whatsapp_adapter(self, channel_id: str) -> None:
        self._whatsapp_adapters.pop(channel_id, None)

    def get_whatsapp_adapter(self, channel_id: str):
        return self._whatsapp_adapters.get(channel_id)
