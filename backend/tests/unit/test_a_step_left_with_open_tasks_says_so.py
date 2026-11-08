"""An Agent step that ends with its own plan unfinished says so on its entry, its event and the run (F862).

QA1c on prod (rc3, 2026-10-22, S3): "Delay triage → board", one Agent step at its default 15 turns. Its last turn's
reply was accepted with three planned tasks open (`tasks_incomplete`), the step and the run read "completed", and
the header said "Completed · 1 of 1 steps" — only Activity said "Step ended with 3 open tasks", and nothing said the
turn limit was what ended it. Two of five delay notes never reached the board.

Naveen (2026-10-23): "Completed with warnings", the status value unchanged. The step's entry and `step_complete`
carry `open_tasks` (and `ended: "turn_limit"` when its last turn was what accepted it); `orchestration_complete`
counts the steps that did as `warnings`. In-process and on the fleet.
"""
from _fakes import seed as S
from _fakes.fake_llm import tool_call
from core.task_list import TASK_TOOL_NAME
from test_human_in_containers import _run, driver, worker  # noqa: F401 — fixtures

AGENT = "agent_f862"
PLAN = [{"content": "triage KF-30422", "status": "in_progress"},
        {"content": "triage KF-30427", "status": "pending"},
        {"content": "triage KF-30433", "status": "pending"}]


def _orch(oid, max_turns):
    return S.make_orchestration(id=oid, entry_step_id="triage", steps=[
        {"id": "triage", "name": "Triage", "type": "agent", "agent_id": AGENT, "prompt_template": "Triage these.",
         "output_key": "triaged", "max_turns": max_turns, "next_step_id": None}])


async def _seed(orch):
    await S.seed_agents([S.make_agent(id=AGENT, tools=[])])  # its plan tool is a default one
    await S.seed_orchestrations([orch])


def _entry(run):
    (entry,) = [h for h in run.step_history if h.get("step_id") == "triage"]
    return entry


class TestTheEntry:
    async def test_a_step_its_turn_limit_ended_with_tasks_open_says_both(self, driver, fake_llm):
        orch = _orch("orch_f862_limit", max_turns=2)
        await _seed(orch)
        fake_llm.script([tool_call(TASK_TOOL_NAME, tasks=PLAN), "One of three triaged."])
        assert await driver.start(orch, "run_f862_limit") == "completed", "the status value is unchanged"
        entry = _entry(await _run("run_f862_limit"))
        assert entry["status"] == "completed"
        assert (entry.get("open_tasks"), entry.get("ended")) == (3, "turn_limit")

    async def test_a_step_whose_nudges_ran_out_says_its_open_tasks_only(self, driver, fake_llm):
        orch = _orch("orch_f862_nudged", max_turns=10)
        await _seed(orch)
        fake_llm.script([tool_call(TASK_TOOL_NAME, tasks=PLAN), "first", "second", "third"])
        assert await driver.start(orch, "run_f862_nudged") == "completed"
        entry = _entry(await _run("run_f862_nudged"))
        assert entry.get("open_tasks") == 3 and "ended" not in entry

    async def test_a_step_that_finished_its_plan_carries_neither(self, driver, fake_llm):
        orch = _orch("orch_f862_clean", max_turns=6)
        await _seed(orch)
        done = [{**t, "status": "completed"} for t in PLAN]
        fake_llm.script([tool_call(TASK_TOOL_NAME, tasks=PLAN), tool_call(TASK_TOOL_NAME, tasks=done), "All three."])
        assert await driver.start(orch, "run_f862_clean") == "completed"
        entry = _entry(await _run("run_f862_clean"))
        assert "open_tasks" not in entry and "ended" not in entry
        assert "tasks" not in entry, "a step's priced tasks are its team's, untouched"


class TestTheEvents:
    async def test_step_complete_and_the_run_say_it(self, fake_llm):
        from types import SimpleNamespace

        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine

        orch = _orch("orch_f862_events", max_turns=2)
        await _seed(orch)
        fake_llm.script([tool_call(TASK_TOOL_NAME, tasks=PLAN), "One of three triaged."])
        server = SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})
        events = [e async for e in OrchestrationEngine(Orchestration.model_validate(orch), server)
                  .run("go", run_id="run_f862_events")]
        [done] = [e for e in events if e.get("type") == "step_complete"]
        assert (done.get("open_tasks"), done.get("ended")) == (3, "turn_limit")
        [end] = [e for e in events if e.get("type") == "orchestration_complete"]
        assert (end["status"], end.get("warnings")) == ("completed", 1)
        [said] = [e for e in events if e.get("type") == "tasks_incomplete"]
        assert said.get("turn_limit") is True

    async def test_a_clean_run_counts_no_warnings(self, fake_llm):
        from types import SimpleNamespace

        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine

        orch = _orch("orch_f862_none", max_turns=4)
        await _seed(orch)
        fake_llm.script(["Nothing to plan."])
        server = SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})
        events = [e async for e in OrchestrationEngine(Orchestration.model_validate(orch), server)
                  .run("go", run_id="run_f862_none")]
        [end] = [e for e in events if e.get("type") == "orchestration_complete"]
        assert not end.get("warnings")


def test_the_note_between_the_step_and_its_entry_is_the_engines_own():
    """Like a step's counted tasks (F854's list): a paused or failed run's public state never shows it."""
    from core.events_public import public_state
    from core.orchestration.pauses import LEFT_OPEN_KEY

    assert public_state({LEFT_OPEN_KEY: {"triage": {"open_tasks": 3}}, "triaged": "x"}) == {"triaged": "x"}
