"""A person's text says a missing value in words, fills a plain workspace variable, never a secret, and shows an
object as JSON — F1556's picks (HUM1; Naveen 11-12).

A Human step's question, its fields' labels and options (F1556) and a Print read through one renderer
(`steps.render_state_refs`). It left a reference that named nothing as written, so a person read `{{ state.score }}`;
it never filled a workspace variable (D104: the question goes to a channel, an email and the API, and a variable may
be a secret); and it printed an object as Python's repr. Naveen's picks:

1. a missing value is said in F1536's words, as a model's prompt says it: "(no value yet: no step has written
   state.x)", "(no value: state.review has no 'score')", "(no value: no variable named 'tone')";
2. a plain workspace variable is filled in, and one the workspace holds as a secret — a value stored as a secret
   reference, which `variables.load` resolves and holds — reads "(a secret is not shown here)": its value reaches no
   place the question goes (the live event and its fields, the run's record, the API's and MCP's payload, a channel,
   a Print after it);
3. an object or a list is shown as JSON, as a model's prompt reads it.

The editor's Human prompt and Print offer the workspace's variables on `{{` and say what they do; the validator's word
for a Print of a whole answer says it prints as JSON.
"""
from __future__ import annotations

import contextvars
import json
import types
from pathlib import Path

import pytest
from _fakes import seed as S

from core.orchestration.steps import render_state_refs, shown_fields

ROOT = Path(__file__).resolve().parents[3]
STATE = {"review": {"approved": True, "note": "Café ok"}, "items": ["a", "é"], "blank": "", "none": None,
         "post": '{"text": "Hello"}'}
#: What a person reads for a secret variable.
SECRET_NOT_SHOWN = "(a secret is not shown here)"
#: Plain words, so no scanner takes the fixture for a credential.
SECRET = "plaintext-of-the-workspace-secret"


def _said(text: str, variables=None, secrets=()) -> str:
    return contextvars.copy_context().run(render_state_refs, text, STATE, variables, secrets)


class TestAGapIsSaid:
    @pytest.mark.parametrize("text, said", [
        ("{{ state.score }}", "(no value yet: no step has written state.score)"),
        ("{{ state.none }}", "(no value yet: no step has written state.none)"),
        ("{{ state.blank.x }}", "(no value yet: no step has written state.blank)"),
        ("{{ state.review.score }}", "(no value: state.review has no 'score')"),
        ("{{ state.review.note.x }}", "(no value: state.review.note has no 'x')"),
        ("{{ global.tone }}", "(no value: no variable named 'tone')"),
        ("{{ state.post.nope }}", "(no value: state.post has no 'nope')"),
    ])
    def test_in_f1536s_words(self, text, said):
        from core.orchestration.context import _fill_task_template

        assert _said(text) == said
        if "state.score" not in text and "state.none" not in text and "state.blank" not in text:
            assert _fill_task_template(text, STATE, {}) == said, "the words a model's prompt says it in"

    @pytest.mark.parametrize("text", ["{{ x }}", "{{ body.data }}", "{state.review}", "{{ }}", "plain {braces}"])
    def test_what_is_no_reference_to_the_run_is_left_as_written(self, text):
        assert _said(text) == text


class TestAValueIsShown:
    @pytest.mark.parametrize("text, said", [
        ("{{ state.review }}", '{"approved": true, "note": "Café ok"}'),
        ("{{ state.items }}", '["a", "é"]'),
        ("{{ state.review.approved }}", "True"),
        ("{{ state.review.note }}", "Café ok"),
        ("{{ state.post.text }}", "Hello"),
        ("{{ state.blank }}", ""),
    ])
    def test_as_a_person_reads_it(self, text, said):
        assert _said(text) == said


class TestAVariable:
    def test_a_plain_one_is_filled(self):
        assert _said("Tone: {{ global.tone }}", {"tone": "warm"}) == "Tone: warm"

    def test_a_secret_never_is(self):
        def held():
            from core.secret_scrub import hold_secrets

            hold_secrets({"API_KEY": SECRET})
            return render_state_refs("Key {{ global.API_KEY }}", STATE, {"API_KEY": SECRET})
        assert contextvars.copy_context().run(held) == f"Key {SECRET_NOT_SHOWN}"
        assert _said("Key {{ global.PIN }}", {"PIN": "4471"}, {"PIN"}) == f"Key {SECRET_NOT_SHOWN}"
        from core.orchestration import steps

        assert steps.SECRET_NOT_SHOWN == SECRET_NOT_SHOWN

    def test_a_label_and_an_option_alike(self):
        fields = [{"name": "f", "type": "choice", "label": "Tone {{ global.tone }}? Key {{ global.KEY }}",
                   "options": ["{{ global.tone }}", "{{ global.KEY }}"]}]
        [shown] = contextvars.copy_context().run(shown_fields, fields, STATE, {"tone": "warm", "KEY": SECRET}, {"KEY"})
        assert shown["label"] == f"Tone warm? Key {SECRET_NOT_SHOWN}"
        assert shown["options"] == ["warm", SECRET_NOT_SHOWN]


# ── A secret reaches no place the question goes ───────────────────────────────────────────────────────────────────────

@pytest.fixture
def secret_variable(monkeypatch):
    """A workspace whose `API_KEY` is stored as a secret reference, as cloud stores one: the store's resolver swaps it
    for its value on the way out, and `variables.load` holds it."""
    from core.store import collections

    async def resolve(document: dict) -> dict:
        values = document.get("values")
        if not isinstance(values, dict):
            return document
        return {**document, "values": {k: SECRET if v == "synsec://api-key" else v for k, v in values.items()}}

    collections.set_document_resolver(resolve)
    yield
    collections.set_document_resolver(None)


async def test_a_secret_reaches_no_place_the_question_goes(secret_variable, monkeypatch):
    from core import variables
    from core.models_orchestration import Orchestration
    from core.orchestration import answers, human_prompts
    from core.orchestration.engine import OrchestrationEngine
    from core.orchestration.run_view import read_run
    from core.orchestration.runner import SENTINEL
    from core.orchestration.state import SharedState

    await variables.save({"tone": "warm", "API_KEY": "synsec://api-key"})
    sent: list[dict] = []

    async def channel(**kwargs):
        sent.append(kwargs)
    human_prompts.set_human_prompt_sender(channel)
    monkeypatch.setattr(human_prompts, "_sender", channel)
    server = types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})
    orch = S.make_orchestration(id="orch_f1556_secret", entry_step_id="ask", steps=[
        {"id": "ask", "name": "Ask", "type": "human", "output_key": "ask", "human_channel_id": "chan_1",
         "human_prompt": "In a {{ global.tone }} voice, with {{ global.API_KEY }}: send {{ state.score }}?",
         "human_fields": [{"name": "ok", "type": "boolean", "label": "Use {{ global.API_KEY }}?"},
                          {"name": "how", "type": "choice", "label": "How",
                           "options": ["{{ global.tone }}", "{{ global.API_KEY }}"]}],
         "next_step_id": "show"},
        {"id": "show", "name": "Show", "type": "print", "output_key": "shown", "next_step_id": None,
         "print_content": "{{ global.tone }} / {{ global.API_KEY }} / {{ state.ask.how }}"},
    ])
    await S.seed_orchestrations([orch])
    try:
        engine = OrchestrationEngine(Orchestration.model_validate(orch), server)
        events = [e async for e in engine.run("go", run_id="run_orch_f1556_secret")]
        asked = events[-1]
        assert asked["prompt"] == ("In a warm voice, with (a secret is not shown here): send (no value yet: no step "
                                   "has written state.score)?")
        assert [f["label"] for f in asked["fields"]] == ["Use (a secret is not shown here)?", "How"]
        assert asked["fields"][1]["options"] == ["warm", "(a secret is not shown here)"]
        assert sent and sent[0]["prompt"] == asked["prompt"], "the channel is asked the same words"

        run = (await SharedState.restore("run_orch_f1556_secret")).run
        payload = (await read_run("run_orch_f1556_secret"))["human_input_required"]
        outcome = await answers.answer_paused_run(types.SimpleNamespace(server_module=server),
                                                  "run_orch_f1556_secret", {"ok": True, "how": "warm"})
        resumed = []
        while (event := await outcome.queue.get()) is not SENTINEL:
            resumed.append(event)
        [printed] = [e["response"] for e in resumed if e.get("type") == "final" and e.get("orch_step_id") == "show"]
        assert printed == "warm / (a secret is not shown here) / warm"

        for where, what in (("the live event", asked), ("the channel", sent), ("the run's record", run.model_dump()),
                            ("the API's payload", payload), ("the run after its Print", resumed)):
            assert SECRET not in json.dumps(what, default=str), where
    finally:
        human_prompts.set_human_prompt_sender(None)


# ── The words around it ────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_print_of_a_whole_answer_is_said_to_print_as_json():
    from core.builder_tools import _validate_orchestration

    orch = {"id": "o", "entry_step_id": "ask", "steps": [
        {"id": "ask", "type": "human", "human_prompt": "?", "output_key": "review",
         "human_fields": [{"name": "summary", "type": "text"}], "next_step_id": "show"},
        {"id": "show", "type": "print", "print_content": "{{ state.review }}", "output_key": "o", "next_step_id": "end"},
        {"id": "end", "type": "end"}]}
    [said] = [i for i in _validate_orchestration(orch)["issues"] if "Print step 'show'" in i]
    assert "prints whole, as JSON" in said and "Python" not in said, said
    validate = (ROOT / "frontend/src/components/orchestration/validate.ts").read_text()
    assert "prints whole, as JSON" in validate and "prints as a Python dict" not in validate


def test_the_editor_offers_and_says_variables_in_a_persons_text():
    steps = (ROOT / "frontend/src/components/orchestration/panel/steps.tsx").read_text()
    for section, start in (("HumanSection", "function HumanSection"), ("PrintSection", "function PrintSection")):
        body = steps[steps.index(start):]
        body = body[:body.index("\nfunction ", 10)]
        assert 'profile="step-prompt"' in body and 'profile="state"' not in body, section
        assert "not filled in here" not in body, section
        assert "a secret one is never shown" in body, section
