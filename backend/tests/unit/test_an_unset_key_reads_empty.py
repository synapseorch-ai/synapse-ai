"""A `{state.key}` no step has written yet reads empty in a model's prompt (U74).

Measured on the fleet (C2, a feedback loop: writer → evaluator → a note → back):
the writer's first prompt carried a literal `{state.feedback}` — the note had not
run yet, so the key was unset and the reference was left as written — and the
model echoed it into its draft.

Naveen's call (D267): in the prompts a model reads (Agent, LLM, Tool, Retrieve)
an unset state key reads empty, in either form; a declared default already
fills it. A Human or a Print step's text, which a person reads, keeps the gap,
and so does `{{ global.x }}` and a wrong path inside a key that is set. The
builder's validator and the editor warn, without blocking, that a step reads a
key before any step writes it.
"""
import pytest

from core.orchestration.branch_keys import unset_reads


def _fill(template, state):
    from core.orchestration.context import _fill_task_template
    return _fill_task_template(template, state, {"KEY": "k"})


class TestRendering:
    @pytest.mark.parametrize(("template", "expected"), [
        ("Improve it. {state.feedback}", "Improve it. "),
        ("Improve it. {{ state.feedback }}", "Improve it. "),
        ("Improve it. {{state.feedback}}", "Improve it. "),
        ("Was: {state.none}", "Was: "),
        ("{{ global.missing }}", "{{ global.missing }}"),
        ("{{ state.d.missing }}", "{{ state.d.missing }}"),
        ("{state.d}", "{'a': 1}"),
        ("{{ state.n }}", "0"),
        ("Asked: {{ state.blank.question }}", "Asked: "),
        ("Answer: {{ state.empty.answer }}", "Answer: "),
        ("{{ state.none.x }}", ""),
        ("{state.d.a}", "1"),
        ("{state.blank.question}", ""),
        ("{state.d.missing}", "{state.d.missing}"),
        ("{state.trap.say}", "{{ global.KEY }}"),
    ], ids=["legacy", "spaced", "unspaced", "none-value", "global-keeps-the-gap",
            "wrong-path-keeps-the-gap", "set-dict", "falsy-zero-is-set", "field-of-an-empty-default",
            "field-of-an-empty-dict", "field-of-none", "legacy-path", "legacy-path-of-an-empty-default",
            "legacy-wrong-path-keeps-the-gap", "legacy-path-is-one-pass"])
    def test_in_a_models_prompt(self, template, expected):
        """A field of a key that holds only its empty default reads empty too (SAI2-P2): it is not written yet, as
        an unset key is not, and a second pass's "what you were told" read as template text on the first pass."""
        assert _fill(template, {"d": {"a": 1}, "none": None, "n": 0, "blank": "", "empty": {},
                                "trap": {"say": "{{ global.KEY }}"}}) == expected

    def test_a_print_keeps_the_gap(self):
        """A person reads a Print or a Human step's text; the gap shows them."""
        from core.orchestration.steps import render_state_refs
        assert render_state_refs("Hello {state.name}!", {}) == "Hello {state.name}!"


def _orch(steps, entry="write", schema=None):
    return {"id": "o", "entry_step_id": entry, "steps": steps, "state_schema": schema or {}}


def _feedback_loop(schema=None):
    return _orch([
        {"id": "write", "name": "Write", "type": "agent", "agent_id": "a",
         "prompt_template": "Write a slogan. {state.feedback}", "output_key": "draft", "next_step_id": "judge"},
        {"id": "judge", "name": "Judge", "type": "evaluator", "route_map": {"pass": "done", "fail": "note"}},
        {"id": "note", "name": "Note", "type": "llm", "prompt_template": "Why did {state.draft} fail?",
         "output_key": "feedback", "next_step_id": "write"},
        {"id": "done", "name": "Done", "type": "end"},
    ], schema=schema)


class TestTheWarning:
    def test_a_feedback_loops_first_pass(self):
        assert unset_reads(_feedback_loop()) == [
            "Step 'write' reads {state.feedback} before any step writes it, so its first pass reads it "
            "empty. Give 'feedback' a default in the state schema if it should read something."]

    def test_a_declared_key_is_not_unset(self):
        assert unset_reads(_feedback_loop(schema={"feedback": {"type": "str", "default": ""}})) == []
        assert unset_reads(_feedback_loop(schema={"feedback": {"type": "str"}})) == [], \
            "a declared key may come from the run's caller"

    def test_a_key_nothing_writes(self):
        orch = _orch([{"id": "write", "type": "llm", "prompt_template": "{{ state.tone }}", "next_step_id": None}])
        assert unset_reads(orch) == [
            "Step 'write' reads {state.tone}, which no step writes, so it always reads empty. "
            "Write it in an earlier step, or give it a default in the state schema."]

    def test_written_before_it_is_fine(self):
        orch = _orch([
            {"id": "a", "type": "llm", "prompt_template": "x", "output_key": "topic", "next_step_id": "b"},
            {"id": "b", "type": "agent", "prompt_template": "About {state.topic}", "next_step_id": None},
        ], entry="a")
        assert unset_reads(orch) == []

    def test_the_engines_own_keys_and_a_loops(self):
        orch = _orch([
            {"id": "lp", "type": "loop", "loop_over": "state.items", "loop_step_ids": ["w"], "next_step_id": None},
            {"id": "w", "type": "llm", "prompt_template": "{state.user_input} {state.loop_item} "
                                                           "{state.loop_index} {state.history}"},
        ], entry="lp")
        assert unset_reads(orch) == []

    def test_a_loop_bodys_later_step_is_unset_on_its_first_iteration(self):
        orch = _orch([
            {"id": "lp", "type": "loop", "loop_count": 3, "loop_step_ids": ["w", "n"], "next_step_id": "after"},
            {"id": "w", "type": "llm", "prompt_template": "Better than: {state.last}", "output_key": "draft"},
            {"id": "n", "type": "transform", "transform_code": "result = 1", "output_key": "last"},
            {"id": "after", "type": "llm", "prompt_template": "Final: {state.last}", "next_step_id": None},
        ], entry="lp")
        assert [w.split(" reads")[0] for w in unset_reads(orch)] == ["Step 'w'"], \
            "the body writes it before the step after the loop"

    def test_a_parallels_next_step_waits_for_every_branch(self):
        orch = _orch([
            {"id": "fan", "type": "parallel", "parallel_branches": [["a"], ["b"]], "next_step_id": "m"},
            {"id": "a", "type": "llm", "prompt_template": "x", "output_key": "one"},
            {"id": "b", "type": "llm", "prompt_template": "y", "output_key": "two"},
            {"id": "m", "type": "llm", "prompt_template": "{state.one} {state.two}", "next_step_id": None},
        ], entry="fan")
        assert unset_reads(orch) == []

    def test_a_retrieve_query(self):
        orch = _orch([{"id": "r", "type": "retrieve", "retrieve": {"query": "{state.topic}"}, "next_step_id": None}],
                     entry="r")
        assert len(unset_reads(orch)) == 1

    def test_a_human_or_print_step_is_not_warned(self):
        orch = _orch([{"id": "p", "type": "print", "print_content": "{state.x}", "next_step_id": "h"},
                      {"id": "h", "type": "human", "human_prompt": "{state.y}", "next_step_id": None}], entry="p")
        assert unset_reads(orch) == []


class TestTheBuildersValidator:
    def test_it_warns_without_blocking(self):
        from core.builder_tools import _validate_orchestration
        verdict = _validate_orchestration(_feedback_loop())
        assert verdict["ok"] is True or verdict["issues"], verdict
        assert verdict["warnings"] == unset_reads(_feedback_loop())
        assert not any("feedback" in i for i in verdict["issues"])


def test_no_model_step_of_the_shipped_builder_reads_a_reference_as_text():
    """The class this file is about, on Synapse AI as shipped: every model step's prompt, rendered on the
    definition's own defaults (a first pass), leaves no state reference as text. It found the change planner's
    second-pass lines on its first pass, and two single-brace paths (`{state.review.final_summary}`,
    `{state.plan_check.research}`) that reached the drafter and the build's researcher as words on every build."""
    import re

    from core.models_orchestration import Orchestration
    from core.native_builder.seed import bundled_definitions
    from core.orchestration.context import _fill_task_template

    orch = Orchestration.model_validate(bundled_definitions()["orchestration"])
    state = {k: (v or {}).get("default") for k, v in (orch.state_schema or {}).items()}
    left = {s.id: re.findall(r"\{\{?\s*state\.[\w.]+\s*\}?\}", _fill_task_template(s.prompt_template, state, {}))
            for s in orch.steps if s.type.value in ("agent", "llm", "evaluator", "tool") and s.prompt_template}
    assert {k: v for k, v in left.items() if v} == {}
