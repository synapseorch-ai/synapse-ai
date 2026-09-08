"""
Mid-node continuity for agent steps.

run_agent_step emits a `_step_progress` snapshot after each completed tool
turn; AgentStepExecutor persists it (private shared_state key + checkpoint)
and seeds the next attempt's `resume_state` from it so a crashed step
continues from its last observation instead of re-executing every tool call.
"""
import json
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call




def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


_TOOL_SCHEMA = {
    "type": "function",
    "function": {"name": "mytool", "description": "test tool",
                 "parameters": {"type": "object", "properties": {}}},
}


def _agent():
    return S.make_agent(tools=["all"], skip_default_tools=True)


class TestProgressEvents:
    async def test_progress_snapshot_after_each_tool_turn(self, fake_llm):
        from core.react_engine import run_agent_step
        fake_llm.script([tool_call("mytool", x=1), "All done."])

        async def executor(name, args):
            return "TOOL_OUTPUT_42"

        events = [ev async for ev in run_agent_step(
            message="task", agent_id="a1", session_id="s1", server_module=_server(),
            agent_override=_agent(), tools_override=[_TOOL_SCHEMA], tool_executor=executor,
            allowed_tools_override=["all"], max_turns=5,
            progress_events=True,
        )]
        progress = [e for e in events if e["type"] == "_step_progress"]
        assert len(progress) == 1  # one completed tool turn
        snap = progress[0]
        assert snap["turn"] == 1
        assert "TOOL_OUTPUT_42" in snap["context_text"]  # the observation survives
        assert snap["has_compacted"] is False
        assert any("TOOL_OUTPUT_42" in s for s in snap["tools_used"])
        assert events[-1]["type"] == "final"

    async def test_no_progress_events_by_default(self, fake_llm):
        from core.react_engine import run_agent_step
        fake_llm.script([tool_call("mytool", x=1), "All done."])

        async def executor(name, args):
            return "out"

        events = [ev async for ev in run_agent_step(
            message="task", agent_id="a1", session_id="s1", server_module=_server(),
            agent_override=_agent(), tools_override=[_TOOL_SCHEMA], tool_executor=executor,
            allowed_tools_override=["all"], max_turns=5,
        )]
        assert not any(e["type"] == "_step_progress" for e in events)

    async def test_resume_state_continues_without_reexecuting_tools(self, fake_llm):
        """The seeded attempt must see the prior observation in its prompt and
        must not re-run turn-1 tools (the LLM goes straight to the answer)."""
        from core.react_engine import run_agent_step
        fake_llm.script(["Final from resume."])
        executed: list[str] = []

        async def executor(name, args):
            executed.append(name)
            return "should not run"

        resume_state = {
            "turn": 1,
            "context_text": (
                "User Request: task\n"
                "[Turn 1 — Action]\n" + tool_call("mytool", x=1) + "\n"
                "[Turn 1 — Observation] Tool 'mytool' returned:\nTOOL_OUTPUT_42\n"
            ),
            "has_compacted": False,
            "browser_actions": [],
            "tools_used": ["mytool: TOOL_OUTPUT_42"],
        }
        events = [ev async for ev in run_agent_step(
            message="task", agent_id="a1", session_id="s1", server_module=_server(),
            agent_override=_agent(), tools_override=[_TOOL_SCHEMA], tool_executor=executor,
            allowed_tools_override=["all"], max_turns=5,
            resume_state=resume_state, progress_events=True,
        )]
        final = [e for e in events if e["type"] == "final"]
        assert final and final[0]["response"] == "Final from resume."
        assert executed == []  # nothing re-executed
        assert fake_llm.call_count == 1
        prompt = fake_llm.last_call.get("prompt_msg", "")
        assert "TOOL_OUTPUT_42" in prompt          # prior observation visible
        assert "interrupted and has been resumed" in prompt  # resume marker
        assert fake_llm.last_call.get("history_messages") in ([], None)


def _agent_orch(step_overrides: dict | None = None) -> dict:
    step = {"id": "s1", "name": "Agent Step", "type": "agent", "agent_id": "a1",
            "output_key": "result", "next_step_id": None, "max_turns": 5}
    step.update(step_overrides or {})
    return S.make_orchestration(id="orch_prog", entry_step_id="s1", steps=[step])


async def _run_engine(orch_dict, server=None, initial_state=None, run_id="run_prog"):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    engine = OrchestrationEngine(Orchestration.model_validate(orch_dict), server or _server())
    return [ev async for ev in engine.run("go", run_id=run_id, initial_state=initial_state)]


class TestExecutorInterception:
    async def test_progress_persisted_swallowed_and_survives_crash(self, monkeypatch):
        """A crash after a completed tool turn leaves a checkpointed transcript."""
        import core.react_engine as re_mod
        import core.orchestration.state as state_mod

        async def fake_run_agent_step(**kwargs):
            yield {"type": "tool_result", "tool_name": "mytool", "preview": "ok"}
            yield {"type": "_step_progress", "turn": 1, "context_text": "CTX",
                   "has_compacted": False, "browser_actions": [], "tools_used": []}
            raise RuntimeError("process died")

        monkeypatch.setattr(re_mod, "run_agent_step", fake_run_agent_step)
        events = await _run_engine(_agent_orch())

        # The run failed, but the transcript survived in the checkpoint.
        assert any(e.get("type") == "step_error" for e in events)
        assert not any(e.get("type") == "_step_progress" for e in events)  # swallowed
        ckpt = (await state_mod.SharedState.restore("run_prog")).run.model_dump(mode="json")
        saved = ckpt["step_progress"]["s1"]
        assert "_step_progress_s1" not in ckpt["shared_state"]   # its own column now
        assert saved["context_text"] == "CTX"
        assert saved["turn"] == 1
        assert saved["attempt"] == 1

    async def test_resume_failed_seeds_transcript_then_cleans_up(self, monkeypatch, seed_orchestration):
        """resume_failed hands the saved transcript to run_agent_step and a
        successful completion removes it."""
        import core.react_engine as re_mod
        import core.orchestration.state as state_mod
        from core.orchestration.engine import OrchestrationEngine

        orch = _agent_orch()
        await seed_orchestration(**orch)  # resume_failed loads the definition from disk

        async def crashing(**kwargs):
            yield {"type": "_step_progress", "turn": 2, "context_text": "CTX2",
                   "has_compacted": False, "browser_actions": [], "tools_used": []}
            raise RuntimeError("boom")

        monkeypatch.setattr(re_mod, "run_agent_step", crashing)
        await _run_engine(orch, run_id="run_resume")

        seen_kwargs: dict = {}

        async def resumed(**kwargs):
            seen_kwargs.update(kwargs)
            yield {"type": "final", "response": "done after resume"}

        monkeypatch.setattr(re_mod, "run_agent_step", resumed)
        events = [ev async for ev in OrchestrationEngine.resume_failed("run_resume", _server())]

        assert seen_kwargs["resume_state"]["context_text"] == "CTX2"
        assert seen_kwargs["progress_events"] is True
        assert any(e.get("type") == "orchestration_complete" for e in events)
        ckpt = (await state_mod.SharedState.restore("run_resume")).run.model_dump(mode="json")
        assert "s1" not in ckpt["step_progress"]  # cleaned up
        assert ckpt["shared_state"]["result"] == "done after resume"

    async def test_attempt_cap_discards_poisoned_transcript(self, monkeypatch):
        """After two failed resume attempts the transcript is dropped and the
        step reruns fresh."""
        import core.react_engine as re_mod

        seen_kwargs: dict = {}

        async def recording(**kwargs):
            seen_kwargs.update(kwargs)
            yield {"type": "final", "response": "fresh rerun"}

        monkeypatch.setattr(re_mod, "run_agent_step", recording)
        events = await _run_engine(_agent_orch(), initial_state={
            "_step_progress_s1": {"turn": 3, "context_text": "POISON",
                                  "attempt": 3},
        })
        assert seen_kwargs["resume_state"] is None
        assert any(e.get("type") == "orchestration_complete" for e in events)

    async def test_attempt_increments_across_resumes(self, monkeypatch):
        """A resumed attempt that crashes again stores attempt=2.

        Seeded through the LEGACY shared_state key on purpose: a run paused
        by a build that kept progress there must resume mid-step on this one,
        and must come out with the key gone and the record in its column."""
        import core.react_engine as re_mod
        import core.orchestration.state as state_mod

        async def crash_again(**kwargs):
            yield {"type": "_step_progress", "turn": 4, "context_text": "CTX4",
                   "has_compacted": False, "browser_actions": [], "tools_used": []}
            raise RuntimeError("boom again")

        monkeypatch.setattr(re_mod, "run_agent_step", crash_again)
        await _run_engine(_agent_orch(), run_id="run_att", initial_state={
            "_step_progress_s1": {"turn": 2, "context_text": "CTX2", "attempt": 1},
        })
        ckpt = (await state_mod.SharedState.restore("run_att")).run.model_dump(mode="json")
        assert ckpt["step_progress"]["s1"]["attempt"] == 2
        assert ckpt["step_progress"]["s1"]["context_text"] == "CTX4"
        assert "_step_progress_s1" not in ckpt["shared_state"]  # migrated, not duplicated

    async def test_clean_run_leaves_no_progress_residue(self, monkeypatch):
        import core.react_engine as re_mod
        import core.orchestration.state as state_mod

        async def clean(**kwargs):
            yield {"type": "_step_progress", "turn": 1, "context_text": "CTX",
                   "has_compacted": False, "browser_actions": [], "tools_used": []}
            yield {"type": "final", "response": "ok"}

        monkeypatch.setattr(re_mod, "run_agent_step", clean)
        await _run_engine(_agent_orch(), run_id="run_clean")
        ckpt = (await state_mod.SharedState.restore("run_clean")).run.model_dump(mode="json")
        assert "_step_progress_s1" not in ckpt["shared_state"]
        assert ckpt["step_progress"] == {}
