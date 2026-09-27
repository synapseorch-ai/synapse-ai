"""
A visitor's turn holds only the tools its owner ticked (F280).

A widget visitor — anyone on a site that embeds the widget — used to run the
agent exactly as a member does: every tool it held, `run_bash` included, and the
read-only defaults every agent carries (`read_file`, `grep`, …), which read the
owner's files and vault. Measured on a scratch install: a visitor's turn was
offered `run_bash` and ten file tools, and ran `run_bash`.

A category policy cannot close this — a trusted read-only tool always passes it —
so a visitor gets an explicit list. These pin the list at the engine, where every
surface's turn ends up: what is offered, what may run, and that a `confirm` never
reaches somebody who cannot answer it.
"""
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from core import visitor
from core.tools import VirtualTool
from core.visitor import VisitorScope


def _ann(**kw):
    from mcp.types import ToolAnnotations
    return ToolAnnotations(**kw)


DANGER = VirtualTool("danger", "changes things", {}, _ann(readOnlyHint=False, destructiveHint=True))
PEEK = VirtualTool("peek", "reads things", {}, _ann(readOnlyHint=True, idempotentHint=True))


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


@pytest.fixture
def two_tools(monkeypatch):
    """The agent holds exactly `danger` (destructive) and `peek` (trusted read-only)."""
    import core.react_engine as re_mod

    async def aggregated(server_module, agent, custom_tools):
        tools = [DANGER, PEEK]
        ollama = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                    "parameters": t.inputSchema}} for t in tools]
        return list(tools), {t.name: t.inputSchema for t in tools}, ollama, str([t.name for t in tools])

    monkeypatch.setattr(re_mod, "aggregate_all_tools", aggregated)


async def _run(fake_llm, script, agent, scope=None, executed=None):
    from core.react_engine import run_agent_step

    async def executor(name, args):
        if executed is not None:
            executed.append(name)
        return f"ran-{name}"

    fake_llm.script(script)
    with visitor.scope(scope):
        return [ev async for ev in run_agent_step(
            message="go", agent_id=agent["id"], session_id="s_visitor", server_module=_server(),
            agent_override=agent, tool_executor=executor, max_turns=6,
        )]


def _offered(fake_llm, i=0) -> set[str]:
    return {t["function"]["name"] for t in (fake_llm.calls[i].get("tools") or [])}


WIDGET_NOTHING = VisitorScope("widget", frozenset())


class TestAMemberTurnIsUnchanged:
    async def test_every_tool_is_offered_and_runs(self, fake_llm, two_tools):
        """The guard that everything below is scoped to visitors: without a scope,
        the agent keeps what it had, the default file tools included."""
        executed = []
        await _run(fake_llm, [tool_call("danger"), tool_call("read_file", path="x"), "done"],
                   S.make_agent(tools=["all"]), executed=executed)
        assert {"danger", "peek"} <= _offered(fake_llm)
        assert executed == ["danger", "read_file"]


class TestAVisitorGetsOnlyWhatWasTicked:
    async def test_nothing_ticked_means_no_tool_is_offered(self, fake_llm, two_tools):
        await _run(fake_llm, ["done"], S.make_agent(tools=["all"]), WIDGET_NOTHING)
        assert _offered(fake_llm) <= {"update_tasks", "load_skill"}

    async def test_a_call_to_a_tool_it_was_not_offered_does_not_run(self, fake_llm, two_tools):
        """A model can name a tool it was never offered. The destructive one, the
        read-only one and a default file tool are all refused for a visitor — the
        last because the default set is admitted by type, not by the agent's list."""
        executed = []
        await _run(fake_llm, [tool_call("danger"), tool_call("peek"), tool_call("read_file", path="x"),
                              "done"], S.make_agent(tools=["all"]), WIDGET_NOTHING, executed)
        assert executed == []

    async def test_a_ticked_tool_is_offered_and_runs(self, fake_llm, two_tools):
        executed = []
        await _run(fake_llm, [tool_call("peek"), tool_call("danger"), "done"],
                   S.make_agent(tools=["all"]), VisitorScope("widget", frozenset({"peek"})), executed)
        offered = _offered(fake_llm)
        assert "peek" in offered and "danger" not in offered
        assert executed == ["peek"]

    async def test_a_ticked_tool_the_agent_does_not_hold_is_not_added(self, fake_llm, two_tools):
        """The owner's list narrows the agent; it never grants."""
        executed = []
        await _run(fake_llm, [tool_call("read_file", path="x"), "done"],
                   S.make_agent(tools=["peek"], skip_default_tools=True),
                   VisitorScope("widget", frozenset({"read_file"})), executed)
        assert executed == []


class TestTheListNeverWidensAStep:
    async def test_a_steps_narrowing_still_holds_for_a_visitor(self, fake_llm, two_tools):
        """An orchestrator agent on a widget: the owner ticked `danger` for one
        step, and another step is narrowed to `peek`. That step must not run
        `danger` for a visitor when it would not for a member."""
        from core.react_engine import run_agent_step

        executed = []

        async def executor(name, args):
            executed.append(name)
            return f"ran-{name}"

        fake_llm.script([tool_call("danger"), tool_call("peek"), "done"])
        agent = S.make_agent(tools=["all"])
        with visitor.scope(VisitorScope("widget", frozenset({"danger", "peek"}))):
            [ev async for ev in run_agent_step(
                message="go", agent_id=agent["id"], session_id="s_narrow", server_module=_server(),
                agent_override=agent, tool_executor=executor, max_turns=6,
                allowed_tools_override=["peek"])]
        assert "danger" not in _offered(fake_llm)
        assert executed == ["peek"]


class TestNobodyIsAskedToApprove:
    async def test_confirm_is_a_deny_for_a_visitor(self, fake_llm, two_tools):
        """Nobody on a public surface can answer an approval card, so the call is
        refused and the model told why — the turn does not park on a question."""
        executed = []
        agent = S.make_agent(tools=["all"], tool_policy={"destructive": "confirm"})
        events = await _run(fake_llm, [tool_call("danger"), "done"], agent,
                            VisitorScope("widget", frozenset({"danger"})), executed)
        assert executed == []
        assert not any(e["type"] == "human_input_required" for e in events)
        assert any(e["type"] == "tool_result" and "Blocked by policy" in (e.get("preview") or "")
                   for e in events)

    async def test_a_member_is_still_asked(self, fake_llm, two_tools):
        agent = S.make_agent(tools=["all"], tool_policy={"destructive": "confirm"})
        events = await _run(fake_llm, [tool_call("danger"), "done"], agent)
        assert events[-1]["type"] == "human_input_required"


class TestTheScopeReachesEveryAgentTheTurnStarts:
    async def test_a_subtask_is_not_spawned_unless_ticked(self, fake_llm, two_tools):
        """`spawn_subtask` runs its batch before the tool guard; a visitor's turn
        must not reach it at all."""
        executed = []
        agent = S.make_agent(tools=["all", "spawn_subtask"])
        events = await _run(fake_llm, [tool_call("spawn_subtask", task="t", tools=["danger"]), "done"],
                            agent, WIDGET_NOTHING, executed)
        assert "spawn_subtask" not in _offered(fake_llm)
        assert not any(e.get("subtask_name") for e in events)
        assert fake_llm.call_count == 2, "no sub-agent ran a model call"

    async def test_a_delegate_is_not_reached_unless_ticked(self, fake_llm, two_tools):
        helper = S.make_agent(tools=["all"])
        await S.seed_agents([helper])
        lead = S.make_agent(type="delegate", tools=["all"], delegate_agent_ids=[helper["id"]])
        events = await _run(fake_llm, [tool_call("delegate_to_agent", agent_id=helper["id"], task="t"),
                                       "done"], lead, WIDGET_NOTHING)
        assert "delegate_to_agent" not in _offered(fake_llm)
        assert not any(e.get("delegate_from") for e in events)
        assert fake_llm.call_count == 2


class TestAToolStepInAVisitorsWorkflow:
    async def test_a_tool_the_visitor_may_not_use_fails_the_step(self, fake_llm, monkeypatch):
        """An orchestrator agent on a widget runs its workflow in the visitor's
        turn. A Tool step is a tool call like any other, so the list holds there too."""
        import core.tools as tools_mod
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine

        async def resolve_one(server_module, name, agent, custom_tools):
            return DANGER if name == "danger" else None

        monkeypatch.setattr(tools_mod, "resolve_one_tool", resolve_one)
        fake_llm.script([tool_call("danger")] * 3)
        orch = S.make_orchestration(entry_step_id="call", steps=[
            {"id": "call", "name": "Call", "type": "tool", "forced_tool": "danger", "prompt_template": "go",
             "output_key": "result", "model": "claude-x", "max_turns": 3, "next_step_id": None}])
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        with visitor.scope(WIDGET_NOTHING):
            events = [e async for e in engine.run("go", run_id="run_visitor_tool")]
        done = [e for e in events if e.get("type") == "orchestration_complete"][-1]
        assert done["status"] == "failed"
        assert fake_llm.call_count == 0, "refused before the model is asked for arguments"
        assert any("visitors" in str(e.get("response") or e.get("error") or "") for e in events)


class TestTheListIsClean:
    def test_memory_tools_can_never_be_ticked(self):
        assert visitor.clean_tools(["peek", "remember", " peek ", 3, "", "search_memory"]) == ["peek"]

    def test_the_fleet_kwarg_round_trips(self):
        scope = VisitorScope("channel", frozenset({"peek"}))
        assert VisitorScope.from_dict(scope.to_dict()) == scope
        assert VisitorScope.from_dict(None) is None

    def test_a_malformed_kwarg_is_a_visitor_with_nothing_not_a_member(self):
        assert VisitorScope.from_dict('{"surface": "widget"}') == VisitorScope("widget", frozenset())
        assert VisitorScope.from_dict(["peek"]) == VisitorScope("widget", frozenset())
