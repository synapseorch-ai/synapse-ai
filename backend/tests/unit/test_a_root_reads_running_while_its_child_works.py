"""After a nested question is answered, the root says it is running (F250).

Answering a question two levels down resumed the grandchild while the root's
record — and its child's — still read `paused`, for as long as the work took
(169 s measured) or until the next question. Every list, v1's status and the
bell said "waiting for input" while the run was working, and only the root is
listed. Now each level records itself running before it resumes the level it
waits on, and records itself paused again if that level asks again.

A resume that cannot reach the child at all used to leave the root paused on a
question nobody could answer any more; recording it running first would have
left it running for ever. It now fails, naming why.
"""
import asyncio

import pytest

from _fakes import seed as S
from tests.unit.test_a_failed_child_fails_its_parent import (  # noqa: F401 — fixtures
    _human, _print, _run, _server, driver, worker,
)


class _Gate:
    """A model call that holds until released — the child mid-step."""

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, *args, **kwargs):
        self.started.set()
        await self.release.wait()
        return "researched"


@pytest.fixture
def gate(monkeypatch):
    import core.llm_providers as llm
    g = _Gate()
    monkeypatch.setattr(llm, "generate_response", g, raising=False)
    return g


def _call(agent_id, sid="call", nxt=None):
    return {"id": sid, "name": f"Call {sid}", "type": "agent", "agent_id": agent_id,
            "prompt_template": "go", "output_key": f"out_{sid}", "next_step_id": nxt}


async def _three_levels(prefix, l3_steps, l3_entry):
    """L1 → L2 → L3, each nested step an orchestrator agent."""
    l3 = S.make_orchestration(id=f"{prefix}_l3", name="L3", entry_step_id=l3_entry, steps=l3_steps)
    l2 = S.make_orchestration(id=f"{prefix}_l2", name="L2", entry_step_id="call2",
                              steps=[_call(f"agent_{prefix}_l3", "call2")])
    l1 = S.make_orchestration(id=f"{prefix}_l1", name="L1", entry_step_id="call1",
                              steps=[_call(f"agent_{prefix}_l2", "call1")])
    await S.seed_agents([S.make_orchestrator_agent(l3["id"], id=f"agent_{prefix}_l3"),
                         S.make_orchestrator_agent(l2["id"], id=f"agent_{prefix}_l2")])
    await S.seed_orchestrations([l1, l2, l3])
    return l1


_SLOW = {"id": "slow", "name": "Research", "type": "llm", "prompt_template": "research",
         "model": "gpt-4o", "output_key": "facts", "next_step_id": None}


class TestTheRootIsRunningWhileAChildWorks:
    async def test_after_a_depth_two_answer(self, driver, gate):
        l1 = await _three_levels("orch_f250", [_human("ask", nxt="slow"), dict(_SLOW)], "ask")
        assert await driver.start(l1, "run_f250") == "paused"
        l2_id = (await _run("run_f250")).nested_run_id

        answering = asyncio.create_task(driver.answer("run_f250", "yes"))
        await asyncio.wait_for(gate.started.wait(), timeout=10)

        root, child = await _run("run_f250"), await _run(l2_id)
        assert (root.status, root.waiting_for_human) == ("running", False)
        assert (child.status, child.waiting_for_human) == ("running", False)

        gate.release.set()
        assert await asyncio.wait_for(answering, timeout=10) == "completed"
        assert (await _run("run_f250")).status == "completed"

    async def test_a_second_question_pauses_every_level_again(self, driver):
        l1 = await _three_levels("orch_f250b", [_human("ask", nxt="again"), _human("again", key="second")], "ask")
        assert await driver.start(l1, "run_f250b") == "paused"
        l2_id = (await _run("run_f250b")).nested_run_id

        assert await driver.answer("run_f250b", "yes") == "paused"

        root, child = await _run("run_f250b"), await _run(l2_id)
        assert (root.status, root.waiting_for_human) == ("paused", True)
        assert (child.status, child.waiting_for_human) == ("paused", True)
        assert root.nested_run_id == l2_id

        assert await driver.answer("run_f250b", "and yes") == "completed"


class TestAChildTheResumeCannotReach:
    async def test_the_root_fails_naming_it(self):
        from sqlalchemy import delete

        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        from core.store import session
        from core.store.models import OrchestrationRunDB
        l1 = await _three_levels("orch_f250c", [_human("ask", nxt=None)], "ask")
        engine = OrchestrationEngine(Orchestration.model_validate(l1), _server())
        [e async for e in engine.run("go", run_id="run_f250c")]
        child_id = (await _run("run_f250c")).nested_run_id
        async with session() as s:
            await s.execute(delete(OrchestrationRunDB).where(OrchestrationRunDB.run_id == child_id))
            await s.commit()

        events = [e async for e in OrchestrationEngine.resume("run_f250c", "yes", _server())]

        root = await _run("run_f250c")
        assert root.status == "failed" and not root.waiting_for_human
        assert any(e.get("type") == "step_error" and e.get("orch_step_id") == "call1" for e in events)
        assert "L2" in root.step_history[-1]["error"]
