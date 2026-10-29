"""Brief 6's shape through the builder's route: a restated request is not asked the same thing again (F1226, CAP3).

The person asked for a weekly pricing workflow; the planner asked whether the knowledge base existed; the person,
instead of answering, said the request again — a new message, so a new run. That run asked the same question, and
so on for 18 questions. Now the route starts each run with the questions its conversation's request asked, and the
planner's repeat is refused with what the person said, so it plans with what it has.
"""
from __future__ import annotations

import json

import pytest

from core import builder_tools  # noqa: F401 — the builder's tool modules load in their own order first
from core.native_builder import seed_native_builder
from core.native_builder.change_tools import ASKED_KEY
from test_builder_routes import route, stream, tool_call

KB = "the knowledge base 'competitor_pricing'"
ASK = {"question": "Has the competitor_pricing knowledge base been created yet?", "about": "what", "subject": KB}
PLAN = {"kind": "create", "title": "Weekly prices", "summary": "Reads three pricing pages each Monday.", "steps": [
    {"id": "read", "type": "tool", "name": "Read the Pages", "does": "Reads the pricing pages", "tool": "scrape_url"}]}


@pytest.fixture
async def builder():
    await seed_native_builder()


async def _state(run_id: str) -> dict:
    from core.orchestration.state import SharedState

    return (await SharedState.restore(run_id)).run.shared_state


async def test_the_second_run_does_not_ask_again(client, builder, fake_llm):
    conv = {"client_session_id": "f1226-conv"}
    fake_llm.script([route("create"), tool_call("ask_person", **ASK)])
    first = await stream(client, "/api/builder/chat", {"message": "Keep competitors' prices in a knowledge base", **conv})
    asked = [e for e in first if e["type"] == "human_input_required"]
    assert asked and "competitor_pricing" in str(asked[-1].get("prompt")), "turn one asks about the knowledge base"

    fake_llm.reset()
    fake_llm.script([route("create"), tool_call("ask_person", **ASK), tool_call("submit_plan", **PLAN)])
    second = await stream(client, "/api/builder/chat",
                          {"message": "I need a weekly workflow that keeps the prices in a knowledge base", **conv})
    said = json.dumps([e for e in second if e["type"] == "tool_result"])
    assert "You asked the person this already" in said, "the repeat is refused in the planner's own turn"
    run_id = next(e["run_id"] for e in second if e.get("run_id"))
    carried = (await _state(run_id)).get(ASKED_KEY) or []
    assert [a["subject"] for a in carried if a.get("carried")] == [KB], "the route carried the first run's question"
