"""A Human step's question names the state key its context came from (U65).

The answer modal drew the step's context — the first of its input keys with a
value, or else the newest step output — in an unlabelled box under its title,
so a reviewer read a bare "pass" (an evaluator's label) with nothing saying
what it was. The event carried the value and not where it came from, so the
page had nothing to label it with.
"""
from core.models_orchestration import OrchestrationRun, StepConfig, StepType
from core.orchestration.steps import HumanStepExecutor


async def _asked(step: StepConfig, run: OrchestrationRun) -> dict:
    events = [e async for e in HumanStepExecutor().execute(step, run, engine=None)]
    return next(e for e in events if e["type"] == "human_input_required")


def _step(**over) -> StepConfig:
    return StepConfig(id="review", name="Review", type=StepType.HUMAN,
                      human_prompt="Send it?", **over)


class TestTheQuestionNamesItsContext:

    async def test_an_input_key_names_itself(self):
        run = OrchestrationRun(run_id="r1", orchestration_id="o1",
                               shared_state={"empty": "", "draft": "Dear Ana…", "later": "x"})
        asked = await _asked(_step(input_keys=["empty", "draft", "later"]), run)
        assert asked["agent_context"] == "Dear Ana…"
        assert asked["context_key"] == "draft"

    async def test_the_fallback_names_the_step_output_it_read(self):
        run = OrchestrationRun(
            run_id="r1", orchestration_id="o1", shared_state={"category": "pass", "old": "x"},
            step_history=[{"step_id": "a", "output_key": "old"},
                          {"step_id": "judge", "output_key": "category"}])
        asked = await _asked(_step(), run)
        assert asked["agent_context"] == "pass"
        assert asked["context_key"] == "category"

    async def test_no_context_names_no_key(self):
        run = OrchestrationRun(run_id="r1", orchestration_id="o1")
        asked = await _asked(_step(), run)
        assert asked["agent_context"] is None
        assert asked["context_key"] is None
