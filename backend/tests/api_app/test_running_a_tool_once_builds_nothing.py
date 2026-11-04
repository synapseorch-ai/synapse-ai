"""Asked twice to run a tool that is here, through the builder's own chat, Synapse AI points to its Test both times,
calls no model and builds nothing (F1383, TRI1; CAP3's ff19-f1 fix-02 repeat; Naveen 11-05: "Prepare and hold")."""
from __future__ import annotations

import pytest

from core.native_builder import seed_native_builder
from test_builder_routes import stream

TOOL = "region_totals"


@pytest.fixture
async def builder():
    from core.store.resources import save_tool

    await seed_native_builder()
    await save_tool({"name": TOOL, "description": "Totals per region between two dates.", "tool_type": "http",
                     "method": "GET", "url": "http://localhost:9418/totals?start={{ start }}&end={{ end }}",
                     "headers": {}, "inputSchema": {"type": "object", "properties": {
                         "start": {"type": "string"}, "end": {"type": "string"}}}})


async def test_the_ask_and_its_repeat_point_to_the_test_and_build_nothing(client, builder, fake_llm):
    from core.store.resources import load_orchestrations

    before = {o["id"] for o in await load_orchestrations()}
    conv = {"client_session_id": "f1383-repeat"}
    said = []
    for message in ("Can you run region_totals for 2026-10-01 to 2026-10-31?",
                    "Please run region_totals with start 2026-10-01 end 2026-10-31"):
        events = await stream(client, "/api/builder/chat", {"message": message, **conv})
        order = [e.get("orch_step_id") for e in events if e["type"] == "step_start"]
        assert "step_classify" not in order and "step_plan" not in order, order
        final = next(e for e in events if e["type"] == "orchestration_complete")["final_state"]
        said.append(final["summary"])
    assert said[0] == said[1] and f'"{TOOL}"' in said[0] and "Test" in said[0], said
    assert fake_llm.calls == [], "no model is asked: code answers it"
    assert {o["id"] for o in await load_orchestrations()} == before, "no workflow is made"
