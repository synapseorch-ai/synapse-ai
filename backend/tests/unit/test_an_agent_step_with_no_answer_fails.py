"""An agent step whose turn ended without an answer fails (F413).

Two exits of the agent loop put the loop's words in place of the model's: the
repeat guard (F243, "I stopped here because I was repeating myself: …") and
running out of turns ("I ran out of turns (N) before finishing this task …").
In chat those words are right — the person reads them. In a workflow the step
took them as its answer: measured on the fleet (C2, `run_01a1117ab812…`), a
Parallel branch's writer agent wandered until the guard ended its turn, the
step was `completed`, `shared_state.ag3` held the guard's sentence, and the run
`completed` — a failure read as a success, the class F248/F253/F254/F256 closed
for routes, children, loops and switches.

Now the step fails with that reason and writes nothing to its output key, at the
top level, in a Loop body and in a Parallel branch, in-process and on the fleet.
A turn whose calls were refused and that then answered is still the model's
answer (Naveen, D267): only the two substitutes fail.
"""
from unittest import mock

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from test_human_in_containers import _run, driver, worker  # noqa: F401 — fixtures

AGENT = "agent_f413"
_LOOK = {"type": "function", "function": {
    "name": "look", "description": "Look somewhere.",
    "parameters": {"type": "object", "properties": {"where": {"type": "string"}}}}}


@pytest.fixture
def tools():
    """The real agent loop with one tool that always gives the same answer."""
    import core.react_engine as re_mod
    real = re_mod.run_agent_step

    async def with_tools(**kwargs):
        async def executor(name, args):
            return "files: a.txt, b.txt"
        kwargs.setdefault("tools_override", [_LOOK])
        kwargs.setdefault("tool_executor", executor)
        async for ev in real(**kwargs):
            yield ev

    with mock.patch.object(re_mod, "run_agent_step", with_tools), \
            mock.patch("core.orchestration.steps.run_agent_step", with_tools, create=True):
        yield


def _agent_step(sid="write", nxt=None, max_turns=6):
    return {"id": sid, "name": "Write", "type": "agent", "agent_id": AGENT,
            "prompt_template": "Write about tides", "output_key": "draft", "max_turns": max_turns,
            "next_step_id": nxt}


def _print(sid, text="ran"):
    return {"id": sid, "name": sid.title(), "type": "print", "print_content": text,
            "output_key": f"o_{sid}", "next_step_id": None}


async def _seed(orch):
    await S.seed_agents([S.make_agent(id=AGENT, tools=["all"], skip_default_tools=True)])
    await S.seed_orchestrations([orch])


def _entry(run, sid="write"):
    (entry,) = [h for h in run.step_history if h.get("step_id") == sid]
    return entry


# The same call three times, and again in the reply that was asked to answer from what it had (F1015).
REPEATS = [tool_call("look", where="vault")] * 4 + ["never reached"]


class TestTheRepeatGuard:
    async def test_it_fails_the_step_and_the_run(self, driver, fake_llm, tools):
        orch = S.make_orchestration(id="orch_f413_rep", entry_step_id="write", steps=[
            _agent_step(nxt="after"), _print("after")])
        await _seed(orch)
        fake_llm.script(REPEATS)

        assert await driver.start(orch, "run_f413_rep") == "failed"
        run = await _run("run_f413_rep")
        assert "draft" not in run.shared_state, "the guard's words are not the step's output"
        assert "o_after" not in run.shared_state, "the next step did not run"
        entry = _entry(run)
        assert entry["status"] == "failed"
        assert "without an answer" in entry["error"] and "look" in entry["error"]
        assert "3 times" in entry["error"]
        assert "I stopped here" not in entry["error"], "the step's reason, not the chat sentence"

    async def test_in_a_loop_body_the_loop_stops(self, fake_llm, tools):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        orch = S.make_orchestration(id="orch_f413_loop", entry_step_id="loop1", steps=[
            {"id": "loop1", "name": "Loop", "type": "loop", "loop_count": 2,
             "loop_step_ids": ["write"], "next_step_id": "tail"},
            _agent_step(), _print("tail")])
        await _seed(orch)
        fake_llm.script(REPEATS)

        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        events = [e async for e in engine.run("go", run_id="run_f413_loop")]
        run = await _run("run_f413_loop")
        assert run.status == "failed"
        assert "draft" not in run.shared_state and "o_tail" not in run.shared_state
        assert [e["iteration"] for e in events if e.get("type") == "loop_iteration"] == [1]

    async def test_in_a_parallel_branch_the_node_fails(self, fake_llm, tools):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        orch = S.make_orchestration(id="orch_f413_par", entry_step_id="fan", steps=[
            {"id": "fan", "name": "Fan", "type": "parallel", "parallel_branches": [["write"], ["p2"]],
             "next_step_id": "tail"},
            _agent_step(), _print("p2"), _print("tail")])
        await _seed(orch)
        fake_llm.script(REPEATS)

        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        [e async for e in engine.run("go", run_id="run_f413_par")]
        run = await _run("run_f413_par")
        assert run.status == "failed"
        assert "draft" not in run.shared_state and "o_tail" not in run.shared_state


class TestARepeatedRound:
    async def test_it_fails_the_step_and_names_the_round(self, driver, fake_llm, tools):
        """F476: a whole reply's calls made again three times ends the turn as one call does (F243), so the step
        fails with that reason, never the chat sentence, on every driver (C5's note at VOX1f's plan)."""
        import json
        both = "\n".join(json.dumps({"tool": "look", "arguments": {"where": w}}) for w in ("vault", "inbox"))
        orch = S.make_orchestration(id="orch_f476_round", entry_step_id="write", steps=[
            _agent_step(nxt="after"), _print("after")])
        await _seed(orch)
        fake_llm.script([both] * 4 + ["never reached"])  # and again in its answer turn (F1015)

        assert await driver.start(orch, "run_f476_round") == "failed"
        run = await _run("run_f476_round")
        assert "draft" not in run.shared_state and "o_after" not in run.shared_state
        entry = _entry(run)
        assert entry["status"] == "failed"
        assert "without an answer" in entry["error"] and "the same 2 calls (look)" in entry["error"]
        assert "3 times" in entry["error"] and "I stopped here" not in entry["error"]


class TestRunningOutOfTurns:
    async def test_it_fails_the_step_and_names_the_limit(self, driver, fake_llm, tools):
        orch = S.make_orchestration(id="orch_f413_turns", entry_step_id="write", steps=[
            _agent_step(nxt="after", max_turns=2), _print("after")])
        await _seed(orch)
        fake_llm.script([tool_call("look", where=f"shelf {i}") for i in range(5)])

        assert await driver.start(orch, "run_f413_turns") == "failed"
        run = await _run("run_f413_turns")
        assert "draft" not in run.shared_state and "o_after" not in run.shared_state
        entry = _entry(run)
        assert entry["status"] == "failed"
        assert "ran out of turns (2)" in entry["error"] and "before answering" in entry["error"]


class TestARealAnswer:
    async def test_it_is_the_steps_output(self, driver, fake_llm, tools):
        orch = S.make_orchestration(id="orch_f413_ok", entry_step_id="write", steps=[
            _agent_step(nxt="after"), _print("after")])
        await _seed(orch)
        fake_llm.script([tool_call("look", where="vault"), "Tides rise twice a day."])

        assert await driver.start(orch, "run_f413_ok") == "completed"
        run = await _run("run_f413_ok")
        assert run.shared_state["draft"] == "Tides rise twice a day."
        assert _entry(run)["status"] == "completed"

    async def test_a_refused_call_then_an_answer_is_the_models_answer(self, driver, fake_llm, tools):
        """Only the loop's own substitutes fail a step (Naveen, D267)."""
        orch = S.make_orchestration(id="orch_f413_ref", entry_step_id="write", steps=[_agent_step()])
        await _seed(orch)
        fake_llm.script([tool_call("not_a_tool", x=1), "I could not use that tool, so: tides are tidal."])

        assert await driver.start(orch, "run_f413_ref") == "completed"
        run = await _run("run_f413_ref")
        assert run.shared_state["draft"] == "I could not use that tool, so: tides are tidal."


class TestChatIsUnchanged:
    async def test_a_chat_turn_still_reads_the_guards_sentence(self, fake_llm):
        """The person reads the sentence in chat; the final event says why it is not an answer."""
        import types
        from core.react_engine import run_agent_step

        async def same(name, args):
            return "files: a.txt"

        agent = S.make_agent(tools=["all"], skip_default_tools=True)
        fake_llm.script(REPEATS)
        events = [ev async for ev in run_agent_step(
            message="look around", agent_id=agent["id"], session_id="s_f413",
            server_module=types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}),
            agent_override=agent, tool_executor=same, max_turns=6, tools_override=[_LOOK])]
        final = next(e for e in reversed(events) if e.get("type") == "final")
        assert final["response"].startswith("I stopped here because I was repeating myself")
        assert final["no_answer"] == {"why": "repeating", "tool_name": "look", "count": 3}


def _server():
    import types
    return types.SimpleNamespace(agent_sessions={"x": object()}, memory_store=None, tool_router={})
