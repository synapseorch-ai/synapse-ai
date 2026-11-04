"""A plan's code step never reads an object as text (F1359's plan check, TRI1; CAP3's FF18 gate, cap-05; Naveen 11-04:
"Build as planned").

cap-05's plan asked a person to "paste the refund request", a Human step with no fields, though the request was the
run's own message, then ran `re.search` over that step's answer in a Transform: a person's answer is an object
(`{approved, feedback}`, or its `human_fields` by name), and the run failed. The plan check now reads a Transform's code
(as compile reads it for lists and objects) and sends back any Human or Extract JSON step's answer used as text: handed
to `re`, a text method called on it, or added to text, a name assigned from it too. The planner reads why, and that the
run's own message is `state.user_input`. A field read out of the answer, `state.user_input` itself, a model step's text
and a Human step with fields (a schedule's run that asks its person for text) all pass. A Tool step's answer is F1358's.
"""
from __future__ import annotations

import pytest

from core.native_builder import build_plan

ASK = {"id": "receive_request", "type": "human", "name": "Receive request", "does": "Asks for the refund request",
       "prompt": "Paste the refund request:", "next": "parse_request"}
WRITE = {"id": "write_note", "type": "llm", "name": "Write note", "does": "Writes a note", "agent": "Writer",
         "prompt": "Write a short note about the refund.", "next": "parse_request"}


def _code(code: str) -> dict:
    return {"id": "parse_request", "type": "transform", "name": "Parse request", "does": "Reads the order number",
            "config": {"transform_code": code}, "next": "end"}


def _plan(*steps: dict) -> dict:
    return {"kind": "create", "title": "Refunds", "summary": "Reads a refund request.", "steps": list(steps),
            "agents": [{"name": "Writer", "purpose": "Writes short notes about refunds."}]}


async def _read_as_text(*steps: dict) -> list[str]:
    found = await build_plan.examine(_plan(*steps), None, ws=build_plan.Workspace(), state={"user_message": "x"})
    return [e for e in found.errors if "as text" in e]


class TestSentBack:
    @pytest.mark.parametrize("code", [
        "import re\ntext = state.receive_request\nm = re.search(r'(SO-\\d+)', text)\nresult = m.group(1) if m else ''",
        "import re\nresult = re.findall(r'SO-\\d+', state['receive_request'])",
        "result = state.receive_request.split(',')",
        "result = 'Request: ' + state.get('receive_request')",
    ])
    async def test_a_persons_answer_read_as_text(self, code):
        said = await _read_as_text(ASK, _code(code))
        assert len(said) == 1, said
        assert said[0].startswith("step parse_request: its code reads `state.receive_request` as text, but step "
                                  "receive_request is a person's answer, an object"), said[0]
        assert "state.user_input" in said[0]

    async def test_extract_jsons_answer_read_as_text(self):
        pulled = {"id": "pull", "type": "extract_json", "name": "Pull", "does": "Pulls the JSON out", "next":
                  "parse_request"}
        said = await _read_as_text(WRITE, pulled, _code("result = state.pull.lower()"))
        assert len(said) == 1 and "step pull is the JSON it pulled out" in said[0], said


class TestLeftAlone:
    @pytest.mark.parametrize("code", [
        "import re\nm = re.search(r'(SO-\\d+)', state.user_input)\nresult = m.group(1) if m else ''",
        "result = state.receive_request.feedback.strip()",
        "result = state.receive_request['request'].split(',')",
        "result = {'ok': state.receive_request.approved}",
    ])
    async def test_a_field_of_the_answer_or_the_runs_own_message(self, code):
        assert await _read_as_text(ASK, _code(code)) == []

    async def test_a_model_steps_text(self):
        assert await _read_as_text(WRITE, _code("result = state.write_note.strip().split('\\n')")) == []

    async def test_a_human_step_with_fields_read_by_its_field(self):
        """A schedule's run that needs its person's text still asks them, with a field (eb's note 3)."""
        fields = {**ASK, "config": {"human_fields": [{"name": "request", "type": "textarea", "label": "Request"}]}}
        assert await _read_as_text(fields, _code("import re\nresult = re.findall('SO-\\\\d+', "
                                                 "state.receive_request.request)")) == []
