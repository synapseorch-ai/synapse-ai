"""v2's resume gives a field the person left out the value the card started it with, and refuses a blank one a send
needs (F1753), as every other door does.

v2 checks an answer itself, against the fields the run recorded when it asked (`human_fields`), rather than through
`answer_paused_run`. Those fields carry what each one started with (`default`, filled in from the run when it asked)
and whether a later step sends it as a tool's required input (`required`), so v2 reads both from the record: a field
left out is queued with the recorded value, a blank the person sent stays blank, and an approval that leaves a
required field blank is refused with 422, naming it, and nothing is queued. A Decline is taken whatever is blank.
"""
from __future__ import annotations

FIELDS = [
    {"name": "approved", "type": "boolean", "label": "Send this email?"},
    {"name": "to", "type": "email", "label": "To", "default": "robin@cedarline.example", "required": True},
    {"name": "note", "type": "string", "label": "Note", "default": "Sent by the desk"},
]
PAUSED_AT_H = dict(status="paused", waiting_for_human=True, current_step_id="h",
                   step_history=[{"step_id": "h", "status": "paused"}], human_fields=FIELDS, step_progress={})
URL = "/api/v2/orchestrations/runs/run_1/resume"


def _app(scale_app):
    from _fakes.fake_pg import fake_session_factory, run_row
    scale_app.state.pg_session_factory = fake_session_factory(run_row("run_1", **PAUSED_AT_H))
    return scale_app


def _queued_answer(app):
    app.state.arq_redis.enqueue_job.assert_called_once()
    call = app.state.arq_redis.enqueue_job.call_args
    return [a for a in (*call.args, *call.kwargs.values()) if isinstance(a, dict)]


class TestAFieldsStartingValue:
    async def test_a_field_left_out_is_queued_as_it_started(self, client, scale_app, api_key):
        app = _app(scale_app)
        resp = await client.post(URL, json={"response": {"approved": True}}, headers=api_key["headers"])
        assert resp.status_code == 202, resp.text
        assert {"approved": True, "to": "robin@cedarline.example", "note": "Sent by the desk"} in _queued_answer(app)

    async def test_a_blank_sent_stays_blank(self, client, scale_app, api_key):
        app = _app(scale_app)
        resp = await client.post(URL, json={"response": {"approved": True, "note": ""}}, headers=api_key["headers"])
        assert resp.status_code == 202, resp.text
        assert {"approved": True, "to": "robin@cedarline.example", "note": ""} in _queued_answer(app)


class TestABlankASendNeeds:
    async def test_an_approval_is_refused_naming_it_and_nothing_is_queued(self, client, scale_app, api_key):
        app = _app(scale_app)
        resp = await client.post(URL, json={"response": {"approved": True, "to": ""}}, headers=api_key["headers"])
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"].startswith("To is empty")
        app.state.arq_redis.enqueue_job.assert_not_called()

    async def test_a_decline_is_taken(self, client, scale_app, api_key):
        app = _app(scale_app)
        resp = await client.post(URL, json={"response": {"approved": False, "to": ""}}, headers=api_key["headers"])
        assert resp.status_code == 202, resp.text
