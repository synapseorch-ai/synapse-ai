"""A helper (`spawn_subtask`) is seen, kept, on its parent's model, marked and bounded (SUB1a, D279; F538, F546).

A helper used to run invisibly: `_run_spawn_subtask_batch` was awaited whole, so its
parent's turn showed nothing while it worked, and only its `final` was read — its calls
were neither shown nor kept. It ran on the workspace's default model, whatever model its
parent was on (F538, and a delegate's member the same), started unmarked although its
brief came from an agent that had read tool output (F279), had no stop of its own and no
time limit, and a reply could start any number. And the batch ran before the reply's
other calls, so one that stopped to ask a person left the helpers' work unread (F546).

Now (`core/helpers.py`): what a helper does goes out wrapped (`helper_event`) between a
`helper_started` and a `helper_finished` carrying its record, which the turn keeps; it
runs on its parent's model or the agent's helper model; it starts as marked as its
parent; it has a Stop, a time limit and a cap of three at once; and it runs where its
call is.
"""
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import Streamed, tool_call
from _fakes.routed_llm import RoutedLLM, install
from core.tools import VirtualTool


def _ann(**kw):
    from mcp.types import ToolAnnotations
    return ToolAnnotations(**kw)


LOOKUP = VirtualTool("lookup", "looks a thing up", {}, _ann(readOnlyHint=True, idempotentHint=True))
DANGER = VirtualTool("danger", "changes things", {}, _ann(readOnlyHint=False, destructiveHint=True))


class _Session:
    def __init__(self):
        self.calls: list[str] = []

    async def send_ping(self):
        return None

    async def call_tool(self, name, args, **_kw):
        self.calls.append(name)
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=f"{name} says ok")], isError=False)


@pytest.fixture
def server(monkeypatch):
    import core.react_engine as re_mod

    async def aggregated(server_module, agent, custom_tools, **_):
        wanted = agent.get("tools") or []
        held = [t for t in (LOOKUP, DANGER) if "all" in wanted or t.name in wanted]
        ollama = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                    "parameters": t.inputSchema}} for t in held]
        return held, {t.name: t.inputSchema for t in held}, ollama, ""

    monkeypatch.setattr(re_mod, "aggregate_all_tools", aggregated)
    session = _Session()
    return types.SimpleNamespace(agent_sessions={"fake": session}, memory_store=None,
                                 tool_router={"lookup": ("fake", "lookup"), "danger": ("fake", "danger")},
                                 session=session)


@pytest.fixture
def llm(monkeypatch):
    return install(monkeypatch, RoutedLLM())


def _parent(**kw):
    return S.make_agent(**{"tools": ["spawn_subtask", "lookup", "danger"], **kw})


async def _turn(server, agent, *, message="PLAN IT", session_id="s_helpers", **kw):
    from core.react_engine import run_agent_step

    return [ev async for ev in run_agent_step(
        message=message, agent_id=agent["id"], session_id=session_id, server_module=server,
        agent_override=agent, max_turns=6, **kw)]


def _of(events, kind):
    return [e for e in events if e.get("type") == kind]


def _wrapped(events, kind=None):
    inner = [e["event"] for e in _of(events, "helper_event")]
    return [e for e in inner if kind is None or e.get("type") == kind]


def _spawn_results(events):
    return [e.get("preview") or "" for e in _of(events, "tool_result") if e.get("tool_name") == "spawn_subtask"]


def spawn(task, tools, **args):
    """A `spawn_subtask` call (its `name` argument would collide with `tool_call`'s own)."""
    import json
    return json.dumps({"tool": "spawn_subtask", "arguments": {"task": task, "tools": tools, **args}})


A_HELPER = [spawn("CHECK A", ["lookup"], name="Check A"), "all done"]
A_HELPERS_WORK = [tool_call("lookup", item="A"), "A is fine"]


class TestAHelperIsSeen:
    async def test_its_work_streams_wrapped_between_started_and_finished(self, server, llm):
        llm.on("PLAN IT", A_HELPER).on("CHECK A", A_HELPERS_WORK)
        events = await _turn(server, _parent())

        kinds = [e["type"] for e in events]
        assert "helper_started" in kinds and "helper_finished" in kinds
        assert kinds.index("helper_started") < kinds.index("helper_finished")
        started = _of(events, "helper_started")[0]
        assert started["helper_name"] == "Check A" and started["task"] == "CHECK A"
        assert started["tools"] == ["lookup"]
        calls = _wrapped(events, "tool_execution")
        assert [c["tool_name"] for c in calls] == ["lookup"]
        # The helper's call is the helper's: never one of the parent's own.
        assert not [e for e in _of(events, "tool_execution") if e["tool_name"] == "lookup"]
        record = _of(events, "helper_finished")[0]["record"]
        assert record["status"] == "completed" and record["answer"] == "A is fine"
        assert [c["tool"] for c in record["calls"]] == ["lookup"]
        assert server.session.calls == ["lookup"]
        assert "A is fine" in _spawn_results(events)[0]

    async def test_the_parent_ends_once(self, server, llm):
        """A helper's own `final` would read as its parent's answer — and end its stream."""
        llm.on("PLAN IT", A_HELPER).on("CHECK A", A_HELPERS_WORK)
        events = await _turn(server, _parent())
        finals = _of(events, "final")
        assert len(finals) == 1 and finals[0]["response"] == "all done"
        assert not _wrapped(events, "final")

    async def test_its_answer_never_streams_as_the_parents(self, server, llm):
        """A phone call speaks every `answer_delta`, OAI1 and v1 stream it: a helper's must stay its own."""
        llm.on("PLAN IT", A_HELPER).on("CHECK A", [tool_call("lookup"), Streamed(["helper ", "words"])])
        events = await _turn(server, _parent(), stream_answer=True)
        assert not [e for e in _of(events, "answer_delta") if "helper" in e.get("text", "")]
        assert "helper words" in "".join(e.get("text", "") for e in _wrapped(events, "answer_delta"))

    async def test_its_prompt_never_leaves(self, server, llm):
        llm.on("PLAN IT", A_HELPER).on("CHECK A", A_HELPERS_WORK)
        events = await _turn(server, _parent())
        shown = {e["type"] for e in _wrapped(events)}
        assert not shown & {"_log_llm_call", "_step_progress", "final"}


class TestAHelperIsKept:
    async def test_the_turn_keeps_its_record(self, server, llm):
        from core.session import _get_conversation_history

        llm.on("PLAN IT", A_HELPER).on("CHECK A", A_HELPERS_WORK)
        parent = _parent()
        await _turn(server, parent, session_id="s_kept")
        turns = await _get_conversation_history("s_kept", parent["id"])
        assert len(turns) == 1
        [kept] = turns[0]["helpers"]
        assert kept["name"] == "Check A" and kept["task"] == "CHECK A" and kept["tools"] == ["lookup"]
        assert kept["answer"] == "A is fine" and [c["tool"] for c in kept["calls"]] == ["lookup"]
        assert kept["status"] == "completed" and kept["mode"] == "wait"

    def test_a_turn_saved_from_outside_keeps_what_it_saw(self):
        """A Stop saves the turn from its events: a helper it cut short is kept, as stopped."""
        from core.session import helpers_from_events

        finished = {"id": "subtask_1", "name": "One", "status": "completed", "answer": "done"}
        kept = helpers_from_events([
            {"type": "helper_started", "helper_id": "subtask_1", "helper_name": "One", "task": "t1"},
            {"type": "helper_started", "helper_id": "subtask_2", "helper_name": "Two", "task": "t2"},
            {"type": "helper_finished", "helper_id": "subtask_1", "record": finished},
        ], resumed={"helpers": [{"id": "subtask_0", "name": "Before the pause", "status": "completed"}]})
        assert [(h["id"], h["status"]) for h in kept] == [
            ("subtask_0", "completed"), ("subtask_1", "completed"), ("subtask_2", "cancelled")]


class TestAHelpersModel:
    async def test_it_runs_on_its_parents_model(self, server, llm):
        """F538: it ran on the workspace default."""
        llm.on("PLAN IT", A_HELPER).on("CHECK A", A_HELPERS_WORK)
        await _turn(server, _parent(model="parent-model-x"))
        assert {c["current_model"] for c in llm.calls_for("CHECK A")} == {"parent-model-x"}

    async def test_a_steps_model_reaches_it(self, server, llm):
        llm.on("PLAN IT", A_HELPER).on("CHECK A", A_HELPERS_WORK)
        await _turn(server, _parent(model="parent-model-x"), model_override="step-model-y")
        assert {c["current_model"] for c in llm.calls_for("CHECK A")} == {"step-model-y"}

    async def test_the_agents_helper_model_is_used(self, server, llm):
        llm.on("PLAN IT", A_HELPER).on("CHECK A", A_HELPERS_WORK)
        await _turn(server, _parent(model="parent-model-x", helper_model="helper-model-z"))
        assert {c["current_model"] for c in llm.calls_for("CHECK A")} == {"helper-model-z"}
        assert {c["current_model"] for c in llm.calls_for("PLAN IT")} == {"parent-model-x"}

    def test_a_role_reads_the_run_then_the_setting_then_the_parent(self):
        from core.helpers import resolve_model

        agent = {"helper_model": "role:quick"}
        settings = {"builder_quick_model": "nova-lite"}
        assert resolve_model(agent, "parent", roles={"quick": "run-quick"}, settings=settings) == "run-quick"
        assert resolve_model(agent, "parent", roles={}, settings=settings) == "nova-lite"
        assert resolve_model(agent, "parent", roles=None, settings={}) == "parent"
        assert resolve_model({"helper_model": "role:unknown"}, "parent", settings=settings) == "parent"
        assert resolve_model({"helper_model": "default"}, "parent") == "parent"
        assert resolve_model({}, "parent") == "parent"

    async def test_a_workflow_runs_roles_reach_it(self, server, llm):
        llm.on("PLAN IT", A_HELPER).on("CHECK A", A_HELPERS_WORK)
        await _turn(server, _parent(model="parent-model-x", helper_model="role:quick"),
                    model_roles={"quick": "run-quick-model"})
        assert {c["current_model"] for c in llm.calls_for("CHECK A")} == {"run-quick-model"}

    async def test_it_thinks_as_hard_as_its_parent_only_on_its_parents_model(self, server, llm):
        llm.on("PLAN IT", A_HELPER).on("CHECK A", A_HELPERS_WORK)
        await _turn(server, _parent(model="parent-model-x", reasoning_effort="high"))
        assert {c["reasoning_effort"] for c in llm.calls_for("CHECK A")} == {"high"}


class TestADelegatesModel:
    """F538's other half: a member with no model of its own ran on the workspace default."""

    async def _delegate(self, server, llm, member_model):
        member = S.make_agent(id="member_f538", name="Member", tools=[], model=member_model)
        lead = S.make_agent(id="lead_f538", name="Lead", type="delegate", tools=[], model="lead-model-x",
                            delegate_agent_ids=[member["id"]])
        await S.seed_agents([member, lead])
        llm.on("LEAD ASK", [tool_call("delegate_to_agent", agent_id=member["id"], task="MEMBER WORK"), "led"])
        llm.on("MEMBER WORK", ["member answer"])
        await _turn(server, lead, message="LEAD ASK", session_id="s_f538")
        return {c["current_model"] for c in llm.calls_for("MEMBER WORK")}

    async def test_a_member_with_no_model_works_on_its_leads(self, server, llm):
        assert await self._delegate(server, llm, None) == {"lead-model-x"}

    async def test_a_member_with_its_own_model_keeps_it(self, server, llm):
        assert await self._delegate(server, llm, "member-model-m") == {"member-model-m"}


class TestAHelperStartsMarked:
    async def test_its_brief_is_untrusted_as_its_parents_turn_is(self, server, llm, monkeypatch):
        """F279: the brief is written by an agent that may have read tool output."""
        import core.react_engine as re_mod

        seen = []
        real = re_mod.run_agent_step

        def spy(**kw):
            if str(kw.get("agent_id") or "").startswith("subtask_"):
                seen.append(kw.get("untrusted_message"))
            return real(**kw)

        monkeypatch.setattr(re_mod, "run_agent_step", spy)
        llm.on("PLAN IT", [tool_call("lookup"), *A_HELPER]).on("CHECK A", A_HELPERS_WORK)
        await _turn(server, _parent())
        assert seen == [True]


FOUR = [spawn(f"CHECK {x}", ["lookup"], name=f"Check {x}") for x in "ABCD"]
#: One reply that makes all four calls.
FOUR_AT_ONCE = "\n".join(FOUR)


class TestAHelpersBudget:
    async def test_three_work_at_once_and_a_fourth_is_told_no(self, server, llm):
        llm.on("PLAN IT", [FOUR_AT_ONCE, "all done"])
        for x in "ABCD":
            llm.on(f"CHECK {x}", [f"{x} is fine"])
        events = await _turn(server, _parent())
        assert [e["helper_name"] for e in _of(events, "helper_started")] == ["Check A", "Check B", "Check C"]
        results = _spawn_results(events)
        assert len(results) == 4
        assert "No helper was started: 3 helpers are already working" in results[3]
        assert "Don't call spawn_subtask again" in results[3]
        assert not llm.calls_for("CHECK D")

    async def test_the_cap_frees_when_they_finish(self, server, llm):
        llm.on("PLAN IT", ["\n".join(FOUR[:3]), FOUR[3], "all done"])
        for x in "ABCD":
            llm.on(f"CHECK {x}", [f"{x} is fine"])
        events = await _turn(server, _parent())
        assert [e["helper_name"] for e in _of(events, "helper_started")] == [
            "Check A", "Check B", "Check C", "Check D"]
        assert not [r for r in _spawn_results(events) if "No helper was started" in r]

    async def test_a_helper_out_of_time_stops_and_says_so(self, server, llm, monkeypatch):
        from core import helpers

        monkeypatch.setattr(helpers, "WAIT_SECONDS", 0)
        llm.on("PLAN IT", A_HELPER).on("CHECK A", A_HELPERS_WORK)
        events = await _turn(server, _parent())
        assert _of(events, "helper_finished")[0]["status"] == "timed_out"
        assert "ran out of time" in _spawn_results(events)[0]
        assert not llm.calls_for("CHECK A")

    async def test_a_helper_a_person_stops_ends_and_says_what_it_did(self, server, llm):
        from core import helpers

        def stop_it(_kw):
            for held in helpers._LIVE.values():
                for helper in held.values():
                    helper.stop_requested = True
            return tool_call("lookup", item="A")

        llm.on("PLAN IT", A_HELPER).on("CHECK A", [stop_it, "never reached"])
        events = await _turn(server, _parent())
        finished = _of(events, "helper_finished")[0]
        assert finished["status"] == "cancelled"
        result = _spawn_results(events)[0]
        assert "was stopped before it finished" in result and "lookup" in result
        assert _of(events, "final")[0]["response"] == "all done", "the parent carries on"

    async def test_a_helper_stops_with_its_parent(self, server, llm):
        import asyncio
        flag = {"stop": False}

        async def parent_stops():
            return flag["stop"]

        def stop_parent(_kw):
            flag["stop"] = True
            return tool_call("lookup", item="A")

        llm.on("PLAN IT", A_HELPER).on("CHECK A", [stop_parent, "never reached"])
        events = []
        with pytest.raises(asyncio.CancelledError):
            from core.react_engine import run_agent_step
            agent = _parent()
            async for ev in run_agent_step(message="PLAN IT", agent_id=agent["id"], session_id="s_stop",
                                           server_module=server, agent_override=agent, max_turns=6,
                                           should_stop=parent_stops):
                events.append(ev)
        assert _of(events, "helper_finished")[0]["status"] == "cancelled"

    async def test_none_is_left_counted(self, server, llm):
        from core import helpers

        llm.on("PLAN IT", [FOUR_AT_ONCE, "all done"])
        for x in "ABCD":
            llm.on(f"CHECK {x}", [f"{x} is fine"])
        await _turn(server, _parent())
        assert helpers._LIVE == {}


class TestAHelperRunsWhereItsCallIs:
    async def test_a_call_that_stops_to_ask_first_leaves_no_helper_run(self, server, llm):
        """F546: the batch ran before the reply's other calls, so it ran and was never read."""
        llm.on("PLAN IT", [tool_call("danger") + "\n" + A_HELPER[0], "all done"])
        llm.on("CHECK A", A_HELPERS_WORK)
        events = await _turn(server, _parent(tool_policy={"destructive": "confirm"}), progress_events=True)
        assert events[-1]["type"] == "human_input_required"
        assert not _of(events, "helper_started") and not llm.calls_for("CHECK A")


class TestWhereBackgroundIsNot:
    async def test_it_is_not_offered_and_an_ask_for_it_runs_as_a_wait(self, server, llm):
        llm.on("PLAN IT", [spawn("CHECK A", ["lookup"], background=True), "all done"])
        llm.on("CHECK A", A_HELPERS_WORK)
        events = await _turn(server, _parent())
        offered = [t["function"] for t in llm.calls_for("PLAN IT")[0]["tools"]
                   if t["function"]["name"] == "spawn_subtask"][0]
        assert "background" not in offered["parameters"]["properties"]
        assert _of(events, "helper_finished")[0]["record"]["mode"] == "wait"
        assert "helpers work in the background only in the app's chat" in _spawn_results(events)[0]


class TestAHelperInAWorkflow:
    async def test_its_plan_never_becomes_the_runs(self, llm):
        """F536's class: a helper's `tasks_updated` travels inside its wrapper, which the step never reads."""
        import core.orchestration.state as state_mod
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        from core.task_list import TASK_TOOL_NAME

        lead_plan = [{"content": "lead: split it", "status": "completed"}]
        helper_plan = [{"content": "helper: its own", "status": "completed"}]
        await S.seed_agents([S.make_agent(id="wf_parent", name="Parent", tools=["spawn_subtask"])])
        orch = S.make_orchestration(id="orch_helper_plan", entry_step_id="s1", steps=[
            {"id": "s1", "name": "Parent", "type": "agent", "agent_id": "wf_parent", "next_step_id": None}])
        llm.on("CHECK A", [tool_call(TASK_TOOL_NAME, tasks=helper_plan), "A is fine"])
        llm.otherwise([tool_call(TASK_TOOL_NAME, tasks=lead_plan),
                       spawn("CHECK A", []), "step done"])
        engine = OrchestrationEngine(Orchestration.model_validate(orch),
                                     types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}))
        events = [ev async for ev in engine.run("go", run_id="run_helper_plan")]
        assert [e for e in events if e.get("type") == "helper_finished"]
        assert llm.calls_for("CHECK A"), "the helper ran"
        tasks = [t["content"] for t in (await state_mod.get_state_backend().load("run_helper_plan")).tasks]
        assert tasks == ["lead: split it"]
