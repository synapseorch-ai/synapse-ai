"""
No agent where none is needed (F967, SAI2-P3, D300).

A step that only calls a tool, with inputs that are known (fixed, or what earlier steps wrote), is a `tool` step with
its inputs in `config.tool_arguments`: the engine calls its tool with no model (E1, `step_models.calls_a_model`). The
planner was told a tool step "calls one tool with no model", which is true only when its inputs are set: left out, a
model reads the run so far and fills them in. Now:
- the steps passage teaches `config.tool_arguments` with an example, and the planner's rule says a step that only calls
  a tool needs no agent;
- a plan whose steps call no model says so on the card, from code: "No agents: it runs on tools only, with no model
  called"; a tool step whose inputs a model fills says that, by its line;
- CE1 reads no model for such a run.
A step that runs a workflow from elsewhere, or searches or fills a knowledge base, is never read as calling no model.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access, builder_tools
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan, objects as O
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks
from core.product_tools.roles import Role

NO_MODEL = "No agents: it runs on tools only, with no model called"
FILLS = "a model fills in its inputs"

WEATHER = {"name": "rotterdam_weather", "description": "Today's weather in Rotterdam.", "tool_type": "http",
           "method": "GET", "url": "https://api.weather.example/v1/rotterdam", "headers": {},
           "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}}
NOTE = {"name": "post_note", "description": "Posts a note to the team's board.", "tool_type": "http",
        "method": "POST", "url": "https://api.notes.example/v1/notes", "headers": {},
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def workspace():
    from core.store.resources import save_tool

    confirm.set_claim_store(None)
    builder_access.forget_runs()

    async def owner(user_id):
        return Role.OWNER

    hooks.set_role_source(owner)
    await save_tool(dict(WEATHER))
    await save_tool(dict(NOTE))
    yield
    hooks.set_role_source(None)
    builder_access.forget_runs()
    builder_tools.set_room_check(None)


WEATHER_STEP = {"id": "weather", "type": "tool", "name": "Read the weather", "does": "Reads today's weather",
                "tool": "rotterdam_weather", "config": {"tool_arguments": {}}}
POST_SET = {"id": "post", "type": "tool", "name": "Post the note", "does": "Posts the weather to the board",
            "tool": "post_note", "config": {"tool_arguments": {"text": "Today: {{state.weather}}"}}}
POST_FILLED = {"id": "post", "type": "tool", "name": "Post the note", "does": "Posts the weather to the board",
               "tool": "post_note"}


def _draft(*steps):
    return {"finish": "submit_plan", "kind": "create", "title": "Weather note", "summary": "Posts the weather.",
            "steps": list(steps)}


async def _check(draft):
    run = OrchestrationRun(run_id="builder_no_model", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create"})
    await SharedState(run).checkpoint()
    run.shared_state[build_plan.DRAFT_KEY] = draft
    with live(run):
        raw, _ = await execute_builder_tool_with_events("check_build", {"draft": draft}, _Server(), run_id=run.run_id)
    return json.loads(raw) if isinstance(raw, str) else raw


def _render(checked):
    from core.native_builder.render import render

    return render(O.Plan.model_validate(checked["plan"]))


# ── the planner is taught ─────────────────────────────────────────────────────────────────────────────────────────

def test_the_steps_passage_teaches_tool_arguments_with_an_example():
    from core.native_builder import seed

    steps = seed.prompt_fragment("plan_steps")
    tool_line = next(line for line in steps.splitlines() if line.startswith("- `tool`"))
    assert "config.tool_arguments" in tool_line and "{{state." in tool_line
    # Said as the engine runs it: with no inputs set, a model fills them in.
    assert "a model" in tool_line and "calls one tool with no model;" not in tool_line


def test_the_planners_rule_says_a_step_that_only_calls_a_tool_needs_no_agent():
    from core.native_builder import seed

    planner = next(d for d in seed.bundled_definitions()["agents"] if d["id"] == "agent_native_builder_planner")
    text = json.dumps(planner, ensure_ascii=False)
    assert "only calls a tool needs no agent" in text and "config.tool_arguments" in text


# ── a run that calls no model ─────────────────────────────────────────────────────────────────────────────────────

async def test_a_tool_chain_with_set_inputs_needs_no_agent_and_the_card_says_no_model_is_called():
    from core.native_builder.compile import compile_plan

    checked = await _check(_draft(WEATHER_STEP, POST_SET))
    assert checked["outcome"] == "review", checked
    assert checked["plan"]["calls_no_model"] is True and checked["plan"]["model_fills"] == []
    compiled = compile_plan(O.Plan.model_validate(checked["plan"]))
    assert compiled.agents == []
    steps = {s["id"]: s for s in compiled.orchestration["steps"]}
    assert steps["post"]["tool_arguments"] == {"text": "Today: {{state.weather}}"}
    text = _render(checked)
    assert NO_MODEL in text and FILLS not in text


async def test_ce1_reads_no_model_for_an_agentless_run():
    from core.native_builder.compile import compile_plan
    from core.orchestration.estimate import step_model

    checked = await _check(_draft(WEATHER_STEP, POST_SET))
    compiled = compile_plan(O.Plan.model_validate(checked["plan"]))
    assert [step_model(s, {}, "workspace-default") for s in compiled.orchestration["steps"]
            if s["type"] != "end"] == [None, None]


async def test_a_tool_step_left_to_a_model_says_a_model_fills_in_its_inputs():
    from core.native_builder.compile import compile_plan
    from core.orchestration.estimate import step_model

    checked = await _check(_draft(WEATHER_STEP, POST_FILLED))
    assert checked["outcome"] == "review", checked
    assert checked["plan"]["calls_no_model"] is False and checked["plan"]["model_fills"] == ["post"]
    text = _render(checked)
    assert NO_MODEL not in text
    post_line = next(line for line in text.splitlines() if "**Post the note**" in line)
    assert FILLS in post_line
    assert FILLS not in next(line for line in text.splitlines() if "**Read the weather**" in line)
    # CE1 prices that step: a model is called.
    compiled = compile_plan(O.Plan.model_validate(checked["plan"]))
    post = next(s for s in compiled.orchestration["steps"] if s["id"] == "post")
    assert step_model(post, {}, "workspace-default") == "workspace-default"


async def test_a_step_that_calls_a_model_never_reads_no_agents():
    draft = _draft(WEATHER_STEP, {"id": "say", "type": "llm", "name": "Say it", "does": "Writes a line",
                                  "prompt": "Write one line about {{state.weather}}."})
    checked = await _check(draft)
    assert checked["outcome"] == "review", checked
    assert checked["plan"]["calls_no_model"] is False
    assert NO_MODEL not in _render(checked)


# ── never claimed where a model may be called ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("step", [
    {"id": "other", "type": "workflow", "name": "Run the digest", "does": "Runs it", "workflow": "orch_digest"},
    {"id": "find", "type": "retrieve", "name": "Search the notes", "does": "Searches",
     "config": {"knowledge_base_ids": ["kb_1"]}},
    {"id": "keep", "type": "ingest", "name": "Keep the note", "does": "Files it",
     "config": {"knowledge_base_ids": ["kb_1"]}},
])
def test_a_workflow_from_elsewhere_or_a_knowledge_step_is_never_read_as_calling_no_model(step):
    plan = O.PlanDraft.model_validate({"kind": "create", "title": "t", "summary": "s",
                                       "steps": [WEATHER_STEP, step]})
    assert O.calls_no_model(plan) is False


def test_an_inner_workflow_that_calls_a_model_is_counted():
    plan = O.PlanDraft.model_validate({
        "kind": "create", "title": "t", "summary": "s",
        "steps": [WEATHER_STEP, {"id": "inner", "type": "workflow", "name": "Write it", "does": "Writes",
                                 "workflow": "Writer"}],
        "inner_workflows": [{"title": "Writer", "does": "Writes the line", "steps": [
            {"id": "w", "type": "llm", "name": "Write", "does": "Writes", "prompt": "Write a line."}]}]})
    assert O.calls_no_model(plan) is False
    without_model = plan.model_copy(deep=True)
    without_model.inner_workflows[0].steps = [O.Step.model_validate(POST_SET)]
    assert O.calls_no_model(without_model) is True


def test_an_empty_plan_is_never_said_to_call_no_model():
    assert O.calls_no_model(O.PlanDraft.model_validate({"kind": "create", "title": "t", "summary": "s",
                                                         "steps": []})) is False


# ── the card ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_card_says_no_model_and_which_step_a_model_fills_from_the_plans_object():
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "frontend" / "src" / "components" / "assistant"
    plan_ts, cards = (root / "plan.ts").read_text(), (root / "cards.tsx").read_text()
    assert f"'{FILLS}'" in plan_ts and "view.plan.model_fills" in plan_ts
    assert "noModel: view.plan.calls_no_model === true," in plan_ts
    assert f"'{NO_MODEL[0].lower()}{NO_MODEL[1:]}'" in cards
