"""Small talk reads nothing (SAI2-P4, F980/F983; P4-PROMPT §7, the headline measure).

F1015 on prod (rc4.2): "hi" on Nova Pro went to the answer step, whose prompt said "find out… using your read tools",
so it read the workspace (`today` three times) until the repeat guard failed the step: an error page for "hi". On dev,
Nova Pro made 4 to 26 varied reads for "hi". The repeat guard cannot catch varied reads, so the front door removes
them: a turn whose answer needs nothing (`needs: none`) is answered by a step offered no tool at all. It still knows
the workspace it is in and what the product can do, from a pack code builds — and says nothing about a business the
workspace's description does not name (rc4.2: Kestrel, a freight company, was "your manufacturing business").
"""
from __future__ import annotations

import pytest

from core.native_builder import seed_native_builder
from test_builder_routes import stream, tool_call

READS = {"read_workspace", "list_agents", "list_orchestrations", "list_schedules", "list_recent_runs",
         "get_run_summary", "search_help", "product_capabilities", "list_skills", "list_custom_tools"}


@pytest.fixture
async def builder():
    await seed_native_builder()


@pytest.mark.parametrize("message", ["hi", "thanks!", "brainstorm a reminder flow for a dental clinic with me"])
async def test_talk_is_answered_with_no_read(client, builder, fake_llm, message):
    fake_llm.script([tool_call("classify_request", kind="answer", needs="none"), "Hello! What shall we work on?"])
    events = await stream(client, "/api/builder/chat", {"message": message})

    order = [e.get("orch_step_id") for e in events if e["type"] == "step_start"]
    assert "step_answer_talk" in order and "step_answer_workspace" not in order, order
    used = {e.get("tool_name") for e in events if e["type"] == "tool_execution"}
    assert not used & READS, used & READS
    assert len(fake_llm.calls) == 2, "the classify call and the reply: nothing else"
    final = next(e for e in events if e["type"] == "orchestration_complete")["final_state"]
    assert final["summary"] == "Hello! What shall we work on?"

    talk = str(fake_llm.calls[-1].get("prompt_msg"))
    assert "What this product does" in talk, "what it can do comes from code, never invented"
    assert "no description recorded" in talk and "say nothing about what the business does" in talk


async def test_a_question_about_the_workspace_still_reads_it(client, builder, fake_llm):
    """The other side of the line: code finds a workspace noun, so the model's `none` is overruled towards reading."""
    fake_llm.script([tool_call("classify_request", kind="answer", needs="none"), "Nothing failed today."])
    events = await stream(client, "/api/builder/chat", {"message": "what failed today?"})
    order = [e.get("orch_step_id") for e in events if e["type"] == "step_start"]
    assert "step_answer_workspace" in order and "step_answer_talk" not in order, order
