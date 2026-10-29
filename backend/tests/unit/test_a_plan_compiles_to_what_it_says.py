"""A Plan compiles to the workflow, agents and inner workflows it describes, and a saved workflow reads back as one
(SAI2-P1, F775). The renderer writes the plan card's text from the same object.

What each guards:
* **the shipped templates** round-trip: workflow → Plan → workflow is the workflow (the mapping is one table, read
  both ways);
* **P0's 130 model plans** each compile to documents the engine loads, and every issue left is one only the planner
  can answer (a condition in words, a transform with no code …), never the compiler's own wiring;
* **SW1's eight programs**, as Plans of their right translation, compile clean (the programs are never committed);
* **F149/F150**: the approval a person asked for is a Human step before the send, and the schedule is made paused;
* **F429**: a nested build's inner workflow is compiled first and run by a Workflow step (E3);
* **F489**: a tool nothing provides is never a step's tool; **F767**: a loop's body prompt leads with its item;
* **BWA1's decision 4**: what acts outside asks first unless the Plan's "acts without asking" lets it act;
* **F784's class**: a Plan cannot set a rubric, the system marker or whether a workflow is active.
"""
import glob
import json
import types
from pathlib import Path

import pytest

from core.models_orchestration import Orchestration
from core.native_builder import objects as O
from core.native_builder import prompt_lint
from core.native_builder.compile import apply_edits, compile_plan, plan_from_workflow, validate
from core.native_builder.objects import Edit, Step
from core.native_builder.render import render

BACKEND = Path(__file__).resolve().parents[2]
FIXTURES = BACKEND / "tests" / "fixtures" / "sai2_plans"
_INV = json.loads((FIXTURES / "inventory.json").read_text())
INVENTORY = O.Inventory(agents=_INV["agents"], workflows=_INV["workflows"], tools=set(_INV["tools"]))
HARBOR_AGENTS = {i: {"id": i, "name": n, "type": "conversational", "tools": ["all"]} for i, n in _INV["agents"].items()}
SW1 = {k: v for k, v in json.loads((FIXTURES / "sw1_plans.json").read_text()).items() if not k.startswith("_")}


def _counter():
    n = {"i": 0}

    def mint(prefix, length=7):
        n["i"] += 1
        return f"{prefix}_{n['i']:03d}"

    return mint


def _normal(orch: dict) -> dict:
    d = Orchestration.model_validate(orch).model_dump(mode="json")
    keep = {k: d[k] for k in ("name", "description", "entry_step_id", "state_schema", "result_key",
                              "max_total_turns", "timeout_minutes", "strict_state_types")}
    keep["steps"] = sorted(({k: v for k, v in s.items() if k not in ("position_x", "position_y")}
                            for s in d["steps"]), key=lambda s: s["id"])
    return keep


def _templates():
    out = []
    for path in sorted(glob.glob(str(BACKEND / "examples" / "*.bundle.json"))):
        bundle = json.loads(Path(path).read_text())
        agents = {a["id"]: a for a in bundle.get("agents") or []}
        out += [(Path(path).name, o, agents) for o in bundle.get("orchestrations") or []]
    return out


TEMPLATES = _templates()


class TestTheTemplatesRoundTrip:
    def test_there_are_the_shipped_workflows(self):
        assert len(TEMPLATES) == 7

    @pytest.mark.parametrize("pack,orch,agents", TEMPLATES, ids=[t[1]["id"] for t in TEMPLATES])
    def test_workflow_to_plan_to_workflow(self, pack, orch, agents):
        plan = plan_from_workflow(orch, agents=agents)
        compiled = compile_plan(plan, existing_agents=agents, end_steps=False, orch_id=orch["id"], mint=_counter())
        assert _normal(compiled.orchestration) == _normal(orch)


def _model_plans():
    rows = [json.loads(line) for line in (FIXTURES / "probe_plans.jsonl").read_text().splitlines() if line.strip()]
    out = []
    for row in rows:
        plan, _ = O.read(row["plan"])
        if not O.check_structure(plan, INVENTORY)[0]:
            out.append((f"{row['run']}-{row['model']}-{row['persona']}", plan))
    return out


MODEL_PLANS = _model_plans()
#: What only the planner can answer: the compiler reports these and never invents them.
PLANNERS = ("Expression problem", "has no transform_code", "has no switch_expression", "routing is ignored inside a "
            "loop body", "which is neither in the loop body nor the loop's done path", "is not set up")


class TestWhatModelsPlanned:
    def test_the_probes_plans_that_check_clean(self):
        assert len(MODEL_PLANS) == 129

    @pytest.mark.parametrize("name,plan", MODEL_PLANS, ids=[p[0] for p in MODEL_PLANS])
    def test_each_compiles_to_what_the_engine_loads_and_only_the_planner_owes_anything(self, name, plan):
        compiled = compile_plan(plan, existing_agents=HARBOR_AGENTS, mint=_counter())
        for doc in [compiled.orchestration, *(i.orchestration for i in compiled.inner)]:
            Orchestration.model_validate(doc)
            assert prompt_lint.loops_not_item_first(doc) == []
        for issue in compiled.all_issues():
            assert any(p in issue for p in PLANNERS), issue

    def test_most_compile_clean(self):
        clean = sum(not compile_plan(p, existing_agents=HARBOR_AGENTS, mint=_counter()).all_issues()
                    for _, p in MODEL_PLANS)
        assert clean >= 84

    @pytest.mark.parametrize("name,plan", MODEL_PLANS[:40], ids=[p[0] for p in MODEL_PLANS[:40]])
    def test_compiling_what_it_reads_back_changes_nothing(self, name, plan):
        once = compile_plan(plan, existing_agents=HARBOR_AGENTS, mint=_counter())
        again = compile_plan(plan_from_workflow(once.orchestration), existing_agents=HARBOR_AGENTS | {
            a["id"]: a for a in once.agents}, mint=_counter(), orch_id=once.orchestration["id"])
        assert _normal(again.orchestration) == _normal(once.orchestration)


class TestSW1sPrograms:
    @pytest.mark.parametrize("name", sorted(SW1))
    def test_each_compiles_clean(self, name):
        plan, errors = O.read(SW1[name])
        assert errors == []
        assert O.check_structure(plan, O.Inventory(tools={"search_the_web", "get_weather"}))[0] == []
        compiled = compile_plan(plan, mint=_counter())
        assert compiled.all_issues() == [], compiled.all_issues()

    def test_e2s_loop_reads_its_item_first(self):
        compiled = compile_plan(O.read(SW1["E2"])[0], mint=_counter())
        research = next(s for s in compiled.orchestration["steps"] if s["id"] == "research")
        assert research["prompt_template"].startswith("This item: {state.keyword}")

    def test_s1s_team_holds_its_members_by_their_new_ids(self):
        compiled = compile_plan(O.read(SW1["S1"])[0], mint=_counter())
        by_name = {a["name"]: a for a in compiled.agents}
        team = by_name["Triage"]
        assert team["type"] == "delegate" and team["team"]["mode"] == "team"
        assert team["delegate_agent_ids"] == [by_name[n]["id"] for n in ("French agent", "Spanish agent",
                                                                         "English agent")]

    def test_s4s_approval_comes_before_the_call(self):
        compiled = compile_plan(O.read(SW1["S4"])[0], mint=_counter())
        steps = {s["id"]: s for s in compiled.orchestration["steps"]}
        assert steps["approve"]["type"] == "human" and steps["approve"]["next_step_id"] == "weather"

    def test_e1s_guardrail_blocks_with_a_sentence(self):
        compiled = compile_plan(O.read(SW1["E1"])[0], mint=_counter())
        steps = {s["id"]: s for s in compiled.orchestration["steps"]}
        assert steps["guardrails_check"]["route_map"] == {"pass": "intent_classifier", "blocked": "blocked_reply"}
        assert steps["blocked_reply"]["print_content"].startswith("Your query contains harmful content")


def _report_plan(**over):
    raw = {"kind": "create", "title": "Weekly report", "summary": "Every Monday the numbers go to ops.",
           "steps": [{"id": "draft", "type": "agent", "name": "Draft", "does": "Writes the report", "agent": "Writer",
                      "prompt": "Write this week's report."},
                     {"id": "approve", "type": "human", "name": "Approve", "does": "A person checks it",
                      "prompt": "Send this report?"},
                     {"id": "send", "type": "tool", "name": "Send", "does": "Sends it", "tool": "send_mail"}],
           "agents": [{"name": "Writer", "purpose": "Writes reports", "tools": ["read_sheet"]}],
           "schedule": {"cron": "0 9 * * 1", "says": "every Monday at 9:00", "input": "This week's sales report"}}
    raw.update(over)
    return O.Plan.from_draft(raw)


class TestWhatWasAskedForIsBuilt:
    """F149/F150 (B4, D122), once prompt checks, now the compiler's: the approval and the schedule are kept."""

    def test_the_approval_is_a_human_step_before_the_send(self):
        steps = {s["id"]: s for s in compile_plan(_report_plan(), mint=_counter()).orchestration["steps"]}
        assert steps["draft"]["next_step_id"] == "approve"
        assert steps["approve"]["type"] == "human" and steps["approve"]["next_step_id"] == "send"
        assert steps["send"]["next_step_id"] == "end" and steps["end"]["type"] == "end"

    def test_the_schedule_is_made_paused_and_starts_with_its_task(self):
        compiled = compile_plan(_report_plan(), mint=_counter())
        assert compiled.schedule["enabled"] is False
        assert compiled.schedule["cron_expression"] == "0 9 * * 1"
        assert compiled.schedule["target_id"] == compiled.orchestration["id"]
        assert compiled.schedule["prompt"] == "This week's sales report"
        assert compiled.issues == []

    def test_a_schedule_that_cannot_run_is_an_issue(self):
        plan = _report_plan(schedule={"cron": "61 9 * * 1", "says": "sometime"})
        assert any("schedule" in i for i in compile_plan(plan, mint=_counter()).issues)

    def test_each_step_reads_what_came_before(self):
        steps = {s["id"]: s for s in compile_plan(_report_plan(), mint=_counter()).orchestration["steps"]}
        assert steps["draft"]["output_key"] == "draft"
        assert steps["send"]["input_keys"] == ["user_input", "approve"]

    def test_caps_are_the_workflows(self):
        orch = compile_plan(_report_plan(caps={"credits": 40, "dollars": 2.5}), mint=_counter()).orchestration
        assert (orch["max_run_credits"], orch["max_run_usd"]) == (40, 2.5)


class TestANestedBuild:
    """F429: the inner workflow is compiled first and run by a Workflow step, validated as a called workflow."""

    def test_inner_first_and_run_by_its_step(self):
        plan = _report_plan(
            steps=[{"id": "feedback", "type": "workflow", "name": "Collect feedback", "does": "Runs the survey",
                    "workflow": "Survey", "prompt": "Survey this week's patients"},
                   {"id": "summary", "type": "llm", "name": "Summary", "does": "Sums it up", "prompt": "Summarise."}],
            agents=[], schedule=None,
            inner_workflows=[{"title": "Survey", "does": "Asks patients", "steps": [
                {"id": "ask", "type": "human", "name": "Ask", "does": "Asks", "prompt": "How was your visit?"}]}])
        compiled = compile_plan(plan, mint=_counter())
        assert compiled.ok, compiled.all_issues()
        inner = compiled.inner[0].orchestration
        step = next(s for s in compiled.orchestration["steps"] if s["id"] == "feedback")
        assert step["type"] == "workflow" and step["workflow_id"] == inner["id"]
        assert inner["name"] == "Survey"


class TestWhatAPlanCannotDo:
    def test_a_tool_nothing_provides_is_never_saved_f489(self):
        plan = _report_plan()
        plan.missing = ["send_mail"]
        assert any("send_mail" in i and "nothing here provides" in i for i in compile_plan(plan, mint=_counter()).issues)

    @pytest.mark.parametrize("key", ["rubric_id", "system", "is_active", "offer_to_mcp"])
    def test_a_setting_outside_the_list_is_refused_by_name(self, key):
        compiled = compile_plan(_report_plan(settings={key: "x"}), mint=_counter())
        assert any(f"cannot set '{key}'" in i for i in compiled.issues)
        assert key not in compiled.orchestration

    def test_an_allowed_setting_is_kept(self):
        compiled = compile_plan(_report_plan(settings={"result_key": "draft"}), mint=_counter())
        assert compiled.orchestration["result_key"] == "draft" and compiled.issues == []

    def test_a_step_setting_the_engine_has_not_is_an_issue(self):
        plan = _report_plan()
        plan.steps[0].config = {"temperature": 2}
        assert any("'temperature' is not a step setting" in i for i in compile_plan(plan, mint=_counter()).issues)


class TestApprovals:
    """BWA1's decision 4: what acts outside asks first, unless the approved Plan lets it act without asking."""

    @pytest.fixture
    def workspace(self, monkeypatch):
        import core.routes.tools as routes_tools
        import core.tools as tools_mod
        from mcp.types import ToolAnnotations

        from core.tools import VirtualTool

        declared = {"read_sheet": VirtualTool("read_sheet", "reads", {}, ToolAnnotations(readOnlyHint=True)),
                    "send_mail": VirtualTool("send_mail", "sends", {}, ToolAnnotations(readOnlyHint=False))}

        async def one(server_module, name, agent, custom_tools):
            return declared.get(name)

        async def no_custom():
            return []

        monkeypatch.setattr(tools_mod, "resolve_one_tool", one)
        monkeypatch.setattr(routes_tools, "load_custom_tools", no_custom)
        return types.SimpleNamespace(agent_sessions={}, tool_router={})

    async def test_a_new_agent_holding_a_tool_that_asks_first_is_set_to_confirm(self, workspace):
        plan = await O.fill(_report_plan(agents=[{"name": "Writer", "purpose": "x",
                                                  "tools": ["read_sheet", "send_mail"]}]), workspace)
        compiled = compile_plan(plan, mint=_counter())
        assert compiled.agents[0]["tool_policy"] == {"tool:send_mail": "confirm"}
        assert await validate(compiled, plan, workspace) == []

    async def test_a_send_with_nobody_approving_is_an_issue(self, workspace):
        steps = [s for s in _report_plan().model_dump()["steps"] if s["id"] != "approve"]
        steps[0]["next"] = "send"
        plan = await O.fill(_report_plan(steps=steps), workspace)
        compiled = compile_plan(plan, mint=_counter())
        found = await validate(compiled, plan, workspace)
        assert any("send_mail" in i and "nobody approving" in i for i in found)

    async def test_unless_the_person_said_it_acts_without_asking(self, workspace):
        steps = [s for s in _report_plan().model_dump()["steps"] if s["id"] != "approve"]
        steps[0]["next"] = "send"
        plan = await O.fill(_report_plan(steps=steps), workspace)
        plan.approvals = O.Approvals(acts_without_asking=["send_mail"])
        assert await validate(compile_plan(plan, mint=_counter()), plan, workspace) == []


class TestAnUpdatesEdits:
    def _base(self):
        return plan_from_workflow(compile_plan(_report_plan(schedule=None), mint=_counter()).orchestration)

    def test_add_a_step_after_another(self):
        plan, issues = apply_edits(self._base(), [Edit(op="add_step", after="draft", step=Step(
            id="check", type="llm", name="Check", does="Checks the numbers", prompt="Check the numbers."))])
        assert issues == []
        by_id = {s.id: s for s in plan.steps}
        assert by_id["draft"].next == "check" and by_id["check"].next == "approve"

    def test_remove_a_step_and_what_led_to_it_leads_on(self):
        plan, issues = apply_edits(self._base(), [Edit(op="remove_step", step_id="approve")])
        assert issues == [] and {s.id: s for s in plan.steps}["draft"].next == "send"

    def test_update_and_set_meta(self):
        plan, issues = apply_edits(self._base(), [
            Edit(op="update_step", step_id="draft", fields={"prompt": "Write the monthly report."}),
            Edit(op="set_meta", fields={"title": "Monthly report", "result_key": "draft"})])
        assert issues == []
        assert plan.title == "Monthly report" and plan.settings["result_key"] == "draft"
        assert {s.id: s for s in plan.steps}["draft"].prompt == "Write the monthly report."

    def test_what_cannot_be_done_is_said(self):
        _, issues = apply_edits(self._base(), [Edit(op="remove_step", step_id="nope"),
                                               Edit(op="update_step", step_id="draft", fields={"id": "x"}),
                                               Edit(op="set_meta", fields={"rubric_id": "r"})])
        assert len(issues) == 3 and "rubric_id" in issues[2]


class TestTheCardsText:
    def test_every_section_it_has_and_none_it_has_not(self):
        plan = _report_plan()
        # An older card's open question, as it is stored: a plan carries none since F1253, and a card still shows one.
        plan.questions = ["Which sheet holds the numbers?"]
        plan.approvals = O.Approvals(asks_first=["send_mail"])
        plan.missing = ["read_sheet"]
        plan.connections_needed = ["Google Sheets"]
        text = render(plan)
        for heading in ("## You asked for", "## Steps", "## Agents", "### New agents to create", "## Schedule",
                        "## Asks you first", "## Missing here", "## Open questions"):
            assert heading in text
        assert "## Acts without asking" not in text and "## Not carried over" not in text
        assert "1. **Draft** (agent): Writes the report — agent: Writer" in text
        assert "**When:** every Monday at 9:00 (`0 9 * * 1`)" in text
        assert "- `send_mail`" in text and "Google Sheets: connect it first" in text

    def test_an_empty_plan_says_nothing_empty(self):
        plan = O.Plan.from_draft({"kind": "create", "title": "T", "summary": "", "steps": [
            {"id": "a", "type": "print", "name": "Say hi", "does": "Say hi", "prompt": "hi"}],
            "schedule": {"cron": "", "says": ""}})
        text = render(plan)
        assert "cron expression" not in text and "None" not in text and "()" not in text
        assert "## Schedule" not in text and "## You asked for" not in text and "## Overview" not in text

    def test_a_nested_plans_inner_workflow_is_its_own_section(self):
        text = render(O.Plan.from_draft(SW1["E1"]))
        assert "## Not carried over" in text and "hosted MCP servers" in text


@pytest.mark.parametrize("over, reads", [("fetch", "state.fetch"), ("fetch.output", "state.fetch"),
                                         ("fetch.items", "state.fetch.items"), ("state.fetch", "state.fetch"),
                                         ("emails", "emails")])
def test_a_loop_over_a_step_goes_through_what_that_step_wrote(over, reads):
    plan, _ = O.read({"kind": "create", "title": "t", "summary": "s", "steps": [
        {"id": "fetch", "type": "tool", "name": "Fetch", "does": "x", "tool": "get_weather"},
        {"id": "each", "type": "loop", "name": "Each", "does": "x", "loop_over": over, "body": ["say"]},
        {"id": "say", "type": "llm", "name": "Say", "does": "x", "prompt": "Say it."}]})
    loop = next(s for s in compile_plan(plan, mint=_counter()).orchestration["steps"] if s["id"] == "each")
    assert loop["loop_over"] == reads



def test_the_card_shows_the_caps_what_a_scheduled_run_starts_with_and_the_code_it_runs():
    """SAI2's review, 19: what the approval covers is on the card (CAP1, F155, a transform's code)."""
    plan = O.Plan.from_draft({"kind": "create", "title": "t", "summary": "s",
                              "steps": [{"id": "shape", "type": "transform", "name": "Shape", "does": "x",
                                         "config": {"transform_code": "result = len(state.x)"}}],
                              "schedule": {"cron": "0 9 * * 1", "says": "Mondays at 9", "input": "Last week's notes"},
                              "caps": {"credits": 5, "dollars": 0.5}})
    text = render(plan)
    assert "**Each run starts with:** Last week's notes" in text
    assert "## Limits\nEach run is capped at 5 credits and $0.5 of models." in text
    assert "result = len(state.x)" in text
