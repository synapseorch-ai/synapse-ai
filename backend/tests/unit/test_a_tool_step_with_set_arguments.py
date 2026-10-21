"""
A Tool step whose arguments are set on the step calls its tool with no model (E1, SAI2-P1, D300).

Every Tool step used to ask a model for its arguments (`ToolStepExecutor`), even when every argument was already in
state: Synapse AI's `finish_build` spent a main-model call that needed no arguments at all. `tool_arguments` sets them
on the step, templated from state, and the tool is called with no model. Everything else a Tool step does holds: the
tool policy and its approval, the events the run log and the harness read, the output, and TAG1's rule about calling
a tool again.
"""
import json
import types

import pytest

from _fakes import seed as S
from core.tools import VirtualTool


def _ann(**kw):
    from mcp.types import ToolAnnotations
    return ToolAnnotations(**kw)


SCHEMA = {"type": "object", "properties": {"to": {"type": "string"}, "items": {"type": "array"},
                                            "note": {"type": "string"}}, "required": ["to"]}
DANGER = VirtualTool("danger", "changes things", SCHEMA, _ann(readOnlyHint=False, destructiveHint=True))
CLOCK = VirtualTool("clock", "reads the time", SCHEMA, _ann(readOnlyHint=True, idempotentHint=True))


class _Session:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.fail_with: list = []

    async def call_tool(self, name, args, **_kw):
        self.calls.append((name, args))
        if self.fail_with:
            outcome = self.fail_with.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return types.SimpleNamespace(content=[types.SimpleNamespace(text=outcome)], isError=True)
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=json.dumps({"ran": name, "got": args}))],
                                     isError=False)


@pytest.fixture
def server(monkeypatch):
    import core.tools as tools_mod

    declared = {"danger": DANGER, "clock": CLOCK}

    async def one(server_module, name, agent, custom_tools):
        return declared.get(name)

    monkeypatch.setattr(tools_mod, "resolve_one_tool", one)
    session = _Session()
    return types.SimpleNamespace(agent_sessions={"fake": session}, memory_store=None,
                                 tool_router={"danger": ("fake", "danger"), "clock": ("fake", "clock")},
                                 session=session)


#: A transform before the tool step puts typed values in state, as an earlier step's output would.
_SETUP = ("result = {'who': 'ops@example.com', 'list': [1, 2], "
          "'plan_json': '{\"title\": \"Weekly report\", \"steps\": [{\"id\": \"a\"}]}'}")


def _orch(arguments, tool="danger", agent_id=None, max_turns=3):
    return S.make_orchestration(id="orch_set_args", entry_step_id="setup", steps=[
        {"id": "setup", "name": "Setup", "type": "transform", "transform_code": _SETUP, "output_key": "data",
         "next_step_id": "call"},
        {"id": "call", "name": "Call", "type": "tool", "forced_tool": tool, "agent_id": agent_id,
         "tool_arguments": arguments, "output_key": "result", "model": "claude-x", "max_turns": max_turns,
         "next_step_id": None}])


async def _run(orch, server, run_id="run_sa"):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    await S.seed_orchestrations([orch])
    events = [e async for e in OrchestrationEngine(Orchestration.model_validate(orch), server).run("go", run_id=run_id)]
    return events, [e for e in events if e.get("type") == "orchestration_complete"]


async def _resume(run_id, answer, server):
    from core.orchestration.engine import OrchestrationEngine

    events = [e async for e in OrchestrationEngine.resume(run_id, answer, server)]
    return events, [e for e in events if e.get("type") == "orchestration_complete"]


def _said(events) -> str:
    return " ".join(str(e.get("response") or e.get("error") or e.get("message") or "") for e in events)


class TestNoModelIsAsked:
    async def test_the_tool_runs_with_the_arguments_set_on_the_step(self, fake_llm, server):
        _, done = await _run(_orch({"to": "{{state.data.who}}", "note": "weekly"}, tool="clock"), server)
        assert fake_llm.call_count == 0
        assert server.session.calls == [("clock", {"to": "ops@example.com", "note": "weekly"})]
        assert done[-1]["status"] == "completed"
        assert json.loads(done[-1]["final_state"]["result"])["got"]["to"] == "ops@example.com"

    async def test_the_events_the_run_log_reads_keep_their_shape(self, fake_llm, server):
        events, _ = await _run(_orch({"to": "x"}, tool="clock"), server)
        executed = [e for e in events if e.get("type") == "tool_execution"]
        results = [e for e in events if e.get("type") == "tool_result"]
        assert executed and executed[0]["tool_name"] == "clock" and executed[0]["args"] == {"to": "x"}
        assert executed[0]["orch_step_id"] == "call" and executed[0]["step_name"] == "Call"
        assert results and results[0]["tool_name"] == "clock" and "preview" in results[0]
        assert not [e for e in events if e.get("type") == "_log_tool_step_llm"]

    async def test_without_set_arguments_the_model_is_asked_as_before(self, fake_llm, server):
        from _fakes.fake_llm import tool_call

        orch = _orch(None, tool="clock")
        orch["steps"][1]["prompt_template"] = "send it"
        fake_llm.script([tool_call("clock", to="y")])
        _, done = await _run(orch, server)
        assert fake_llm.call_count == 1
        assert server.session.calls == [("clock", {"to": "y"})]
        assert done[-1]["status"] == "completed"


class TestTheArgumentsAreRenderedFromState:
    async def test_a_reference_standing_alone_keeps_its_type(self, fake_llm, server):
        await _run(_orch({"to": "a", "items": "{{state.data.list}}"}, tool="clock"), server)
        assert server.session.calls == [("clock", {"to": "a", "items": [1, 2]})]

    async def test_an_object_inside_text_is_written_as_json(self, fake_llm, server):
        await _run(_orch({"to": "a", "note": "items: {{state.data.list}}"}, tool="clock"), server)
        assert server.session.calls == [("clock", {"to": "a", "note": "items: [1, 2]"})]

    async def test_the_older_single_brace_form_is_read_too(self, fake_llm, server):
        await _run(_orch({"to": "{state.data.who}"}, tool="clock"), server)
        assert server.session.calls == [("clock", {"to": "ops@example.com"})]

    async def test_a_path_reads_into_a_value_held_as_json_text(self, fake_llm, server):
        """A builder tool's output is JSON text in state; a set argument reads into it."""
        await _run(_orch({"to": "{{state.data.plan_json.title}}", "items": "{{state.data.plan_json.steps}}"},
                         tool="clock"), server)
        assert server.session.calls == [("clock", {"to": "Weekly report", "items": [{"id": "a"}]})]

    async def test_nested_values_are_rendered_too(self, fake_llm, server):
        await _run(_orch({"to": "a", "items": [{"who": "{{state.data.who}}"}, "{{state.data.list}}"]},
                         tool="clock"), server)
        assert server.session.calls == [("clock", {"to": "a", "items": [{"who": "ops@example.com"}, [1, 2]]})]

    async def test_a_reference_to_nothing_fails_the_step_and_names_it(self, fake_llm, server):
        events, done = await _run(_orch({"to": "{{state.data.nobody}}"}, tool="clock"), server)
        assert server.session.calls == []
        assert done[-1]["status"] == "failed"
        assert "state.data.nobody" in _said(events)
        assert fake_llm.call_count == 0


class TestThePolicyHolds:
    async def test_deny_fails_the_step_and_nothing_runs(self, fake_llm, server):
        agent = S.make_agent(tools=["danger"], tool_policy={"destructive": "deny"})
        await S.seed_agents([agent])
        events, done = await _run(_orch({"to": "x"}, agent_id=agent["id"]), server)
        assert server.session.calls == []
        assert done[-1]["status"] == "failed"
        assert "tool policy" in _said(events)

    async def test_confirm_pauses_with_the_rendered_call(self, fake_llm, server):
        agent = S.make_agent(tools=["danger"], tool_policy={"destructive": "confirm"})
        await S.seed_agents([agent])
        events, _ = await _run(_orch({"to": "{{state.data.who}}"}, agent_id=agent["id"]), server, run_id="run_sa_c")
        pause = [e for e in events if e.get("type") == "human_input_required"]
        assert pause and pause[0]["kind"] == "tool_approval"
        assert pause[0]["tool_args"] == {"to": "ops@example.com"}
        assert server.session.calls == []

    async def test_approving_runs_the_call_once_and_no_model_is_asked(self, fake_llm, server):
        agent = S.make_agent(tools=["danger"], tool_policy={"destructive": "confirm"})
        await S.seed_agents([agent])
        await _run(_orch({"to": "{{state.data.who}}"}, agent_id=agent["id"]), server, run_id="run_sa_a")
        _, done = await _resume("run_sa_a", {"approve": True}, server)
        assert server.session.calls == [("danger", {"to": "ops@example.com"})]
        assert fake_llm.call_count == 0
        assert done[-1]["status"] == "completed"


class TestCallingAgain:
    async def test_a_tools_own_error_is_not_sent_again_with_the_same_arguments(self, fake_llm, server):
        """The model path retries a refusal with other arguments; set arguments cannot change, so the same call
        would fail the same way."""
        server.session.fail_with = ["400 bad recipient"]
        events, done = await _run(_orch({"to": "x"}, tool="clock"), server)
        assert server.session.calls == [("clock", {"to": "x"})]
        assert done[-1]["status"] == "failed"
        assert "400 bad recipient" in _said(events)

    async def test_a_call_that_never_arrived_is_tried_again_when_it_is_safe_to_repeat(self, fake_llm, server):
        server.session.fail_with = [ConnectionRefusedError("connection refused")]
        _, done = await _run(_orch({"to": "x"}, tool="clock"), server)
        assert server.session.calls == [("clock", {"to": "x"})] * 2
        assert done[-1]["status"] == "completed"

    async def test_a_call_that_may_have_run_is_not_made_again(self, fake_llm, server):
        """TAG1 (F737): `danger` declares nothing safe to repeat, and an MCP request that timed out may have reached
        it."""
        from mcp.shared.exceptions import McpError
        from mcp.types import ErrorData

        server.session.fail_with = [McpError(ErrorData(code=408, message="Timed out while waiting for response"))]
        events, done = await _run(_orch({"to": "x"}), server)
        assert server.session.calls == [("danger", {"to": "x"})]
        assert done[-1]["status"] == "failed"



class TestItIsNotCountedAsAModelCall:
    """CE1's estimate and a workflow's readiness count the steps that call a model; this one calls none."""

    def test_the_estimate_reads_no_model_for_it(self):
        from core.orchestration.estimate import step_model

        asked = {"id": "a", "type": "tool", "forced_tool": "clock"}
        assert step_model(asked, {}, "ollama.mistral") == "ollama.mistral"
        assert step_model({**asked, "tool_arguments": {"to": "x"}}, {}, "ollama.mistral") is None

    async def test_a_workflow_of_set_steps_needs_no_model(self):
        from core.orchestration.step_models import walk_agent

        orch = {"id": "o", "name": "o", "entry_step_id": "t", "steps": [
            {"id": "t", "name": "T", "type": "tool", "forced_tool": "clock", "tool_arguments": {"to": "x"},
             "next_step_id": None}]}

        async def load_orchestration(oid):
            return orch

        async def load_agent(aid):
            return None

        walked = await walk_agent({"id": "w", "type": "orchestrator", "orchestration_id": "o"},
                                  load_agent=load_agent, load_orchestration=load_orchestration,
                                  workspace_model="ollama.mistral")
        assert walked.steps == []
