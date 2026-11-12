"""A Human step's fields are filled in from the run, as its question is — F1556 (HUM1; Naveen 11-12: "In human steps
also let's render {{ fields").

A Human step's question has filled in `{{ state.x }}` and `{{ state.x.field }}` since F1252 (`render_state_refs`); its
fields never were. The step sent `human_fields` as written, so a label "Approve {{ state.draft.title }}?" reached the
person as those words — in the app's form and its Approve button, in the API's and MCP's waiting payload — and a
dropdown whose options name values the run holds offered the placeholders themselves.

Now each field's label and each dropdown option is filled in the way the question is, by the one renderer a person's
text takes, and the run asks with them so: the live event, the run's record (which the app, the API, MCP's
`list_waiting`/`answer_run` and the bell read) and the answer's check, which takes an option as the person was shown
it. The step as saved keeps its templates; a field with nothing to fill is sent as it was written.
"""
from __future__ import annotations

import asyncio
import types

import pytest
from _fakes import seed as S

from core.orchestration.steps import render_state_refs

STATE = {"draft": {"title": "Q3 plan", "owner": "Ana"}, "region": "EMEA"}
FIELDS = [
    {"name": "ok", "type": "boolean", "label": "Approve {{ state.draft.title }}?"},
    {"name": "pick", "type": "choice", "label": "Send to", "options": ["{{ state.draft.owner }}", "{{state.region}} desk",
                                                                        "Someone else"]},
    {"name": "note", "type": "textarea", "label": "Anything for {{ state.draft.owner }}?", "hint": "kept as written"},
    {"name": "plain", "type": "string", "label": "No reference here"},
    {"name": "bare"},
]
SHOWN = [
    {"name": "ok", "type": "boolean", "label": "Approve Q3 plan?"},
    {"name": "pick", "type": "choice", "label": "Send to", "options": ["Ana", "EMEA desk", "Someone else"]},
    {"name": "note", "type": "textarea", "label": "Anything for Ana?", "hint": "kept as written"},
    {"name": "plain", "type": "string", "label": "No reference here"},
    {"name": "bare"},
]


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _orch(orch_id: str, fields: list[dict], prompt: str = "Is {{ state.draft.title }} ready?") -> dict:
    return S.make_orchestration(id=orch_id, entry_step_id="review", steps=[
        {"id": "review", "name": "Review", "type": "human", "human_prompt": prompt, "human_fields": fields,
         "output_key": "review", "next_step_id": "done"},
        {"id": "done", "name": "Done", "type": "print", "print_content": "{{ state.review.pick }}", "output_key": "o",
         "next_step_id": None},
    ])


async def _asked(orch: dict) -> tuple[str, dict, object]:
    """Run `orch` to its question: (run id, the live event, the engine)."""
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    await S.seed_orchestrations([orch])
    run_id = f"run_{orch['id']}"
    engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
    events = [e async for e in engine.run("go", run_id=run_id, initial_state=dict(STATE))]
    assert events[-1]["type"] == "human_input_required"
    return run_id, events[-1], engine


async def _answer(run_id: str, answer) -> list[dict]:
    from core.orchestration import answers
    from core.orchestration.runner import SENTINEL

    outcome = await answers.answer_paused_run(types.SimpleNamespace(server_module=_server()), run_id, answer)
    resumed = []
    while (event := await asyncio.wait_for(outcome.queue.get(), 10)) is not SENTINEL:
        resumed.append(event)
    return resumed


class TestTheQuestionIsAskedFilledIn:
    async def test_the_live_event_and_the_runs_record(self):
        from core.orchestration.state import SharedState

        run_id, event, _ = await _asked(_orch("orch_f1556_event", FIELDS))
        assert event["fields"] == SHOWN
        assert event["prompt"] == "Is Q3 plan ready?", "the question, as before"
        run = (await SharedState.restore(run_id)).run
        assert run.human_fields == SHOWN, "what the app, the bell and every door read"
        assert run.human_prompt == "Is Q3 plan ready?"

    async def test_the_api_and_mcp_read_it_filled_in(self):
        from core.orchestration.run_view import read_run

        run_id, _, _ = await _asked(_orch("orch_f1556_view", FIELDS))
        asked = (await read_run(run_id))["human_input_required"]
        assert asked["fields"] == SHOWN
        assert "{{" not in str(asked)

    async def test_the_step_as_saved_keeps_its_templates(self):
        _, _, engine = await _asked(_orch("orch_f1556_saved", FIELDS))
        assert [f.copy() for f in engine.step_map["review"].human_fields] == FIELDS

    def test_a_label_reads_as_the_question_does(self):
        """One renderer for a person's text: whatever the question does with a reference, a label does too."""
        from core.orchestration.steps import shown_fields

        for text in ("{{ state.draft.title }}", "{{ state.nope }}", "{{ state.draft.nope }}", "{{ global.tone }}",
                     "{{ state.draft }}", "plain"):
            [field] = shown_fields([{"name": "f", "label": text, "type": "choice", "options": [text]}], STATE)
            assert field["label"] == render_state_refs(text, STATE), text
            assert field["options"] == [render_state_refs(text, STATE)], text

    @pytest.mark.parametrize("fields", [None, [], "not a list", [None, "x", {"name": "n", "label": None,
                                                                             "options": "not a list"}]])
    def test_what_is_not_a_field_is_passed_on_as_it_came(self, fields):
        from core.orchestration.steps import shown_fields

        assert shown_fields(fields, STATE) == (fields or [])


class TestTheAnswerIsCheckedAgainstWhatWasShown:
    async def test_an_option_as_shown_is_taken(self):
        run_id, _, _ = await _asked(_orch("orch_f1556_pick", FIELDS))
        resumed = await _answer(run_id, {"ok": True, "pick": "ana", "note": "", "plain": "", "bare": ""})
        printed = [e["response"] for e in resumed if e.get("type") == "final" and e.get("orch_step_id") == "done"]
        assert printed == ["Ana"], resumed

    async def test_the_placeholder_itself_is_not_an_option(self):
        from core.orchestration.human_fields import BadAnswer

        run_id, _, _ = await _asked(_orch("orch_f1556_raw", FIELDS))
        with pytest.raises(BadAnswer, match="choose one of Ana, EMEA desk, Someone else"):
            await _answer(run_id, {"ok": True, "pick": "{{ state.draft.owner }}"})
