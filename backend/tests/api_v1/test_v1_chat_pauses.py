"""
A chat turn that stops for a person says so (F68).

A tool whose policy is `confirm` ends the turn with `human_input_required` and
nothing after it — the call has not run. The v1 sync endpoint had no `final`
event, so it answered "I completed the requested actions."; the stream dropped
the event and never said `done`; internal `/chat` answered the same false text.
An orchestrator agent whose orchestration pauses on a human step ended the same
way. Now the status is honest and the pending call is named. There is no v1
approve route — that waits until someone asks for one.
"""
from _fakes import engine_events as E
from _fakes.fake_redis_stream import data_events

APPROVAL = {
    "type": "human_input_required", "kind": "tool_approval",
    "prompt": "The agent wants to run **send_email** — it acts outside this workspace. Allow it?",
    "tool_name": "send_email", "tool_args": {"to": "a@b.c"}, "category": "external",
    "approval_id": "appr-1", "fields": [],
}
HUMAN_STEP = {"type": "human_input_required", "orch_step_id": "h", "prompt": "Ship it?",
              "fields": [], "run_id": "run_orch_1"}


class TestV1Sync:
    async def test_an_approval_pause_is_awaiting_approval(self, client, api_key, seed_agent, monkeypatch):
        await seed_agent()
        import core.react_engine as re
        monkeypatch.setattr(re, "run_react_loop", E.gen_from([E.status(), APPROVAL]))

        body = (await client.post("/api/v1/chat", json={"message": "email them"},
                                  headers=api_key["headers"])).json()

        assert body["status"] == "awaiting_approval"
        assert "completed" not in body["response"].lower()
        assert body["pending_call"] == {"tool_name": "send_email", "tool_args": {"to": "a@b.c"},
                                        "category": "external", "approval_id": "appr-1"}

    async def test_an_orchestration_pause_is_paused_with_its_run(self, client, api_key, seed_agent, monkeypatch):
        await seed_agent()
        import core.react_engine as re
        monkeypatch.setattr(re, "run_react_loop", E.gen_from([HUMAN_STEP]))

        body = (await client.post("/api/v1/chat", json={"message": "go"}, headers=api_key["headers"])).json()

        assert body["status"] == "paused"
        assert body["run_id"] == "run_orch_1"
        assert body["response"] == "Ship it?"

    async def test_an_answer_is_completed(self, client, api_key, seed_agent, monkeypatch):
        await seed_agent()
        import core.react_engine as re
        monkeypatch.setattr(re, "run_react_loop", E.gen_from([E.final("done it")]))

        body = (await client.post("/api/v1/chat", json={"message": "hi"}, headers=api_key["headers"])).json()

        assert body["status"] == "completed"
        assert body["response"] == "done it"


class TestV1Stream:
    async def test_an_approval_pause_is_sent_and_the_stream_ends(self, client, api_key, seed_agent, monkeypatch):
        await seed_agent()
        import core.react_engine as re
        monkeypatch.setattr(re, "run_react_loop", E.gen_from([APPROVAL]))

        resp = await client.post("/api/v1/chat/stream", json={"message": "email them"},
                                 headers=api_key["headers"])
        events = data_events(resp.text.split("\n\n"))

        pause = next(e for e in events if e["type"] == "awaiting_approval")
        assert pause["pending_call"]["tool_name"] == "send_email"
        assert pause["session_id"]
        assert events[-1] == {"type": "done"}

    async def test_an_orchestration_pause_is_sent(self, client, api_key, seed_agent, monkeypatch):
        await seed_agent()
        import core.react_engine as re
        monkeypatch.setattr(re, "run_react_loop", E.gen_from([HUMAN_STEP]))

        resp = await client.post("/api/v1/chat/stream", json={"message": "go"}, headers=api_key["headers"])
        events = data_events(resp.text.split("\n\n"))

        pause = next(e for e in events if e["type"] == "paused")
        assert pause["run_id"] == "run_orch_1"
        assert events[-1] == {"type": "done"}


class TestInternalChat:
    async def test_an_approval_pause_is_not_reported_as_done(self, client, monkeypatch):
        import core.routes.chat as chat
        monkeypatch.setattr(chat, "run_react_loop", E.gen_from([E.status(), APPROVAL]))

        body = (await client.post("/chat", json={"message": "email them"})).json()

        assert body["intent"] == "awaiting_approval"
        assert body["response"] == APPROVAL["prompt"]
        assert body["data"]["pending_call"]["approval_id"] == "appr-1"
