"""A turn cut before it answered still ran its calls, and the next turn is told so (F477, VOX1f, D273).

VOX1f's call 5 (`CAea7df7…`, gpt-4.1-mini, planning on): the caller paused mid-ask; the turn their next words cut had
searched "phone case" and "charger", and F477 kept both calls with their results. The next turn searched all three
again. The call saves a cut turn with an empty answer, and its digest read "Tool calls you made for this answer"
over nothing, which F426 found reads as nothing having run. An empty answer is now unanswered, as a stopped one is.
"""

SEARCH = {"tool": "shop_search_products", "args": {"query": "charger"}, "result": '{"products": [{"price": 19.99}]}'}


async def _saved(session_id, agent_id, answer, calls):
    from core.session import _save_conversation_turn
    await _save_conversation_turn(session_id=session_id, agent_id=agent_id, user="prices of a charger",
                                  assistant=answer, tools=[f"{c['tool']}: {c['result']}" for c in calls], calls=calls)


async def test_a_cut_turns_digest_says_its_calls_ran():
    from core.session import get_recent_history_messages
    await _saved("s_f477_cut", "agent_c", "", [SEARCH])

    _, answer = await get_recent_history_messages("s_f477_cut", agent_id="agent_c", tool_digest=True)
    assert "they did run" in answer["content"]
    assert "for this answer" not in answer["content"]
    assert "shop_search_products" in answer["content"] and "19.99" in answer["content"]


async def test_a_turn_with_only_spaces_for_an_answer_is_cut_too():
    from core.session import get_recent_history_messages
    await _saved("s_f477_blank", "agent_b", "  \n", [SEARCH])

    _, answer = await get_recent_history_messages("s_f477_blank", agent_id="agent_b", tool_digest=True)
    assert "they did run" in answer["content"]
