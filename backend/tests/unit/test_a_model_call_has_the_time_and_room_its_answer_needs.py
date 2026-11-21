"""A model call is given the time and the room the answer it is allowed needs (F1682).

Synapse AI's planner on Sonnet 4.6 (adaptive thinking, effort medium) was allowed 10,240 output tokens: the medium
thinking budget (6,144) and room for an answer. Adaptive thinking takes no budget, so it thought through the whole
allowance three calls running and wrote no plan; and each such call took about 191 seconds, past the 180-second cut on
one try, so at the default cut the call was cut and sent again on every try and the build waited sixteen minutes.

Now an Anthropic call's cut is sized to what it may write, at the slowest rate it is expected to write (never under the
default), and an adaptive-thinking call's allowance makes room for the thinking its effort asks for, then an answer.

Near misses: a short call keeps the default cut; a budget model's allowance is as before.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import core.llm_providers as llm
from core import reasoning
from core.config import LLM_REQUEST_TIMEOUT

#: What the planner's long call wrote, and how long it took (the 2.1.1 candidate's X2 repro).
WROTE, TOOK = 10_240, 191


def _capture(monkeypatch) -> dict:
    import anthropic

    seen: dict = {}
    usage = SimpleNamespace(input_tokens=12, output_tokens=7, cache_creation_input_tokens=0, cache_read_input_tokens=0)
    resp = SimpleNamespace(content=[SimpleNamespace(type="text", text="ok", name=None, input=None)], usage=usage)

    class _Messages:
        async def create(self, **kwargs):
            seen["max_tokens"] = kwargs.get("max_tokens")
            return resp

    class _Client:
        def __init__(self, *a, **k):
            seen["timeout"] = k.get("timeout")
            self.messages = _Messages()

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _Client)
    return seen


async def test_a_call_allowed_a_long_answer_is_never_cut_before_it_could_write_it(monkeypatch):
    seen = _capture(monkeypatch)
    await llm.call_anthropic("claude-sonnet-4-6", [{"role": "user", "content": "q"}], "sys", "key", max_tokens=WROTE)
    assert seen["timeout"] >= TOOK * 1.25, f"{WROTE} tokens took {TOOK} s; the cut was {seen['timeout']} s"


async def test_a_short_call_keeps_the_default_cut(monkeypatch):
    seen = _capture(monkeypatch)
    await llm.call_anthropic("claude-sonnet-4-6", [{"role": "user", "content": "q"}], "sys", "key", max_tokens=1024)
    assert seen["timeout"] == LLM_REQUEST_TIMEOUT


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_an_adaptive_thinking_call_has_room_for_its_thinking_and_an_answer(effort):
    plan = reasoning.plan_for("claude-sonnet-4-6", effort, 8192)
    assert plan is not None
    # The medium allowance that ran out in thinking was 10,240: at least twice that now, and more for more effort.
    floor = {"low": 10_240, "medium": 2 * WROTE, "high": 32_768}[effort]
    assert plan.max_output_tokens >= floor, (effort, plan.max_output_tokens)


def test_a_budget_models_allowance_is_as_before():
    plan = reasoning.plan_for("claude-sonnet-4-5-20250929", "medium", 8192)
    assert plan is not None and plan.max_output_tokens == 6144 + 4096, plan.max_output_tokens
