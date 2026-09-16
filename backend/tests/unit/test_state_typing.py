"""A step's raw text, typed against `state_schema` (F14).

Every step writes its output to shared state as whatever the model or the
template produced — almost always a `str`. `state_schema.type` was read by
nothing at run time (`_init_state` takes only `default`), so a step that
returned the perfectly good JSON text `["a", "b"]` into a key declared `list`
left a *string* there, and the loop reading it warned "not a list — zero
iterations" and ran nothing.

The typing happens at one seam — `engine._execute_loop`, after a step completes
— because that is the line every path reaches: run, resume, resume-failed,
tool-approval resume, nested resume, and the fleet's own adapter.

Bodies here are `print` and `transform` steps, so nothing needs an LLM.
"""
import types

import pytest

from _fakes import seed as S


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _engine(orch_dict):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    return OrchestrationEngine(Orchestration.model_validate(orch_dict), _server())


async def _run(orch_dict, initial_state=None, run_id="run_typing"):
    engine = _engine(orch_dict)
    return [ev async for ev in engine.run("go", run_id=run_id, initial_state=initial_state)]


def _final_state(events):
    return next(e for e in reversed(events)
                if e.get("type") == "orchestration_complete")["final_state"]


def _warnings(events):
    return [e["message"] for e in events if e.get("type") == "step_warning"]


def _writes(text, key="rows", schema=None, extra_steps=(), entry=None):
    """One print step writing `text` into `key`, under `schema`."""
    steps = [
        {"id": "s1", "name": "Writer", "type": "print", "print_content": text,
         "output_key": key, "next_step_id": extra_steps[0]["id"] if extra_steps else None},
        *extra_steps,
    ]
    return S.make_orchestration(
        id="orch_typing", entry_step_id=entry or "s1", steps=steps,
        state_schema=schema if schema is not None else {"rows": {"type": "list", "default": []}},
    )


class TestJsonTextBecomesItsDeclaredType:
    async def test_a_json_array_under_a_list_key_is_typed(self):
        events = await _run(_writes('["a", "b"]'))
        assert _final_state(events)["rows"] == ["a", "b"]
        assert _warnings(events) == []

    async def test_a_fenced_block_is_typed(self):
        events = await _run(_writes('Here you go:\n```json\n["a", "b"]\n```\n'))
        assert _final_state(events)["rows"] == ["a", "b"]

    async def test_prose_around_an_array_is_typed(self):
        # Strategy 3, brace matching — the model answered in a sentence.
        events = await _run(_writes('Sure! The queries are ["gold", "silver"] — good luck.'))
        assert _final_state(events)["rows"] == ["gold", "silver"]

    async def test_a_json_object_under_a_dict_key_is_typed(self):
        events = await _run(_writes('{"a": 1}', key="doc",
                                    schema={"doc": {"type": "dict", "default": {}}}))
        assert _final_state(events)["doc"] == {"a": 1}

    @pytest.mark.parametrize("text,declared,expected", [
        ("7", "int", 7),
        ("7.5", "float", 7.5),
        ("7", "float", 7),
        ("true", "bool", True),
        ("false", "bool", False),
    ])
    async def test_a_scalar_is_typed(self, text, declared, expected):
        events = await _run(_writes(text, key="n", schema={"n": {"type": declared}}))
        value = _final_state(events)["n"]
        assert value == expected and type(value) is type(expected)


class TestWhatIsLeftAlone:
    async def test_text_that_is_not_json_is_kept_and_warns(self):
        events = await _run(_writes("I could not find any queries."))
        assert _final_state(events)["rows"] == "I could not find any queries."
        warn = _warnings(events)
        assert len(warn) == 1
        # The key, the declared type, the step that wrote it, and what arrived —
        # a run log should not make anyone go and look any of those up.
        assert warn[0] == ("State key 'rows' is declared list but step 'Writer' "
                           "wrote text that is not JSON — kept as is.")

    async def test_json_of_the_wrong_shape_is_kept_and_warns(self):
        # Parsed fine — but a dict under a key declared `list` only moves the
        # type error downstream into whatever reads it.
        events = await _run(_writes('{"a": 1}'))
        assert _final_state(events)["rows"] == '{"a": 1}'
        assert len(_warnings(events)) == 1

    async def test_an_int_key_refuses_a_decimal(self):
        events = await _run(_writes("7.5", key="n", schema={"n": {"type": "int"}}))
        assert _final_state(events)["n"] == "7.5"
        assert len(_warnings(events)) == 1

    async def test_an_int_key_refuses_a_boolean(self):
        # `isinstance(True, int)` is True in Python; it must not be here.
        events = await _run(_writes("true", key="n", schema={"n": {"type": "int"}}))
        assert _final_state(events)["n"] == "true"
        assert len(_warnings(events)) == 1

    async def test_a_key_with_no_schema_entry_is_never_touched(self):
        events = await _run(_writes('["a"]', key="loose", schema={}))
        assert _final_state(events)["loose"] == '["a"]'
        assert _warnings(events) == []

    async def test_a_type_nobody_recognises_is_never_touched(self):
        # The engine does not guess. `_validate_orchestration` is what tells the
        # builder its schema is wrong.
        events = await _run(_writes('["a"]', schema={"rows": {"type": "frobnicate"}}))
        assert _final_state(events)["rows"] == '["a"]'
        assert _warnings(events) == []

    async def test_a_key_this_step_did_not_write_is_never_touched(self):
        events = await _run(
            _writes("hello", key="note", schema={"note": {"type": "str"},
                                                 "rows": {"type": "list"}}),
            initial_state={"rows": '["untouched"]'},
        )
        assert _final_state(events)["rows"] == '["untouched"]'
        assert _warnings(events) == []

    async def test_a_string_under_a_str_key_is_left_exactly_as_it_is(self):
        events = await _run(_writes('["a"]', key="note", schema={"note": {"type": "str"}}))
        assert _final_state(events)["note"] == '["a"]'
        assert _warnings(events) == []


class TestReachingTheNextStep:
    async def test_the_typed_value_is_what_a_later_step_reads(self):
        # The point of the whole thing: the next step sees a list, not text.
        events = await _run(_writes(
            '["a", "b"]',
            extra_steps=[{"id": "s2", "name": "Reader", "type": "print",
                          "print_content": "count={state.rows}", "output_key": "seen",
                          "next_step_id": None}],
        ))
        assert _final_state(events)["seen"] == "count=['a', 'b']"

    async def test_parallel_branch_outputs_are_typed(self):
        # `output_key` alone would miss these — the parallel container's own
        # output_key is not what the branches wrote.
        orch = S.make_orchestration(
            id="orch_typing_par", entry_step_id="fan",
            steps=[
                {"id": "fan", "name": "Fan", "type": "parallel",
                 "parallel_branches": [["b1"], ["b2"]], "next_step_id": None},
                {"id": "b1", "name": "B1", "type": "print", "print_content": '["a"]',
                 "output_key": "one", "next_step_id": None},
                {"id": "b2", "name": "B2", "type": "print", "print_content": '["b"]',
                 "output_key": "two", "next_step_id": None},
            ],
            state_schema={"one": {"type": "list"}, "two": {"type": "list"}},
        )
        state = _final_state(await _run(orch))
        assert state["one"] == ["a"] and state["two"] == ["b"]


class TestFailedSteps:
    async def test_a_failed_step_produces_no_typing_warning(self):
        # Nothing to say about a step that did not finish, and a run that has
        # already failed does not need a second line of noise.
        orch = S.make_orchestration(
            id="orch_typing_fail", entry_step_id="s1",
            steps=[{"id": "s1", "name": "Nope", "type": "tool",
                    "tool_name": "does_not_exist", "output_key": "rows",
                    "next_step_id": None}],
            state_schema={"rows": {"type": "list", "default": []}},
        )
        events = await _run(orch)
        assert not any("declared" in m for m in _warnings(events))
