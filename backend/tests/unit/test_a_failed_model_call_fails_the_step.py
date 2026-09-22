"""
A model call that failed is not an answer (F132).

`generate_response_ex` caught any provider exception that was not an `LLMError`
in five places — the cloud branch, the CLI branch, the HuggingFace branch, the
local Ollama branch, and an unknown cloud model — and returned its text as the
model's reply. So an LLM step on a down Ollama wrote "Local Agent Error: …" into
its output key and the run completed, and an evaluator took its fallback route
on a sentence it could not parse. D112's rule: a failure is never reported as a
success. Each case runs in-process and through the fleet's job functions.
"""
import pytest

import core.llm_providers as _llm_mod
from _fakes import seed as S
from test_human_in_containers import _run, driver, worker  # noqa: F401 — fixtures

# The real dispatch, captured before the autouse `fake_llm` swaps it out.
_REAL_GENERATE = _llm_mod.generate_response


@pytest.fixture
def unreachable_openai(monkeypatch):
    """The real dispatch, with an OpenAI call that fails the way a dropped
    connection does — not an `LLMError`, which is what took the string branch."""
    async def refused(*args, **kwargs):
        raise ConnectionError("All connection attempts failed")

    monkeypatch.setattr(_llm_mod, "generate_response", _REAL_GENERATE)
    monkeypatch.setattr(_llm_mod, "call_openai", refused)


class TestAStepWhoseModelFailedFails:
    async def test_an_llm_step(self, driver, unreachable_openai):
        orch = S.make_orchestration(id="orch_llm_down", entry_step_id="write", steps=[
            {"id": "write", "name": "Write", "type": "llm", "model": "gpt-4o",
             "prompt_template": "write", "output_key": "draft", "next_step_id": "after"},
            {"id": "after", "name": "After", "type": "print", "print_content": "ran",
             "output_key": "o_after", "next_step_id": None},
        ])
        await S.seed_orchestrations([orch])

        assert await driver.start(orch, "run_llm_down") == "failed"
        state = (await _run("run_llm_down")).shared_state
        assert "Cloud API Error" not in str(state.get("draft")), "the error is not the step's output"
        assert "o_after" not in state, "the next step did not run"

    async def test_an_evaluator_does_not_take_its_fallback_route(self, driver, unreachable_openai):
        orch = S.make_orchestration(id="orch_eval_down", entry_step_id="ev", steps=[
            {"id": "ev", "name": "Decide", "type": "evaluator", "model": "gpt-4o",
             "evaluator_prompt": "pick", "route_map": {"yes": "yes_step", "no": None},
             "input_keys": [], "next_step_id": None},
            {"id": "yes_step", "name": "Yes", "type": "print", "print_content": "routed",
             "output_key": "o_yes", "next_step_id": None},
        ])
        await S.seed_orchestrations([orch])

        assert await driver.start(orch, "run_eval_down") == "failed"
        assert "o_yes" not in (await _run("run_eval_down")).shared_state, "no route was taken"
