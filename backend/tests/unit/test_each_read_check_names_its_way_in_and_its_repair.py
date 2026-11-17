"""Each read check names the way a run reaches a step without what it reads, and the repair, word for word (F1621,
part 0).

What a step reads is checked by several rules: a Transform's code, a Loop's list and a Tool step's set arguments are
refused when a run can reach them with the key unset (F1346, F1617), naming the step that leads there and the way it
takes; a model's prompt reading a key before any step writes it is a warning (U74); a set argument reading a key no
step writes is one issue, said by the Tool-arguments rule alone (E1). The builder's plan check reads two of the
validator's words to give the planner its own hint (`build_plan._HINTS`: "never write", "does not declare in its
output_fields").

This file holds every one of those sentences as they read, so moving the checks onto one walk of the workflow changes
none of them: each way a run can take (an If/Else's true and false branch, a Switch's case and default, an Evaluator's
route, the way when a step's tries run out, a Loop's body, a Parallel's branch, a straight line), each reader's
"breaks" and "repair", and the hints' two words.
"""
from __future__ import annotations

import pytest

from core.builder_tools import _validate_orchestration
from core.native_builder.build_plan import _HINTS, _hinted
from core.orchestration.branch_keys import missing_key_reads, unset_reads

READ = "result = state.k"


def _transform(id: str, nxt: str | None = None, code: str = "result = 1", out: str | None = None, **more) -> dict:
    step = {"id": id, "name": id, "type": "transform", "transform_code": code, "next_step_id": nxt}
    if out:
        step["output_key"] = out
    return {**step, **more}


def _orch(entry: str, *steps: dict, schema: dict | None = None) -> dict:
    return {"id": "orch_reads", "name": "Harbor & Pine reads", "entry_step_id": entry, "state_schema": schema or {},
            "steps": [*steps, {"id": "end", "name": "End", "type": "end"}]}


def _writer(nxt: str | None = "reader") -> dict:
    return _transform("writer", nxt, out="k")


def _reader(kind: str = "transform") -> dict:
    if kind == "loop":
        return {"id": "reader", "name": "reader", "type": "loop", "loop_over": "state.k", "loop_step_ids": ["each"],
                "next_step_id": "end"}
    if kind == "tool":
        return {"id": "reader", "name": "reader", "type": "tool", "tool_name": "web_search",
                "tool_arguments": {"q": "{{ state.k }}"}, "output_key": "r", "next_step_id": "end"}
    return _transform("reader", "end", code=READ, out="r")


def _if(true: str, false: str) -> dict:
    return {"id": "cond", "name": "cond", "type": "if_else", "if_condition": "1 == 1", "if_true_step_id": true,
            "if_false_step_id": false}


EACH = _transform("each", out="e")

CODE_STOPS = "so its code stops there (no key 'k'). "
CODE_FIX = "read it with state.get('k'), or give it a default in the state schema."
LOOP_STOPS = "so it loops over nothing there and the run goes on as if it had. "
LOOP_FIX = "an empty list where there is nothing, or give it a default of [] in the state schema."


def _reached(way: str, stops: str = CODE_STOPS, fix: str = CODE_FIX) -> str:
    return f"Step 'reader' reads state.k, but a run reaches it {way}, without 'k' set, {stops}Write it on that way too, {fix}"


BEFORE_ANY = f"Step 'reader' reads state.k, but a run can reach it before any step writes it, {CODE_STOPS}Write it on that way too, {CODE_FIX}"

WAYS = {
    "an If/Else's true branch": (_orch("cond", _if("reader", "writer"), _writer(), _reader()),
                                 _reached("from 'cond' by its true branch")),
    "an If/Else's false branch": (_orch("cond", _if("writer", "reader"), _writer(), _reader()),
                                  _reached("from 'cond' by its false branch")),
    "a Switch's default case": (
        _orch("pick", {"id": "pick", "name": "pick", "type": "switch", "switch_expression": "state.user_input",
                       "switch_cases": {"a": "writer"}, "switch_default_step_id": "reader"}, _writer(), _reader()),
        _reached("from 'pick' by its default case")),
    "a Switch's case": (
        _orch("pick", {"id": "pick", "name": "pick", "type": "switch", "switch_expression": "state.user_input",
                       "switch_cases": {"a": "writer", "b": "reader"}, "switch_default_step_id": "writer"},
              _writer(), _reader()),
        _reached("from 'pick' by its 'b' case")),
    "an Evaluator's route": (
        _orch("judge", {"id": "judge", "name": "judge", "type": "evaluator", "evaluator_prompt": "Is it ready?",
                        "route_map": {"yes": "writer", "no": "reader"}}, _writer(), _reader()),
        _reached("from 'judge' by its 'no' route")),
    "the way when its tries run out": (
        _orch("tryit", _transform("tryit", "writer", out="x", on_exhausted_step_id="reader"), _writer(), _reader()),
        _reached("from 'tryit' by its way when its tries run out")),
    "a branch, through a step": (_orch("cond", _if("writer", "mid"), _transform("mid", "reader", out="m"),
                                       _writer(), _reader()),
                                 _reached("from 'cond' by its false branch, through 'mid'")),
    "a straight line": (_orch("reader", _transform("reader", "writer", code=READ, out="r"),
                              _transform("writer", "end", out="k")), BEFORE_ANY),
    "a Loop's body": (_orch("each_one", {"id": "each_one", "name": "each_one", "type": "loop", "loop_count": 2,
                                         "loop_step_ids": ["reader", "writer"], "next_step_id": "end"},
                            _transform("reader", code=READ, out="r"), _transform("writer", out="k")), BEFORE_ANY),
    "a Parallel's branch": (_orch("both", {"id": "both", "name": "both", "type": "parallel",
                                           "parallel_branches": [["reader"], ["writer"]], "next_step_id": "end"},
                                  _transform("reader", code=READ, out="r"), _transform("writer", out="k")),
                            _reached("from 'both' by its branch 1")),
}


@pytest.mark.parametrize("way", list(WAYS))
def test_a_read_a_run_reaches_unset_names_the_way_it_takes(way):
    orch, said = WAYS[way]
    assert missing_key_reads(orch) == [said]
    assert _validate_orchestration(orch)["issues"].count(said) == 1


READERS = {
    "a Loop's list, on a way": (
        _orch("cond", _if("writer", "reader"), _writer(), _reader("loop"), EACH),
        _reached("from 'cond' by its false branch", LOOP_STOPS, LOOP_FIX)),
    "a Tool step's set arguments, on a way": (
        _orch("cond", _if("writer", "reader"), _writer(), _reader("tool")),
        _reached("from 'cond' by its false branch", "so its arguments cannot be set there and the run stops. ",
                 "or write one key every arm writes (each arm's step answering the same output_key) and read that, "
                 "or do this step in each arm.")),
    "a Transform's code, a key no step writes": (
        _orch("reader", _reader()),
        f"Step 'reader' reads state.k, which no step writes, {CODE_STOPS}Write it in an earlier step, {CODE_FIX}"),
    "a Loop's list, a key no step writes": (
        _orch("reader", _reader("loop"), EACH),
        f"Step 'reader' reads state.k, which no step writes, {LOOP_STOPS}Write it in an earlier step, {LOOP_FIX}"),
}


@pytest.mark.parametrize("reader", list(READERS))
def test_each_reader_says_what_breaks_and_its_repair(reader):
    orch, said = READERS[reader]
    assert missing_key_reads(orch) == [said]
    assert _validate_orchestration(orch)["issues"].count(said) == 1


class TestASetArgumentReadingAKeyNoStepWrites:
    def test_is_one_issue_said_by_the_tool_arguments_rule(self):
        orch = _orch("reader", _reader("tool"))
        assert missing_key_reads(orch) == [], "the branch rule leaves a key no step writes to the Tool-arguments rule"
        about = [i for i in _validate_orchestration(orch)["issues"] if "state.k" in i]
        assert about == ["Tool step 'reader' sets an argument from 'state.k', but nothing writes 'k' — give an earlier "
                         "step 'k' as its output_key, or declare it in the state schema."]

    def test_arguments_that_are_no_object_are_said(self):
        orch = _orch("reader", {**_reader("tool"), "tool_arguments": "q=state.k"})
        assert ("Tool step 'reader' has tool_arguments that are not an object of argument → value."
                in _validate_orchestration(orch)["issues"])


def test_a_prompt_reading_a_key_before_its_writer_is_a_warning_only():
    orch = _orch("ask", {"id": "ask", "name": "ask", "type": "llm", "prompt_template": "Say {{ state.k }}",
                         "output_key": "a", "next_step_id": "writer"}, _transform("writer", "end", out="k"))
    said = ("Step 'ask' reads {{ state.k }} before any step writes it, so its first pass reads it empty. Give 'k' a "
            "default in the state schema if it should read something.")
    verdict = _validate_orchestration(orch)
    assert unset_reads(orch) == [said] and verdict["warnings"].count(said) == 1
    assert not [i for i in verdict["issues"] if "state.k" in i]


# ── the plan check's hints read the validator's words ────────────────────────────────────────────────────────────

def _field_read(writer: dict) -> dict:
    return _orch(writer["id"], {**writer, "next_step_id": "cond"},
                 {"id": "cond", "name": "cond", "type": "if_else", "if_condition": f"state.{writer['id']}.urgent",
                  "if_true_step_id": "end", "if_false_step_id": "end"})


HINTED = {
    "never write": _field_read({"id": "review", "name": "review", "type": "human", "human_prompt": "Is it urgent?",
                                "output_key": "review"}),
    "does not declare in its output_fields": _field_read(
        {"id": "judge", "name": "judge", "type": "llm", "prompt_template": "Judge the ticket", "output_key": "judge",
         "output_fields": [{"name": "risky", "type": "boolean"}]}),
}


@pytest.mark.parametrize("words", list(HINTED))
def test_a_field_reads_issue_carries_the_words_its_plan_hint_reads(words):
    about = [i for i in _validate_orchestration(HINTED[words])["issues"] if "`urgent`" in i]
    assert len(about) == 1 and words in about[0], about
    assert _hinted(about[0]) == f"{about[0]} {dict(_HINTS)[words]}"


def test_a_loop_through_a_whole_answer_takes_no_plan_hint():
    orch = _orch("review", {"id": "review", "name": "review", "type": "human", "human_prompt": "Which ones?",
                            "output_key": "review", "next_step_id": "each_item",
                            "human_fields": [{"name": "items", "type": "text", "label": "Items"},
                                             {"name": "note", "type": "text", "label": "Note"}]},
                 {"id": "each_item", "name": "each_item", "type": "loop", "loop_over": "state.review",
                  "loop_step_ids": ["each"], "next_step_id": "end"}, EACH)
    about = [i for i in _validate_orchestration(orch)["issues"] if i.startswith("Loop 'each_item'")]
    assert len(about) == 1 and _hinted(about[0]) == about[0], about
