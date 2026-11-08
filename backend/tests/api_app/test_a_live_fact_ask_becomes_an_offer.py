"""A live-fact ask becomes an offer, and the yes builds it, through the builder's own chat (F1382, TRI1; Naveen 11-04:
"Build as planned").

Turn one asks a city's weather: the talk step answers it, holding no tool, from a pack that says who Synapse AI is and
names the fact, and the reply ends with the offer (code's own, when the reply forgot to ask). Turn two says "yes": it
is the offer's ask, through the role door to the planner. A question about this workspace's own state, the same day,
still reads the workspace.
"""
from __future__ import annotations

import pytest

from core.native_builder import seed_native_builder
from test_builder_routes import stream, tool_call

FACT = "the weather in Lisbon today"


@pytest.fixture
async def builder():
    await seed_native_builder()


def _order(events: list[dict]) -> list[str]:
    return [e.get("orch_step_id") for e in events if e["type"] == "step_start"]


def _final(events: list[dict]) -> dict:
    return next(e for e in events if e["type"] == "orchestration_complete")["final_state"]


async def test_the_offer_then_the_yes_builds_it(client, builder, fake_llm):
    from core.native_builder.answer_pack import offer_line
    from core.native_builder.classify import offer_ask

    conv = {"client_session_id": "f1382-offer"}
    fake_llm.script([tool_call("classify_request", kind="answer", needs="workspace", live_fact=FACT),
                     "I don't look that up here."])
    first = await stream(client, "/api/builder/chat", {"message": "What's the weather in Lisbon today?", **conv})
    order = _order(first)
    assert "step_answer_talk" in order and "step_answer_workspace" not in order, order
    talk = str(fake_llm.calls[-1].get("prompt_msg"))
    assert "Who you are: Synapse AI" in talk and "What the product delivers, with an example of each:" in talk
    assert f"outside this workspace and changes over time: {FACT}." in talk
    said = _final(first)["summary"]
    assert said.startswith("I don't look that up here.") and said.endswith(offer_line(FACT)), said

    fake_llm.reset()
    fake_llm.script([tool_call("classify_request", kind="answer", needs="none")])
    fake_llm.set_default(tool_call("say", text="Here is the plan."))
    second = await stream(client, "/api/builder/chat", {"message": "yes", **conv})
    order = _order(second)
    assert "step_may_change" in order and "step_plan" in order, order
    assert "step_answer_talk" not in order, order
    classified = str(fake_llm.calls[0].get("prompt_msg"))
    assert offer_ask(FACT) in classified, "the classify reads the offer's ask, not a bare yes"


async def test_a_workspace_question_the_same_day_still_reads_it(client, builder, fake_llm):
    fake_llm.script([tool_call("classify_request", kind="answer", needs="none", live_fact="runs failed today"),
                     "Nothing failed today."])
    events = await stream(client, "/api/builder/chat", {"message": "How many runs failed today?"})
    order = _order(events)
    assert "step_answer_workspace" in order and "step_answer_talk" not in order, order
    assert not _final(events).get("offered_build")
