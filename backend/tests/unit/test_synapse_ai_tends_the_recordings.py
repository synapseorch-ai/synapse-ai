"""Synapse AI tends the recordings as the Recordings screen does (F1270, REACH1; D340 §6, Naveen 10-30).

A Browser step that met a changed page keeps its fix on the recording, waiting for a person (F1144). Synapse AI could
list recordings and delete one, never use or drop a kept fix or rename one. Each is now a card whose button is a
product tool over the recordings' own functions (what the screen's routes call), at the builder role, in the app only
(BRW-API's MCP surface stays as Naveen set it, F1188):

- the fix card says the step now and what the fix does, step by step, and only the press uses it;
- dropping a fix leaves the recording as it is; a fix used or dropped already is said, not guessed;
- a rename is a new version; deleting names what uses the recording;
- a plan without Browser is said in its host's words before any card;
- topic `waiting` lists each kept fix with what answers it, and topic `recordings` says how many wait;
- none of the three new tools is offered over MCP; the census's Browser row names them.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks

ORDER = {"role": "textbox", "name": "Order number", "label": "Order number", "css": "#order", "field": True}


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
def fresh():
    confirm.set_claim_store(None)
    builder_access.forget_runs()
    yield
    confirm.set_claim_store(None)
    hooks.set_role_source(None)
    builder_access.forget_runs()


def _steps():
    return [{"id": "s1", "action": "open", "url": "https://portal.saltmarsh.example/orders", "target": None,
             "value": None},
            {"id": "s2", "action": "type", "target": ORDER, "value": {"fixed": "A-1001"}},
            {"id": "s3", "action": "press", "key": "Enter", "target": ORDER, "value": None}]


async def _with_a_fix():
    from core.browser import recordings

    doc = await recordings.create("Find an order", _steps(), by="you")
    fix = [{"id": "s2b", "action": "type", "target": {**ORDER, "name": "Order no.", "label": "Order no."},
            "value": {"fixed": "A-1001"}}]
    edit = await recordings.add_edit(doc["id"], step_id="s2", steps=fix, by="agent", run_id="run9")
    return doc, edit


async def _prepare(args):
    await SharedState(OrchestrationRun(run_id="builder_rec", orchestration_id="orch_native_builder",
                                       shared_state={builder_access.CALLER_KEY: {"user_id": None}})).checkpoint()
    raw, events = await execute_builder_tool_with_events("prepare_action", args, _Server(), run_id="builder_rec")
    return json.loads(raw), [e for e in events if e.get("type") == "action_needed"]


async def _read(**args) -> dict:
    raw, _ = await execute_builder_tool_with_events("read_workspace", args, server_module=None)
    return json.loads(raw)


class TestAKeptFix:
    async def test_its_card_shows_the_step_now_and_the_fix_and_only_the_press_uses_it(self, client):
        from core.browser import recordings

        doc, edit = await _with_a_fix()
        said, [card] = await _prepare({"action": "recording_approve_edit", "recording": "find an order",
                                       "edit_id": edit})
        assert "nothing has been done" in said["say"].lower()
        assert card["title"] == 'Use the kept fix in "Find an order"'
        assert "Type “A-1001” into Order number" in card["detail"]
        assert "Type “A-1001” into Order no." in card["detail"]
        [button] = card["buttons"]
        assert button["tool"] == "approve_recording_edit"
        assert button["arguments"] == {"recording_id": doc["id"], "edit_id": edit}
        assert (await recordings.get(doc["id"]))["version"] == 1, "shown, not done"

        done = await client.post("/api/assistant/act", json=button)
        assert done.status_code == 200, done.text
        after = await recordings.get(doc["id"])
        assert [s["id"] for s in after["steps"]] == ["s1", "s2b", "s3"] and after["version"] == 2
        assert after["pending_edits"] == []

    async def test_dropping_it_leaves_the_recording_as_it_is(self, client):
        from core.browser import recordings

        doc, edit = await _with_a_fix()
        _, [card] = await _prepare({"action": "recording_discard_edit", "recording": doc["id"], "edit_id": edit})
        assert card["title"] == 'Drop the kept fix in "Find an order"' and "stays as it is" in card["detail"]
        done = await client.post("/api/assistant/act", json=card["buttons"][0])
        assert done.status_code == 200, done.text
        after = await recordings.get(doc["id"])
        assert after["version"] == 1 and after["pending_edits"] == [] and after["steps"][1]["id"] == "s2"

    async def test_one_used_or_dropped_already_is_said(self, client):
        from core.browser import recordings

        doc, edit = await _with_a_fix()
        await recordings.discard_edit(doc["id"], edit)
        said, cards = await _prepare({"action": "recording_approve_edit", "recording": doc["id"], "edit_id": edit})
        assert cards == [] and "That fix is gone" in said["error"]


class TestTheRecordingItself:
    async def test_a_rename_is_a_new_version(self, client):
        from core.browser import recordings

        doc, _ = await _with_a_fix()
        _, [card] = await _prepare({"action": "recording_rename", "recording": doc["id"], "name": "Look up an order"})
        assert card["title"] == 'Rename the recording "Find an order" to "Look up an order"'
        done = await client.post("/api/assistant/act", json=card["buttons"][0])
        assert done.status_code == 200, done.text
        after = await recordings.get(doc["id"])
        assert after["name"] == "Look up an order" and after["version"] == 2

    async def test_the_same_name_is_said_not_carded(self, client):
        doc, _ = await _with_a_fix()
        said, cards = await _prepare({"action": "recording_rename", "recording": doc["id"], "name": "Find an order"})
        assert cards == [] and "already called" in said["error"]

    async def test_a_delete_names_what_uses_it(self, client):
        from core.store.resources import save_orchestration
        from tests._fakes.seed import make_orchestration

        doc, _ = await _with_a_fix()
        flow = make_orchestration(name="Order lookups")
        flow["steps"][0]["recording_id"] = doc["id"]
        await save_orchestration(flow)
        _, [card] = await _prepare({"action": "recording_delete", "recording": "Find an order"})
        assert card["danger"] and card["buttons"][0]["tool"] == "delete_recording"
        assert 'the workflow "Order lookups"' in card["detail"]

    async def test_a_plan_without_browser_is_said_first(self, client):
        from core import builder_tools

        doc, edit = await _with_a_fix()

        async def no_browser(kind):
            return "Your plan has no Browser: it comes with Pro."

        builder_tools.set_browser_check(no_browser)
        try:
            said, cards = await _prepare({"action": "recording_approve_edit", "recording": doc["id"],
                                          "edit_id": edit})
        finally:
            builder_tools.set_browser_check(None)
        assert cards == [] and said["error"] == "Your plan has no Browser: it comes with Pro."


class TestWhatSynapseAiReads:
    async def test_waiting_lists_each_kept_fix_with_what_answers_it(self, client):
        doc, edit = await _with_a_fix()
        got = await _read(topic="waiting")
        [fix] = [i for i in got["items"] if i.get("kind") == "recording_fix"]
        assert fix["recording"] == "Find an order" and fix["edit_id"] == edit
        assert fix["answer_with"] == ["recording_approve_edit", "recording_discard_edit"]

    async def test_recordings_say_how_many_fixes_wait(self, client):
        await _with_a_fix()
        [item] = (await _read(topic="recordings"))["items"]
        assert item["fixes_waiting"] == 1


def test_the_new_tools_are_the_apps_only():
    from core.product_tools import areas, registry
    from core.product_tools.caller import Caller
    from core.product_tools.roles import Role

    areas.ensure_loaded()
    mcp = Caller(role=Role.BUILDER, via="mcp")
    for name in ("approve_recording_edit", "discard_recording_edit", "rename_recording"):
        tool = registry.get(name)
        assert tool.app_only and tool.min_role == Role.BUILDER and not tool.offered_to(mcp)


def test_the_census_row_names_them():
    from core.feature_census import CENSUS

    row = next(s for s in CENSUS if s.id == "browser")
    assert {"approve_recording_edit", "discard_recording_edit", "rename_recording", "delete_recording"} <= set(
        row.tools)
