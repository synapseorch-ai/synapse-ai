"""A turn that stops for a person's approval parks where it stands (F312, F319).

Every parker — chat, the fleet's chat job, v1, a schedule, a workflow's agent
step — parks the last `_step_progress` it saw. The engine only took one at the
end of a finished tool turn, so a gate asked before then parked whatever came
earlier: nothing at all in a turn's first step, the previous boundary
otherwise. Two things were lost:

* **F319** — what the turn reasoned and thought before the pause. A reopened
  answer had no reasoning, which is the usual case: the first step asks.
* **F312** — the work itself. Asked in the first turn after a resume, the
  parked snapshot was empty, so answering it started the task over at turn 0
  and the model redid it, tools with side effects included.

Now the engine snapshots itself at the gate, just before the card, from its
live state: an approval already spent on a replay is not in it, so it can
never run a call twice unasked.
"""
import json
import types
import unittest.mock as _m

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call

DANGER = {"type": "function", "function": {"name": "danger", "description": "d",
                                           "parameters": {"type": "object", "properties": {}}}}
LOOKUP = {"type": "function", "function": {"name": "lookup", "description": "l",
                                           "parameters": {"type": "object", "properties": {}}}}


def _server():
    return types.SimpleNamespace(agent_sessions={"x": object()}, memory_store=None, tool_router={})


@pytest.fixture
def only_danger_asks(monkeypatch):
    """`danger` waits on a person; every other tool runs. (A tool given as a
    dict declares nothing, so a policy alone cannot tell the two apart.)"""
    import core.tool_policy as tp

    def decide(tool, policy):
        name = getattr(tool, "name", None) or (tool.get("function") or {}).get("name")
        return ("confirm", "destructive") if name == "danger" else ("allow", "read_only")

    monkeypatch.setattr(tp, "decide", decide)


async def _step(fake_llm, script, *, executed, session_id, resume_state=None, agent=None):
    from core.react_engine import run_agent_step
    agent = agent or S.make_agent(tools=["all"], skip_default_tools=True,
                                  tool_policy={"undeclared": "confirm"})

    async def executor(name, args):
        executed.append((name, args.get("n")))
        return f"ran {name}"

    fake_llm.script(script)
    return [e async for e in run_agent_step(
        message="clean up", agent_id=agent["id"], session_id=session_id, server_module=_server(),
        agent_override=agent, tools_override=[DANGER, LOOKUP], tool_executor=executor, max_turns=6,
        progress_events=True, resume_state=resume_state)]


def _parked(events):
    """What chat parks: the card, and the last snapshot before it."""
    from core.chat_approval import parked_payload
    at = next(i for i, e in enumerate(events) if e.get("type") == "human_input_required")
    snaps = [{k: v for k, v in e.items() if k != "type"}
             for e in events[:at] if e.get("type") == "_step_progress"]
    return events[at], parked_payload(events[at], message="clean up", progress=snaps[-1] if snaps else None)


class TestTheGateSnapshotsItself:
    async def test_a_gate_in_the_first_step_keeps_what_the_turn_thought(self, fake_llm):
        """F319: the usual case — the first step asks."""
        executed: list = []
        events = await _step(fake_llm, ["[REASONING]I need to clean.[/REASONING]" + tool_call("danger", n=1)],
                             executed=executed, session_id="s_bf2_a")

        card, parked = _parked(events)
        progress = parked["progress"]
        assert card["tool_name"] == "danger" and executed == []
        assert events[events.index(card) - 1]["type"] == "_step_progress", "the snapshot comes just before the card"
        assert progress["reasoning"] == ["I need to clean."]
        assert progress["thoughts"][0] == "→ Called danger", "the call in words, never its JSON (F907)"
        assert "clean up" in progress["context_text"]
        assert progress["calls"] == [], "the call being asked about has not run"

    async def test_a_call_that_ran_earlier_in_the_response_is_kept(self, fake_llm, only_danger_asks):
        """A response that made two calls: the first ran, the second asks."""
        executed: list = []
        events = await _step(fake_llm, [tool_call("lookup", n=0) + "\n" + tool_call("danger", n=1)],
                             executed=executed, session_id="s_bf2_b")

        _card, parked = _parked(events)
        assert executed == [("lookup", 0)]
        assert [c["tool"] for c in parked["progress"]["calls"]] == ["lookup"]
        assert "ran lookup" in parked["progress"]["context_text"]

    async def test_answering_it_carries_on_from_there(self, fake_llm, only_danger_asks):
        from core.chat_approval import resume_state_for
        executed: list = []
        first = await _step(fake_llm, [tool_call("lookup", n=0) + "\n" + tool_call("danger", n=1)],
                            executed=executed, session_id="s_bf2_c")
        _card, parked = _parked(first)

        await _step(fake_llm, ["Done."], executed=executed, session_id="s_bf2_c",
                    resume_state=resume_state_for(parked, approve=True))

        assert executed == [("lookup", 0), ("danger", 1)], "each call ran once"
        assert "ran lookup" in str(fake_llm.calls[-1]["prompt_msg"]), "the model still sees what ran first"


class TestTheMemoryGate:
    async def test_a_held_remember_parks_with_what_the_turn_read(self, fake_llm):
        """The other gate: a `remember` after read content waits on a person (F279)."""
        from core.react_engine import run_agent_step
        agent = S.make_agent(tools=["all"], memory=True)

        async def executor(name, args):
            return "READ: the handover note"

        fake_llm.script([tool_call("read_doc", path="ops/handover.md"),
                         json.dumps({"tool": "remember",
                                     "arguments": {"name": "prefs", "content": "Use the handover note."}})])
        events = [e async for e in run_agent_step(
            message="summarise the handover", agent_id=agent["id"], session_id="s_bf2_m", server_module=_server(),
            agent_override=agent, max_turns=6, tool_executor=executor, progress_events=True)]

        card, parked = _parked(events)
        assert card["category"] == "memory_from_content"
        assert events[events.index(card) - 1]["type"] == "_step_progress"
        assert "READ: the handover note" in parked["progress"]["context_text"]
        assert parked["progress"]["memory_tainted"] is True


class TestASecondQuestionAfterAResume:
    """F312: the first turn after a resume asks again."""

    async def test_after_a_refusal_the_parked_turn_keeps_its_earlier_work(self, fake_llm):
        from core.chat_approval import resume_state_for
        from core.react_engine import _approval_id
        executed: list = []
        refused = resume_state_for({
            "tool_name": "danger", "tool_args": {"n": 1}, "category": "destructive",
            "approval_id": _approval_id("danger", {"n": 1}),
            "progress": {"turn": 1, "context_text": "User Request: clean up\nEARLIER WORK\n", "calls": []},
        }, approve=False, reason="not that one")

        events = await _step(fake_llm, [tool_call("danger", n=2)], executed=executed,
                             session_id="s_bf2_d", resume_state=refused)
        _card, parked = _parked(events)
        assert "EARLIER WORK" in parked["progress"].get("context_text", "")

        await _step(fake_llm, ["Done."], executed=executed, session_id="s_bf2_d",
                    resume_state=resume_state_for(parked, approve=True))
        assert executed == [("danger", 2)], "the approved call ran once, the refused one never"
        assert "EARLIER WORK" in str(fake_llm.calls[-1]["prompt_msg"]), "not started over"

    async def test_a_spent_approval_is_never_parked_again(self, fake_llm):
        """Approve A, then B: A is spent, so the model asking for A again is asked
        about, not run. (Seeding the parked snapshot from the resume state — the
        tempting fix — would have parked A's approval with B's question.)"""
        from core.chat_approval import resume_state_for
        executed: list = []
        first = await _step(fake_llm, [tool_call("danger", n=1)], executed=executed, session_id="s_bf2_e")
        _card, parked_a = _parked(first)

        second = await _step(fake_llm, [tool_call("danger", n=2)], executed=executed, session_id="s_bf2_e",
                             resume_state=resume_state_for(parked_a, approve=True))
        _card, parked_b = _parked(second)
        assert parked_b["progress"]["approved_calls"] == []

        third = await _step(fake_llm, [tool_call("danger", n=1)], executed=executed, session_id="s_bf2_e",
                            resume_state=resume_state_for(parked_b, approve=True))
        card, _ = _parked(third)
        assert executed == [("danger", 1), ("danger", 2)]
        assert card["tool_args"] == {"n": 1}, "asked again, not run on the old approval"


class TestTheTurnsKnowledgeLabels:
    async def test_a_second_pause_keeps_the_labels_its_answer_will_cite(self, fake_llm):
        """A turn that searched a knowledge base carries its [K#] labels in every
        snapshot, so the answer after a resume still cites them — a gate's too."""
        from core.knowledge.citations import KnowledgeTurn
        labels = KnowledgeTurn()
        labels.record(1, chunk_id="kch_1", kb_id="kb_a")
        resume = {"turn": 1, "context_text": "User Request: clean up\n[K1] the handbook says so\n",
                  "knowledge": labels.to_state()}

        events = await _step(fake_llm, [tool_call("danger", n=1)], executed=[], session_id="s_bf2_k",
                             resume_state=resume)

        _card, parked = _parked(events)
        assert parked["progress"]["knowledge"] == labels.to_state()


LEAD, HELPER = "lead_bf2", "helper_bf2"


@pytest.fixture
async def team():
    await S.seed_agents([
        S.make_agent(id=HELPER, name="Helper", tools=["all"], skip_default_tools=True,
                     tool_policy={"undeclared": "confirm"}),
        S.make_agent(id=LEAD, name="Lead", type="delegate", tools=[], delegate_agent_ids=[HELPER]),
    ])
    import core.react_engine as re_mod
    real = re_mod.run_agent_step

    async def wrapped(**kwargs):
        if kwargs.get("agent_id") == HELPER:
            kwargs.setdefault("tools_override", [DANGER])
            kwargs.setdefault("tool_executor", lambda name, args: None)
        async for ev in real(**kwargs):
            yield ev

    with _m.patch.object(re_mod, "run_agent_step", wrapped):
        yield


class TestADelegatesQuestion:
    async def test_the_lead_parks_its_own_turn_as_it_stands(self, team, fake_llm):
        import core.react_engine as re_mod
        fake_llm.script(["[REASONING]The helper can do this.[/REASONING]"
                         + tool_call("delegate_to_agent", agent_id=HELPER, task="clean"),
                         tool_call("danger")])
        events = [e async for e in re_mod.run_agent_step(
            message="clean up", agent_id=LEAD, session_id="s_bf2_f", server_module=_server(), max_turns=4,
            progress_events=True)]

        _card, parked = _parked(events)
        progress = parked["progress"]
        assert progress["delegate_pending"]["agent_id"] == HELPER
        assert "clean up" in progress.get("context_text", ""), "the lead's own context, not just a turn number"
        assert progress.get("reasoning") == ["The helper can do this."]


ASKS = "agent_bf2_asks"


@pytest.fixture
async def asks():
    await S.seed_agents([S.make_agent(id=ASKS, name="Careful", tools=["all"], skip_default_tools=True,
                                      tool_policy={"undeclared": "confirm"})])
    import core.react_engine as re_mod
    real = re_mod.run_agent_step
    ran: list[str] = []

    async def wrapped(**kwargs):
        if kwargs.get("agent_id") == ASKS:
            async def executor(name, args):
                ran.append(name)
                return "cleaned"
            kwargs.setdefault("tools_override", [DANGER])
            kwargs.setdefault("tool_executor", executor)
        async for ev in real(**kwargs):
            yield ev

    with _m.patch.object(re_mod, "run_agent_step", wrapped):
        yield ran


class TestChat:
    async def test_an_approved_turn_keeps_the_reasoning_before_its_pause(self, client, asks, fake_llm):
        from core.session import _get_conversation_history
        from core.store import sessions as store
        fake_llm.script(["[REASONING]I need to clean.[/REASONING]" + tool_call("danger"),
                         "[REASONING]It is clean.[/REASONING]Cleaned."])
        await client.post("/chat/stream", json={"message": "clean up", "agent_id": ASKS, "session_id": "s_bf2_g"})
        pending = await store.get_pending_approval("s_bf2_g", ASKS)
        assert pending["progress"].get("context_text"), "parked with where the turn stood"

        answered = await client.post("/chat/s_bf2_g/approve", json={"approve": True, "agent_id": ASKS})
        assert answered.status_code == 200
        assert asks == ["danger"]
        [turn] = await _get_conversation_history("s_bf2_g", ASKS)
        assert turn["reasoning"] == ["I need to clean.", "It is clean."]
        assert turn["thoughts"] == ["→ Called danger", "Cleaned."], "the asked call, said once, in words (F907)"


class TestTheFleet:
    async def test_the_chat_job_parks_and_resumes_with_it(self, monkeypatch, fake_llm):
        import fakeredis.aioredis
        import core.react_engine as re_mod
        from core.chat_approval import resume_state_for
        from core.scale import mcp_pool
        from core.scale import worker as worker_mod
        from core.store import sessions as sessions_mod

        agent = S.make_agent(id="agent_bf2_fleet", tools=["all"], skip_default_tools=True,
                             tool_policy={"undeclared": "confirm"})
        upserts, parked, ran = [], [], []

        async def nothing(*_a, **_kw):
            return None

        async def history(*_a):
            return []

        async def upsert(_sf, _sid, _aid, status, messages):
            upserts.append((status, [dict(m) for m in messages]))

        async def set_pending(session_id, agent_id, pending):
            parked.append(pending)

        async def acquire(*_a, **_kw):
            return object()

        async def executor(name, args):
            ran.append(name)
            return "cleaned"

        async def loop(request, server_module, should_stop=None, **kw):
            # The real engine, as `run_react_loop` would drive it for this agent.
            async for ev in re_mod.run_agent_step(
                    message=request.message, agent_id=agent["id"], session_id=request.session_id,
                    server_module=_server(), agent_override=agent, tools_override=[DANGER],
                    tool_executor=executor, persist_turn=False,
                    progress_events=kw.get("progress_events", False), resume_state=kw.get("resume_state")):
                yield ev

        monkeypatch.setattr(worker_mod, "_load_job_settings", nothing)
        monkeypatch.setattr(worker_mod, "_load_chat_history", history)
        monkeypatch.setattr(worker_mod, "_upsert_chat_session", upsert)
        monkeypatch.setattr(sessions_mod, "set_pending_approval", set_pending)
        monkeypatch.setattr(mcp_pool, "acquire", acquire)
        monkeypatch.setattr(mcp_pool, "release", nothing)
        monkeypatch.setattr(re_mod, "run_react_loop", loop)
        monkeypatch.setattr(worker_mod, "CHAT_CANCEL_POLL_SECONDS", 0.01)

        def ctx():
            return {"redis": fakeredis.aioredis.FakeRedis(), "session_factory": None, "job_try": 1, "job_id": "j"}

        fake_llm.script(["[REASONING]I need to clean.[/REASONING]" + tool_call("danger")])
        first = await worker_mod.run_agent_chat_job(ctx(), "s_bf2_fleet", agent["id"], "clean up")
        assert first["status"] == "paused"
        [pending] = parked
        assert pending["progress"].get("context_text")
        assert pending["progress"].get("reasoning") == ["I need to clean."]

        fake_llm.script(["[REASONING]It is clean.[/REASONING]Cleaned."])
        await worker_mod.run_agent_chat_job(ctx(), "s_bf2_fleet", agent["id"], "clean up",
                                            resume_state=resume_state_for(pending, approve=True))
        assert ran == ["danger"]
        answer = upserts[-1][1][-1]
        assert answer["reasoning"] == ["I need to clean.", "It is clean."]
