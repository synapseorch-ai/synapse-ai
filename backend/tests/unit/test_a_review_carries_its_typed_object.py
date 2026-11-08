"""
A Human step whose context is a typed object carries the object on its question, not only its words (F795,
SAI2-P2, D308).

Synapse AI's change review used to be markdown a model wrote, which the panel parsed back into rows (`plan.ts`).
The review's text is now code's, rendered from the checked Change; the panel draws the change card from the Change
itself. So `human_input_required` carries `context`: the step's context value when it is an object that names
itself (`"object": "change"`), or JSON text of one, as a builder tool's output is. Every other question is as it was.
The prompt's words are unchanged, so channels, v1, MCP and older panels still read them.
"""
import json

from core.models_orchestration import OrchestrationRun, StepConfig, StepType
from core.orchestration.steps import HumanStepExecutor

CHANGE = {"object": "change", "outcome": "review", "ops": [{"op": "rename_agent", "status": "ready"}],
          "review": "- Rename the agent"}


async def _asked(state: dict) -> dict:
    step = StepConfig(id="step_manage_review", name="Review", type=StepType.HUMAN,
                      human_prompt="Here is what I would do.\n\n---\n\n{state.checked.review}", input_keys=["checked"])
    run = OrchestrationRun(run_id="r1", orchestration_id="o1", shared_state=state)
    events = [e async for e in HumanStepExecutor().execute(step, run, engine=None)]
    return next(e for e in events if e["type"] == "human_input_required")


async def test_json_text_of_a_typed_object_is_carried_as_the_object():
    asked = await _asked({"checked": json.dumps(CHANGE)})
    assert asked["context"] == CHANGE
    assert asked["prompt"] == "Here is what I would do.\n\n---\n\n- Rename the agent"
    assert asked["context_key"] == "checked"


async def test_a_typed_object_held_as_a_dict_is_carried_too():
    assert (await _asked({"checked": CHANGE}))["context"] == CHANGE


async def test_any_other_context_carries_no_object():
    for value in ("plain words", json.dumps({"no": "name"}), json.dumps([1, 2]), {"object": 3}):
        assert "context" not in await _asked({"checked": value})
