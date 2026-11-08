"""The planner and the step panel say what a code step reads (F1359's words, TRI1; CAP3's FF18 gate, cap-05; Naveen
11-04: "Build as planned"; the tool clause is BLD1's, F1358's contract).

cap-05's planner asked a person to "paste the refund request", though the request was the run's own message, then read
the Human step's answer, an object, as text: nothing it was told named the run's message or what a Human step answers.
The planner's step guide (plan_steps.md, through the prompt library) now says: the run's own message is
`state.user_input`, read there and never asked of a person again; a Human step answers an object, `{approved, feedback}`,
or its `human_fields` by name, so text from a person is a field (what a schedule's run asks its person for); a code step
reads a Human step's answer by its field; a Tool step's JSON is parsed for a code step that reads its fields, otherwise it
is the tool's text. The step panel's Transform note says the same for a workflow written by hand, where the tool clause
is the state schema's. Read as the planner reads it, and from the panel's source.
"""
from __future__ import annotations

import re
from pathlib import Path

PANEL = Path(__file__).resolve().parents[3] / "frontend/src/components/orchestration/panel/steps.tsx"


def _planner() -> str:
    from core.native_builder import prompt_lint

    return " ".join(prompt_lint.expanded_places()["agent:agent_native_builder_planner"].split())


def _sentence(text: str, *words: str) -> str:
    """The first sentence of `text` that holds every one of `words`."""
    for sentence in re.split(r"(?<=[.;])\s+", text):
        if all(w in sentence for w in words):
            return sentence
    raise AssertionError(f"no sentence holds {words}")


class TestThePlanner:
    def test_the_runs_own_message_is_state_user_input_never_asked_again(self):
        said = _sentence(_planner(), "own message", "`state.user_input`")
        assert "never" in said or "no step asks" in said, said

    def test_a_human_steps_answer_is_an_object_and_text_from_a_person_is_a_field(self):
        text = _planner()
        assert "{approved, feedback}" in _sentence(text, "human_fields", "{approved, feedback}")
        assert "schedule" in _sentence(text, "human_fields", "for text")

    def test_a_code_step_reads_a_human_answer_by_its_field_and_a_tools_json_when_it_reads_fields(self):
        text = _planner()
        code = text[text.index("- `transform`:"):]
        assert "read by its field" in code and "never as text" in code
        assert "parsed for you" in code and "otherwise it is the tool's text" in code
        assert "a `tool` step writes an object" not in text


class TestThePanel:
    def test_the_transform_note_says_what_its_code_reads(self):
        note = " ".join(PANEL.read_text()[PANEL.read_text().index("function TransformSection("):].split())[:2400]
        assert "state.user_input" in note
        assert "{approved, feedback}" in note and "read a field" in note
        assert "state schema declares its key one" in note and "json.loads(v)" in note
