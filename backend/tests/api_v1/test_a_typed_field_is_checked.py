"""An answer to an email, date, phone or dropdown field is checked at every door (F455).

The app's Answer control and v1's resume both go through `answer_paused_run`, so a
bad answer is a 422 naming the field and the question stays open.
"""
from __future__ import annotations

import pytest

from tests.api_app.test_mcp_server import mcp_app  # noqa: F401 — fixture
from tests.api_v1.test_v1_run_control import _status, _until_status

_ASK_FIELDS = [
    {"id": "ask", "name": "Ask", "type": "human", "human_prompt": "ok?", "output_key": "answer",
     "human_fields": [
         {"name": "email", "type": "email", "label": "Work email"},
         {"name": "pick", "type": "choice", "label": "Decision", "options": ["Approve", "Later"]},
     ],
     "next_step_id": "s"},
    {"id": "s", "name": "Think", "type": "llm", "prompt_template": "hi", "output_key": "out",
     "next_step_id": None},
]


@pytest.fixture
async def fielded_run(client, api_key, seed_orchestration):
    orch = await seed_orchestration(entry_step_id="ask", steps=_ASK_FIELDS)
    started = await client.post(f"/api/v1/orchestrations/{orch['id']}/run",
                                json={"message": "go"}, headers=api_key["headers"])
    run_id = started.json()["run_id"]
    assert await _until_status(run_id, "paused")
    return run_id


class TestEveryDoorChecks:
    async def test_v1_refuses_a_bad_email_by_name_and_keeps_the_question(self, client, api_key, fielded_run):
        resp = await client.post(f"/api/v1/orchestrations/runs/{fielded_run}/resume",
                                 json={"response": {"email": "nope", "pick": "Approve"}},
                                 headers=api_key["headers"])
        assert resp.status_code == 422, resp.text
        assert "Work email" in resp.json()["detail"]
        assert await _status(fielded_run) == "paused"

    async def test_the_app_refuses_an_option_that_is_not_there(self, client, fielded_run):
        resp = await client.post(f"/api/orchestrations/runs/{fielded_run}/human-input",
                                 json={"response": {"email": "ana@example.com", "pick": "maybe"}})
        assert resp.status_code == 422, resp.text
        assert "Approve, Later" in resp.json()["detail"]
        assert await _status(fielded_run) == "paused"

    async def test_a_good_answer_resumes_with_the_option_as_written(self, client, api_key, fielded_run):
        from core.orchestration.state import SharedState

        resp = await client.post(f"/api/v1/orchestrations/runs/{fielded_run}/resume",
                                 json={"response": {"email": "ana@example.com", "pick": "later"}},
                                 headers=api_key["headers"])
        assert resp.status_code == 200, resp.text
        state = (await SharedState.restore(fielded_run)).run.shared_state
        assert state["answer"] == {"email": "ana@example.com", "pick": "Later"}


class TestV2AndMcp:
    @pytest.fixture
    async def v2(self, scale_app):
        from _fakes.fake_pg import fake_session_factory, run_row
        scale_app.state.pg_session_factory = fake_session_factory(
            run_row("run_1", status="paused", waiting_for_human=True,
                    human_fields=[{"name": "when", "type": "date", "label": "Start date"}]))
        return scale_app

    async def test_v2_refuses_a_bad_date_before_anything_is_queued(self, client, v2, api_key):
        resp = await client.post("/api/v2/orchestrations/runs/run_1/resume",
                                 json={"response": {"when": "next tuesday"}}, headers=api_key["headers"])
        assert resp.status_code == 422, resp.text
        assert "Start date" in resp.json()["detail"]
        v2.state.arq_redis.enqueue_job.assert_not_called()

    async def test_mcp_answer_run_refuses_a_bad_email_and_the_run_keeps_waiting(
        self, mcp_app, api_key, seed_orchestration,  # noqa: F811 — fixture
    ):
        from tests.api_app.test_mcp_server import _body, _paused_run, mcp_session

        orch = await seed_orchestration(entry_step_id="ask", steps=_ASK_FIELDS)
        async with mcp_session(mcp_app, api_key) as s:
            run = await _paused_run(s, orch["id"])
            bad = await s.call_tool("answer_run", {"run_id": run["run_id"],
                                                   "answer": {"email": "nope", "pick": "Approve"}})
            assert bad.isError and "Work email" in _body(bad)["error"]
            assert await _status(run["run_id"]) == "paused"
