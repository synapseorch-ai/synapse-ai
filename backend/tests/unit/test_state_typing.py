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


def _host_transforms(monkeypatch):
    """Run transform steps in the host subprocess runtime — no Docker needed."""
    import core.config as config
    monkeypatch.setattr(config, "load_settings",
                        lambda: {"transform_runtime": "host", "model": "mistral"})


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


class TestUnsetIsNotATypeError:
    """`_type_written_state` is a plain function, so the rule is tested directly.

    Going through a real run cannot express this: a `transform` writing None
    stores `''`, which is a perfectly good `str` and proves nothing.
    """

    @staticmethod
    def _apply(schema, shared_state, output_key="note"):
        from core.models_orchestration import Orchestration, StepConfig
        from core.orchestration.engine import OrchestrationEngine
        orch = Orchestration.model_validate(
            S.make_orchestration(id="o", state_schema=schema))
        engine = OrchestrationEngine(orch, _server())
        step = StepConfig(id="s1", name="Writer", type="print", output_key=output_key)
        run = types.SimpleNamespace(shared_state=dict(shared_state))
        warnings, unconvertible = engine._type_written_state(run, step, {})
        return run.shared_state, [w["message"] for w in warnings], unconvertible

    def test_none_under_a_declared_type_is_silent(self):
        # 17 of 23 mismatched runs on the dev stack were this: the AI builder's
        # own `_model_override`, declared `str` and set to None when there is no
        # override. "Unset" is not a failed conversion and must not read as one.
        state, warnings, blocking = self._apply({"note": {"type": "str"}}, {"note": None})
        assert state["note"] is None
        assert warnings == [] and blocking == []

    def test_none_is_silent_under_a_structure_too(self):
        state, warnings, blocking = self._apply({"note": {"type": "list"}}, {"note": None})
        assert state["note"] is None
        assert warnings == [] and blocking == []

    def test_a_wrong_type_that_is_not_text_warns_but_does_not_block(self):
        _, warnings, blocking = self._apply({"note": {"type": "str"}}, {"note": {"a": 1}})
        assert len(warnings) == 1 and blocking == []

    def test_text_that_will_not_parse_warns_and_blocks(self):
        _, warnings, blocking = self._apply({"note": {"type": "list"}}, {"note": "nope"})
        assert len(warnings) == 1 and blocking == [("note", "list")]


def _strict(text, strict, key="rows", declared="list"):
    return S.make_orchestration(
        id="orch_strict", entry_step_id="s1",
        steps=[
            {"id": "s1", "name": "Writer", "type": "print", "print_content": text,
             "output_key": key, "next_step_id": "s2"},
            {"id": "s2", "name": "After", "type": "print", "print_content": "ran",
             "output_key": "tail", "next_step_id": None},
        ],
        state_schema={key: {"type": declared}},
        strict_state_types=strict,
    )


def _errors(events):
    return [e["error"] for e in events if e.get("type") == "orchestration_error"]


def _status(events):
    done = [e for e in events if e.get("type") == "orchestration_complete"]
    return done[-1].get("status") if done else None


class TestStrictStateTypes:
    """The guardrail: text that will not become its declared structure.

    Off by default, because until this was built `type` was read by nothing —
    every type value in every orchestration is unvalidated metadata, and 21% of
    runs on the dev stack already hold something that contradicts one.
    """

    async def test_off_by_default(self):
        from core.models_orchestration import Orchestration
        assert Orchestration(id="o", name="O").strict_state_types is False

    async def test_lenient_warns_and_carries_on(self):
        events = await _run(_strict("I could not find any queries.", strict=False))
        assert _errors(events) == []
        assert _final_state(events)["tail"] == "ran"

    async def test_strict_stops_the_run_and_names_the_key(self):
        events = await _run(_strict("I could not find any queries.", strict=True))
        assert len(_errors(events)) == 1
        assert "rows" in _errors(events)[0] and "list" in _errors(events)[0]
        # and the step after it never ran
        assert "tail" not in _final_state(events)

    async def test_strict_lets_convertible_text_through(self):
        events = await _run(_strict('["a", "b"]', strict=True))
        assert _errors(events) == []
        assert _final_state(events)["rows"] == ["a", "b"]
        assert _final_state(events)["tail"] == "ran"

    async def test_strict_still_records_the_step_that_completed(self):
        # The step did its work; it is the typing that failed. A run whose
        # history quietly loses the step is a worse record than one that says
        # "this ran, and then the run stopped because of what it wrote".
        events = await _run(_strict("not json", strict=True))
        started = [e for e in events if e.get("type") == "step_complete"]
        assert [e["orch_step_id"] for e in started] == ["s1"]

    async def test_strict_does_not_stop_for_a_mismatch_that_is_not_text(self, monkeypatch):
        # A dict under a key declared `str` is a wrong schema, not a failed
        # conversion — it still warns, but it is not what this flag is for.
        _host_transforms(monkeypatch)
        orch = S.make_orchestration(
            id="orch_strict_nontext", entry_step_id="s1",
            steps=[
                {"id": "s1", "name": "Writer", "type": "transform",
                 "transform_code": "result = {'a': 1}", "output_key": "note",
                 "next_step_id": "s2"},
                {"id": "s2", "name": "After", "type": "print", "print_content": "ran",
                 "output_key": "tail", "next_step_id": None},
            ],
            state_schema={"note": {"type": "str"}},
            strict_state_types=True,
        )
        events = await _run(orch)
        assert _errors(events) == []
        assert len(_warnings(events)) == 1
        assert _final_state(events)["tail"] == "ran"
