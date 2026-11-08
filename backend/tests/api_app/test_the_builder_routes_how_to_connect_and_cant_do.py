"""How-to, connect and can't-do are answered from what code looks up (BWA1, D274, decision 6; in code since SAI2-P4).

Measured on cloud dev after the first fixes landed (gpt-oss-120b): "How do I connect Notion…?" and "How do I get our
support bot answering in Slack?" were answered from the model's memory of a product — the answering agent held
`search_help` and `prepare_connection` and called neither — and "use my own OpenAI key" was planned as an
`update_agent` change. BWA1 gave each its own route whose first step called the tool on the quick model. Since SAI2-P4
they are one answer route whose pack is built in code, with no quick call at all (`answer_pack`):

- how-to (`needs: help`) → the help's hits, found by `search_help`'s own search;
- connect → the Connect card for the provider the person named, put up in code, reaching the panel's stream;
- can't-do → `product_capabilities`' text, in every pack.
"""
from __future__ import annotations

import pytest

from core.native_builder import seed_native_builder
from test_builder_routes import route, stream


@pytest.fixture
async def builder():
    await seed_native_builder()


def _steps(events):
    return [e.get("step_name") for e in events if e["type"] == "step_start"]


async def test_a_how_to_question_is_answered_from_the_help(client, builder, fake_llm):
    fake_llm.script([route("howto"), "Slack connects on the Channels screen."])
    events = await stream(client, "/api/builder/chat", {"message": "How do I connect Slack?"})
    assert "What the Answer Needs" in _steps(events) and "Answer From the Help" in _steps(events)
    assert len(fake_llm.calls) == 2, "the classify call and the answer: the help is looked up in code"
    answer_prompt = str(fake_llm.calls[-1])
    assert "Channels" in answer_prompt, "the help's own words reach the answering agent"


async def test_a_connect_request_puts_up_the_card(client, builder, fake_llm):
    fake_llm.script([route("connect", providers=["OpenAI"]), "The card above takes your OpenAI key."])
    events = await stream(client, "/api/builder/chat", {"message": "Use my own OpenAI key for our agents"})
    cards = [e for e in events if e["type"] == "connection_needed"]
    assert len(cards) == 1 and cards[0]["provider"]["id"] == "openai"
    assert "What the Answer Needs" in _steps(events)
    assert "Connect card for OpenAI is up above" in str(fake_llm.calls[-1])


async def test_a_cant_do_request_reads_what_the_product_does(client, builder, fake_llm):
    fake_llm.script([route("cantdo"), "It does not build native mobile apps; the nearest thing is a chat channel."])
    events = await stream(client, "/api/builder/chat", {"message": "Build me an iPhone app for our clinic"})
    assert "What the Answer Needs" in _steps(events)
    assert "does not build native mobile or desktop apps" in str(fake_llm.calls[-1])
    assert not [e for e in events if e["type"] == "resource_saved"], "nothing was built"
