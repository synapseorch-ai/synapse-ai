"""
One Synapse AI chat writes a workflow at a time, on the build route too (TABS2, D327; F1063).

"Change the steps of X" is the build route's update (the change route hands `edit_steps` over to it), and since F1035
the builder's MCP and CLI saves go through `save_build` as well. An update another chat holds is not offered for a yes
(`check_build`), and a save that raced past its review is refused before anything is made (`save_build`).
"""
from __future__ import annotations

from core.store.resources import get_orchestration, save_orchestration
from test_a_build_is_saved_in_code import TELL, _call, _reviewed, _yes, workspace  # noqa: F401 — fixture
from test_one_writer_per_workflow import _aged, _ready_rename, _turn

WIND = {"id": "orch_wind", "name": "Wind", "description": "", "entry_step_id": "check",
        "steps": [{"id": "check", "name": "Check the weather", "type": "tool", "forced_tool": "rotterdam_weather",
                   "output_key": "check", "next_step_id": "done"}, {"id": "done", "name": "End", "type": "end"}]}
EDITS = {"finish": "submit_plan", "kind": "update", "title": "Wind", "summary": "Also draft a warning.",
         "edits": [{"op": "add_step", "after": "check", "step": {**TELL, "agent": "agent_desk"}}]}


async def test_an_update_of_a_workflow_open_in_another_chat_is_not_offered(client):
    await save_orchestration(dict(WIND))
    existing = await get_orchestration("orch_wind")
    await _turn("run_a", "chat-a", asked="move the wind check to 6am", change_checked=_ready_rename("orch_wind"))

    run, checked = await _reviewed(EDITS, intent="update", existing_orch=existing, current_orchestration_id="orch_wind")

    assert checked["outcome"] == "said", checked
    assert 'open in another Synapse AI chat ("move the wind check to 6am")' in checked["say"]


async def test_a_save_that_raced_past_its_review_makes_nothing(client):
    await save_orchestration(dict(WIND))
    existing = await get_orchestration("orch_wind")
    run, checked = await _reviewed(EDITS, intent="update", existing_orch=existing, current_orchestration_id="orch_wind")
    assert checked["outcome"] == "review", checked
    await _yes(run)
    # Another chat's review of the same workflow began before this one, and still waits on its yes.
    await _turn("run_a", "chat-a", change_checked=_ready_rename("orch_wind"))
    await _aged("run_a", hours=0.5)

    out, events = await _call("save_build", {}, run)

    assert out["outcome"] == "stopped" and "another Synapse AI chat" in out["reason"], out
    assert [s["id"] for s in (await get_orchestration("orch_wind"))["steps"]] == ["check", "done"]
    assert not [e for e in events if e.get("type") == "resource_saved"]


async def test_a_new_workflow_is_never_held(client):
    await _turn("run_a", "chat-a", change_checked=_ready_rename("orch_wind"))
    run, checked = await _reviewed({"finish": "submit_plan", "kind": "create", "title": "Rotterdam wind warning",
                                    "summary": "Warn when the wind is strong.",
                                    "steps": [{"id": "check", "type": "tool", "name": "Check the weather",
                                               "does": "Reads today's weather", "tool": "rotterdam_weather"}]})
    assert checked["outcome"] == "review", checked
