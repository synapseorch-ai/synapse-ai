"""
An API caller's answer to a person's question has to say something (F232).

`POST /api/v1/orchestrations/runs/{id}/resume` with `{"human_response": "first"}`
(the field is `response`) answered 200 and the run carried on: `response`
defaulted to `{}` and unknown fields were ignored, so a typo answered the
question with nothing ("Round 0: {}"). Every v1 body now refuses a field it
does not know (422 naming it, as cloud's v1 does), and the API doors — v1, v2
and MCP's `answer_run` — refuse an empty answer. The app's own route and a
channel reply are unchanged (Naveen, 2026-09-26).
"""
from __future__ import annotations

import pytest

from tests.api_app.test_mcp_server import mcp_app  # noqa: F401 — fixture
from tests.api_v1.test_v1_run_control import _status, paused_run  # noqa: F401 — fixture

EMPTY = "The answer is empty. Send text, or an object keyed by the question's field names."
ROUTES = ["resume", "resume/stream"]


def _extra(resp, field: str) -> bool:
    return resp.status_code == 422 and any(
        e.get("type") == "extra_forbidden" and e["loc"][-1] == field for e in resp.json()["detail"])


class TestV1Resume:
    @pytest.mark.parametrize("route", ROUTES)
    async def test_a_misspelt_field_is_refused_by_name(self, client, api_key, paused_run, route):
        resp = await client.post(f"/api/v1/orchestrations/runs/{paused_run}/{route}",
                                 json={"human_response": "first"}, headers=api_key["headers"])
        assert _extra(resp, "human_response"), resp.text
        assert await _status(paused_run) == "paused", "the question is still open"

    @pytest.mark.parametrize("route", ROUTES)
    async def test_no_answer_at_all_is_refused(self, client, api_key, paused_run, route):
        resp = await client.post(f"/api/v1/orchestrations/runs/{paused_run}/{route}",
                                 json={}, headers=api_key["headers"])
        assert resp.status_code == 422, resp.text
        assert await _status(paused_run) == "paused"

    @pytest.mark.parametrize("route", ROUTES)
    @pytest.mark.parametrize("answer", ["", "   ", {}, {"response": ""}, {"response": " "}])
    async def test_an_empty_answer_is_refused(self, client, api_key, paused_run, route, answer):
        resp = await client.post(f"/api/v1/orchestrations/runs/{paused_run}/{route}",
                                 json={"response": answer}, headers=api_key["headers"])
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == EMPTY
        assert await _status(paused_run) == "paused"

    async def test_fields_left_blank_are_an_answer(self, client, api_key, paused_run):
        """What the app's form sends for a fielded question nobody filled in."""
        resp = await client.post(f"/api/v1/orchestrations/runs/{paused_run}/resume",
                                 json={"response": {"note": ""}}, headers=api_key["headers"])
        assert resp.status_code == 200, resp.text


class TestEveryV1Body:
    @pytest.mark.parametrize("path,body,field", [
        ("/api/v1/chat", {"message": "hi", "agnet": "x"}, "agnet"),
        ("/api/v1/chat/stream", {"message": "hi", "agnet": "x"}, "agnet"),
        ("/api/v1/orchestrations/orch_x/run", {"input": "x"}, "input"),
        ("/api/v1/orchestrations/orch_x/run/stream", {"input": "x"}, "input"),
        ("/api/v1/chat/s1/approve", {"approve": True, "approvalid": "a"}, "approvalid"),
    ])
    async def test_a_field_it_does_not_know_is_refused_by_name(self, client, api_key, path, body, field):
        resp = await client.post(path, json=body, headers=api_key["headers"])
        assert _extra(resp, field), resp.text


class TestV2Resume:
    @pytest.fixture
    async def v2(self, scale_app):
        from _fakes.fake_pg import fake_session_factory, run_row
        scale_app.state.pg_session_factory = fake_session_factory(
            run_row("run_1", status="paused", waiting_for_human=True))
        return scale_app

    @pytest.mark.parametrize("body", [{}, {"response": ""}, {"response": {}}, {"human_response": "x"}])
    async def test_an_empty_or_misspelt_answer_is_not_queued(self, client, v2, api_key, body):
        resp = await client.post("/api/v2/orchestrations/runs/run_1/resume", json=body,
                                 headers=api_key["headers"])
        assert resp.status_code == 422, resp.text
        v2.state.arq_redis.enqueue_job.assert_not_called()


class TestMcpAnswerRun:
    async def test_an_empty_answer_is_an_error_and_the_run_keeps_waiting(
        self, mcp_app, api_key, seed_orchestration,
    ):
        from tests.api_app.test_mcp_server import _ASK_THEN_THINK, _body, _paused_run, mcp_session

        orch = await seed_orchestration(entry_step_id="ask", steps=_ASK_THEN_THINK)
        async with mcp_session(mcp_app, api_key) as s:
            run = await _paused_run(s, orch["id"])
            empty = await s.call_tool("answer_run", {"run_id": run["run_id"], "answer": "  "})
            assert empty.isError and _body(empty) == {"error": EMPTY}
            assert await _status(run["run_id"]) == "paused"
            done = _body(await s.call_tool("answer_run", {"run_id": run["run_id"], "answer": "Acme",
                                                          "wait_seconds": 10}))
            assert done["status"] == "completed", done

