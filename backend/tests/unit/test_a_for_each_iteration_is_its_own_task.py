"""In a for-each loop, each iteration's prompt is that item's task — not "the next item" (F767).

CLIM1's measurement, 2026-10-20: a workflow looped over six orders, an agent step asked
"Status of order {state.loop_item}?". From the second iteration on, the step's prompt
said "Use your judgement: produce the next item, refine, or take a different angle.
Just don't silently re-emit what you produced before", and filed the task — which named
the right order — under "ORIGINAL TASK (reference only)". Claude Haiku did as told and
invented the next order ids (A-3002, A-4002…) instead of the ones it was given; Sonnet
kept to the task. Any model can, through any provider.

That wording is right for a loop that refines its own output (a while or count loop);
a for-each loop runs the same task once per item. Naveen: fix it. Its iterations now
lead with the task for their item, and earlier items are context only.
"""
import types

import pytest

from _fakes import seed as S

ORDERS = ["A-1042", "A-2001", "A-2003"]


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _engine(orch_dict):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    return OrchestrationEngine(Orchestration.model_validate(orch_dict), _server())


def _orch(loop: dict) -> dict:
    return {
        "id": "orch_foreach_task", "name": "Statuses", "entry_step_id": "each",
        "state_schema": {"orders": {"type": "list", "default": ORDERS}},
        "steps": [
            {"id": "each", "name": "Each order", "type": "loop", "loop_step_ids": ["stat"],
             "loop_max_iterations": 5, "next_step_id": None, **loop},
            {"id": "stat", "name": "Status", "type": "agent", "agent_id": "agent_status",
             "prompt_template": "Status of order {state.loop_item}?", "output_key": "statuses",
             "next_step_id": None},
        ],
    }


@pytest.fixture
async def status_agent():
    agent = S.make_agent(id="agent_status", tools=[], model="claude-haiku-4-5")
    await S.seed_agents([agent])
    return agent


async def _prompts(fake_llm, loop: dict) -> list[dict]:
    fake_llm.script(["A-1042: shipped", "A-2001: delayed", "A-2003: delayed"])
    engine = _engine(_orch(loop))
    [ev async for ev in engine.run("go", run_id="run_foreach_task")]
    return fake_llm.calls


def _text(call: dict) -> tuple[str, str]:
    return str(call.get("prompt_msg") or call.get("_args") or ""), str(call.get("sys_prompt") or "")


class TestAForEachIterationIsItsItemsTask:
    async def test_every_iteration_leads_with_its_own_items_task(self, fake_llm, status_agent):
        calls = await _prompts(fake_llm, {"loop_over": "state.orders"})
        assert len(calls) == 3
        for order, call in zip(ORDERS, calls):
            prompt, _ = _text(call)
            assert f"## TASK\nStatus of order {order}?" in prompt, (order, prompt[-600:])
            assert "reference only" not in prompt

    async def test_no_iteration_is_asked_for_the_next_item_or_a_change_note(self, fake_llm, status_agent):
        calls = await _prompts(fake_llm, {"loop_over": "state.orders"})
        for call in calls[1:]:
            prompt, system = _text(call)
            both = prompt + system
            for words in ("produce the next item", "take a different angle", "don't silently re-emit",
                          "what this iteration adds or changes", "refine, redo, or push back"):
                assert words not in both, words

    async def test_the_item_and_its_place_are_named(self, fake_llm, status_agent):
        calls = await _prompts(fake_llm, {"loop_over": "state.orders"})
        prompt, _ = _text(calls[1])
        assert "iteration 2 of 3" in prompt
        assert "A-2001" in prompt

    async def test_earlier_items_are_context_only(self, fake_llm, status_agent):
        calls = await _prompts(fake_llm, {"loop_over": "state.orders"})
        prompt, _ = _text(calls[2])
        assert "A-1042: shipped" in prompt          # still shown
        assert "context only" in prompt.lower()


class TestALoopThatRefinesKeepsItsWording:
    """A count loop runs the step again on its own output: that is what the wording is for."""

    async def test_a_count_loop_is_still_asked_to_build_on_its_last_output(self, fake_llm, status_agent):
        calls = await _prompts(fake_llm, {"loop_count": 3})
        prompt, _ = _text(calls[1])
        assert "produce the next item, refine, or take a different angle" in prompt
        assert "## ORIGINAL TASK (reference only)" in prompt
