"""Chat's History lists the person's conversations, not the AI builder's machinery (F228).

Every builder run keeps a conversation per sub-agent under a `builder:` session
id, so once the builder had been used, History listed rows for
`agent_native_builder_analyst` and `…_saver_update`, each previewing its whole
step prompt ("## YOUR ROLE IN THIS WORKFLOW …"). F186's class: the builder's
own agents leaking into chat. A builder-typed agent the person made is theirs,
and its chat conversation stays listed.
"""
from __future__ import annotations

from core.session import _save_conversation_turn


async def _listed(client, **params) -> set[str]:
    response = await client.get("/api/sessions", params=params)
    assert response.status_code == 200, response.text
    return {s["session_id"] for s in response.json()}


class TestHistory:
    async def test_leaves_out_the_builders_own_conversations(self, client):
        await _save_conversation_turn("chat-mine", "agent_mine", "hello", "hi")
        await _save_conversation_turn("builder:default:panel:conv-1", "agent_native_builder_analyst",
                                      "## YOUR ROLE IN THIS WORKFLOW", "Requirements: …")
        await _save_conversation_turn("builder:default:chat-mine", "agent_native_builder_saver_update",
                                      "## YOUR ROLE IN THIS WORKFLOW", "Saved.")

        assert await _listed(client) == {"chat-mine"}

    async def test_keeps_a_conversation_with_a_builder_typed_agent_the_person_made(self, client):
        # The chat page's builder-typed agent keeps its turns under the chat's own id.
        await _save_conversation_turn("chat-with-my-builder", "agent_my_builder", "build x", "done")
        assert "chat-with-my-builder" in await _listed(client)

    async def test_the_agent_filter_still_works(self, client):
        await _save_conversation_turn("chat-a", "agent_a", "q", "a")
        await _save_conversation_turn("chat-b", "agent_b", "q", "a")
        assert await _listed(client, agent_id="agent_a") == {"chat-a"}
