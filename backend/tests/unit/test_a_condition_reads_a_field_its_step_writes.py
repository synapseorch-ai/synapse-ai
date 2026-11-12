"""A step reads only a field the step it reads writes; a sign-off a later step branches on asks a yes or no — F1549
(BLD3; MIG1's bug A, 11-12).

MIG1 pasted OpenAI's Customer service template and a while-loop export into Build with AI: each user approval became
a Human step with no fields and an If/Else on `state.<its id>.approved`, and approving went down the "no" branch, 2 of
2 runs. A Human step with no fields keeps the person's words (`engine._as_answered`, F106; the docs say the same), so
the condition read a field of text, which `expressions` takes as a value the run has not written (F1433) and the If/Else
took its documented False. Nothing checked it: `validate_orchestration` checks keys, never a field of one. And the
planner was told the opposite of the engine: F1359's sentence says a Human step with no fields answers
`{approved, feedback}`, which nothing ever writes.

Now `_validate_orchestration` (the plan's compile, the builder's and MCP's `validate_orchestration`) refuses a read of
a field where every step that writes the key is one whose answer the step itself says, and none writes that field:
a Human step (its fields by name; none: the person's words), an Evaluator (its route's label), an If/Else (true or
false), a Retrieve or Ingest step (its contract's output). Read in a condition (If/Else, Switch, a Loop's while, break
and over), a template anywhere in a step, and a Transform's code. Repair first: give the Human step the field (a yes/no
one for a yes or no, which the run asks with Approve and Decline), or read one it writes. A Tool step's answer is
F1394's (the workspace's tools say it, said once); any other step's is not known and is left alone.

The planner, the paste guide, F1359's own message and the Transform panel's note say what a Human step answers: its
fields by name, and with none the person's words; a yes or no a later step branches on is a yes/no field `approved`.
The engine end to end: the repaired sign-off routes an approval yes and a decline no, as the run page and a channel's
reply answer it; the shape the validator refuses routes "no" on an approval, which is the bug.
"""
from __future__ import annotations

import asyncio
import json
import re
import types
from pathlib import Path

import pytest
from _fakes import seed as S

from core.builder_tools import _validate_orchestration

ROOT = Path(__file__).resolve().parents[3]
YES_NO = {"name": "approved", "type": "boolean", "label": "Approve these keywords?"}


def _orch(*steps: dict, schema: dict | None = None) -> dict:
    """`steps` in a line, each to the next, the last to an End."""
    steps = [dict(s) for s in steps]
    for here, after in zip(steps, steps[1:]):
        if here.get("type") not in ("if_else", "switch", "end"):
            here.setdefault("next_step_id", after["id"])
    if steps[-1].get("type") != "end":
        steps[-1].setdefault("next_step_id", "end")
        steps.append({"id": "end", "name": "End", "type": "end"})
    return {"id": "orch_f1549", "name": "F1549", "entry_step_id": steps[0]["id"], "steps": steps,
            **({"state_schema": schema} if schema else {})}


def _ask(fields=None, **over) -> dict:
    """MIG1's sign-off: the user approval of a pasted export, as the builder made it."""
    return {"id": "approve_keywords", "name": "Approve keywords", "type": "human",
            "human_prompt": "Do these keywords look right?", "human_fields": list(fields or []),
            "output_key": "approve_keywords", **over}


def _gate(condition: str) -> dict:
    return {"id": "check_approval", "name": "Approved?", "type": "if_else", "if_condition": condition,
            "if_true_step_id": "research", "if_false_step_id": "end"}


RESEARCH = {"id": "research", "name": "Research", "type": "llm", "prompt_template": "Research the keywords.",
            "output_key": "research"}


def _never(orch: dict) -> list[str]:
    return [i for i in _validate_orchestration(orch)["issues"] if "never write" in i]


class TestASignOffsConditionIsChecked:
    def test_migs_shape_is_refused_with_the_repair_first(self):
        """T2 and T4: a Human step with no fields, then an If/Else on its `.approved`."""
        [said] = _never(_orch(_ask(), _gate("state.approve_keywords.approved == True"), RESEARCH))
        assert said.startswith("Step 'check_approval' reads `approved` from state.approve_keywords, which human step "
                               "'approve_keywords' never writes: give 'approve_keywords' a field named 'approved' in "
                               "human_fields"), said
        assert '"type": "boolean"' in said and "Approve and Decline" in said, said
        assert "the person's words" in said, said
        # The F1359 check's own filter reads "as text" (test_a_plans_code_never_reads_an_object_as_text): not this.
        assert "as text" not in said

    def test_the_repaired_sign_off_passes(self):
        assert _never(_orch(_ask([YES_NO]), _gate("state.approve_keywords.approved == True"), RESEARCH)) == []

    def test_the_planners_yes_no_spelling_and_fields_given_as_json_pass(self):
        """F1434's `yes_no`, and `human_fields_json` as the builder's tools send a step."""
        planner = _ask([{**YES_NO, "type": "yes_no"}])
        assert _never(_orch(planner, _gate("state.approve_keywords.approved"), RESEARCH)) == []
        as_json = {k: v for k, v in _ask().items() if k != "human_fields"}
        as_json["human_fields_json"] = json.dumps([YES_NO])
        assert _never(_orch(as_json, _gate("state.approve_keywords.approved"), RESEARCH)) == []
        as_json["human_fields_json"] = "[]"
        assert len(_never(_orch(as_json, _gate("state.approve_keywords.approved"), RESEARCH))) == 1

    def test_a_field_the_step_does_not_have_names_the_ones_it_has(self):
        fields = [{"name": "decision", "type": "choice", "options": ["go", "stop"], "label": "Decision"},
                  {"name": "note", "type": "textarea", "label": "Note"}]
        [said] = _never(_orch(_ask(fields), _gate("state.approve_keywords.approved"), RESEARCH))
        assert "read one of its fields (`decision`, `note`)" in said and "'approved'" in said, said

    @pytest.mark.parametrize("condition", [
        "state.approve_keywords.approved == True",
        "state.approve_keywords.approved",
        "not state.approve_keywords.approved",
        "state['approve_keywords']['approved'] is True",
        "state.approve_keywords.get('approved')",
        "str(state.approve_keywords.approved).lower() == 'true'",
        "{{ state.approve_keywords.approved }}",  # the whole text in braces is the expression inside (F1433)
    ])
    def test_every_way_a_condition_reads_the_field(self, condition):
        assert len(_never(_orch(_ask(), _gate(condition), RESEARCH))) == 1, condition

    def test_a_switch_and_a_loops_conditions(self):
        switch = {"id": "route", "name": "Route", "type": "switch", "switch_expression": "state.approve_keywords.choice",
                  "switch_cases": {"a": "research"}, "switch_default_step_id": "end"}
        assert len(_never(_orch(_ask(), switch, RESEARCH))) == 1
        for field in ("loop_while_condition", "loop_break_condition"):
            loop = {"id": "again", "name": "Again", "type": "loop", "loop_step_ids": ["body"], field:
                    "not state.approve_keywords.approved", "loop_max_iterations": 3, "output_key": "again",
                    "next_step_id": "end"}
            body = {"id": "body", "name": "Body", "type": "llm", "prompt_template": "x", "output_key": "body",
                    "next_step_id": None}
            assert len(_never(_orch(_ask(), loop, body))) == 1, field

    @pytest.mark.parametrize("field,value", [
        ("prompt_template", "Use the keywords. Note: {{ state.approve_keywords.feedback }}"),
        ("prompt_template", "Older form: {state.approve_keywords.feedback}"),
    ])
    def test_a_template_in_a_prompt(self, field, value):
        said = _never(_orch(_ask(), {**RESEARCH, field: value}))
        assert len(said) == 1 and "`feedback`" in said[0], said

    def test_a_template_in_a_print_an_evaluators_prompt_and_a_tools_arguments(self):
        shown = {"id": "show", "name": "Show", "type": "print", "output_key": "show",
                 "print_content": "They said: {{ state.approve_keywords.feedback }}"}
        judge = {"id": "judge", "name": "Judge", "type": "evaluator", "agent_id": "a1",
                 "evaluator_prompt": "Judge {{ state.approve_keywords.feedback }}", "route_map": {"ok": "end"}}
        send = {"id": "send", "name": "Send", "type": "tool", "forced_tool": "send_email", "output_key": "send",
                "tool_arguments": {"body": "{{ state.approve_keywords.feedback }}"}}
        for step in (shown, judge, send):
            assert len(_never(_orch(_ask(), step))) == 1, step["id"]

    def test_a_transforms_code(self):
        code = {"id": "parse", "name": "Parse", "type": "transform", "output_key": "parse",
                "transform_code": "note = state.approve_keywords\nresult = note['feedback']"}
        assert len(_never(_orch(_ask(), code))) == 1
        assert _never(_orch(_ask([{"name": "feedback", "type": "textarea"}]), code)) == []

    def test_human_response_is_every_human_steps(self):
        """The engine writes a Human step's answer to `human_response` as well (`engine.resume_paused`)."""
        assert len(_never(_orch(_ask(), _gate("state.human_response.approved"), RESEARCH))) == 1
        assert _never(_orch(_ask([YES_NO]), _gate("state.human_response.approved"), RESEARCH)) == []
        other = {"id": "second", "name": "Second", "type": "human", "human_prompt": "And?", "human_fields": [YES_NO],
                 "output_key": "second"}
        [said] = _never(_orch(_ask(), other, _gate("state.human_response.feedback"), RESEARCH))
        assert "'approve_keywords'" in said and "'second'" in said, said
        assert _never(_orch(_ask(), other, _gate("state.human_response.approved"), RESEARCH)) == []


class TestOtherStepsWhoseAnswerIsKnown:
    def test_an_evaluator_writes_its_routes_label(self):
        judge = {"id": "judge", "name": "Judge", "type": "evaluator", "agent_id": "a1", "evaluator_prompt": "ok?",
                 "route_map": {"good": "research", "bad": "end"}, "output_key": "verdict"}
        [said] = _never(_orch(judge, _gate("state.verdict.label == 'good'"), RESEARCH))
        assert "evaluator step 'judge' never writes" in said and "state.verdict itself" in said, said
        assert _never(_orch(judge, _gate("state.verdict == 'good'"), RESEARCH)) == []

    def test_an_if_else_writes_true_or_false(self):
        first = {**_gate("len(state.user_input) > 3"), "id": "long", "output_key": "long",
                 "if_true_step_id": "check_approval", "if_false_step_id": "end"}
        [said] = _never(_orch(first, _gate("state.long.result"), RESEARCH))
        assert "if/else step 'long' never writes" in said and "true or false" in said, said

    def test_a_retrieve_steps_contract(self):
        found = {"id": "find", "name": "Find", "type": "retrieve", "output_key": "find",
                 "retrieve": {"kb_ids": ["kb_1"], "query": "{{ state.user_input }}"}}
        answer = {**RESEARCH, "prompt_template": "Answer from {{ state.find.context }}"}
        assert _never(_orch(found, answer)) == []
        [said] = _never(_orch(found, {**RESEARCH, "prompt_template": "From {{ state.find.answer }}"}))
        assert "`context`" in said and "`chunks`" in said, said


class TestLeftAlone:
    @pytest.mark.parametrize("condition", [
        "state.approve_keywords == 'approve'",
        "str(state.approve_keywords).strip().lower() in ('yes', 'approve')",
        "state.approve_keywords.lower().startswith('y')",
        "len(state.approve_keywords) > 0",
        "state.approve_keywords[0] == 'y'",
    ])
    def test_a_whole_read_a_text_method_or_an_index(self, condition):
        assert _never(_orch(_ask(), _gate(condition), RESEARCH)) == []

    def test_a_whole_read_in_a_template(self):
        assert _never(_orch(_ask(), {**RESEARCH, "prompt_template": "Keywords: {{ state.approve_keywords }}"})) == []

    @pytest.mark.parametrize("writer", [
        {"type": "llm", "prompt_template": "Answer as JSON."},
        {"type": "agent", "agent_id": "a1", "prompt_template": "Do it."},
        {"type": "tool", "forced_tool": "get_weather"},
        {"type": "transform", "transform_code": "result = {'x': 1}"},
        {"type": "extract_json", "input_keys": ["user_input"]},
        {"type": "print", "print_content": '{"approved": true}'},
        {"type": "workflow", "workflow_id": "orch_child"},
    ])
    def test_a_step_whose_answer_the_step_does_not_say(self, writer):
        step = {"id": "make", "name": "Make", "output_key": "make", **writer}
        assert _never(_orch(step, _gate("state.make.approved"), RESEARCH)) == []

    def test_a_key_another_kind_of_step_writes_too(self):
        """A key written by a Human step and by a Tool step may hold either: not known. (A model step's answer is
        known since F1553: its declared fields, and none for an llm step without, so it is no longer the example.)"""
        draft = {"id": "draft", "name": "Draft", "type": "tool", "forced_tool": "get_weather",
                 "output_key": "approve_keywords"}
        assert _never(_orch(draft, _ask(), _gate("state.approve_keywords.approved"), RESEARCH)) == []

    def test_a_key_no_step_writes_is_another_rule(self):
        assert _never(_orch(_ask(), _gate("state.from_the_caller.approved"), RESEARCH)) == []

    @pytest.mark.parametrize("path", sorted(str(p.relative_to(ROOT)) for p in (ROOT / "backend/examples").glob(
        "*.bundle.json")) + ["backend/core/native_builder/orchestration.json"])
    def test_the_shipped_workflows(self, path):
        doc = json.loads((ROOT / path).read_text())
        for orch in doc.get("orchestrations", [doc]):
            assert _never(orch) == [], (path, orch.get("id"))


# ── The builder is told what a Human step answers ──────────────────────────────────────────────────────────────────

def _planner() -> str:
    from core.native_builder import prompt_lint

    return " ".join(prompt_lint.expanded_places()["agent:agent_native_builder_planner"].split())


class TestTheBuilderIsToldTheTruth:
    def test_the_planner_no_longer_says_a_fieldless_step_answers_an_object(self):
        text = _planner()
        assert "{approved, feedback}" not in text
        human = text[text.index("- `human`:"):text.index("- `if_else`:")]
        assert "with no `config.human_fields`, the person's words" in human, human

    def test_the_planner_makes_a_yes_or_no_a_branch_reads_a_yes_no_field(self):
        human = (t := _planner())[t.index("- `human`:"):t.index("- `if_else`:")]
        assert '{"name": "approved", "type": "boolean"' in human and "`state.<step id>.approved`" in human, human
        assert "Approve and Decline" in human, human

    def test_the_paste_guide_gives_a_user_approval_its_yes_no_field(self):
        from core.native_builder import AGENTS_SDK_GUIDE

        line = next(ln for ln in " ".join(AGENTS_SDK_GUIDE.split("\\\n")).splitlines() if "User approval" in ln)
        assert '{"name": "approved", "type": "boolean"' in line and "`state.<its id>.approved`" in line, line

    async def test_f1359s_message_no_longer_claims_it(self):
        from core.native_builder import build_plan

        ask = {"id": "receive_request", "type": "human", "name": "Receive request", "does": "Asks for the request",
               "prompt": "Paste the refund request:", "next": "parse_request"}
        code = {"id": "parse_request", "type": "transform", "name": "Parse", "does": "Reads it",
                "config": {"transform_code": "import re\nresult = re.findall('SO-\\\\d+', state.receive_request)"},
                "next": "end"}
        plan = {"kind": "create", "title": "Refunds", "summary": "Reads a refund request.", "steps": [ask, code]}
        found = await build_plan.examine(plan, None, ws=build_plan.Workspace(), state={"user_message": "x"})
        [said] = [e for e in found.errors if "as text" in e]
        assert "{approved, feedback}" not in said and "human_fields" in said, said

    async def test_the_planner_reads_the_validators_issue_in_its_own_terms(self):
        from core.native_builder import build_plan

        ask = {"id": "approve_keywords", "type": "human", "name": "Approve keywords", "does": "Asks for a yes",
               "prompt": "Do these keywords look right?", "next": "check_approval"}
        gate = {"id": "check_approval", "type": "if_else", "name": "Approved?", "does": "Goes on if approved",
                "condition": "state.approve_keywords.approved == True",
                "branches": {"true": "research", "false": "end"}}
        research = {"id": "research", "type": "llm", "name": "Research", "does": "Researches", "agent": "Writer",
                    "prompt": "Research the keywords.", "next": "end"}
        plan = {"kind": "create", "title": "Keywords", "summary": "Researches approved keywords.",
                "steps": [ask, gate, research],
                "agents": [{"name": "Writer", "purpose": "Researches keywords."}]}
        found = await build_plan.examine(plan, None, ws=build_plan.Workspace(), state={"user_message": "x"})
        [said] = [e for e in found.errors if "never write" in e]
        assert "`config.human_fields`" in said, said
        ask["config"] = {"human_fields": [YES_NO]}
        found = await build_plan.examine(plan, None, ws=build_plan.Workspace(), state={"user_message": "x"})
        assert not [e for e in found.errors if "never write" in e], found.errors

    def test_the_transform_panel_says_it_too(self):
        panel = (ROOT / "frontend/src/components/orchestration/panel/steps.tsx").read_text()
        note = " ".join(panel[panel.index("function TransformSection("):].split())[:2400]
        assert "{approved, feedback}" not in note
        assert "the person's words" in note, note


# ── The engine, end to end ─────────────────────────────────────────────────────────────────────────────────────────

def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _print(sid: str) -> dict:
    return {"id": sid, "name": sid.upper(), "type": "print", "print_content": sid, "output_key": "o",
            "next_step_id": None}


def _signed_off(orch_id: str, fields: list[dict]) -> dict:
    return S.make_orchestration(id=orch_id, entry_step_id="approve_keywords", steps=[
        {**_ask(fields), "next_step_id": "check_approval"},
        {**_gate("state.approve_keywords.approved == True"), "if_true_step_id": "yes", "if_false_step_id": "no"},
        _print("yes"), _print("no"),
    ])


async def _answered(orch: dict, answer) -> str:
    """Run `orch` to its question, answer it through the doors' own path, and say which arm the If/Else took."""
    from core.models_orchestration import Orchestration
    from core.orchestration import answers
    from core.orchestration.engine import OrchestrationEngine
    from core.orchestration.runner import SENTINEL

    await S.seed_orchestrations([orch])
    run_id = f"run_{orch['id']}"
    engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
    events = [e async for e in engine.run("go", run_id=run_id)]
    assert events[-1]["type"] == "human_input_required"
    outcome = await answers.answer_paused_run(types.SimpleNamespace(server_module=_server()), run_id, answer)
    resumed = []
    while (event := await asyncio.wait_for(outcome.queue.get(), 10)) is not SENTINEL:
        resumed.append(event)
    [decided] = [e["result"] for e in resumed if e.get("type") == "if_decision"]
    return decided


class TestTheEngineRoutesOnIt:
    @pytest.mark.parametrize("answer,arm", [
        ({"approved": True}, "true"),   # the run page's Approve
        ({"approved": False}, "false"),  # its Decline
        ("yes", "true"),                 # a channel's reply to a lone yes/no (F1434)
        ("no", "false"),
    ])
    async def test_the_repaired_sign_off(self, answer, arm):
        name = re.sub(r"\W", "", str(answer)).lower()
        assert await _answered(_signed_off(f"orch_f1549_{name}", [YES_NO]), answer) == arm

    async def test_the_shape_the_validator_refuses_routes_an_approval_no(self):
        """What MIG1 ran: the run page sends a fieldless step's answer as the words typed."""
        assert await _answered(_signed_off("orch_f1549_fieldless", []), "approve") == "false"
