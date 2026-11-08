"""Asked to run a tool that is here once, Synapse AI points to its Test, every time, and builds nothing (F1383, TRI1;
CAP3's final gate ff19-f1, fix-02; eb; Naveen 11-05: "Prepare and hold").

F1368 put it in the planner's # Choose: running or trying one tool once is its Test, never a workflow unless they ask
for one. The first "Can you run region_totals for …?" held; the repeat ("Please run region_totals with start … end
…") built a new workflow, a Human approval step then the tool step. A rule a model weighs each turn can tip on a
repeat, so code reads it before any model (`read_turn.decide`):

- a message that asks to run, try or test a tool here by name, once, is answered by code alone (`tool_test`): open
  Tools, pick it, press Test; or, when a fix to that tool waits on its "Test this fix" card above, press that;
- the same words on every repeat, with no model called and no workflow;
- never when it asks for it again and again (every, each, daily, a schedule), to be automated, or for a workflow or a
  build; never for a workflow's name, which runs it as before ("run the Weekly Report"), nor a tool's name that is a
  workflow's too.
"""
from __future__ import annotations

import pytest

from core.native_builder.read_turn import decide

TOOL = "region_totals"
INVENTORY = {"tool": [TOOL, "price_feed"], "workflow": ["Weekly Report"]}
FIX_CARD = {"id": "c1", "title": f'Test this fix to "{TOOL}"',
            "buttons": [{"label": "Test this fix", "tool": "test_code_change", "arguments": {}}]}


def _decide(message, *, inventory=None, facts=None, previous=None):
    return decide(message, previous=previous or {}, facts=facts or {},
                  inventory=INVENTORY if inventory is None else inventory)


def _tools_link() -> str:
    from core import feature_census

    [row] = [s for s in feature_census.current() if s.id == "custom_tools"]
    return feature_census.link(row)


class TestItsTest:
    @pytest.mark.parametrize("message", [
        "Can you run region_totals for 2026-10-01 to 2026-10-31?",
        "Please run region_totals with start 2026-10-01 end 2026-10-31",
        "try region_totals",
        "test region_totals with region north",
        "could you call region_totals for October",
    ])
    def test_a_tool_here_run_once_is_pointed_to_its_test(self, message):
        turn = _decide(message)
        assert (turn.route, turn.then) == ("tool_test", "said"), turn
        assert _tools_link() in turn.say and f'"{TOOL}"' in turn.say and "Test" in turn.say, turn.say
        assert "workflow" in turn.say, "it says how to have it run on its own: ask for a workflow"

    def test_the_same_on_a_repeat(self):
        first = _decide("Can you run region_totals for 2026-10-01 to 2026-10-31?")
        again = _decide("Please run region_totals with start 2026-10-01 end 2026-10-31",
                        previous={"summary": first.say, "turn": {"route": "tool_test"}})
        assert again.route == "tool_test" and again.say == first.say

    def test_a_fix_waiting_on_its_card_is_tested_there(self):
        turn = _decide("Please run region_totals with start 2026-10-01 end 2026-10-31",
                       facts={"cards": [FIX_CARD], "presses": []})
        assert turn.route == "tool_test" and "Test this fix" in turn.say and FIX_CARD["title"] in turn.say, turn.say
        assert _tools_link() not in turn.say

    def test_another_tools_fix_card_is_not_this_ones(self):
        other = {**FIX_CARD, "title": 'Test this fix to "price_feed"'}
        turn = _decide("run region_totals for October", facts={"cards": [other], "presses": []})
        assert turn.route == "tool_test" and _tools_link() in turn.say


class TestNeverOverABuild:
    @pytest.mark.parametrize("message", [
        "run region_totals every morning",
        "run region_totals each Monday and send me the result",
        "automate region_totals",
        "build a workflow that runs region_totals weekly",
        "schedule region_totals daily",
        "create an agent that can run region_totals",
    ])
    def test_again_and_again_automated_or_built(self, message):
        assert _decide(message).route != "tool_test"

    @pytest.mark.parametrize("message", [
        "run the Weekly Report",
        "run it every morning",
        "what does region_totals do?",
        "fix region_totals so it reads the start date",
        "run the price checker",
    ])
    def test_a_workflow_no_tool_named_or_no_run(self, message):
        assert _decide(message).route != "tool_test"

    def test_a_tools_name_that_is_a_workflows_too_is_as_before(self):
        inventory = {"tool": [TOOL], "workflow": ["region totals"]}
        assert _decide("run region_totals for October", inventory=inventory).route != "tool_test"

    def test_no_inventory_is_as_before(self):
        assert decide("run region_totals for October", previous={}, facts={}).route != "tool_test"
