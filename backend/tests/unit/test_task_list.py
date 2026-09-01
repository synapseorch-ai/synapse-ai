"""
The native task list (core/task_list.py) — the engine-owned replacement for
the npx sequentialthinking server.

What must be true (the reasons the replacement was worth building):

  * updates land as `tasks_updated` events — structural, so they survive the
    journal's event cap — and write through to `run.tasks` so the plan
    survives a resume on any worker;
  * binding: an agent that opened tasks cannot end its step while they are
    open — two refusals with a nudge, then acceptance plus a structural
    `tasks_incomplete`;
  * the retired tool name keeps working as an alias for saved agents and
    shipped bundles, including a helpful rejection for old-schema `thought`
    calls;
  * resubmitting an identical list is named as a no-op — the think-loop shape
    the old server's changing thoughtNumber used to smuggle past detection.
"""
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call

from core.task_list import (
    LEGACY_TASK_TOOL_ALIAS,
    TASK_LIST_STATIC_SCHEMA,
    TASK_TOOL_NAME,
    open_tasks,
    render_tasks,
    summarize_tasks,
    validate_tasks_args,
)

PLAN_OPEN = [
    {"content": "research the topic", "status": "in_progress"},
    {"content": "write the report", "status": "pending"},
]
PLAN_DONE = [
    {"content": "research the topic", "status": "completed"},
    {"content": "write the report", "status": "completed"},
]


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _agent(**overrides):
    # Default tools stay ON — that is what injects the task tool.
    return S.make_agent(tools=[], **overrides)


async def _events(fake_llm, script, agent=None, **kw):
    from core.react_engine import run_agent_step

    fake_llm.script(script)
    agent = agent or _agent()
    return [ev async for ev in run_agent_step(
        message="do the work", agent_id=agent["id"], session_id="s_task",
        server_module=_server(), agent_override=agent, max_turns=6, **kw,
    )]


def _of_type(events, event_type):
    return [e for e in events if e.get("type") == event_type]


def _final(events):
    finals = _of_type(events, "final")
    assert finals, f"no final event in {[e.get('type') for e in events]}"
    return finals[-1]["response"]


def _deferrals(events):
    return [e for e in _of_type(events, "status")
            if "deferred" in (e.get("message") or "").lower()]


# ── validation ───────────────────────────────────────────────────────────────

class TestValidation:
    def test_valid_list_normalizes(self):
        tasks, err = validate_tasks_args({"tasks": [
            {"content": "  a  ", "status": "IN_PROGRESS"},
            {"content": "b"},          # status defaults to pending
            "bare string task",        # lenient: promoted to a task
        ]})
        assert err is None
        assert tasks == [
            {"content": "a", "status": "in_progress"},
            {"content": "b", "status": "pending"},
            {"content": "bare string task", "status": "pending"},
        ]

    @pytest.mark.parametrize("args,fragment", [
        ({}, "'tasks' array"),
        ({"tasks": "not a list"}, "must be an array"),
        ({"tasks": [{"content": ""}]}, "no content"),
        ({"tasks": [{"content": "x", "status": "doing"}]}, "invalid status"),
        ({"tasks": [42]}, "must be an object"),
        ({"thought": "step 1", "thoughtNumber": 1}, "has been replaced"),
    ])
    def test_rejections_explain_themselves(self, args, fragment):
        tasks, err = validate_tasks_args(args)
        assert tasks is None
        assert fragment in err

    def test_too_many_tasks(self):
        tasks, err = validate_tasks_args({"tasks": [{"content": f"t{i}"} for i in range(51)]})
        assert tasks is None and "Too many" in err

    def test_helpers(self):
        assert [t["content"] for t in open_tasks(PLAN_OPEN)] == [
            "research the topic", "write the report"]
        assert open_tasks(PLAN_DONE) == []
        board = render_tasks(PLAN_OPEN)
        assert "[>] research the topic" in board and "[ ] write the report" in board
        assert summarize_tasks(PLAN_DONE) == "2 tasks — 2 done"

    def test_static_schema_shape(self):
        # Flat types only — a union here is the exact shape that took every
        # tool from every Gemini agent on 2026-09-01.
        assert TASK_LIST_STATIC_SCHEMA["name"] == TASK_TOOL_NAME
        assert TASK_LIST_STATIC_SCHEMA["source"] == "native_engine"
        item_props = TASK_LIST_STATIC_SCHEMA["schema"]["properties"]["tasks"]["items"]["properties"]
        assert item_props["content"]["type"] == "string"
        assert item_props["status"]["type"] == "string"


# ── engine flow ──────────────────────────────────────────────────────────────

class TestEngineFlow:
    async def test_update_events_and_clean_final(self, fake_llm):
        events = await _events(fake_llm, [
            tool_call(TASK_TOOL_NAME, tasks=PLAN_OPEN),
            tool_call(TASK_TOOL_NAME, tasks=PLAN_DONE),
            "All finished.",
        ])
        updates = _of_type(events, "tasks_updated")
        assert [t["status"] for t in updates[0]["tasks"]] == ["in_progress", "pending"]
        assert [t["status"] for t in updates[1]["tasks"]] == ["completed", "completed"]
        assert _final(events) == "All finished."
        assert not _deferrals(events)
        assert not _of_type(events, "tasks_incomplete")

    async def test_binding_defers_then_accepts_after_completion(self, fake_llm):
        events = await _events(fake_llm, [
            tool_call(TASK_TOOL_NAME, tasks=PLAN_OPEN),
            "premature final",
            tool_call(TASK_TOOL_NAME, tasks=PLAN_DONE),
            "the real final",
        ])
        assert len(_deferrals(events)) == 1
        assert _final(events) == "the real final"
        assert not _of_type(events, "tasks_incomplete")

    async def test_binding_gives_up_after_two_nudges(self, fake_llm):
        events = await _events(fake_llm, [
            tool_call(TASK_TOOL_NAME, tasks=PLAN_OPEN),
            "first attempt",
            "second attempt",
            "third attempt",
        ])
        assert len(_deferrals(events)) == 2
        incomplete = _of_type(events, "tasks_incomplete")
        assert len(incomplete) == 1
        assert len(incomplete[0]["tasks"]) == 2
        assert _final(events) == "third attempt"

    async def test_legacy_alias_routes_to_native_tool(self, fake_llm):
        events = await _events(fake_llm, [
            tool_call(LEGACY_TASK_TOOL_ALIAS, tasks=PLAN_DONE),
            "done",
        ])
        assert len(_of_type(events, "tasks_updated")) == 1
        assert _final(events) == "done"

    async def test_legacy_thought_args_get_migration_guidance(self, fake_llm):
        events = await _events(fake_llm, [
            tool_call(LEGACY_TASK_TOOL_ALIAS, thought="step 1", thoughtNumber=1),
            "done",
        ])
        assert not _of_type(events, "tasks_updated")
        rejected = [e for e in _of_type(events, "tool_result")
                    if "has been replaced" in (e.get("preview") or "")]
        assert rejected, "the old-schema call should be answered with migration guidance"

    async def test_identical_resubmission_is_a_named_noop(self, fake_llm):
        events = await _events(fake_llm, [
            tool_call(TASK_TOOL_NAME, tasks=PLAN_OPEN),
            tool_call(TASK_TOOL_NAME, tasks=PLAN_OPEN),
            tool_call(TASK_TOOL_NAME, tasks=PLAN_DONE),
            "final",
        ])
        assert len(_of_type(events, "tasks_updated")) == 2  # the no-op emitted none
        noop = [e for e in _of_type(events, "tool_result")
                if "No change" in (e.get("preview") or "")]
        assert len(noop) == 1

    async def test_inherited_open_tasks_do_not_bind_an_untouching_agent(self, fake_llm):
        """A downstream step that never engages the list is not held hostage
        by a plan some earlier step wrote — but it does see the plan."""
        events = await _events(fake_llm, ["just the answer"],
                               initial_tasks=[dict(t) for t in PLAN_OPEN])
        assert _final(events) == "just the answer"
        assert not _deferrals(events)
        assert not _of_type(events, "tasks_incomplete")
        # The seeded list rode the system prompt of the very first turn.
        assert "Current task list for this run" in str(fake_llm.calls[0])

    async def test_skip_default_tools_agent_gets_no_task_tool(self, fake_llm):
        events = await _events(
            fake_llm,
            [tool_call(TASK_TOOL_NAME, tasks=PLAN_DONE), "done"],
            agent=_agent(skip_default_tools=True),
        )
        assert not _of_type(events, "tasks_updated")

    async def test_planning_false_turns_the_task_list_off(self, fake_llm):
        """The agent's own switch, for the one-shot agents a plan does not help.

        Narrower than `skip_default_tools`, which drops the whole default set:
        this removes the plan and leaves everything else in place.
        """
        events = await _events(
            fake_llm,
            [tool_call(TASK_TOOL_NAME, tasks=PLAN_DONE), "done"],
            agent=_agent(planning=False),
        )
        assert not _of_type(events, "tasks_updated")

    async def test_planning_is_on_when_unset(self, fake_llm):
        """Default ON, and the default is the point.

        An agent whose author never considered planning should still show its
        work — every agent saved before the switch existed has no `planning`
        key at all, and none of them should lose the task list to a bare
        `.get()`.
        """
        events = await _events(
            fake_llm, [tool_call(TASK_TOOL_NAME, tasks=PLAN_DONE), "done"]
        )
        assert _of_type(events, "tasks_updated")

    async def test_planning_true_is_explicit_and_works(self, fake_llm):
        events = await _events(
            fake_llm,
            [tool_call(TASK_TOOL_NAME, tasks=PLAN_DONE), "done"],
            agent=_agent(planning=True),
        )
        assert _of_type(events, "tasks_updated")


# ── orchestration persistence ────────────────────────────────────────────────

class TestOrchestrationPersistence:
    async def test_task_state_written_through_to_the_run(self, fake_llm, seed_agent):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        import core.orchestration.state as state_mod

        agent = await seed_agent(tools=[])
        orch = S.make_orchestration(
            id="orch_tasks",
            entry_step_id="s1",
            steps=[{"id": "s1", "name": "Plan", "type": "agent",
                    "agent_id": agent["id"], "next_step_id": None}],
        )
        fake_llm.script([tool_call(TASK_TOOL_NAME, tasks=PLAN_DONE), "step done"])
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        events = [ev async for ev in engine.run("go", run_id="run_tasks_1")]

        assert any(e.get("type") == "tasks_updated" for e in events)
        assert any(e.get("type") == "orchestration_complete" for e in events)

        reloaded = await state_mod.get_state_backend().load("run_tasks_1")
        assert reloaded is not None
        assert [t["status"] for t in reloaded.tasks] == ["completed", "completed"]

    def test_task_events_are_structural(self):
        from core.orchestration.journal import STRUCTURAL_EVENT_TYPES
        assert "tasks_updated" in STRUCTURAL_EVENT_TYPES
        assert "tasks_incomplete" in STRUCTURAL_EVENT_TYPES

    def test_run_model_defaults_empty_task_list(self):
        from core.models_orchestration import OrchestrationRun
        run = OrchestrationRun(run_id="r", orchestration_id="o")
        assert run.tasks == []
