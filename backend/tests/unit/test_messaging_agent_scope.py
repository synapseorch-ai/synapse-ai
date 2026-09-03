"""
A channel has exactly one answer to "which agents does it have" (D72).

Before this, `multi_agent_mode` predated the assignment join and ignored it:
`/agents` and `/agent <name>` both went through `manager.list_agents()`, which
returned every agent in the workspace. So the settings screen could say "these
two agents are on this channel" over a bot that would happily switch to a third.

The other half is *when* the answer is read. The adapter is handed a channel
dict at construction and used to keep that agent for its lifetime, so assigning
a new default did nothing until the channel was restarted — and restarting
re-registers Telegram's webhook and drops Slack's socket, for a settings edit.
"""
import pytest

from core.messaging import store as channel_store
from core.messaging.base import NO_AGENT_MESSAGE
from core.messaging.manager import MessagingManager
from tests._fakes import seed as _seed


class _RecordingAdapter:
    """The shared adapter behaviour with the platform transport taken out."""

    def __init__(self, channel, manager):
        from core.messaging.base import MessagingAdapter

        self.sent: list[str] = []
        # Borrow the real __init__/_handle_command/_dispatch rather than
        # re-describing them: it is exactly that shared code under test.
        MessagingAdapter.__init__(self, channel, manager)

    async def send_message(self, chat_id, text):
        self.sent.append(text)

    start = stop = None

    async def _send_raw(self, chat_id, text):  # pragma: no cover - unused
        self.sent.append(text)


def _adapter(channel, manager):
    from core.messaging.base import MessagingAdapter

    cls = type("TestAdapter", (_RecordingAdapter, MessagingAdapter), {})
    cls.__abstractmethods__ = frozenset()
    return cls(channel, manager)


@pytest.fixture
def manager():
    import core.server as server

    return MessagingManager(server_module=server)


async def _channel(**overrides):
    ch = {"name": "Support", "platform": "telegram", "credentials": {"bot_token": "1:x"}}
    ch.update(overrides)
    return await channel_store.save_channel(ch)


async def _agents(*names):
    agents = [_seed.make_agent(name=n) for n in names]
    await _seed.seed_agents(agents)
    return agents


class TestScope:
    async def test_list_agents_is_the_join_not_the_workspace(self, manager):
        assigned, other = await _agents("Assigned", "Elsewhere")
        ch = await _channel()
        await channel_store.set_assignments(ch["id"], [assigned["id"]])

        assert [a["id"] for a in await manager.list_agents()] == [assigned["id"], other["id"]]
        assert [a["id"] for a in await manager.list_agents(ch["id"])] == [assigned["id"]]
        assert await manager.list_agent_names(ch["id"]) == ["Assigned"]

    async def test_the_default_is_listed_first(self, manager):
        a, b = await _agents("First", "Second")
        ch = await _channel()
        await channel_store.set_assignments(ch["id"], [a["id"], b["id"]], default_agent_id=b["id"])
        assert await manager.list_agent_names(ch["id"]) == ["Second", "First"]

    async def test_switch_agent_refuses_an_unassigned_agent(self, manager):
        assigned, other = await _agents("Assigned", "Elsewhere")
        ch = await _channel()
        await channel_store.set_assignments(ch["id"], [assigned["id"]])

        assert await manager.switch_agent(ch["id"], "chat1", "Elsewhere") is False
        assert manager.get_chat_agent(ch["id"], "chat1") is None
        assert await manager.switch_agent(ch["id"], "chat1", "Assigned") is True
        assert manager.get_chat_agent(ch["id"], "chat1") == assigned["id"]

    async def test_agents_command_lists_only_the_assigned(self, manager):
        a, b, _c = await _agents("Alpha", "Beta", "Gamma")
        ch = await _channel(multi_agent_mode=True)
        await channel_store.set_assignments(ch["id"], [a["id"], b["id"]])

        adapter = _adapter(await channel_store.get_channel(ch["id"]), manager)
        assert await adapter._handle_command("chat1", "/agents", "", "sess") is True
        listed = adapter.sent[-1]
        assert "Alpha" in listed and "Beta" in listed
        assert "Gamma" not in listed

    async def test_agents_command_says_so_when_nobody_is_assigned(self, manager):
        await _agents("Alpha")
        ch = await _channel(multi_agent_mode=True)

        adapter = _adapter(await channel_store.get_channel(ch["id"]), manager)
        await adapter._handle_command("chat1", "/agents", "", "sess")
        assert "No agents are assigned" in adapter.sent[-1]


class TestResolutionIsPerMessage:
    async def test_a_reassignment_takes_effect_without_a_restart(self, manager):
        a, b = await _agents("First", "Second")
        ch = await _channel()
        await channel_store.set_assignments(ch["id"], [a["id"]])

        # Built the way the manager builds it: the default of the moment,
        # injected into the runtime copy.
        doc = await channel_store.get_channel(ch["id"])
        doc["agent_id"] = await channel_store.get_default_agent_id(ch["id"])
        adapter = _adapter(doc, manager)
        assert await adapter.resolve_agent_id("chat1") == a["id"]

        await channel_store.set_assignments(ch["id"], [a["id"], b["id"]], default_agent_id=b["id"])
        assert await adapter.resolve_agent_id("chat1") == b["id"]

    async def test_a_per_chat_switch_still_wins(self, manager):
        a, b = await _agents("First", "Second")
        ch = await _channel(multi_agent_mode=True)
        await channel_store.set_assignments(ch["id"], [a["id"], b["id"]])

        adapter = _adapter(await channel_store.get_channel(ch["id"]), manager)
        await manager.switch_agent(ch["id"], "chat1", "Second")
        assert await adapter.resolve_agent_id("chat1") == b["id"]
        assert await adapter.resolve_agent_id("chat2") == a["id"]

    async def test_an_unassigned_channel_says_so_instead_of_running_default(self, manager):
        """`"default"` is not an agent id — it was a fallback string.

        Dispatching against it ran the ReAct loop for an agent that does not
        exist, the same shape of bug as an orchestration step naming a missing
        agent and silently getting `agents[0]`.
        """
        await _agents("Alpha")
        ch = await _channel()
        adapter = _adapter(await channel_store.get_channel(ch["id"]), manager)

        assert adapter.agent_id == "default"
        assert await adapter.resolve_agent_id("chat1") is None

        ran = []
        async def _never(**kwargs):
            ran.append(kwargs)
            return "should not happen"
        manager.run_agent = _never

        await adapter._dispatch("chat1", "hello", "user1")
        assert ran == []
        assert adapter.sent[-1] == NO_AGENT_MESSAGE
