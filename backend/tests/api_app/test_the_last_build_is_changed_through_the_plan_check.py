"""F1292 through the plan check itself, as the planner meets it (79): `examine` sends cap-04 A1's second workflow
back, and a second of the same name is never sent back at all, F1229's words or F1292's: it is the person's question,
which `check_build` asks (F1395, tests/unit/test_a_name_clash_is_asked.py). The rule's own edges are in
tests/unit/test_the_last_build_is_changed_not_made_again.py.
"""
from __future__ import annotations

from core import builder_access, builder_tools  # noqa: F401 — the builder's modules, loaded as at boot
from core.native_builder import build_plan
from core.native_builder.target import LAST_BUILT_KEY


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


AGENTS = {"agent_csv": {"id": "agent_csv", "name": "CSV Analyzer", "tools": []},
          "agent_chart": {"id": "agent_chart", "name": "Chart Generator", "tools": []},
          "agent_board": {"id": "agent_board", "name": "Board Manager", "tools": []}}
LAST = {"id": "orch_weekly", "name": "Sales by Region Weekly Report", "steps": [
    {"id": "parse_csv", "type": "agent", "agent_id": "agent_csv"},
    {"id": "analyze_sales", "type": "agent", "agent_id": "agent_chart"},
    {"id": "post_to_board", "type": "agent", "agent_id": "agent_board"},
]}
MADE_AGAIN = "made again with its agents"
SAME_NAME = "Never save a second of the same name"


def _draft(title: str) -> dict:
    steps = [{"id": f"s{i}", "type": "agent", "name": f"Step {i}", "does": "Does its part", "agent": a,
              "prompt": "Do your part with {{ state.input }}.", "next": "end"} for i, a in enumerate(AGENTS)]
    return {"kind": "create", "title": title, "summary": "Totals last week's sales by region.", "steps": steps,
            "reuse_agents": list(AGENTS)}


async def _errors(title: str) -> list[str]:
    ws = build_plan.Workspace(agents=AGENTS, workflows={"orch_weekly": LAST})
    found = await build_plan.examine(_draft(title), _Server(), ws=ws,
                                     state={"user_message": "Here is the CSV.", LAST_BUILT_KEY: "orch_weekly"})
    return found.errors


async def test_cap_04s_second_workflow_is_sent_back_by_the_plan_check(client):
    errors = await _errors("Process Uploaded Sales CSV")
    assert any(MADE_AGAIN in e and "Sales by Region Weekly Report" in e for e in errors), errors


async def test_a_second_of_the_same_name_is_the_persons_question_never_sent_back(client):
    errors = await _errors("Sales by Region Weekly Report")
    assert not any(SAME_NAME in e for e in errors), errors
    assert not any(MADE_AGAIN in e for e in errors), "a name the person is asked about is never also sent back"
    ws = build_plan.Workspace(agents=AGENTS, workflows={"orch_weekly": LAST})
    clash = build_plan.name_clashes(_draft("Sales by Region Weekly Report"), ws)
    assert [(c["kind"], c["id"]) for c in clash] == [("workflow", "orch_weekly")], clash
