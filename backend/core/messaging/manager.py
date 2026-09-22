"""
Messaging Manager — central lifecycle controller.

Responsibilities:
- Load all channels from the store on startup
- Start/stop adapters for enabled channels
- Provide the adapter registry to HTTP routes (Teams/WhatsApp webhooks)
- Manage per-chat active agent selection (multi-agent mode)
- Answer a paused run from a reply in the channel its human step names
- Provide agent runner function used by all adapters
"""
import logging
from typing import Any, Optional

from core.messaging import store as channel_store

logger = logging.getLogger(__name__)

#: What a channel hears when an agent's turn fails — cloud's words for the same.
FAILED_REPLY = "⚠️ Something went wrong answering that. Please try again."

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
        Switch the active agent for a chat by name, within this channel's own
        agents. Returns True if the agent was found, False otherwise.
        """
        agents = await self.list_agents(channel_id)
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

    async def list_agents(self, channel_id: Optional[str] = None) -> list[dict]:
        """Configured agents — every one, or just this channel's assignments.

        Scoped by channel since D72: the assignment join is what the settings
        screen shows and what a channel's `/agents` must therefore answer.
        Before that, `/agent <name>` could switch a channel to any agent in the
        workspace, so a screen listing three agents fronted a bot that would
        happily become a fourth.

        The join's order is kept — default first — so `/agents` lists the agent
        that answers a plain message at the top.
        """
        try:
            from core.routes.agents import load_user_agents
            agents = await load_user_agents()
        except Exception:
            return []
        if channel_id is None:
            return agents

        assigned = await channel_store.get_agent_ids_for_channel(channel_id)
        by_id = {a["id"]: a for a in agents}
        return [by_id[aid] for aid in assigned if aid in by_id]

    async def list_agent_names(self, channel_id: Optional[str] = None) -> list[str]:
        agents = await self.list_agents(channel_id)
        return [a.get("name", a.get("id", "")) for a in agents]

    # ------------------------------------------------------------------ #
    # Agent Runner
    # ------------------------------------------------------------------ #

    async def run_agent(
        self, message: str, agent_id: str, session_id: str, channel_id: Optional[str] = None,
    ) -> str:
        """
        Run an agent's ReAct loop and return the final text response.

        An Orchestrator agent runs its orchestration instead (F71): the reply is
        the orchestration's answer, its error, or where it paused.
        """
        from core.react_engine import resolve_agent_for_run, run_agent_step
        try:
            agent = await resolve_agent_for_run(agent_id)
        except Exception:
            agent = None
        if agent and agent.get("type") == "orchestrator":
            return await self._run_orchestrator(agent, message, session_id, channel_id)

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
            # Whoever is talking to the bot is told something went wrong, not
            # what: a model's failure is the provider's words about the
            # workspace's key or account (F133), and anything else is ours.
            logger.error("[Manager] run_agent error (agent=%s): %s", agent_id, e)
            final_response = FAILED_REPLY
        return final_response

    async def _run_orchestrator(
        self, agent: dict, message: str, session_id: str, channel_id: Optional[str],
    ) -> str:
        from core.react_engine import run_orchestrator_agent

        final_response = ""
        error: Optional[str] = None
        failed = False
        paused: Optional[dict] = None
        try:
            async for event in run_orchestrator_agent(
                agent, message, self.server_module, session_id=session_id,
            ):
                etype = event.get("type")
                if etype == "final":
                    final_response = event.get("response", "")
                elif etype == "orchestration_error":
                    error = str(event.get("error") or "the orchestration failed")
                elif etype == "error":
                    error = str(event.get("message") or "the orchestration failed")
                elif etype == "orchestration_complete" and event.get("status") in ("failed", "cancelled"):
                    # A step failure ends this way; the reason is in `final`.
                    failed = True
                elif etype == "human_input_required":
                    paused = event
        except Exception as e:
            logger.error("[Manager] orchestrator agent %s failed: %s", agent.get("id"), e)
            error = str(e)

        if error:
            return f"⚠️ Error: {error}"
        if failed:
            return f"⚠️ {final_response or 'The workflow failed.'}"
        if paused:
            if channel_id and paused.get("channel_id") == channel_id:
                # The step asked its question in this channel already, and a
                # reply here answers it (F54). Saying it twice would be noise.
                return ""
            return (
                f"⏸ {paused.get('prompt') or 'The workflow is waiting for input.'}\n\n"
                f"Answer it in SynapseOrch (run {paused.get('run_id')})."
            )
        return final_response

    # ------------------------------------------------------------------ #
    # Human steps: a reply in the channel answers the run waiting on it
    # ------------------------------------------------------------------ #

    async def answer_from_channel(self, channel_id: str, text: str) -> Optional[str]:
        """Answer the newest run waiting on this channel with `text`.

        Returns the run id answered, or None when no run is waiting here (the
        message is then an ordinary chat message). Resumed the way the app's
        Answer control resumes it — on the fleet for a run a worker ran. Raises
        `QuestionExpired` when the question has outlived its window (F93).
        """
        run_id = await find_run_waiting_on(channel_id)
        if run_id is None:
            return None
        if await question_expired(run_id):
            from core.messaging.base import QuestionExpired
            raise QuestionExpired(run_id)
        from core.orchestration.answers import RunAlreadyActive, RunNotWaiting, answer_paused_run

        app = getattr(self.server_module, "app", None)
        try:
            await answer_paused_run(app.state, run_id, text)
        except (RunNotWaiting, RunAlreadyActive):
            # Answered a moment ago by someone else, in the app or here.
            return None
        return run_id

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


#: How far back a reply looks for a run waiting on its channel.
WAITING_RUNS_SCANNED = 50


async def find_run_waiting_on(channel_id: str) -> Optional[str]:
    """The newest run paused on a question that is waiting on `channel_id`.

    The engine records the channel in the run's state when a human step pauses
    (`core/orchestration/human_prompts.py`), on the top-level run for a nested
    step too — so this reads the store, and works whichever process ran the
    run and across a restart. Sub-runs are skipped: the top-level run is the
    one an answer resumes.
    """
    from sqlalchemy import select

    from core.orchestration.human_prompts import HUMAN_CHANNEL_KEY
    from core.orchestration.state_store import _is_structural
    from core.store import session
    from core.store.models import OrchestrationRunDB
    from core.tenancy import get_tenant

    async with session() as s:
        rows = (await s.execute(
            select(OrchestrationRunDB.run_id, OrchestrationRunDB.shared_state)
            .where(OrchestrationRunDB.tenant_id == get_tenant())
            .where(OrchestrationRunDB.status == "paused")
            .where(OrchestrationRunDB.waiting_for_human.is_(True))
            .order_by(OrchestrationRunDB.created_at.desc())
            .limit(WAITING_RUNS_SCANNED)
        )).all()
    for run_id, state in rows:
        if _is_structural(run_id or ""):
            continue
        if (state or {}).get(HUMAN_CHANNEL_KEY) == channel_id:
            return run_id
    return None


async def send_human_prompt(
    channel_id: str,
    prompt: str,
    run_id: str,
    manager: Optional["MessagingManager"] = None,
) -> None:
    """Post a paused human step's question into its channel.

    Installed as the engine's prompt sender by the API server and by the
    worker's startup (`set_human_prompt_sender`). Goes to the conversation run
    notifications use — the channel's `notify_chat_id`, else the last one
    spoken in — over the platform's REST API, so a worker with no adapter can
    send it. Teams and WhatsApp have no REST sender; they are reached through
    the running adapter when this is the API process, and not otherwise.
    """
    from core.config import load_settings
    from core.messaging import senders

    if not (load_settings() or {}).get("messaging_enabled", False):
        return
    channel = await channel_store.get_channel(channel_id)
    if not channel or not channel.get("enabled", True):
        logger.warning("[Manager] run %s: channel %s is missing or disabled; question not sent", run_id, channel_id)
        return
    adapter = manager.get_adapter(channel_id) if manager else None
    conversation = (
        channel.get("notify_chat_id")
        or channel.get("default_conversation_id")
        or getattr(adapter, "_last_chat_id", None)
    )
    if not conversation:
        logger.warning(
            "[Manager] run %s: nobody has written in channel %s yet, so there is no conversation "
            "to ask in; question not sent", run_id, channel_id,
        )
        return
    text = f"⏸ {prompt}\n\nReply here to answer."
    platform = channel.get("platform", "")
    if senders.supports_rest_send(platform):
        await senders.send(platform, channel.get("credentials") or {}, conversation, text)
    elif adapter is not None:
        await adapter.send_message(conversation, text)
    else:
        logger.warning("[Manager] run %s: %s has no sender in this process; question not sent", run_id, platform)


async def question_expired(run_id: str, *, now: Optional[float] = None) -> bool:
    """Whether the question `run_id` waits on has outlived its step's `human_timeout_seconds`.

    Cloud's rule (F93, D109): past the window a channel stops taking the
    answer, and the run keeps waiting for one in the app. The window runs from
    the pause — the `paused` entry the engine writes to `step_history` — and is
    measured on the run whose human step asked: a parent paused on a nested
    orchestration hands over to that sub-run, whose pause is its own. The
    entry, not the run's position, names the step: inside a Loop or a Parallel
    branch the position is the container.

    A question whose pause or step cannot be read is not expired: the reply is
    answered, as it was before there was a window at all.
    """
    import calendar
    import time

    from sqlalchemy import select

    from core.config import ORCH_HUMAN_TIMEOUT
    from core.orchestration.pauses import asking_step_id
    from core.scale.context import resolve_orchestration
    from core.store import session
    from core.store.models import OrchestrationRunDB
    from core.tenancy import get_tenant

    async def load(rid: str):
        async with session() as s:
            return (await s.execute(
                select(OrchestrationRunDB)
                .where(OrchestrationRunDB.tenant_id == get_tenant())
                .where(OrchestrationRunDB.run_id == rid)
            )).scalar_one_or_none()

    row = await load(run_id)
    for _ in range(8):  # nesting is bounded; a cycle must not spin
        if row is None or not row.nested_run_id:
            break
        row = await load(row.nested_run_id)
    if row is None or not row.current_step_id:
        return False

    # The step that asked, which inside a Loop or a Parallel branch is not the
    # run's position — that is the container (F109).
    asked_by = asking_step_id(row.step_history, row.current_step_id)
    paused_at = None
    for entry in reversed(row.step_history or []):
        if entry.get("step_id") == asked_by and entry.get("status") == "paused":
            try:
                paused_at = calendar.timegm(time.strptime(entry.get("started_at") or "", "%Y-%m-%dT%H:%M:%SZ"))
            except ValueError:
                paused_at = None
            break
    if paused_at is None:
        return False

    orch = await resolve_orchestration(row.orchestration_id) or {}
    step = next((st for st in orch.get("steps") or [] if st.get("id") == asked_by), None)
    if step is None:
        return False
    window = float(step.get("human_timeout_seconds") or ORCH_HUMAN_TIMEOUT)
    return (time.time() if now is None else now) > paused_at + window
