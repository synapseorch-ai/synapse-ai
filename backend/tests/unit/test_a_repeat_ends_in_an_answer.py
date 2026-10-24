"""A loop the repeat guard stops is asked for its answer before it fails (F1015).

Prod, rc4.1, the Default model (synapse.nova-pro): "hi" in Synapse AI went to the answer step, which read
`read_workspace` "today" three times with the same arguments and the same result. The guard (F243 / F476) ended the
turn unanswered, the step failed, and the person saw "Something went wrong" — though nothing the model needed was
missing: it had the results, and "hi" needed none.

Now the guard's third repeat gives the model one more reply with its tools withheld (a step that finishes with a tool
keeps only that tool), told it already has the results. An answer there is the turn's answer. Only a reply that still
calls a tool ends the turn as the guard always did, and on the step's last turn there is no extra reply.
"""
import json
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from test_human_in_containers import _run, driver, worker  # noqa: F401 — fixtures
from test_an_agent_step_with_no_answer_fails import AGENT, _LOOK, _agent_step, _print, _seed, _entry, tools  # noqa: F401

LOOK = tool_call("look", where="today")


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _chat(fake_llm, script, *, max_turns=6, finish=None, tools_override=None):
    from core.react_engine import run_agent_step

    async def same(name, args):
        return json.dumps({"runs": 0, "window": "the last 1 days"}) if name == "look" else \
            json.dumps({"status": "accepted"})

    agent = S.make_agent(tools=["all"], skip_default_tools=True)
    fake_llm.script(script)
    kwargs = dict(message="hi", agent_id=agent["id"], session_id="s_f1015", server_module=_server(),
                  agent_override=agent, tool_executor=same, max_turns=max_turns,
                  tools_override=tools_override or [_LOOK])
    if finish:
        kwargs.update(finish_tools=list(finish), source="orchestration_step")
    return [ev async for ev in run_agent_step(**kwargs)]


def _final(events):
    return next(e for e in reversed(events) if e.get("type") == "final")


class TestTheGuardAsksForTheAnswer:
    async def test_a_reply_after_three_repeats_is_the_answer(self, fake_llm):
        events = await _chat(fake_llm, [LOOK, LOOK, LOOK, "Hi! Nothing has run today. What can I help with?"])
        final = _final(events)
        assert final["response"] == "Hi! Nothing has run today. What can I help with?"
        assert "no_answer" not in final
        assert not [e for e in events if e.get("type") == "repeat_stopped"], "answered, so never 'stopped'"
        asked = fake_llm.calls[3]
        assert "Give your final response now" in asked["prompt_msg"]
        assert not asked.get("tools"), "the answer turn offers no tool"

    async def test_a_repeated_round_is_asked_too(self, fake_llm):
        both = "\n".join(json.dumps({"tool": "look", "arguments": {"where": w}}) for w in ("today", "inbox"))
        final = _final(await _chat(fake_llm, [both, both, both, "Here is what I found."]))
        assert final["response"] == "Here is what I found." and "no_answer" not in final

    async def test_calling_again_in_the_answer_turn_ends_as_the_guard_did(self, fake_llm):
        events = await _chat(fake_llm, [LOOK] * 4 + ["never reached"])
        final = _final(events)
        assert final["response"].startswith("I stopped here because I was repeating myself")
        assert final["no_answer"]["why"] == "repeating" and final["no_answer"]["count"] == 3
        assert fake_llm.call_count == 4, "one answer turn, then nothing"
        assert [e for e in events if e.get("type") == "repeat_stopped"]

    async def test_on_the_last_turn_there_is_no_extra_reply(self, fake_llm):
        events = await _chat(fake_llm, [LOOK, LOOK, LOOK, "never reached"], max_turns=3)
        assert fake_llm.call_count == 3
        assert _final(events)["no_answer"]["why"] == "repeating"


class TestAStepThatFinishesWithATool:
    SUBMIT = {"type": "function", "function": {"name": "submit", "description": "Submit.",
                                               "parameters": {"type": "object", "properties": {
                                                   "text": {"type": "string"}}}}}

    async def test_the_answer_turn_offers_only_the_finish_tool_and_its_call_finishes(self, fake_llm):
        events = await _chat(fake_llm, [LOOK, LOOK, LOOK, tool_call("submit", text="done")],
                             finish=["submit"], tools_override=[_LOOK, self.SUBMIT])
        final = _final(events)
        assert final.get("finish", {}).get("tool") == "submit" and "no_answer" not in final
        offered = [(t.get("function") or {}).get("name") for t in fake_llm.calls[3].get("tools") or []]
        assert offered == ["submit"], "only the finish tool in the answer turn"
        assert "by calling `submit`" in fake_llm.calls[3]["prompt_msg"]


class TestTheStepNoLongerFails:
    async def test_an_agent_step_that_answers_after_the_guard_completes(self, driver, fake_llm, tools):
        orch = S.make_orchestration(id="orch_f1015", entry_step_id="write", steps=[
            _agent_step(nxt="after"), _print("after")])
        await _seed(orch)
        fake_llm.script([tool_call("look", where="vault")] * 3 + ["Tides rise twice a day."])

        assert await driver.start(orch, "run_f1015") == "completed"
        run = await _run("run_f1015")
        assert run.shared_state.get("draft") == "Tides rise twice a day."
        assert "o_after" in run.shared_state, "the next step ran"
        assert _entry(run)["status"] != "failed"
