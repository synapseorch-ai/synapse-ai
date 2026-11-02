"""The workflow this conversation just built is changed, not made again under a new name (F1292, CAP3's cap-04 A1;
79 11-02).

F1229 holds a second workflow of the same name. Cap-04's A1 built "Sales by Region Weekly Report" with three new
agents; the person's next turn ([uploaded the CSV]) built "Process Uploaded Sales CSV" from the same three agents, a
second of the same workflow under another name, which F1229 could not see. Now a new workflow whose steps use the
agents of the one this conversation built (`LAST_BUILT_KEY`): at least two of them, or all of them when it has fewer,
is that workflow again, and goes back to the planner: change it (an update of that one), or ask once whether the
person wants two. A new workflow that shares one agent with it is a workflow of its own.
"""
from __future__ import annotations

from core import builder_access, builder_tools  # noqa: F401 — the builder's modules, loaded as at boot
from core.native_builder import build_plan, objects as O
from core.native_builder.target import LAST_BUILT_KEY

LAST = {"id": "orch_weekly", "name": "Sales by Region Weekly Report", "steps": [
    {"id": "parse_csv", "type": "agent", "agent_id": "agent_csv"},
    {"id": "analyze_sales", "type": "agent", "agent_id": "agent_chart"},
    {"id": "post_to_board", "type": "agent", "agent_id": "agent_board"},
]}
AGENTS = {"agent_csv": {"name": "CSV Analyzer"}, "agent_chart": {"name": "Chart Generator"},
          "agent_board": {"name": "Board Manager"}}


def _ws() -> build_plan.Workspace:
    return build_plan.Workspace(agents=AGENTS, workflows={"orch_weekly": LAST})


def _plan(*agent_ids: str, title: str = "Process Uploaded Sales CSV") -> O.Plan:
    steps = [{"id": f"s{i}", "type": "agent", "name": f"Step {i}", "does": "Does it", "agent": a,
              "prompt": "Do it.", "next": "end"} for i, a in enumerate(agent_ids)]
    return O.Plan.from_draft({"kind": "create", "title": title, "summary": "S.", "steps": steps,
                              "reuse_agents": list(agent_ids)})


def _said(plan: O.Plan, state: dict) -> list[str]:
    return build_plan._the_last_build_again(plan, _ws(), state)


class TestMadeAgain:
    def test_cap_04s_second_workflow_goes_back_naming_the_first(self):
        said = _said(_plan("agent_csv", "agent_chart", "agent_board"), {LAST_BUILT_KEY: "orch_weekly"})
        assert said and '"Sales by Region Weekly Report"' in said[0] and "kind update" in said[0], said

    def test_two_of_its_agents_are_enough(self):
        assert _said(_plan("agent_csv", "agent_chart"), {LAST_BUILT_KEY: "orch_weekly"})


class TestAWorkflowOfItsOwn:
    def test_one_shared_agent_is_not_the_same_workflow(self):
        assert _said(_plan("agent_board"), {LAST_BUILT_KEY: "orch_weekly"}) == []

    def test_no_build_in_this_conversation_no_rule(self):
        assert _said(_plan("agent_csv", "agent_chart", "agent_board"), {}) == []

    def test_a_change_is_not_a_second(self):
        plan = _plan("agent_csv", "agent_chart")
        plan.kind = "update"
        assert _said(plan, {LAST_BUILT_KEY: "orch_weekly"}) == []

    def test_a_last_build_of_one_agent_needs_that_one(self):
        ws = build_plan.Workspace(agents=AGENTS, workflows={"orch_one": {
            "id": "orch_one", "name": "Board poster", "steps": [{"id": "p", "type": "agent", "agent_id": "agent_board"}]}})
        assert build_plan._the_last_build_again(_plan("agent_board"), ws, {LAST_BUILT_KEY: "orch_one"})
        assert build_plan._the_last_build_again(_plan("agent_csv"), ws, {LAST_BUILT_KEY: "orch_one"}) == []
