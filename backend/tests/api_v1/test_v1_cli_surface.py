"""What v1 lacked for a client that is not the app (D126).

The CLI talks to v1 alone, with an API key, so the same client drives a local
install, a remote one and — later — cloud. v1 had no run list, no run history,
no schedules and no way to answer a chat turn's tool approval; the app reads all
four through internal routes an API key cannot reach. It also had no way to
give a run its inputs. These are the v1 routes for each.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from _fakes import engine_events as E
from _fakes.fake_redis_stream import data_events

_THINK = [{"id": "s", "name": "Think", "type": "llm",
           "prompt_template": "hi", "output_key": "out", "next_step_id": None}]
_ASK = [{"id": "ask", "name": "Ask", "type": "human", "human_prompt": "ok?",
         "output_key": "answer", "next_step_id": None}]


async def _status(run_id: str) -> str | None:
    from core.orchestration.state import SharedState
    try:
        return (await SharedState.restore(run_id)).run.status
    except FileNotFoundError:
        return None


async def _until_status(run_id: str, status: str, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await _status(run_id) == status:
            return True
        await asyncio.sleep(0.05)
    return False


async def _seed(*orchs: dict) -> list[dict]:
    """Seed several orchestrations at once: the store saves the whole list."""
    from _fakes import seed as _s
    made = [_s.make_orchestration(**o) for o in orchs]
    await _s.seed_orchestrations(made)
    return made


async def _run(client, api_key, orch_id: str, **body) -> str:
    started = await client.post(f"/api/v1/orchestrations/{orch_id}/run",
                                json={"message": "go", **body}, headers=api_key["headers"])
    assert started.status_code == 200, started.text
    return started.json()["run_id"]


# ── Runs: a list ───────────────────────────────────────────────────────────


class TestRunList:
    async def test_the_list_is_not_read_as_an_orchestration_named_runs(self, client, api_key):
        """`/orchestrations/{orch_id}` would take `runs` as an id if it were
        declared first, and answer 404 "Orchestration 'runs' not found"."""
        resp = await client.get("/api/v1/orchestrations/runs", headers=api_key["headers"])
        assert resp.status_code == 200, resp.text
        assert resp.json() == []

    async def test_runs_of_every_orchestration_newest_first(self, client, api_key, fake_llm):
        one, two = await _seed(dict(id="orch_one", name="One", entry_step_id="s", steps=_THINK),
                               dict(id="orch_two", name="Two", entry_step_id="s", steps=_THINK))
        first = await _run(client, api_key, one["id"])
        second = await _run(client, api_key, two["id"])

        runs = (await client.get("/api/v1/orchestrations/runs", headers=api_key["headers"])).json()

        assert [r["run_id"] for r in runs] == [second, first]
        assert {"run_id", "orchestration_id", "status", "started_at",
                "waiting_for_human", "total_cost_usd"} <= set(runs[0])

    async def test_filtered_by_orchestration_and_status(self, client, api_key, fake_llm):
        think, ask = await _seed(dict(id="orch_think", name="Think", entry_step_id="s", steps=_THINK),
                                 dict(id="orch_ask", name="Ask", entry_step_id="ask", steps=_ASK))
        done = await _run(client, api_key, think["id"])
        waiting = await _run(client, api_key, ask["id"])
        assert await _until_status(waiting, "paused")

        h = api_key["headers"]
        by_orch = (await client.get(f"/api/v1/orchestrations/runs?orchestration_id={think['id']}",
                                    headers=h)).json()
        by_status = (await client.get("/api/v1/orchestrations/runs?status=paused", headers=h)).json()

        assert [r["run_id"] for r in by_orch] == [done]
        assert [r["run_id"] for r in by_status] == [waiting]

    async def test_limit(self, client, api_key, fake_llm, seed_orchestration):
        orch = await seed_orchestration(entry_step_id="s", steps=_THINK)
        for _ in range(3):
            await _run(client, api_key, orch["id"])

        runs = (await client.get("/api/v1/orchestrations/runs?limit=2",
                                 headers=api_key["headers"])).json()
        assert len(runs) == 2

    @pytest.mark.parametrize("query", ["status=done", "limit=0", "limit=201"])
    async def test_a_query_it_cannot_answer_is_refused(self, client, api_key, query):
        resp = await client.get(f"/api/v1/orchestrations/runs?{query}", headers=api_key["headers"])
        assert resp.status_code == 422, resp.text


# ── Runs: inputs ───────────────────────────────────────────────────────────


class TestRunInputs:
    @pytest.mark.parametrize("route", ["run", "run/stream"])
    async def test_initial_state_reaches_the_run(self, client, api_key, fake_llm,
                                                 seed_orchestration, route):
        from core.orchestration.state import SharedState

        orch = await seed_orchestration(entry_step_id="s", steps=_THINK)
        resp = await client.post(f"/api/v1/orchestrations/{orch['id']}/{route}",
                                 json={"message": "go", "initial_state": {"pages": 3}},
                                 headers=api_key["headers"])
        assert resp.status_code == 200, resp.text
        runs = (await client.get("/api/v1/orchestrations/runs", headers=api_key["headers"])).json()
        assert await _until_status(runs[0]["run_id"], "completed")

        state = (await SharedState.restore(runs[0]["run_id"])).run.shared_state
        assert state["pages"] == 3

    async def test_the_engines_own_keys_are_not_a_callers(self, client, api_key, seed_orchestration):
        """A leading underscore is engine bookkeeping (`_model_override`, …):
        a caller setting one would steer the engine, not give the run an input."""
        orch = await seed_orchestration(entry_step_id="s", steps=_THINK)
        resp = await client.post(f"/api/v1/orchestrations/{orch['id']}/run",
                                 json={"message": "go", "initial_state": {"_model_override": "x"}},
                                 headers=api_key["headers"])
        assert resp.status_code == 422, resp.text


# ── Runs: history ──────────────────────────────────────────────────────────


class TestRunEvents:
    async def test_a_finished_runs_events_from_a_cursor(self, client, api_key, fake_llm,
                                                        seed_orchestration):
        orch = await seed_orchestration(entry_step_id="s", steps=_THINK)
        run_id = await _run(client, api_key, orch["id"])
        h = api_key["headers"]

        whole = (await client.get(f"/api/v1/orchestrations/runs/{run_id}/events", headers=h)).json()
        types = [e["event"]["type"] for e in whole["events"]]
        assert "step_start" in types and "orchestration_complete" in types
        assert whole["last_id"] == whole["events"][-1]["id"]

        rest = (await client.get(f"/api/v1/orchestrations/runs/{run_id}/events?after={whole['last_id']}",
                                 headers=h)).json()
        assert rest["events"] == []

    async def test_the_stream_replays_and_ends(self, client, api_key, fake_llm, seed_orchestration):
        orch = await seed_orchestration(entry_step_id="s", steps=_THINK)
        run_id = await _run(client, api_key, orch["id"])

        resp = await client.get(f"/api/v1/orchestrations/runs/{run_id}/events/stream",
                                headers=api_key["headers"])
        assert resp.status_code == 200
        events = data_events(resp.text.split("\n\n"))
        assert any(e["type"] == "orchestration_complete" for e in events)
        assert events[-1] == {"type": "stream_complete"}
        assert "\nid: " in "\n" + resp.text, "frames carry ids, so a follower can reconnect"

    @pytest.mark.parametrize("route", ["events", "events/stream"])
    async def test_an_unknown_run_is_404(self, client, api_key, route):
        resp = await client.get(f"/api/v1/orchestrations/runs/run_nope_1/{route}",
                                headers=api_key["headers"])
        assert resp.status_code == 404


# ── Schedules ──────────────────────────────────────────────────────────────


@pytest.fixture
def schedule_manager(test_app):
    from core.scheduler import ScheduleManager
    mgr = ScheduleManager()
    mgr._server_module = test_app.state.server_module
    test_app.state.schedule_manager = mgr
    return mgr


_WEEKLY = {"name": "Weekly", "target_type": "orchestration", "target_id": "orch_x",
           "prompt": "report", "schedule_type": "cron", "cron_expression": "0 9 * * 1"}


class TestSchedules:
    async def test_create_read_pause_resume_delete(self, client, api_key, schedule_manager):
        h = api_key["headers"]
        made = await client.post("/api/v1/schedules", json={**_WEEKLY, "enabled": False}, headers=h)
        assert made.status_code == 200, made.text
        sid = made.json()["id"]
        assert made.json()["enabled"] is False

        assert [s["id"] for s in (await client.get("/api/v1/schedules", headers=h)).json()] == [sid]
        assert (await client.get(f"/api/v1/schedules/{sid}", headers=h)).json()["name"] == "Weekly"

        resumed = await client.patch(f"/api/v1/schedules/{sid}", json={"enabled": True}, headers=h)
        assert resumed.json()["enabled"] is True
        assert resumed.json()["next_run_at"]
        paused = await client.patch(f"/api/v1/schedules/{sid}", json={"enabled": False}, headers=h)
        assert paused.json()["enabled"] is False

        assert (await client.delete(f"/api/v1/schedules/{sid}", headers=h)).status_code == 200
        assert (await client.get(f"/api/v1/schedules/{sid}", headers=h)).status_code == 404

    async def test_triggering_an_unknown_schedule_is_404(self, client, api_key, schedule_manager):
        resp = await client.post("/api/v1/schedules/sched_nope/run", headers=api_key["headers"])
        assert resp.status_code == 404


# ── Chat: a tool approval ──────────────────────────────────────────────────

APPROVAL = {
    "type": "human_input_required", "kind": "tool_approval",
    "prompt": "The agent wants to run **send_email** — it acts outside this workspace. Allow it?",
    "tool_name": "send_email", "tool_args": {"to": "a@b.c"}, "category": "external",
    "approval_id": "appr-1", "fields": [],
}


async def _parked(client, api_key, agent, monkeypatch, session="cli_s1"):
    import core.react_engine as re
    monkeypatch.setattr(re, "run_react_loop", E.gen_from([E.status(), APPROVAL]))
    resp = await client.post("/api/v1/chat/stream", json={
        "message": "email them", "agent": agent["id"], "session_id": session,
    }, headers=api_key["headers"])
    assert any(e["type"] == "awaiting_approval" for e in data_events(resp.text.split("\n\n")))
    return session


class TestChatApproval:
    async def test_an_approval_resumes_the_turn_it_stopped(self, client, api_key, seed_agent,
                                                           monkeypatch):
        """v1 said a turn was awaiting approval and then had no way to answer
        it (F68 left it there). The turn now parks, as the app's does, and
        v1 answers it in v1's own frames."""
        import core.react_engine as re
        agent = await seed_agent()
        session = await _parked(client, api_key, agent, monkeypatch)

        seen = {}

        async def resumed(*args, **kwargs):
            seen["resume_state"] = kwargs.get("resume_state")
            yield {"type": "final", "response": "sent it"}
        monkeypatch.setattr(re, "run_react_loop", resumed)

        resp = await client.post(f"/api/v1/chat/{session}/approve", json={
            "approve": True, "approval_id": "appr-1", "agent": agent["id"],
        }, headers=api_key["headers"])
        assert resp.status_code == 200, resp.text
        events = data_events(resp.text.split("\n\n"))

        assert {"type": "response", "content": "sent it", "intent": "chat",
                "session_id": session} in events
        assert events[-1] == {"type": "done"}
        assert seen["resume_state"] is not None

    async def test_an_answer_naming_another_call_is_refused(self, client, api_key, seed_agent,
                                                            monkeypatch):
        from core.store import sessions as store
        agent = await seed_agent()
        session = await _parked(client, api_key, agent, monkeypatch)

        resp = await client.post(f"/api/v1/chat/{session}/approve", json={
            "approve": True, "approval_id": "appr-other", "agent": agent["id"],
        }, headers=api_key["headers"])

        assert resp.status_code == 409, resp.text
        assert await store.get_pending_approval(session, agent["id"]) is not None

    async def test_a_conversation_not_waiting_is_refused(self, client, api_key, seed_agent):
        agent = await seed_agent()
        resp = await client.post("/api/v1/chat/cli_nothing/approve", json={
            "approve": True, "agent": agent["id"],
        }, headers=api_key["headers"])
        assert resp.status_code == 409


# ── Every new route wants a key ────────────────────────────────────────────


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/v1/orchestrations/runs"),
    ("GET", "/api/v1/orchestrations/runs/r/events"),
    ("GET", "/api/v1/orchestrations/runs/r/events/stream"),
    ("GET", "/api/v1/schedules"),
    ("POST", "/api/v1/schedules"),
    ("GET", "/api/v1/schedules/s"),
    ("PATCH", "/api/v1/schedules/s"),
    ("DELETE", "/api/v1/schedules/s"),
    ("POST", "/api/v1/schedules/s/run"),
    ("POST", "/api/v1/chat/s/approve"),
])
async def test_every_new_route_wants_a_key(client, method, path):
    resp = await client.request(method, path, headers={"Authorization": "Bearer sk-syn-" + "0" * 32})
    assert resp.status_code == 401, (path, resp.status_code)
