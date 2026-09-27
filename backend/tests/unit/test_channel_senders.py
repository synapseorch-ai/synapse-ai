"""
A channel says whether the people messaging it are workspace users (F282).

A messaging channel has no sender list — anyone who can message the bot drives
its agent — and every sender ran as a member: the agent's whole tool set and its
memory. Naveen's call (2026-09-27): a channel is mainly a member's tool
(answering a workflow's questions, reaching your agents from anywhere), so the
box "Everyone who messages this bot is a workspace user" is **on by default**
and nothing changes on upgrade; switched **off**, a sender is a visitor — the
channel's own `visitor_tools`, no memory (`core.visitor`), and no answering a
run waiting on the channel.
"""
import pytest

from core import visitor
from core.messaging import store as channel_store
from core.messaging.manager import MessagingManager
from core.visitor import VisitorScope


class _RecordingAdapter:
    def __init__(self, channel, manager):
        from core.messaging.base import MessagingAdapter

        self.sent: list[str] = []
        MessagingAdapter.__init__(self, channel, manager)

    async def send_message(self, chat_id, text):
        self.sent.append(text)

    start = stop = None


def _adapter(channel, manager):
    from core.messaging.base import MessagingAdapter

    cls = type("TestAdapter", (_RecordingAdapter, MessagingAdapter), {})
    cls.__abstractmethods__ = frozenset()
    return cls(channel, manager)


@pytest.fixture
def manager(monkeypatch):
    """A manager whose agent turn records the scope it ran in, and whose
    waiting-run lookup records that it was asked."""
    import core.server as server

    m = MessagingManager(server_module=server)
    m.scopes = []
    m.asked_waiting = 0

    async def run_agent(message, agent_id, session_id, channel_id=None):
        m.scopes.append(visitor.current())
        return "ok"

    async def answer_from_channel(channel_id, text):
        m.asked_waiting += 1
        return False

    monkeypatch.setattr(m, "run_agent", run_agent)
    monkeypatch.setattr(m, "answer_from_channel", answer_from_channel)
    return m


async def _channel(**overrides):
    from tests._fakes import seed as _seed

    agent = _seed.make_agent(name="Helper")
    await _seed.seed_agents([agent])
    ch = {"name": "Support", "platform": "telegram", "credentials": {"bot_token": "1:x"}}
    ch.update(overrides)
    ch = await channel_store.save_channel(ch)
    await channel_store.set_assignments(ch["id"], [agent["id"]])
    return await channel_store.get_channel(ch["id"])


class TestTheBoxAndItsDefault:
    async def test_a_channel_treats_its_senders_as_users_by_default(self):
        ch = await _channel()
        assert ch["senders_are_members"] is True
        assert ch["visitor_tools"] == []

    async def test_the_list_is_stored_clean(self):
        ch = await _channel(senders_are_members=False, visitor_tools=["peek", " peek", "remember"])
        assert ch["senders_are_members"] is False
        assert ch["visitor_tools"] == ["peek"]


    @pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
    async def test_anything_but_a_real_true_is_a_visitor_channel(self, value):
        """The route saves raw JSON: `"false"` used to be stored as on."""
        ch = await _channel(senders_are_members=value)
        assert ch["senders_are_members"] is False


class TestWhoTheSenderIs:
    async def test_on_the_turn_runs_as_a_member(self, manager):
        ch = await _channel()
        await _adapter(ch, manager)._dispatch("c1", "hello", "u1")
        assert manager.scopes == [None]

    async def test_off_the_turn_runs_in_the_channels_visitor_scope(self, manager):
        ch = await _channel(senders_are_members=False, visitor_tools=["peek"])
        await _adapter(ch, manager)._dispatch("c1", "hello", "u1")
        assert manager.scopes == [VisitorScope("channel", frozenset({"peek"}))]
        assert visitor.current() is None, "the adapter's own context is left as found"

    async def test_the_box_is_read_on_every_message(self, manager):
        """Not once when the adapter was built: a settings edit must not need a
        channel restart (D72 paid for that once)."""
        ch = await _channel()
        adapter = _adapter(ch, manager)
        await channel_store.save_channel({**ch, "senders_are_members": False, "visitor_tools": []})
        await adapter._dispatch("c1", "hello", "u1")
        assert manager.scopes == [VisitorScope("channel", frozenset())]


class TestAVisitorCannotAnswerAWaitingRun:
    async def test_off_a_message_is_never_taken_as_an_answer(self, manager):
        ch = await _channel(senders_are_members=False)
        await _adapter(ch, manager)._dispatch("c1", "yes, ship it", "u1")
        assert manager.asked_waiting == 0
        assert len(manager.scopes) == 1

    async def test_on_a_message_may_answer_one(self, manager):
        ch = await _channel()
        await _adapter(ch, manager)._dispatch("c1", "yes, ship it", "u1")
        assert manager.asked_waiting == 1


class TestTheRouteKeepsBoth:
    async def test_a_save_through_the_api_keeps_the_box_and_the_list(self, client):
        response = await client.post("/api/messaging/channels", json={
            "name": "Public bot", "platform": "telegram", "credentials": {"bot_token": "1:x"},
            "senders_are_members": False, "visitor_tools": ["peek"],
        })
        assert response.status_code == 200, response.text
        saved = await channel_store.get_channel(response.json()["id"])
        assert saved["senders_are_members"] is False and saved["visitor_tools"] == ["peek"]
