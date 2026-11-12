"""v2's resume reads a Human form's text as every other door does — F1564 (eb; found by DOC6, 11-12).

F1561 made a Human step's form read text field by field (`name: value`, one line each) and refuse text that names no
field, at the app's Answer control, v1's resume, MCP's answer_run and a channel: they all go through
`answer_paused_run`, which tells `check_answer` the run waits on a form (`asks_a_form`). v2's resume checks the answer
itself and did not say so, so plain text to a step with several fields was queued as the whole answer and every field a
later step read was missing — the bug F1561 fixed everywhere else. What is not a Human form keeps its text, as at the
other doors: a tool call's approval ("yes"), a browser turn.
"""
from __future__ import annotations

import pytest

FORM = [
    {"name": "approved", "type": "boolean", "label": "Approve the plan?"},
    {"name": "decision", "type": "choice", "label": "Decision", "options": ["Yes", "No", "Later"]},
    {"name": "count", "type": "number", "label": "How many"},
    {"name": "comment", "type": "textarea", "label": "Comment"},
]
PAUSED_AT_H = dict(status="paused", waiting_for_human=True, current_step_id="h",
                   step_history=[{"step_id": "h", "status": "paused"}])


def _app(scale_app, **row):
    from _fakes.fake_pg import fake_session_factory, run_row
    scale_app.state.pg_session_factory = fake_session_factory(run_row("run_1", **{**PAUSED_AT_H, **row}))
    return scale_app


def _queued_answer(app):
    app.state.arq_redis.enqueue_job.assert_called_once()
    call = app.state.arq_redis.enqueue_job.call_args
    return [a for a in (*call.args, *call.kwargs.values()) if isinstance(a, dict)]


class TestAFormsText:
    async def test_plain_text_naming_no_field_is_refused_and_nothing_is_queued(self, client, scale_app, api_key):
        app = _app(scale_app, human_fields=FORM, step_progress={})
        resp = await client.post("/api/v2/orchestrations/runs/run_1/resume",
                                 json={"response": "sounds good"}, headers=api_key["headers"])
        assert resp.status_code == 422, resp.text
        assert "as name: value" in resp.json()["detail"]
        app.state.arq_redis.enqueue_job.assert_not_called()

    async def test_each_field_on_its_own_line_is_read_into_the_answer(self, client, scale_app, api_key):
        app = _app(scale_app, human_fields=FORM, step_progress={})
        resp = await client.post("/api/v2/orchestrations/runs/run_1/resume",
                                 json={"response": "approved: yes\ndecision: later\ncount: 1,200\ncomment: ok"},
                                 headers=api_key["headers"])
        assert resp.status_code == 202, resp.text
        assert {"approved": True, "decision": "Later", "count": 1200, "comment": "ok"} in _queued_answer(app)


class TestWhatIsNoFormKeepsItsText:
    @pytest.mark.parametrize("pending", [{"pending_approval": {"tool_name": "send_email"}},
                                         {"pending_browser_turn": {"session_id": "s"}}])
    async def test_a_tool_approval_or_a_browser_turn(self, client, scale_app, api_key, pending):
        app = _app(scale_app, human_fields=FORM, step_progress={"h": pending})
        resp = await client.post("/api/v2/orchestrations/runs/run_1/resume",
                                 json={"response": "yes"}, headers=api_key["headers"])
        assert resp.status_code == 202, resp.text
        assert {"response": "yes"} in _queued_answer(app)
