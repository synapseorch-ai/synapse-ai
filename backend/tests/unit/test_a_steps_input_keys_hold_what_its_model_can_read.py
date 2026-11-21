"""A model step's input keys hold a long value up to what its model can read, as its prompt's references do (F1678).

An LLM or Agent step's input keys reached its prompt through the context builder, which cut every value at 5,000
characters (`_format_context_value`'s default) whatever the model can read: a 14,000-character draft handed to a step
that applies a reviewer's changes reached its model as its head and its tail with the middle cut out, so the step
answered with placeholders ("[…remaining … unchanged…]") or rewrote what it could not see. The same value written into
the prompt as `{{ state.draft }}` was whole: a reference holds what fits the step's read share (`step_refs.reading`,
set from its model's window). Now an input key holds a value up to the same read share.

Near misses: a value longer than the read share is still cut, as a reference's is; a model whose read share is under
the old 5,000 is never cut harder than before; the template path is unchanged.
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import core.llm_providers as llm
from _fakes import seed as S

SECTION = "SECTION-{n:04d} Harbor & Pine weekly report paragraph, made up for a test, padded to length. "


def _draft(chars: int) -> tuple[str, int]:
    out, n = [], 0
    while sum(len(x) for x in out) < chars:
        out.append(SECTION.format(n=n))
        n += 1
    text = "".join(out)[:chars]
    return text, text.count("SECTION-")


async def _sent(monkeypatch, draft: str, *, input_keys: list[str], prompt: str, tag: str) -> str:
    """What the step's model call received."""
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    seen: list[str] = []

    async def capture(prompt_msg, sys_prompt, *a, **kw):
        seen.append(str(prompt_msg))
        return "ok"

    monkeypatch.setattr(llm, "generate_response", capture)
    orch = S.make_orchestration(id=f"orch_f1678_{tag}", entry_step_id="revise", steps=[
        {"id": "revise", "name": "Revise", "type": "llm", "model": "mistral", "prompt_template": prompt,
         "input_keys": input_keys, "output_key": "revised", "next_step_id": None}])
    await S.seed_orchestrations([orch])
    engine = OrchestrationEngine(Orchestration.model_validate(orch),
                                 NS(agent_sessions={}, memory_store=None, tool_router={}))
    _ = [e async for e in engine.run("go", run_id=f"run_f1678_{tag}", initial_state={"draft": draft})]
    assert seen, "the step called its model"
    return seen[0]


async def _read_share() -> int:
    from core.config import load_settings
    from core.context_budget import step_budget

    return (await step_budget("mistral", load_settings())).read_cap


async def test_a_14k_draft_by_input_keys_reaches_the_model_whole(monkeypatch):
    draft, sections = _draft(14_000)
    sent = await _sent(monkeypatch, draft, input_keys=["draft"], prompt="Apply the reviewer's changes to the draft.",
                       tag="whole")
    assert sent.count("SECTION-") == sections, (sent.count("SECTION-"), sections)
    assert f"SECTION-{sections // 2:04d}" in sent, "its middle reaches the model"


async def test_a_value_longer_than_the_read_share_is_still_cut(monkeypatch):
    draft, sections = _draft(await _read_share() + 5_000)
    sent = await _sent(monkeypatch, draft, input_keys=["draft"], prompt="Apply the reviewer's changes to the draft.",
                       tag="over")
    assert sent.count("SECTION-") < sections and len(sent) < len(draft)


async def test_a_small_windows_input_key_holds_what_it_held_before(monkeypatch):
    """A model whose read share is under the old 5,000 (a window under ~10K tokens: `READ_MIN`) is never cut harder."""
    import dataclasses

    import core.context_budget as cb
    from core.orchestration.summarizer import smart_truncate

    real = cb.step_budget

    async def small(model, settings, **kw):
        return dataclasses.replace(await real(model, settings, **kw), read_cap=cb.READ_MIN)

    monkeypatch.setattr(cb, "step_budget", small)
    draft, _ = _draft(14_000)
    sent = await _sent(monkeypatch, draft, input_keys=["draft"], prompt="Apply the reviewer's changes to the draft.",
                       tag="small")
    before = smart_truncate(draft, 5_000).count("SECTION-")
    assert cb.READ_MIN < 5_000 and sent.count("SECTION-") >= before, (sent.count("SECTION-"), before)


async def test_the_template_path_is_unchanged(monkeypatch):
    draft, sections = _draft(14_000)
    sent = await _sent(monkeypatch, draft, input_keys=[],
                       prompt="Apply the reviewer's changes to this draft: {{ state.draft }}", tag="template")
    assert sent.count("SECTION-") == sections
