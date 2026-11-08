"""A saved workflow the engine's step model refuses is read in its shape before it is planned from (F1235).

On prod (rc5.3, 10-29) every builder update of a workflow rc4.3's builder had saved logged "the workflow to update did
not read as a Plan: StepConfig.human_fields Input should be a valid list" and changed nothing: the human step held
`{"fields": [...]}`, as the step tools saved what a model wrote until F1047, and with no Plan of it the change planner
went looking with read_workspace until the repeat guard stopped it. The reader (`core.orchestration.read_shapes`)
follows `StepConfig`'s own errors, so every field it has is covered and none is listed; the model itself, a new Plan
and F1047's save stay as strict as they were.
"""
from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from core import builder_tools  # noqa: F401 — loaded first, as the app loads it (build_plan and build_save import each other)
from core.models_orchestration import StepConfig, StepType
from core.native_builder import build_plan
from core.native_builder.compile import compile_plan, plan_from_workflow
from core.orchestration.read_shapes import read_step, read_steps

# The step as prod saved it (HOT2's read, 10-27; F1047's test), its values cut to their shape.
PROD_FIELDS = {"fields": [{"name": "api_key", "type": "str"}, {"name": "api_secret_key", "type": "str"}]}
HUMAN = {"id": "step_generate_api_keys", "name": "Generate API Keys", "type": "human",
         "human_prompt": "Please generate and input your Twitter API keys.", "human_fields": PROD_FIELDS,
         "output_key": "keys", "next_step_id": "step_post"}
POST = {"id": "step_post", "name": "Write the post", "type": "llm", "prompt_template": "A post with {state.keys}",
        "output_key": "post", "next_step_id": None}
PROD_DOC = {"id": "orch_h", "name": "Synapse AI Weekly Twitter Post", "entry_step_id": HUMAN["id"],
            "steps": [HUMAN, POST]}


def _mint():
    n = {"i": 0}

    def mint(prefix, length=7):
        n["i"] += 1
        return f"{prefix}_{n['i']:03d}"

    return mint


class TestProdsWorkflowReadsAsAPlan:
    def test_the_engine_s_model_still_refuses_the_step(self):
        with pytest.raises(ValidationError):
            StepConfig.model_validate(HUMAN)

    def test_it_reads_as_a_plan_with_its_form_fields_kept(self):
        plan = plan_from_workflow(copy.deepcopy(PROD_DOC))
        human = next(s for s in plan.steps if s.id == HUMAN["id"])
        assert human.type == "human" and human.prompt == HUMAN["human_prompt"]
        assert human.config["human_fields"] == PROD_FIELDS["fields"]

    def test_an_update_of_it_has_its_base(self):
        base = build_plan._base({"intent": "update", build_plan.EXISTING_KEY: copy.deepcopy(PROD_DOC)})
        assert base is not None
        assert [s.id for s in base.steps] == [HUMAN["id"], POST["id"]]

    def test_the_stored_document_is_not_changed(self):
        doc = copy.deepcopy(PROD_DOC)
        plan_from_workflow(doc)
        read_steps(doc["steps"])
        assert doc == PROD_DOC

    def test_a_new_plan_in_that_shape_is_still_refused(self):
        def issues(fields):
            plan = plan_from_workflow(copy.deepcopy(PROD_DOC))
            next(s for s in plan.steps if s.id == HUMAN["id"]).config["human_fields"] = fields
            return compile_plan(plan, existing_agents={}, orch_id="orch_h", mint=_mint()).all_issues()

        assert not [i for i in issues(PROD_FIELDS["fields"]) if i.startswith("The workflow does not load")]
        assert [i for i in issues(PROD_FIELDS) if i.startswith("The workflow does not load")]


class TestWhatIsReadDifferentlyIsSaid:
    def test_the_note_names_the_step_and_the_field(self):
        step, notes = read_step(HUMAN)
        assert step["human_fields"] == PROD_FIELDS["fields"]
        assert notes == ["Step 'step_generate_api_keys': its `human_fields` was saved inside an object; it is read "
                         "as the list it holds."]

    def test_a_step_in_the_engine_s_shape_comes_back_as_it_was_with_nothing_said(self):
        assert read_step(POST) == (POST, [])

    def test_a_type_the_engine_does_not_know_is_left_for_its_reader_to_refuse(self):
        odd = {**POST, "type": "teleport", "human_fields": PROD_FIELDS}
        assert read_step(odd) == (odd, [])

    def test_steps_that_are_not_a_list_or_not_steps(self):
        assert read_steps(None) == ([], [])
        assert read_steps({"a": 1})[0] == []
        steps, notes = read_steps([POST, "step_x"])
        assert steps == [POST] and notes == ["Step 2 was saved as text, not a step, and was left out."]


class TestEntriesAndValues:
    def test_a_form_field_that_is_not_an_object_is_left_out(self):
        step, notes = read_step({**HUMAN, "human_fields": [{"name": "a", "type": "string"}, "b"]})
        assert step["human_fields"] == [{"name": "a", "type": "string"}]
        assert "human_fields" in notes[0] and "left out" in notes[0]

    def test_a_lone_id_where_a_branch_goes_is_that_branch(self):
        step, _ = read_step({"id": "p", "name": "P", "type": "parallel", "parallel_branches": ["a", ["b", "c"]]})
        assert step["parallel_branches"] == [["a"], ["b", "c"]]

    def test_a_number_where_text_goes_is_that_text(self):
        step, notes = read_step({**POST, "prompt_template": 42})
        assert step["prompt_template"] == "42" and notes == []

    def test_an_object_where_text_goes_is_left_unset(self):
        step, notes = read_step({**POST, "prompt_template": {"text": "hi"}})
        assert "prompt_template" not in step
        assert "left unset" in notes[0]

    def test_an_object_holding_more_than_one_list_is_not_guessed_at(self):
        step, notes = read_step({**HUMAN, "human_fields": {"a": [], "b": []}})
        assert "human_fields" not in step and "left unset" in notes[0]


class TestAFieldOnAStepThatDoesNotReadIt:
    @pytest.mark.parametrize("field,value,type_", [
        ("pass_images", True, "llm"),
        ("finish_tools", ["submit"], "tool"),
        ("tool_arguments", {"q": "x"}, "agent"),
    ])
    def test_it_is_left_out(self, field, value, type_):
        step, notes = read_step({"id": "s", "name": "S", "type": type_, field: value})
        assert field not in step
        StepConfig.model_validate(step)
        assert notes == [f"Step 's': its `{field}` is not read by {type_} steps, so it was left out."]

    def test_a_fallback_with_no_finish_tools_is_left_out(self):
        step, _ = read_step({"id": "s", "name": "S", "type": "tool", "finish_tools": ["x"], "finish_fallback": {}})
        assert "finish_tools" not in step and "finish_fallback" not in step
        StepConfig.model_validate(step)

    def test_on_the_step_that_reads_it_it_is_kept(self):
        step = {"id": "s", "name": "S", "type": "agent", "agent_id": "a", "pass_images": True,
                "finish_tools": ["submit"], "finish_fallback": {"done": False}}
        assert read_step(step) == (step, [])


# Every field of the engine's step model, on every step type, in each shape a saver could have left it in: what the
# reader gives back is a step the model reads. No list of fields here: a field added to `StepConfig` is swept too.
_WRONG = [{"wrapped": ["x"]}, {"a": 1, "b": 2}, 7, True, "text", ["x", 3, {"y": 1}], None]
_FIELDS = sorted(set(StepConfig.model_fields) - {"id", "name", "type"})


@pytest.mark.parametrize("type_", [t.value for t in StepType])
@pytest.mark.parametrize("field", _FIELDS)
def test_every_field_in_every_shape_reads_on_every_step_type(field, type_):
    for wrong in _WRONG:
        step, _ = read_step({"id": "s", "name": "S", "type": type_, field: copy.deepcopy(wrong)})
        StepConfig.model_validate(step)
