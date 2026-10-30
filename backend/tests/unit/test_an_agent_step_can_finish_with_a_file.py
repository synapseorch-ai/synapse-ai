"""An Agent step can save files and finish with one, and its answer cut at the output limit twice is a file (D339 P4).

Naveen (10-30): "agents finish with a file". An Agent step with no finish tools is offered two of the engine's own
tools: `write_output` saves a file to this run's outputs in the vault, and `submit_output` ends the step with a file
as its output — a reference the next step reads as `core.step_refs` says — so a long result is never retyped into an
answer. A reply in words still ends the step, as before.

A step's answer cut at the output limit twice is saved whole as a file — its pieces are one answer (F1273) — and that
file is the step's output, instead of a third call to learn again that it does not fit.
"""
from __future__ import annotations

import json
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from core import step_refs
from test_a_big_step_output_is_passed_as_a_file import PAGE, store  # noqa: F401 — the store fixture

RUN, STEP = "run_p4_agent", "step_write"


def _write(file_name: str, content: str) -> str:
    """A model's write_output call (its `name` argument is `tool_call`'s own first parameter)."""
    return "<tool_call>" + json.dumps({"tool": "write_output",
                                       "arguments": {"name": file_name, "content": content}}) + "</tool_call>"


async def _turn(*, output_step=STEP, finish=None, max_turns=6, run_id=RUN):
    from core.react_engine import run_agent_step

    agent = S.make_agent(tools=["all"], skip_default_tools=True)
    return [ev async for ev in run_agent_step(
        message="Collect the pages.", agent_id=agent["id"], session_id="s_p4",
        server_module=types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}),
        agent_override=agent, tools_override=[], max_turns=max_turns, source="orchestration_step",
        run_id=run_id, output_step=output_step, finish_tools=list(finish) if finish else None)]


def _final(events):
    finals = [e for e in events if e.get("type") == "final"]
    assert finals, "the turn ended with a final event"
    return finals[-1]


class TestAStepFinishesWithAFile:
    async def test_it_saves_a_file_and_finishes_with_it(self, store, fake_llm):
        key = step_refs.key_for(RUN, STEP, "pages.md")
        fake_llm.script([_write("pages.md", PAGE),
                         tool_call("submit_output", text="The 39 pages, one per section.", file=key)])
        final = _final(await _turn())
        ref = final["finish"]["result"]
        assert step_refs.is_ref(ref) and ref["$vault"] == key and ref["chars"] == len(PAGE)
        assert ref["text"] == "The 39 pages, one per section."
        step_refs.forget_cache()
        assert step_refs.load(ref) == PAGE

    async def test_a_reply_in_words_still_ends_the_step(self, store, fake_llm):
        fake_llm.script(["Here is the short answer."])
        final = _final(await _turn())
        assert final["response"] == "Here is the short answer." and "finish" not in final

    async def test_a_file_that_is_not_there_goes_back_to_the_model(self, store, fake_llm):
        fake_llm.script([tool_call("submit_output", text="x", file="step_outputs/nowhere/at/all.md"),
                         "I could not save it, so here it is in words."])
        final = _final(await _turn())
        assert "finish" not in final and final["response"].startswith("I could not save it")

    async def test_a_step_with_finish_tools_is_not_offered_them(self, store, fake_llm):
        fake_llm.script([_write("a.md", "x"), "words", "words"])
        await _turn(finish=["submit_plan"])
        assert not store.blobs, "a typed finish step writes no files of its own"

    async def test_a_chat_turn_is_not_offered_them(self, store, fake_llm):
        fake_llm.script([_write("a.md", "x"), "done"])
        await _turn(output_step=None)
        assert not store.blobs


class TestTheFilesItMayWrite:
    def test_one_file_is_capped(self, store):
        said, out = step_refs.call_output_tool(step_refs.WRITE_OUTPUT,
                                               {"name": "big.txt", "content": "x" * (step_refs.FILE_MAX_BYTES + 1)},
                                               run_id=RUN, step_id=STEP, written=[])
        assert "error" in out and not store.blobs

    def test_a_step_saves_at_most_its_number_of_files(self, store):
        written: list[str] = []
        for i in range(step_refs.FILES_PER_STEP):
            assert "error" not in step_refs.call_output_tool(
                step_refs.WRITE_OUTPUT, {"name": f"f{i}.md", "content": "x"}, run_id=RUN, step_id=STEP,
                written=written)[1]
        said, out = step_refs.call_output_tool(step_refs.WRITE_OUTPUT, {"name": "one-more.md", "content": "x"},
                                               run_id=RUN, step_id=STEP, written=written)
        assert "error" in out

    def test_a_file_stays_in_its_steps_folder_and_holds_no_secret(self, store):
        from core.secret_scrub import hold_secrets

        secret = "sk-" + "Z9y8X7w6" * 4
        hold_secrets({"BILLING_KEY": secret})
        try:
            said, out = step_refs.call_output_tool(step_refs.WRITE_OUTPUT,
                                                   {"name": "../../../other/x.md", "content": f"key {secret}"},
                                                   run_id=RUN, step_id=STEP, written=[])
        finally:
            hold_secrets({})
        assert out["file"].startswith(f"step_outputs/{RUN}/{STEP}/") and ".." not in out["file"]
        assert secret not in "".join(store.blobs.values())

    def test_a_host_may_refuse_a_write(self, store):
        def full(_bytes):
            raise RuntimeError("the workspace's Vault is full")

        step_refs.set_write_hook(full)
        try:
            said, out = step_refs.call_output_tool(step_refs.WRITE_OUTPUT, {"name": "a.md", "content": "x"},
                                                   run_id=RUN, step_id=STEP, written=[])
        finally:
            step_refs.set_write_hook(None)
        assert "error" in out and "full" in said and not store.blobs

    def test_finishing_with_a_path_outside_the_vault_is_refused(self, store):
        said, out = step_refs.call_output_tool(step_refs.SUBMIT_OUTPUT, {"text": "x", "file": "../etc/passwd"},
                                               run_id=RUN, step_id=STEP, written=[])
        assert "error" in out


# ── a step's answer cut at the output limit twice (its pieces are one answer, F1273) ──────────────────────────────────

def _model(pieces, monkeypatch):
    """A model that answers with `pieces` in turn: (text, stop_reason)."""
    import core.llm_providers as llm
    import core.react_engine as re_mod

    replies = list(pieces)

    async def model(*_a, **_kw):
        text, stop = replies.pop(0)
        return llm.LLMResult(text, 10, 10, 0, 0, stop)

    monkeypatch.setattr(re_mod, "llm_generate_response", model)


class TestAStepsLongAnswerIsSavedAsAFile:
    async def test_a_steps_answer_cut_twice_is_saved_whole_as_its_file(self, store, monkeypatch):
        _model([("Part one. ", "max_tokens"), ("Part two. ", "max_tokens"), ("never asked", "end_turn")],
               monkeypatch)
        events = await _turn()
        final = _final(events)
        ref = final["finish"]["result"]
        assert step_refs.is_ref(ref) and step_refs.load(ref) == "Part one. Part two. "
        assert any(e.get("type") == "truncated_output" and e.get("saved") == ref["$vault"] for e in events)
