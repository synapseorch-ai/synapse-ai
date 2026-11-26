"""A Human step's field can start with a value from the run, and approved as shown it sends that value (F1753).

A plan that shows a draft for approval and lets the person change it before it goes ("show me the welcome to approve,
then send it") wrote the draft into the step's words and To, Subject and Body fields under them, which the send then
read. A field was `{name, type, label, options}`: nothing could start with a value, so every field started empty, and
approved as shown the form sent the blanks; the send failed after the person's yes.

Now a field may carry `default`, the value it starts with, written as the step's words are (`{{state.…}}`). It is
filled in from the run when the run asks (`shown_fields`) and recorded so, in the live event and the run's record that
the app's forms, a channel, the API and MCP read. An answer that leaves such a field out takes the value the run
recorded when it asked, never one rendered again. A blank the person sent stays blank (F232): they cleared it, and
what they approved is what goes. A starting value that names nothing, a secret, or a file's excerpt, or that the
field cannot take (not one of its options, not a number, not an email), starts empty.

A blank is caught where it would be sent: a field a later step sets as a tool's required input is asked as
`required`, and an approval that leaves it blank is refused before anything resumes, naming it, at every door, and the
run keeps waiting. A Decline is taken whatever is blank.

Near misses: a field with no starting value is answered as before; a field only a later Print reads, or that the tool
does not require, may be blank.
"""
from __future__ import annotations

import asyncio
import types

import httpx
import pytest
from _fakes import seed as S
from _fakes.fake_llm import tool_call

from core.orchestration import human_fields as H
from core.orchestration.human_fields import BadAnswer
from tests.unit.test_human_step_channel import _settled, messaging_on, rest, wired  # noqa: F401

ROBIN = "robin@cedarline.example"
SAM = "sam@cedarline.example"
DRAFT = {"to": ROBIN, "subject": "Welcome to Harbor & Pine", "body": "Hello Robin,\n\nYour first beans ship Tuesday."}
EMAIL_SCHEMA = {"type": "object", "required": ["to", "subject", "text"], "properties": {
    "to": {"type": "string", "description": "Who it goes to"},
    "subject": {"type": "string", "description": "The subject"},
    "text": {"type": "string", "description": "The email"},
    "reply_to": {"type": "string", "description": "Where replies go"}}}

APPROVED = {"name": "approved", "type": "boolean", "label": "Send this email?"}
TO = {"name": "to", "type": "email", "label": "To", "default": "{{state.draft.to}}"}
SUBJECT = {"name": "subject", "type": "string", "label": "Subject", "default": "{{ state.draft.subject }}"}
BODY = {"name": "body", "type": "textarea", "label": "Body", "default": "{{state.draft.body}}"}
NOTE = {"name": "note", "type": "string", "label": "Note to the team", "default": "Sent by the desk"}
FIELDS = [APPROVED, TO, SUBJECT, BODY, NOTE]
SHOWN = [APPROVED, {**TO, "default": ROBIN, "required": True},
         {**SUBJECT, "default": DRAFT["subject"], "required": True},
         {**BODY, "default": DRAFT["body"], "required": True}, NOTE]
SET_ARGS = {"to": "{{state.review.to}}", "subject": "{{state.review.subject}}", "text": "{{state.review.body}}",
            "reply_to": "{{state.review.note}}"}


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


@pytest.fixture
def sent(monkeypatch):
    """Every request the send tool makes."""
    import core.http_tool as http_tool

    record = types.SimpleNamespace(calls=[])

    async def send(client, tool, args, variables, **kw):
        record.calls.append(args)
        return httpx.Response(200, text="{}", request=httpx.Request("POST", tool["url"]))

    monkeypatch.setattr(http_tool, "send", send)
    return record


async def _tool():
    from core.routes.tools import _upsert_custom_tools

    await _upsert_custom_tools([{"name": "send_email", "description": "Sends an email through our mail service.",
                                 "tool_type": "http", "method": "POST", "url": "https://relay.harborpine.example/send",
                                 "inputSchema": EMAIL_SCHEMA}])


def _orch(orch_id: str, fields=None, send: dict | None = None, channel: str | None = None) -> dict:
    review = {"id": "review", "name": "Review the welcome", "type": "human", "output_key": "review",
              "human_prompt": "Review the welcome email before it is sent.",
              "human_fields": FIELDS if fields is None else fields, "next_step_id": "gate"}
    if channel:
        review["human_channel_id"] = channel
    return S.make_orchestration(id=orch_id, entry_step_id="review", steps=[
        review,
        {"id": "gate", "name": "Approved?", "type": "if_else", "if_condition": "state.review.approved == True",
         "if_true_step_id": "send", "if_false_step_id": None},
        send or {"id": "send", "name": "Send the welcome", "type": "tool", "forced_tool": "send_email",
                 "tool_arguments": SET_ARGS, "output_key": "sent", "next_step_id": None},
    ])


async def _asked(orch: dict, server=None) -> tuple[str, dict]:
    """Run `orch` to its question: (run id, the live event)."""
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    await _tool()
    await S.seed_orchestrations([orch])
    run_id = f"run_{orch['id']}"
    engine = OrchestrationEngine(Orchestration.model_validate(orch), server or _server())
    events = [e async for e in engine.run("go", run_id=run_id, initial_state={"draft": dict(DRAFT)})]
    assert events[-1]["type"] == "human_input_required", events[-1]
    return run_id, events[-1]


async def _answer(run_id: str, answer, door: str = "app") -> list[dict]:
    from core.orchestration import answers
    from core.orchestration.runner import SENTINEL

    outcome = await answers.answer_paused_run(types.SimpleNamespace(server_module=_server()), run_id, answer,
                                              door=door)
    resumed = []
    while (event := await asyncio.wait_for(outcome.queue.get(), 10)) is not SENTINEL:
        resumed.append(event)
    return resumed


async def _run(run_id: str):
    from core.orchestration.state import SharedState

    return (await SharedState.restore(run_id)).run


class TestTheCardStartsWithTheDraft:
    async def test_each_field_is_asked_with_the_drafts_value(self):
        from core.orchestration.run_view import read_run

        run_id, event = await _asked(_orch("orch_f1753_asked"))
        assert event["fields"] == SHOWN
        assert (await _run(run_id)).human_fields == SHOWN, "what the app's forms, a channel and the bell read"
        assert (await read_run(run_id))["human_input_required"]["fields"] == SHOWN, "what the API and MCP read"

    async def test_the_step_as_saved_keeps_its_templates(self):
        from core.store.resources import get_orchestration

        await _asked(_orch("orch_f1753_saved"))
        saved = await get_orchestration("orch_f1753_saved")
        assert saved["steps"][0]["human_fields"] == FIELDS

    @pytest.mark.parametrize("default", [
        pytest.param("{{state.nope}}", id="a key no step wrote"),
        pytest.param("{{state.draft.nope}}", id="a field the value lacks"),
        pytest.param("{{ global.relay_key }}", id="a secret"),
        pytest.param("{{ global.no_such }}", id="a variable there is none of"),
        pytest.param("Dear {{state.nope}}", id="words around a value that names nothing"),
    ])
    def test_a_value_that_names_nothing_or_a_secret_starts_empty(self, default):
        from core.orchestration.steps import shown_fields

        [field] = shown_fields([{"name": "f", "type": "string", "default": default}], {"draft": dict(DRAFT)},
                               {"relay_key": "rk-123456", "tone": "warm"}, {"relay_key"})
        assert "default" not in field, field

    def test_words_around_values_are_filled_in(self):
        from core.orchestration.steps import shown_fields

        [field] = shown_fields([{"name": "f", "type": "string", "default": "Re: {{state.draft.subject}}, {{global.tone}}"}],
                               {"draft": dict(DRAFT)}, {"tone": "warm"})
        assert field["default"] == "Re: Welcome to Harbor & Pine, warm"

    @pytest.mark.parametrize("field, value", [
        ({"name": "f", "type": "choice", "options": ["Now", "Later"]}, "soon"),
        ({"name": "f", "type": "number"}, "a few"),
        ({"name": "f", "type": "email"}, "not an address"),
        ({"name": "f", "type": "date"}, "next Tuesday"),
        ({"name": "f", "type": "phone"}, "call me"),
        ({"name": "f", "type": "boolean"}, "perhaps"),
    ])
    def test_a_value_its_field_cannot_take_starts_empty(self, field, value):
        from core.orchestration.steps import shown_fields

        [shown] = shown_fields([{**field, "default": "{{state.v}}"}], {"v": value})
        assert "default" not in shown, shown
        [literal] = shown_fields([{**field, "default": value}], {})
        assert "default" not in literal, literal

    @pytest.mark.parametrize("field, value, kept", [
        ({"name": "f", "type": "choice", "options": ["Now", "Later"]}, "later", "Later"),
        ({"name": "f", "type": "number"}, "1,200", 1200),
        ({"name": "f", "type": "email"}, f" {ROBIN} ", ROBIN),
        ({"name": "f", "type": "boolean"}, "yes", True),
        ({"name": "f", "type": "textarea"}, "Two\nlines", "Two\nlines"),
    ])
    def test_a_value_it_can_take_starts_in_its_stored_form(self, field, value, kept):
        from core.orchestration.steps import shown_fields

        [shown] = shown_fields([{**field, "default": "{{state.v}}"}], {"v": value})
        assert shown["default"] == kept

    def test_a_field_with_no_starting_value_is_asked_as_before(self):
        from core.orchestration.steps import shown_fields

        assert shown_fields([APPROVED, {"name": "why", "type": "string", "label": "Why"}], {}) == [
            APPROVED, {"name": "why", "type": "string", "label": "Why"}]


class TestApprovedAsShown:
    @pytest.mark.parametrize("door", ["app", "api", "mcp"])
    async def test_it_sends_the_draft(self, sent, door):
        run_id, _ = await _asked(_orch(f"orch_f1753_as_shown_{door}"))
        await _answer(run_id, {"approved": True}, door=door)
        assert sent.calls == [{"to": ROBIN, "subject": DRAFT["subject"], "text": DRAFT["body"],
                               "reply_to": "Sent by the desk"}]
        assert (await _run(run_id)).status == "completed"

    async def test_a_field_typed_over_sends_what_was_typed(self, sent):
        run_id, _ = await _asked(_orch("orch_f1753_typed"))
        await _answer(run_id, {"approved": True, "to": SAM, "subject": "Welcome aboard"})
        assert sent.calls == [{"to": SAM, "subject": "Welcome aboard", "text": DRAFT["body"],
                               "reply_to": "Sent by the desk"}]

    async def test_a_cleared_starting_value_stays_cleared(self, sent):
        run_id, _ = await _asked(_orch("orch_f1753_cleared"))
        await _answer(run_id, {"approved": True, "note": ""})
        assert sent.calls and sent.calls[0]["reply_to"] == "", "what they approved is what goes"
        assert (await _run(run_id)).shared_state["review"]["note"] == ""

    async def test_the_value_given_is_the_one_recorded_when_it_asked(self, sent):
        from core.orchestration.state import SharedState

        run_id, _ = await _asked(_orch("orch_f1753_recorded"))
        run = await _run(run_id)
        run.shared_state["draft"] = {**DRAFT, "subject": "Changed after the card was shown"}
        await SharedState(run).checkpoint()
        await _answer(run_id, {"approved": True})
        assert sent.calls[0]["subject"] == DRAFT["subject"]

    def test_left_out_is_filled_and_blank_is_kept(self):
        shown = [APPROVED, {**TO, "default": ROBIN}, {**NOTE}]
        assert H.check_answer(shown, {"approved": True}) == {"approved": True, "to": ROBIN, "note": "Sent by the desk"}
        assert H.check_answer(shown, {"approved": True, "note": "  "}) == {"approved": True, "to": ROBIN, "note": "  "}

    def test_a_field_with_no_starting_value_left_out_stays_out(self):
        assert H.check_answer([APPROVED, {"name": "why", "type": "string"}], {"approved": True}) == {"approved": True}


class TestABlankASendNeeds:
    async def test_an_approval_with_it_cleared_is_refused_naming_it_and_the_run_waits(self, sent):
        run_id, _ = await _asked(_orch("orch_f1753_refused"))
        with pytest.raises(BadAnswer, match="To is empty"):
            await _answer(run_id, {"approved": True, "to": ""})
        run = await _run(run_id)
        assert run.status == "paused" and run.waiting_for_human and "review" not in run.shared_state
        assert sent.calls == []

    async def test_a_decline_with_blanks_is_taken(self, sent):
        run_id, _ = await _asked(_orch("orch_f1753_declined"))
        await _answer(run_id, {"approved": False, "to": "", "subject": "", "body": ""})
        run = await _run(run_id)
        assert run.status == "completed" and run.shared_state["review"]["approved"] is False
        assert sent.calls == []

    async def test_a_field_with_no_starting_value_a_send_needs_is_required_too(self, sent):
        typed = {"name": "to", "type": "email", "label": "Recipient email"}
        run_id, event = await _asked(_orch("orch_f1753_typed_to", fields=[APPROVED, typed, SUBJECT, BODY, NOTE]))
        assert event["fields"][1] == {**typed, "required": True}
        with pytest.raises(BadAnswer, match="Recipient email is empty"):
            await _answer(run_id, {"approved": True})
        await _answer(run_id, {"approved": True, "to": SAM})
        assert [c["to"] for c in sent.calls] == [SAM]

    async def test_a_field_only_an_optional_input_or_a_print_reads_may_be_blank(self, sent):
        _, event = await _asked(_orch("orch_f1753_optional"))
        assert "required" not in event["fields"][4], "reply_to is not one the tool requires"
        printed = {"id": "send", "name": "Show it", "type": "print", "print_content": "{{state.review.to}}",
                   "output_key": "shown", "next_step_id": None}
        _, event = await _asked(_orch("orch_f1753_print", send=printed))
        assert not any(f.get("required") for f in event["fields"])

    def test_without_an_approval_field_any_answer_is_a_go(self):
        shown = [{"name": "to", "type": "email", "label": "To", "required": True}]
        with pytest.raises(BadAnswer, match="To is empty"):
            H.check_answer(shown, {"to": " "})
        assert H.check_answer(shown, {"to": ROBIN}) == {"to": ROBIN}

    def test_an_approval_left_out_is_no_go_and_not_refused(self):
        shown = [APPROVED, {**TO, "default": ROBIN, "required": True}]
        assert H.check_answer(shown, {"to": ""}) == {"to": ""}


class TestTheAddressGuard:
    """A model-filled send after the card: the address the field started with, or one typed over it, is the run's
    to send (`grounded.held` reads the answer), never asked about a second time."""

    def _model_send(self):
        return {"id": "send", "name": "Send the welcome", "type": "tool", "forced_tool": "send_email",
                "prompt_template": "Send the approved welcome.", "output_key": "sent", "model": "claude-x",
                "max_turns": 3, "next_step_id": None}

    async def test_the_starting_address_approved_as_shown_is_not_asked_again(self, sent, fake_llm):
        fake_llm.script([tool_call("send_email", to=ROBIN, subject=DRAFT["subject"], text=DRAFT["body"])])
        run_id, _ = await _asked(_orch("orch_f1753_guard_shown", send=self._model_send()))
        resumed = await _answer(run_id, {"approved": True})
        assert not [e for e in resumed if e.get("type") == "human_input_required"], resumed
        assert [c["to"] for c in sent.calls] == [ROBIN]

    async def test_an_address_typed_over_it_goes_where_it_was_typed(self, sent, fake_llm):
        fake_llm.script([tool_call("send_email", to=SAM, subject=DRAFT["subject"], text=DRAFT["body"])])
        run_id, _ = await _asked(_orch("orch_f1753_guard_typed", send=self._model_send()))
        resumed = await _answer(run_id, {"approved": True, "to": SAM})
        assert not [e for e in resumed if e.get("type") == "human_input_required"], resumed
        assert [c["to"] for c in sent.calls] == [SAM]


class TestInAChannel:
    def test_the_words_show_each_starting_value_and_what_a_field_left_out_sends(self):
        words = H.asked_in_words("Review the welcome email.", SHOWN)
        assert f"- `to` — To (an email address), now: {ROBIN}" in words
        assert "\n> Hello Robin,\n>\n> Your first beans ship Tuesday." in words, "a long value whole, quoted"
        assert "A field you leave out is sent as it is now." in words
        assert "- `approved` — Send this email? (yes or no)" in words

    def test_a_long_starting_value_is_never_cut(self):
        body = "\n".join(f"Line {i}: " + "beans " * 40 for i in range(40))
        words = H.asked_in_words("Review it.", [APPROVED, {**BODY, "default": body}])
        assert body.replace("\n", "\n> ") in words

    def test_words_with_no_starting_value_are_as_before(self):
        fields = [APPROVED, {"name": "why", "type": "string", "label": "Why"}]
        assert H.asked_in_words("Q", fields) == (
            "Q\n\nAnswer each on its own line, as `name: value`:\n"
            "- `approved` — Send this email? (yes or no)\n- `why` — Why (text)")

    async def test_a_reply_naming_only_the_approval_sends_the_draft(self, sent, wired):
        run_id, _ = await _asked(_orch("orch_f1753_channel", channel=wired.channel["id"]), wired.server)
        await wired.adapter._dispatch("c1", "approved: yes", "u1")
        await _settled(run_id)
        assert sent.calls == [{"to": ROBIN, "subject": DRAFT["subject"], "text": DRAFT["body"],
                               "reply_to": "Sent by the desk"}]

    async def test_a_cleared_field_a_send_needs_is_said_in_the_channel_and_the_run_waits(self, sent, wired):
        run_id, _ = await _asked(_orch("orch_f1753_channel_no", channel=wired.channel["id"]), wired.server)
        await wired.adapter._dispatch("c1", "approved: yes\nto:", "u1")
        said = wired.adapter.sent[-1]
        assert said.startswith("⚠️ To is empty"), said
        run = await _run(run_id)
        assert run.status == "paused" and run.waiting_for_human and sent.calls == []

