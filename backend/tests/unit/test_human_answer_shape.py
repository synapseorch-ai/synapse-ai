"""
A human step with no fields holds its answer as text, however it was answered (F106).

The same step's output key held a string or `{"response": text}` depending on
the road the answer took: OSS chat sent the dict, the orchestration screen's
modal and a channel reply sent the string — which the fleet's enqueue then
wrapped into the dict, and v1's and v2's resume routes always wrapped. So
`{state.approval}` rendered `yes` from one place and `{'response': 'yes'}` from
another. Cloud's forms and channel always sent the string. Naveen's call: one
shape, the text, decided once in the engine — every sender unchanged.

A step that declares fields keeps the mapping it was answered with, and so does
a no-field answer that is any other dict: a caller that chose a shape keeps it.
"""
import pytest

from _fakes import seed as S
from test_human_in_containers import _human, _print, _run, driver, worker  # noqa: F401 — fixtures


def _asks(orch_id, **human):
    return S.make_orchestration(id=orch_id, entry_step_id="ask", steps=[
        _human("ask", key="approval", nxt="say", **human),
        _print("say", "got {state.approval}", key="said"),
    ])


class TestANoFieldStepHoldsText:
    @pytest.mark.parametrize("answer", [{"response": "yes"}, "yes"], ids=["wrapped", "bare"])
    async def test_either_shape_is_held_as_the_text(self, driver, answer):
        orch = _asks("orch_shape_text")
        await S.seed_orchestrations([orch])

        assert await driver.start(orch, "run_shape_text") == "paused"
        assert await driver.answer("run_shape_text", answer) == "completed"
        state = (await _run("run_shape_text")).shared_state
        assert state["approval"] == "yes"
        assert state["human_response"] == "yes"
        assert state["said"] == "got yes", "a template reads the words, not a dict"


class TestAShapeTheCallerChoseIsKept:
    async def test_a_step_with_fields_keeps_its_mapping(self, driver):
        orch = _asks("orch_shape_fields", human_fields=[{"name": "response", "type": "string"}])
        await S.seed_orchestrations([orch])

        await driver.start(orch, "run_shape_fields")
        await driver.answer("run_shape_fields", {"response": "yes"})
        assert (await _run("run_shape_fields")).shared_state["approval"] == {"response": "yes"}

    async def test_another_mapping_on_a_no_field_step_is_kept(self, driver):
        orch = _asks("orch_shape_other")
        await S.seed_orchestrations([orch])

        await driver.start(orch, "run_shape_other")
        await driver.answer("run_shape_other", {"decision": "approve", "note": "fine"})
        assert (await _run("run_shape_other")).shared_state["approval"] == {
            "decision": "approve", "note": "fine"}
