"""Synapse AI reads a skill before it changes one line of it (F1379, TRI1; BLD1's v2.0 shop-hours pass; eb 11-04).

Asked to change one day in a skill and keep the rest, Synapse AI asked the person to paste the skill's whole text: the
planner could list skills but held no tool that reads one, and update_skill replaces a skill's whole body. The planner
now holds `get_skill`: through the builder's own chat, its call answers with the skill's text, which the planner's next
turn reads, and the change it writes from it reaches the review with the rest of the skill as it was.
"""
from __future__ import annotations

import json

import pytest

from core.native_builder import seed_native_builder

from test_builder_routes import route, stream, tool_call

BODY = "# Opening times\nMondays to Fridays, 9am to 5pm.\nSaturdays, 10am to 3pm.\nClosed on Sundays."
NEW = BODY.replace("10am to 3pm", "10am to 2pm")


@pytest.fixture
async def skill():
    from core import skills

    await seed_native_builder()
    await skills.save_library([{"name": "opening-times", "description": "When we are open.", "body": BODY}])


async def test_its_text_is_read_then_the_change_reaches_the_review(client, skill, fake_llm):
    fake_llm.script([
        route("manage"),
        tool_call("get_skill", name="opening-times"),
        tool_call("submit_change", ops=[{"op": "update_skill", "target": {"kind": "skill", "name": "opening-times"},
                                         "fields": {"body": NEW}, "says": "Saturdays close at 2pm"}]),
    ])
    events = await stream(client, "/api/builder/chat", {
        "message": "In the opening-times skill, change only Saturday's closing time to 2pm; keep the rest as it is.",
        "client_session_id": "f1379"})
    # The planner's turn after its get_skill call read the skill's text: "Closed on Sundays." is in no message of the
    # person's, only in the skill.
    later = [json.dumps(c, default=str, ensure_ascii=False) for c in fake_llm.calls[2:]]
    assert any("Closed on Sundays." in c for c in later), "the planner never read the skill's text"
    [asked] = [e for e in events if e["type"] == "human_input_required"]
    assert asked["orch_step_id"] == "step_manage_review", asked
    assert asked.get("context", {}).get("calls") == [["update_skill", {"name": "opening-times", "body": NEW}]], asked
