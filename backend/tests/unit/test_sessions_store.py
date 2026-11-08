"""
Chat sessions in the store.

The properties that matter are the ones the JSON files gave for free and a
single shared table has to be designed for: two agents under one session id
stay two conversations, the standalone server and the worker see the same
rows, and the v2 API's message shape does not change underneath its clients.
"""
import pytest

from core.scale.context import set_resource_provider
from core.session import (
    _get_conversation_history,
    _save_conversation_turn,
    clear_all_chat_sessions,
    delete_chat_session,
    get_cli_session_id,
    get_last_response_snapshot,
    get_recent_history_messages,
    list_chat_sessions,
    save_cli_session_id,
)
from core.store import sessions as store
from core.tenancy import tenant_scope


class _Provider:
    async def resolve_agent(self, agent_id):
        return None

    async def resolve_orchestration(self, orch_id):
        return None

    async def resolve_custom_tools(self):
        return []

    async def resolve_mcp_servers(self):
        return []


@pytest.fixture
def multi_tenant():
    set_resource_provider(_Provider())
    yield
    set_resource_provider(None)


# ── the reason the key includes the agent ────────────────────────────────────

async def test_two_agents_under_one_session_id_stay_two_conversations():
    """The UI keeps one session id per browser and switches agents under it.

    Keyed on session_id alone these merge, and the next turn shows one agent
    the other agent's conversation.
    """
    await _save_conversation_turn("s1", "agent_a", "hello A", "reply A")
    await _save_conversation_turn("s1", "agent_b", "hello B", "reply B")

    a = await _get_conversation_history("s1", "agent_a")
    b = await _get_conversation_history("s1", "agent_b")

    assert [t["user"] for t in a] == ["hello A"]
    assert [t["user"] for t in b] == ["hello B"]
    assert len(await list_chat_sessions()) == 2


async def test_deleting_one_agents_session_leaves_the_others():
    await _save_conversation_turn("s1", "agent_a", "hello A", "reply A")
    await _save_conversation_turn("s1", "agent_b", "hello B", "reply B")

    assert await delete_chat_session("s1", "agent_a") is True

    assert await _get_conversation_history("s1", "agent_a") == []
    assert len(await _get_conversation_history("s1", "agent_b")) == 1


# ── the record shape ─────────────────────────────────────────────────────────

async def test_a_turn_round_trips_with_its_tools_and_timestamp():
    await _save_conversation_turn("s1", "a1", "question", "answer", tools=["search"])

    turn = (await _get_conversation_history("s1", "a1"))[0]
    assert turn["user"] == "question"
    assert turn["assistant"] == "answer"
    assert turn["tools"] == ["search"]
    assert turn["timestamp"].endswith("Z")


async def test_stored_messages_are_the_shape_the_v2_api_returns():
    """`GET /v2/chat/{id}/status` hands `messages` straight to the client.

    The per-turn extras ride along as additional keys, so a reader that knows
    only about role and content is unaffected.
    """
    await _save_conversation_turn("s1", "a1", "question", "answer", tools=["search"])

    messages = (await store.get("s1", "a1"))["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "question"
    assert messages[1]["tools"] == ["search"]


async def test_recent_history_is_the_last_ten_turns_as_messages():
    for i in range(12):
        await _save_conversation_turn("s1", "a1", f"q{i}", f"a{i}")

    messages = await get_recent_history_messages("s1", "a1")
    assert len(messages) == 20
    assert messages[0]["content"] == "q2"
    assert messages[-1]["content"] == "a11"


async def test_the_snapshot_reports_the_last_answer():
    await _save_conversation_turn("s1", "a1", "q1", "a1")
    await _save_conversation_turn("s1", "a1", "q2", "final answer")

    snapshot = await get_last_response_snapshot("s1", "a1")
    assert snapshot["last_response"] == "final answer"
    assert snapshot["last_updated"].endswith("Z")


async def test_a_session_summary_carries_its_counts():
    await _save_conversation_turn("s1", "a1", "first question", "a")
    await _save_conversation_turn("s1", "a1", "second", "b")

    summary = (await list_chat_sessions(agent_id="a1"))[0]
    assert summary["turn_count"] == 2
    assert summary["first_user_message"] == "first question"
    assert summary["last_response"] == "b"


# ── CLI session ids ──────────────────────────────────────────────────────────

async def test_cli_session_ids_survive_a_turn():
    """The worker shim used to write these away as empty on every job."""
    await save_cli_session_id("s1", "a1", "anthropic_cli", "cli-xyz")
    await _save_conversation_turn("s1", "a1", "q", "a")

    assert await get_cli_session_id("s1", "a1", "anthropic_cli") == "cli-xyz"


async def test_cli_session_ids_are_per_agent():
    await save_cli_session_id("s1", "agent_a", "anthropic_cli", "cli-a")
    await save_cli_session_id("s1", "agent_b", "anthropic_cli", "cli-b")

    assert await get_cli_session_id("s1", "agent_a", "anthropic_cli") == "cli-a"
    assert await get_cli_session_id("s1", "agent_b", "anthropic_cli") == "cli-b"


# ── tenancy ──────────────────────────────────────────────────────────────────

async def test_one_tenants_conversation_is_invisible_to_another(multi_tenant):
    with tenant_scope("acme"):
        await _save_conversation_turn("s1", "a1", "acme secret", "reply")

    with tenant_scope("globex"):
        assert await _get_conversation_history("s1", "a1") == []
        assert await list_chat_sessions() == []
        assert await get_recent_history_messages("s1", "a1") == []
        assert await delete_chat_session("s1", "a1") is False
        assert await clear_all_chat_sessions() == 0

    with tenant_scope("acme"):
        assert len(await _get_conversation_history("s1", "a1")) == 1
