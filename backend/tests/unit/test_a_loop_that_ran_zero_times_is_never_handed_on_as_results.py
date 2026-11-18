"""A Loop that ran zero times is never handed on as results (F1622).

A Loop over an empty list (a legitimate answer: the run carries on) used to hand the steps after it an empty object
and an empty read: the step after it was asked "Sum up this research:  (gathered: {})" with nothing saying the Loop
never ran, and wrote a summary. Worse, a body step's key that already held a value (an earlier pass, an earlier
writer, a default) was left as it was and handed on as this run's results.

Now a Loop that ran zero times sets each body step's key to `[]`, the shape it holds after its iterations with none,
so nothing from before survives as its result; and where a model or a person reads one of its keys (a prompt, the
step's context, a Print) it reads as "the Loop '<name>' ran 0 times: <why>". A condition, code and set arguments
read the data, `[]`. The run is not failed. A fallback a step before the Loop wrote to the body's key is not kept
under it either: the Loop's answer is that it ran zero times, and a fallback the person wants goes after the Loop.

Near misses: a Loop that ran once hands on its results as before; the note never reaches the run's public state.
"""
from __future__ import annotations

import types

from _fakes import seed as S

SAID = "ran 0 times"


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _llm(sid: str, prompt: str, nxt: str | None, **more) -> dict:
    return {"id": sid, "name": sid.title(), "type": "llm", "model": "gpt-4o", "prompt_template": prompt,
            "output_key": sid, "next_step_id": nxt, **more}


FINDER = _llm("finder", "Find keywords for {{ state.user_input }}.", "each",
              output_fields=[{"name": "keywords", "type": "list", "description": "The keywords"}])
LOOP = {"id": "each", "name": "Research loop", "type": "loop", "loop_over": "state.finder.keywords",
        "loop_step_ids": ["research"], "output_key": "gathered", "next_step_id": "summary"}
RESEARCH = _llm("research", "Research the keyword {{ state.loop_item }}.", None)
SUMMARY = _llm("summary", "Sum up this research: {{ state.research }} (gathered: {{ state.gathered }})", None,
               input_keys=["research", "gathered"])


def _workflow(*steps: dict, entry: str = "finder", schema: dict | None = None) -> dict:
    orch = S.make_orchestration(id="orch_f1622", entry_step_id=entry, steps=list(steps))
    if schema:
        orch["state_schema"] = schema
    return orch


async def _ran(orch: dict) -> tuple[list[dict], dict]:
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    await S.seed_orchestrations([orch])
    engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
    events = [e async for e in engine.run("garden furniture", run_id="run_f1622")]
    return events, next(e for e in reversed(events) if e.get("type") == "orchestration_complete")


def _prompt(fake_llm) -> str:
    return str(fake_llm.calls[-1].get("prompt_msg") or "")


class TestTheStepAfterItIsTold:
    async def test_its_prompt_says_the_loop_ran_zero_times_never_an_empty_object(self, fake_llm):
        fake_llm.script(['{"keywords": []}', "Nothing was researched."])
        _, done = await _ran(_workflow(FINDER, LOOP, RESEARCH, SUMMARY))
        assert done["status"] == "completed", done
        prompt = _prompt(fake_llm)
        task = prompt[prompt.index("## TASK"):]
        assert SAID in task and "(gathered: {})" not in task, task
        context = prompt[prompt.index("## CONTEXT FROM PREVIOUS STEPS"):prompt.index("## TASK")]
        assert "### research" in context and context.count(SAID) >= 2, context
        assert "its list (state.finder.keywords) was empty" in prompt, prompt

    async def test_an_earlier_value_of_the_body_key_is_never_handed_on(self, fake_llm):
        fake_llm.script(['{"keywords": []}', "Nothing was researched."])
        schema = {"research": {"type": "string", "default": "Earlier research: teak bench has 12 suppliers."}}
        _, done = await _ran(_workflow(FINDER, LOOP, RESEARCH, SUMMARY, schema=schema))
        assert "teak bench" not in _prompt(fake_llm), _prompt(fake_llm)
        assert done["final_state"]["research"] == [], done["final_state"].get("research")

    async def test_a_fallback_written_before_the_loop_is_not_kept_under_it(self, fake_llm):
        """The Loop's answer is that it ran zero times: a fallback the person wants goes after the Loop."""
        fallback = _llm("research", "Say there is no research yet.", "finder")
        body = _llm("research_one", "Research the keyword {{ state.loop_item }}.", None, output_key="research")
        loop = {**LOOP, "loop_step_ids": ["research_one"]}
        fake_llm.script(["No research needed.", '{"keywords": []}', "Nothing was researched."])
        _, done = await _ran(_workflow(fallback, FINDER, loop, body, SUMMARY, entry="research"))
        assert "No research needed." not in _prompt(fake_llm) and SAID in _prompt(fake_llm), _prompt(fake_llm)
        assert done["final_state"]["research"] == []

    async def test_a_print_after_it_shows_the_sentence(self, fake_llm):
        show = {"id": "show", "name": "Show", "type": "print", "print_content": "Research: {{ state.research }}",
                "output_key": "shown"}
        fake_llm.script(['{"keywords": []}'])
        _, done = await _ran(_workflow(FINDER, {**LOOP, "next_step_id": "show"}, RESEARCH, show))
        assert SAID in done["final_state"]["shown"], done["final_state"]["shown"]

    async def test_a_condition_reads_the_empty_list_and_takes_its_branch(self, fake_llm):
        check = {"id": "check", "name": "Anything?", "type": "if_else", "if_condition": "len(state.research) == 0",
                 "if_true_step_id": "none", "if_false_step_id": "summary"}
        none = _llm("none", "Say that nothing was found.", None)
        fake_llm.script(['{"keywords": []}', "Nothing was found."])
        events, done = await _ran(_workflow(FINDER, {**LOOP, "next_step_id": "check"}, RESEARCH, check, none, SUMMARY))
        assert done["status"] == "completed", done
        assert "none" in done["final_state"] and "summary" not in done["final_state"], done["final_state"]

    async def test_a_source_never_set_on_this_path_says_so(self, fake_llm):
        fake_llm.script(['{"keywords": []}', "Nothing."])
        _, _ = await _ran(_workflow(FINDER, {**LOOP, "loop_over": "state.nowhere"}, RESEARCH, SUMMARY))
        assert "state.nowhere was never set on this path" in _prompt(fake_llm), _prompt(fake_llm)

    async def test_a_while_loop_false_from_the_start_says_so(self, fake_llm):
        fake_llm.script(['{"keywords": []}', "Nothing."])
        loop = {k: v for k, v in LOOP.items() if k != "loop_over"} | {"loop_while_condition": "False"}
        _, _ = await _ran(_workflow(FINDER, loop, RESEARCH, SUMMARY))
        assert "its condition was false from the start" in _prompt(fake_llm), _prompt(fake_llm)


class TestNearMisses:
    async def test_a_loop_that_ran_once_hands_on_its_results_as_before(self, fake_llm):
        fake_llm.script(['{"keywords": ["teak bench"]}', "Teak bench: 12 suppliers.", "A summary."])
        _, done = await _ran(_workflow(FINDER, LOOP, RESEARCH, SUMMARY))
        assert SAID not in _prompt(fake_llm) and "Teak bench: 12 suppliers." in _prompt(fake_llm)
        assert [e["result"] for e in done["final_state"]["research"]] == ["Teak bench: 12 suppliers."]

    async def test_the_note_never_reaches_the_public_state(self, fake_llm):
        from core.events_public import public_state

        fake_llm.script(['{"keywords": []}', "Nothing."])
        _, done = await _ran(_workflow(FINDER, LOOP, RESEARCH, SUMMARY))
        assert not any(str(k).startswith("_loop") for k in public_state(done["final_state"])), done["final_state"]
