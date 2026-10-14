"""The AI builder's router sends how-to, connect and can't-do requests to steps that look the answer up in code (BWA1,
D274, decision 6).

Measured on cloud dev after the first fixes landed (gpt-oss-120b): "How do I connect Notion…?" and "How do I get our
support bot answering in Slack?" were answered from the model's memory of a product — the answering agent held
`search_help` and `prepare_connection` and called neither — and "use my own OpenAI key" was planned as an
`update_agent` change. Each now has its own route, whose first step calls the tool (a tool step, on the quick role)
and hands what it returned to the answering agent:

- `howto` → `search_help` with the person's words;
- `connect` → `prepare_connection`, whose Connect card reaches the panel's stream;
- `cantdo` → `product_capabilities`.
"""
from __future__ import annotations

import pytest

from core.native_builder import seed_native_builder
from test_builder_routes import route, stream, tool_call


@pytest.fixture
async def builder():
    await seed_native_builder()


def _steps(events):
    return [e.get("step_name") for e in events if e["type"] == "step_start"]


async def test_a_how_to_question_is_answered_from_the_help(client, builder, fake_llm):
    fake_llm.script([
        route("howto"),
        tool_call("search_help", question="How do I connect Slack?"),
        "Slack connects on the Channels screen.",
    ])
    events = await stream(client, "/api/builder/chat", {"message": "How do I connect Slack?"})
    assert "Look It Up in the Help" in _steps(events) and "Answer the Question" in _steps(events)
    answer_prompt = str(fake_llm.calls[-1])
    assert "Channels" in answer_prompt, "the help's own words reach the answering agent"


async def test_a_connect_request_puts_up_the_card(client, builder, fake_llm):
    fake_llm.script([
        route("connect"),
        tool_call("prepare_connection", kind="provider_key", provider="openai", title="Add your OpenAI key",
                  why="Your agents run on your own OpenAI account."),
        "The card above takes your OpenAI key.",
    ])
    events = await stream(client, "/api/builder/chat", {"message": "Use my own OpenAI key for our agents"})
    cards = [e for e in events if e["type"] == "connection_needed"]
    assert len(cards) == 1 and cards[0]["provider"]["id"] == "openai"
    assert "Put Up a Connect Card" in _steps(events)


async def test_a_cant_do_request_reads_what_the_product_does(client, builder, fake_llm):
    fake_llm.script([
        route("cantdo"),
        tool_call("product_capabilities"),
        "It does not build native mobile apps; the nearest thing is a chat channel.",
    ])
    events = await stream(client, "/api/builder/chat", {"message": "Build me an iPhone app for our clinic"})
    assert "Read What It Can Do" in _steps(events)
    assert "does not build native mobile or desktop apps" in str(fake_llm.calls[-1])
    assert not [e for e in events if e["type"] == "resource_saved"], "nothing was built"
