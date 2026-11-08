"""A new message in a conversation refuses the call it was waiting on (F247).

A card asked to run `echo "second" >> hello.txt`; the person typed a new
message instead and got an answer. The old card still offered Allow once /
Refuse, a reload drew it *after* the newer turn, as if it were the current
question, and Allow then ran the abandoned command.

Now the new message settles the question first: the call is not run, the turn
that asked is saved in its own place with the card marked expired, the model is
told the call was not run, and nothing can allow it afterwards. A workflow's
call stops the run that asked. The same holds for v1 and a worker's turn.
"""
import unittest.mock as _m

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from core.store import sessions as store

AGENT = "agent_f247"
DANGER = {"type": "function", "function": {"name": "danger", "description": "appends to a file",
                                           "parameters": {"type": "object",
                                                          "properties": {"line": {"type": "string"}}}}}


@pytest.fixture
async def agent():
    a = S.make_agent(id=AGENT, name="Pair", tools=["all"], skip_default_tools=True,
                     tool_policy={"undeclared": "confirm"})
    await S.seed_agents([a])
    return a


@pytest.fixture
def executed():
    """The gated tool, run for real only if something allows it."""
    import core.react_engine as re_mod
    real = re_mod.run_agent_step
    ran: list[tuple[str, dict]] = []

    async def wrapped(**kwargs):
        if kwargs.get("agent_id") in (AGENT, "agent_f247_worker"):
            async def executor(name, args):
                ran.append((name, args))
                return f"ran-{name}"
            kwargs.setdefault("tools_override", [DANGER])
            kwargs.setdefault("tool_executor", executor)
        async for ev in real(**kwargs):
            yield ev

    with _m.patch.object(re_mod, "run_agent_step", wrapped):
        yield ran


async def _ask(client, fake_llm, session="s_f247", line="second"):
    fake_llm.script([tool_call("danger", line=line)])
    resp = await client.post("/chat/stream", json={"message": f"append {line} to hello.txt",
                                                   "agent_id": AGENT, "session_id": session})
    assert resp.status_code == 200 and '"tool_approval"' in resp.text
    pending = await store.get_pending_approval(session, AGENT)
    assert pending and pending["tool_name"] == "danger"
    return pending


class TestANewMessageRefusesTheCall:
    async def test_the_call_is_not_run_and_cannot_be_allowed_after(self, client, agent, executed, fake_llm):
        await _ask(client, fake_llm)
        fake_llm.script(["It is noon."])
        resp = await client.post("/chat/stream", json={"message": "Actually, never mind. What time is it?",
                                                       "agent_id": AGENT, "session_id": "s_f247"})
        assert resp.status_code == 200 and "It is noon." in resp.text

        assert await store.get_pending_approval("s_f247", AGENT) is None
        late = await client.post("/chat/s_f247/approve", json={"approve": True, "agent_id": AGENT})
        assert late.status_code == 409
        assert executed == [], "the abandoned call never runs"

    async def test_the_turn_that_asked_keeps_its_place_marked_expired(self, client, agent, executed, fake_llm):
        await _ask(client, fake_llm)
        fake_llm.script(["It is noon."])
        await client.post("/chat/stream", json={"message": "What time is it?", "agent_id": AGENT,
                                                "session_id": "s_f247"})

        body = (await client.get("/api/sessions/s_f247/history", params={"agent_id": AGENT})).json()
        assert body["pending_approval"] is None, "a reopened page has no live card to draw last"
        turns = body["turns"]
        assert [t["user"] for t in turns] == ["append second to hello.txt", "What time is it?"]
        approval = turns[0]["approval"]
        assert approval["outcome"] == "expired"
        assert approval["tool_name"] == "danger" and approval["tool_args"] == {"line": "second"}
        assert "not run" in turns[0]["assistant"].lower()
        [call] = [c for c in turns[0]["calls"] if c["tool"] == "danger"]
        assert call["result"].lower().startswith("not run")
        assert "approval" not in turns[1] or not turns[1]["approval"]

    async def test_the_model_is_told_the_call_was_not_run(self, client, agent, executed, fake_llm):
        await _ask(client, fake_llm)
        fake_llm.script(["It is noon."])
        start = fake_llm.call_count
        await client.post("/chat/stream", json={"message": "What time is it?", "agent_id": AGENT,
                                                "session_id": "s_f247"})
        history = fake_llm.calls[start]["history_messages"]
        assert [m["role"] for m in history] == ["user", "assistant"]
        assert history[0]["content"] == "append second to hello.txt"
        told = history[1]["content"]
        assert "danger" in told and "not run" in told.lower()

    async def test_an_answer_names_the_question_it_answers(self, client, agent, executed, fake_llm):
        """Two tabs: one moved on and was asked again; the other still holds the
        first card. Its Allow must not answer the second question."""
        first = await _ask(client, fake_llm, line="second")
        second = await _ask(client, fake_llm, line="third")
        assert first["approval_id"] != second["approval_id"]

        stale = await client.post("/chat/s_f247/approve", json={
            "approve": True, "agent_id": AGENT, "approval_id": first["approval_id"]})
        assert stale.status_code == 409
        assert (await store.get_pending_approval("s_f247", AGENT))["approval_id"] == second["approval_id"]

        fake_llm.script(["Appended third."])
        ok = await client.post("/chat/s_f247/approve", json={
            "approve": True, "agent_id": AGENT, "approval_id": second["approval_id"]})
        assert ok.status_code == 200
        assert executed == [("danger", {"line": "third"})]

        turns = (await client.get("/api/sessions/s_f247/history", params={"agent_id": AGENT})).json()["turns"]
        assert [t["user"] for t in turns] == ["append second to hello.txt", "append third to hello.txt"]
        assert turns[0]["approval"]["outcome"] == "expired"


class TestAWorkflowsCall:
    async def test_the_run_that_asked_is_stopped(self, client, executed, fake_llm):
        worker = "agent_f247_worker"
        orch = S.make_orchestration(id="orch_f247", name="Append", entry_step_id="work", steps=[
            {"id": "work", "name": "Do it", "type": "agent", "agent_id": worker, "prompt_template": "go",
             "output_key": "result", "max_turns": 4, "next_step_id": None}])
        wf = S.make_orchestrator_agent("orch_f247", id="orch_agent_f247", name="Append")
        await S.seed_orchestrations([orch])
        await S.seed_agents([wf, S.make_agent(id=worker, tools=["all"], skip_default_tools=True,
                                              tool_policy={"undeclared": "confirm"})])
        from core.orchestration.state import SharedState

        fake_llm.script([tool_call("danger", line="second")])
        resp = await client.post("/chat/stream", json={"message": "append", "agent_id": wf["id"],
                                                       "session_id": "s_f247_wf"})
        assert '"tool_approval"' in resp.text
        [asked] = [r for r in await SharedState.list_runs(limit=20) if r["orchestration_id"] == "orch_f247"]
        assert asked["status"] == "paused"

        fake_llm.script(["worker: nothing to do"])
        await client.post("/chat/stream", json={"message": "never mind", "agent_id": wf["id"],
                                                "session_id": "s_f247_wf"})

        runs = {r["run_id"]: r for r in await SharedState.list_runs(limit=20)
                if r["orchestration_id"] == "orch_f247"}
        assert runs[asked["run_id"]]["status"] == "cancelled", "the run that asked does not wait for ever"
        assert executed == []
        assert await store.get_pending_approval("s_f247_wf", wf["id"]) is None


class TestV1:
    async def test_a_new_message_refuses_it_too(self, client, api_key, agent, executed, fake_llm):
        fake_llm.script([tool_call("danger", line="second")])
        resp = await client.post("/api/v1/chat", json={"message": "append second", "agent": AGENT,
                                                       "session_id": "v1_f247"}, headers=api_key["headers"])
        assert resp.json()["status"] == "awaiting_approval"

        fake_llm.script(["It is noon."])
        resp = await client.post("/api/v1/chat", json={"message": "what time is it?", "agent": AGENT,
                                                       "session_id": "v1_f247"}, headers=api_key["headers"])
        assert resp.json()["status"] == "completed"
        assert await store.get_pending_approval("v1_f247", AGENT) is None
        late = await client.post("/api/v1/chat/v1_f247/approve", json={"approve": True},
                                 headers=api_key["headers"])
        assert late.status_code == 409
        assert executed == []


# The fleet's chat job (v2, and cloud's chat) is the same door on a worker.
from tests.unit.test_chat_approval_queued import _FakeRedis, _patch_loop, chat_job  # noqa: E402,F401

_real_set_pending = store.set_pending_approval   # before `chat_job` swaps it for a recorder


class TestAWorkersTurn:
    async def test_the_moved_past_turn_is_written_before_the_new_one(self, chat_job, monkeypatch):
        from core.scale import worker
        upserts, parked = chat_job
        await _real_set_pending("chat_1", "default", {
            "tool_name": "run_sql_query", "tool_args": {"sql": "delete from invoices"},
            "approval_id": "a1b2c3d4", "category": "destructive", "message": "clean up the invoices",
            "progress": {"calls": [{"tool": "read_file", "args": {"path": "a"}, "result": "rows"}]},
        })
        _patch_loop(monkeypatch, [{"type": "final", "response": "It is noon."}])

        result = await worker.run_agent_chat_job({"redis": _FakeRedis(), "session_factory": object()},
                                                 "chat_1", None, "what time is it?")

        assert result["status"] == "completed"
        assert await store.get_pending_approval("chat_1", "default") is None
        running = next(messages for status, messages in upserts if status == "running")
        assert [m["content"] for m in running[2:3]] == ["clean up the invoices"], (
            "written before the loop reads the conversation")
        status, messages = upserts[-1]
        assert [m["role"] for m in messages] == ["user", "assistant"] * 3
        moved = messages[3]
        assert moved["approval"]["outcome"] == "expired" and "not run" in moved["content"].lower()
        assert [c["tool"] for c in moved["calls"]] == ["read_file", "run_sql_query"]
        assert messages[4]["content"] == "what time is it?" and messages[5]["content"] == "It is noon."

    async def test_a_resume_is_not_a_new_message(self, chat_job, monkeypatch):
        from core.scale import worker
        await _real_set_pending("chat_1", "default", {"tool_name": "x", "approval_id": "a", "message": "m"})
        _patch_loop(monkeypatch, [{"type": "final", "response": "done"}])
        await worker.run_agent_chat_job({"redis": _FakeRedis(), "session_factory": object()},
                                        "chat_1", None, "m", resume_state={"turn": 1})
        assert await store.get_pending_approval("chat_1", "default") is not None, (
            "only a new message settles the question; an answer's own resume never does")
