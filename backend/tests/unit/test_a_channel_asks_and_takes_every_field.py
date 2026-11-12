"""A channel asks a Human step's every field and takes each answer back — F1561 (HUM1; Naveen 11-12).

A Human step that names a messaging channel (Telegram, Discord, Slack, Teams, WhatsApp) asks its question there and a
reply answers it (F54). Only the question went: a step with a Yes / No, a One of, a number and a comment showed the
person none of them, and the reply, plain words, was kept as the whole answer, so every field a later step read was
missing (F1434 read a lone Yes / No out of a yes or a no, nothing else).

Now the question carries the fields, in Markdown the channel's own formatter turns into its format (each field's name,
its label and what it takes: yes or no, one of its options, a number, text, …) and how to answer: one line each, as
`name: value`. A reply is read that way — by name or by label, `:` or `=`, a bullet before it, a text field running on
over the lines after it — and each value is checked as the app's form checks it. A reply that names no field, or a
value a field cannot take, is refused in the channel, saying how, and the run keeps waiting. A lone One of takes a
plain reply that is one of its options, as a lone Yes / No takes yes or no.

Every door reads a Human form's text the same way (the API and MCP's answer_run too). What is not a Human form keeps
its text: a tool's approval (its "yes"), a browser turn, and a lone text field — the AI builder's own questions.
Every platform asks and is answered in text; none has buttons for it here.
"""
from __future__ import annotations

import pytest
from _fakes import seed as S

from core.orchestration import human_fields as H
from core.orchestration.human_fields import BadAnswer
from tests.unit.test_human_step_channel import _run, _run_to_pause, _settled, messaging_on, rest, wired  # noqa: F401

YES_NO = {"name": "approved", "type": "boolean", "label": "Approve the plan?"}
ONE_OF = {"name": "decision", "type": "choice", "label": "Decision", "options": ["Yes", "No", "Later"]}
COUNT = {"name": "count", "type": "number", "label": "How many"}
COMMENT = {"name": "comment", "type": "textarea", "label": "Comment"}
FORM = [YES_NO, ONE_OF, COUNT, COMMENT]


def _asking(orch_id: str, channel_id: str, fields: list[dict]) -> dict:
    return S.make_orchestration(id=orch_id, entry_step_id="h", steps=[
        {"id": "h", "name": "Review", "type": "human", "human_prompt": "Review the plan",
         "human_channel_id": channel_id, "output_key": "review", "human_fields": fields, "next_step_id": "p"},
        {"id": "p", "name": "Show", "type": "print", "print_content": "{{ state.review }}", "output_key": "final",
         "next_step_id": None},
    ])


class TestTheQuestionCarriesItsFields:
    def test_each_field_and_how_to_answer(self):
        text = H.asked_in_words("Review the plan", FORM)
        assert text.startswith("Review the plan\n\n")
        assert "as `name: value`" in text
        for line in ("- `approved` — Approve the plan? (yes or no)", "- `decision` — Decision (one of: Yes, No, Later)",
                     "- `count` — How many (a number)", "- `comment` — Comment (text)"):
            assert line in text, (line, text)

    def test_a_lone_typed_field_is_one_reply(self):
        assert H.asked_in_words("Pick one", [ONE_OF]) == "Pick one\n\nReply with one of: Yes, No, Later — Decision."
        assert H.asked_in_words("Ship?", [YES_NO]) == "Ship?\n\nReply with yes or no — Approve the plan?"

    @pytest.mark.parametrize("fields", [[], [COMMENT], [{"name": "browser_turn", "type": "browser_turn"}], None])
    def test_a_question_with_no_typed_fields_is_as_before(self, fields):
        assert H.asked_in_words("Ship it?", fields) == "Ship it?"

    async def test_the_channel_is_sent_them(self, wired):
        await _run_to_pause(_asking("orch_f1561_ask", wired.channel["id"], FORM), wired.server, "run_f1561_ask")
        [(chat, text)] = wired.rest
        assert chat == "c1"
        assert "Review the plan" in text and "approved" in text and "Later" in text and "a number" in text


class TestAReplyAnswersEachField:
    @pytest.mark.parametrize("reply, answer", [
        ("approved: yes\ndecision: later\ncomment: looks good\nship it Friday",
         {"approved": True, "decision": "Later", "comment": "looks good\nship it Friday"}),
        ("- Approve the plan: no\n* Decision = Yes\nComment: fine",
         {"approved": False, "decision": "Yes", "comment": "fine"}),
        ("Hi!\n`approved`: yes\nAPPROVED_x: no\ndecision: No",
         {"approved": True, "decision": "No"}),
        ("comment: note: the second colon is the comment's", {"comment": "note: the second colon is the comment's"}),
    ])
    def test_name_value_lines(self, reply, answer):
        assert H.check_answer(FORM, reply, form=True) == answer

    def test_a_reply_that_names_no_field_is_refused_saying_how(self):
        with pytest.raises(BadAnswer) as said:
            H.check_answer(FORM, "looks fine to me", form=True)
        message = str(said.value)
        assert message.startswith("This question asks 4 things: answer each on its own line, as name: value — "), message
        assert "approved (yes or no), decision (one of Yes, No, Later), count (a number), comment (text)" in message

    def test_a_value_a_field_cannot_take_is_refused(self):
        with pytest.raises(BadAnswer, match="Decision: choose one of Yes, No, Later"):
            H.check_answer(FORM, "approved: yes\ndecision: maybe", form=True)

    def test_a_lone_one_of_takes_a_plain_reply(self):
        assert H.check_answer([ONE_OF], "later", form=True) == {"decision": "Later"}
        assert H.check_answer([ONE_OF], "decision: no", form=True) == {"decision": "No"}
        with pytest.raises(BadAnswer, match="choose one of Yes, No, Later"):
            H.check_answer([ONE_OF], "perhaps", form=True)

    async def test_in_the_channel_end_to_end(self, wired, fake_llm):
        await _run_to_pause(_asking("orch_f1561_e2e", wired.channel["id"], FORM), wired.server, "run_f1561_e2e")
        await wired.adapter._dispatch("c1", "approved: yes\ndecision: Later\ncomment: ok", "u1")
        run = await _settled("run_f1561_e2e")
        assert run.status == "completed"
        assert run.shared_state["review"] == {"approved": True, "decision": "Later", "comment": "ok"}

    async def test_a_refused_reply_is_said_in_the_channel_and_the_run_waits(self, wired):
        await _run_to_pause(_asking("orch_f1561_no", wired.channel["id"], FORM), wired.server, "run_f1561_no")
        await wired.adapter._dispatch("c1", "sounds good", "u1")
        [said] = wired.adapter.sent
        assert said.startswith("⚠️ This question asks 4 things: answer each on its own line, as name: value"), said
        run = await _run("run_f1561_no")
        assert run.status == "paused" and run.waiting_for_human and "review" not in run.shared_state

    async def test_the_api_reads_a_forms_text_the_same_way(self, wired):
        from core.orchestration.answers import answer_paused_run

        await _run_to_pause(_asking("orch_f1561_api", wired.channel["id"], FORM), wired.server, "run_f1561_api")
        with pytest.raises(BadAnswer, match="as name: value"):
            await answer_paused_run(wired.server.app.state, "run_f1561_api", "sounds good", door="api")


class TestWhatIsNoFormKeepsItsText:
    def test_a_tool_approval(self):
        fields = [{"name": "approve", "type": "boolean", "label": "Run send_email?"},
                  {"name": "reason", "type": "text", "label": "Reason (optional)"}]
        assert H.check_answer(fields, "yes") == "yes"
        assert H.check_answer(fields, "yes", form=True) == "yes", "a tool approval's own fields are no form"

    def test_a_lone_text_field_the_builders_questions(self):
        for field in (COMMENT, {"name": "feedback", "type": "textarea", "label": "Approve or describe changes"}):
            assert H.check_answer([field], "feedback: make it blue", form=True) == "feedback: make it blue"

    def test_a_lone_yes_no_as_f1434_reads_it(self):
        assert H.check_answer([YES_NO], "Yes", form=True) == {"approved": True}
        assert H.check_answer([YES_NO], "next tuesday", form=True) == "next tuesday"

    def test_a_mapping_is_checked_as_before(self):
        assert H.check_answer(FORM, {"approved": "no", "decision": "yes"}, form=True) == {"approved": False,
                                                                                        "decision": "Yes"}

    def test_the_door_tells_a_form_from_a_tool_approval(self):
        from core.orchestration.answers import asks_a_form

        history = [{"step_id": "h", "status": "paused"}]
        assert asks_a_form(FORM, {}, history, "h") is True
        assert asks_a_form(FORM, {"h": {"pending_approval": {"tool_name": "x"}}}, history, "h") is False
        assert asks_a_form(FORM, {"h": {"pending_browser_turn": {"session_id": "s"}}}, history, "h") is False
