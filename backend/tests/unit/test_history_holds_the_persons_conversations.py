"""Chat History holds the person's conversations (F261).

Chatting with "L1 Launch Planner" (an orchestrator agent whose turn paused for
an approval, answered in chat, and completed) left its own conversation at
`turn_count 0` — it could not be reopened — while History listed the workflow's
step agents as chats (ST_W_WRITER, ST_W_RESEARCH with 4 turns across three loop
iterations, each titled with its multi-kilobyte rendered prompt) and three
`subtask_*` sessions from `spawn_subtask`. Of 23 sessions, a handful were
conversations anyone had.

An orchestration's step works for its run, not in a conversation: it neither
reads nor writes chat history — the conversation reaches its model steps as
`state.history` (F260) — and a sub-agent is a hand-off like a delegate. A
workflow agent's turn that paused is saved once, by whatever finishes its run.
"""
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from tests.unit.test_human_in_containers import _human, driver, worker  # noqa: F401 — fixtures


def _server():
    return types.SimpleNamespace(agent_sessions={"x": object()}, memory_store=None, tool_router={})


async def _rows(session_id: str) -> dict:
    from core.store import sessions as store
    return {r["agent_id"]: r.get("messages") or [] for r in await store.load()
            if r["session_id"] == session_id}


async def _chat(agent, message, session_id, **kw):
    from core.models import ChatRequest
    from core.react_engine import run_react_loop
    return [e async for e in run_react_loop(
        ChatRequest(message=message, session_id=session_id, agent_id=agent["id"]), _server(), **kw)]


def _step(sid, agent_id, nxt=None):
    return {"id": sid, "name": sid.upper(), "type": "agent", "agent_id": agent_id,
            "prompt_template": "## YOUR ROLE IN THIS WORKFLOW\nWrite about {state.user_input}",
            "output_key": f"o_{sid}", "next_step_id": nxt}


async def _workflow_agent(orch_id, steps, entry, others=()):
    """The workflow agent, seeded with every other agent in one write (the store is replaced)."""
    orch = S.make_orchestration(id=orch_id, entry_step_id=entry, steps=steps)
    await S.seed_orchestrations([orch])
    agent = S.make_orchestrator_agent(orch_id, id=f"agent_{orch_id}", name="L1 Launch Planner")
    await S.seed_agents([agent, *others])
    return agent


class TestAWorkflowsStepAgents:
    async def test_they_leave_no_conversation_of_their_own(self, fake_llm):
        writer = S.make_agent(id="agent_writer", tools=[], skip_default_tools=True)
        planner = await _workflow_agent("orch_f261", [_step("write", "agent_writer")], "write", [writer])
        fake_llm.set_default("a draft")

        await _chat(planner, "plan the launch", "s_f261")
        await _chat(planner, "and the follow-up", "s_f261")

        rows = await _rows("s_f261")
        assert list(rows) == [planner["id"]], f"only the person's conversation: {list(rows)}"
        assert [m["content"] for m in rows[planner["id"]] if m["role"] == "user"] == [
            "plan the launch", "and the follow-up"]
        step_calls = [c for c in fake_llm.calls if "YOUR ROLE IN THIS WORKFLOW" in c.get("prompt_msg", "")]
        assert step_calls and all(not c.get("history_messages") for c in step_calls), (
            "a step reads no chat history — the conversation reaches it as state.history")

    async def test_an_editor_run_leaves_none_either(self, fake_llm):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        from core.store import sessions as store

        writer = S.make_agent(id="agent_writer2", tools=[], skip_default_tools=True)
        await S.seed_agents([writer])
        orch = S.make_orchestration(id="orch_f261e", entry_step_id="write",
                                    steps=[_step("write", "agent_writer2")])
        fake_llm.set_default("a draft")
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        [e async for e in engine.run("go", run_id="run_f261e")]
        assert not [r for r in await store.load() if r["agent_id"] == "agent_writer2"]


class TestASubtask:
    async def test_it_leaves_no_session(self, fake_llm):
        from core.react_engine import run_agent_step
        from core.store import sessions as store

        lead = S.make_agent(id="agent_lead", tools=["all", "spawn_subtask"], skip_default_tools=True)
        fake_llm.script([tool_call("spawn_subtask", task="summarise A"), "sub answer", "done"])
        [e async for e in run_agent_step(message="split it", agent_id=lead["id"], session_id="s_sub",
                                         server_module=_server(), agent_override=lead, max_turns=4)]
        agents = [r["agent_id"] for r in await store.load() if r["session_id"] == "s_sub"]
        assert agents == ["agent_lead"], agents


class TestAPausedWorkflowTurn:
    async def test_it_is_saved_once_when_its_run_is_answered(self, fake_llm, driver):
        planner = await _workflow_agent("orch_f261p", [
            _human("confirm", prompt="Ship it?", nxt="say"),
            {"id": "say", "name": "SAY", "type": "llm", "prompt_template": "Report: {state.answer}",
             "model": "gpt-4o", "output_key": "reply", "next_step_id": None}], "confirm")
        fake_llm.set_default("Launch plan approved and filed.")

        events = await _chat(planner, "plan the launch", f"s_f261p_{driver.kind}")
        run_id = next(e["run_id"] for e in events if e.get("type") == "orchestration_start")
        assert await _rows(f"s_f261p_{driver.kind}") == {}, "a paused turn has not answered yet"

        assert await driver.answer(run_id, {"answer": "yes"}) == "completed"

        rows = await _rows(f"s_f261p_{driver.kind}")
        assert [(m["role"], m["content"]) for m in rows[planner["id"]]] == [
            ("user", "plan the launch"), ("assistant", "Launch plan approved and filed.")]

    async def test_an_approval_answered_in_chat_is_saved_once(self, client, fake_llm):
        """F290's path: Allow resumes the run from the chat itself — one pair, not two."""
        import unittest.mock as _m

        import core.react_engine as re_mod

        worker_id = "agent_f261_worker"
        planner = await _workflow_agent("orch_f261a", [
            {"id": "work", "name": "Do the work", "type": "agent", "agent_id": worker_id,
             "prompt_template": "go", "output_key": "result", "max_turns": 4, "next_step_id": None}], "work",
            [S.make_agent(id=worker_id, tools=["all"], skip_default_tools=True,
                          tool_policy={"undeclared": "confirm"})])
        real = re_mod.run_agent_step

        async def wrapped(**kwargs):
            if kwargs.get("agent_id") == worker_id:
                async def executor(name, args):
                    return f"ran-{name}"
                kwargs.setdefault("tools_override", [{"type": "function", "function": {
                    "name": "danger", "description": "d", "parameters": {"type": "object", "properties": {}}}}])
                kwargs.setdefault("tool_executor", executor)
            async for ev in real(**kwargs):
                yield ev

        fake_llm.script([tool_call("danger"), "worker: done"])
        with _m.patch.object(re_mod, "run_agent_step", wrapped):
            resp = await client.post("/chat/stream", json={
                "message": "clean up", "agent_id": planner["id"], "session_id": "s_f261a"})
            assert '"tool_approval"' in resp.text
            assert await _rows("s_f261a") == {} or not (await _rows("s_f261a")).get(planner["id"])
            resp = await client.post("/chat/s_f261a/approve", json={"approve": True, "agent_id": planner["id"]})
            assert resp.status_code == 200

        rows = await _rows("s_f261a")
        assert [(m["role"], m["content"]) for m in rows[planner["id"]]] == [
            ("user", "clean up"), ("assistant", "worker: done")], rows


class TestTheFleetChatJob:
    async def test_a_workflow_waiting_on_a_person_leaves_no_empty_answer(self, monkeypatch):
        from core.scale import mcp_pool
        from core.scale import worker as worker_mod
        import core.react_engine as re_mod
        upserts = []

        async def history(*_a):
            return [{"role": "user", "content": "before"}, {"role": "assistant", "content": "earlier"}]

        async def upsert(_sf, _sid, _aid, status, messages):
            upserts.append((status, [dict(m) for m in messages]))

        async def nothing(*_a, **_kw):
            return None

        async def acquire(*_a, **_kw):
            return object()

        async def loop(request, server_module, should_stop=None, **_kw):
            yield {"type": "orchestration_start", "run_id": "run_w"}
            yield {"type": "human_input_required", "orch_step_id": "confirm", "prompt": "Ship it?",
                   "run_id": "run_w", "fields": []}

        monkeypatch.setattr(worker_mod, "_load_job_settings", nothing)
        monkeypatch.setattr(worker_mod, "_load_chat_history", history)
        monkeypatch.setattr(worker_mod, "_upsert_chat_session", upsert)
        monkeypatch.setattr(mcp_pool, "acquire", acquire)
        monkeypatch.setattr(mcp_pool, "release", nothing)
        monkeypatch.setattr(re_mod, "run_react_loop", loop)
        monkeypatch.setattr(worker_mod, "CHAT_CANCEL_POLL_SECONDS", 0.01)

        import fakeredis.aioredis
        await worker_mod.run_agent_chat_job(
            {"redis": fakeredis.aioredis.FakeRedis(), "session_factory": None, "job_try": 1, "job_id": "j"},
            "s_fleet", "agent_x", "plan the launch")

        status, messages = upserts[-1]
        assert messages == [{"role": "user", "content": "before"}, {"role": "assistant", "content": "earlier"}], (
            "the run's end saves the turn; the pause writes nothing")


class TestTheListLeavesOutWhatRunsLeft:
    async def test_step_and_subtask_rows_already_saved_are_not_listed(self, client):
        from core.session import _save_conversation_turn

        await _save_conversation_turn("s_person", "agent_a", "hello", "hi")
        await _save_conversation_turn("orch_run_old_123", "agent_writer", "## YOUR ROLE IN THIS WORKFLOW", "x")
        await _save_conversation_turn("s_person", "subtask_1a2b3c4d", "summarise A", "y")
        listed = [(s["session_id"], s["agent_id"]) for s in (await client.get("/api/sessions")).json()]
        assert listed == [("s_person", "agent_a")], listed
