"""
A Tool step obeys the tool policy and runs only its own tool (F267, F281).

An Agent step's tool calls go through the policy: `deny` refuses, `confirm`
pauses the run for a person. A Tool step's did not. `run_bash` forced in a Tool
step ran under an agent `deny`, under a workspace `deny`, and under `confirm`
with no pause (F267). And the step ran whatever tool its model *named*:
`forced_tool` was a sentence in the prompt, and the name in the model's JSON was
looked up across every server, custom tool and builder tool (F281). Measured on
a scratch install: all four touched their flag file and the runs "completed".
"""
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from core.tools import VirtualTool


def _ann(**kw):
    from mcp.types import ToolAnnotations
    return ToolAnnotations(**kw)


DANGER = VirtualTool("danger", "changes things", {}, _ann(readOnlyHint=False, destructiveHint=True))
CLOCK = VirtualTool("clock", "reads the time", {}, _ann(readOnlyHint=True, idempotentHint=True))


class _Session:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, args, **_kw):
        self.calls.append((name, args))
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=f"ran-{name}")], isError=False)


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
                                 session=session, declared=declared)


@pytest.fixture
def workspace_policy(monkeypatch):
    import core.config as config_mod

    real = config_mod.load_settings
    policy: dict = {}

    def load_settings(*a, **k):
        return {**real(*a, **k), "tool_policy_defaults": dict(policy)}

    monkeypatch.setattr(config_mod, "load_settings", load_settings)
    return policy


def _orch(tool="danger", agent_id=None):
    return S.make_orchestration(id="orch_tool_policy", entry_step_id="call", steps=[
        {"id": "call", "name": "Call", "type": "tool", "forced_tool": tool, "agent_id": agent_id,
         "prompt_template": "use it", "output_key": "result", "model": "claude-x", "max_turns": 3,
         "next_step_id": "after"},
        {"id": "after", "name": "After", "type": "print", "print_content": "ran", "output_key": "after",
         "next_step_id": None}])


async def _run(orch, server, run_id="run_tp"):
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


class TestTheToolStepObeysThePolicy:
    async def test_an_agent_deny_fails_the_step_before_the_model_is_asked(self, fake_llm, server):
        agent = S.make_agent(tools=["danger"], tool_policy={"destructive": "deny"})
        await S.seed_agents([agent])
        fake_llm.script([tool_call("danger", cmd="x")] * 3)
        events, done = await _run(_orch(agent_id=agent["id"]), server)
        assert server.session.calls == []
        assert done and done[-1]["status"] == "failed"
        assert "tool policy" in _said(events)
        assert fake_llm.call_count == 0

    async def test_a_workspace_deny_holds_for_a_step_with_no_agent(self, fake_llm, server, workspace_policy):
        workspace_policy["destructive"] = "deny"
        fake_llm.script([tool_call("danger", cmd="x")] * 3)
        events, done = await _run(_orch(), server)
        assert server.session.calls == []
        assert done and done[-1]["status"] == "failed"

    async def test_allow_is_unchanged(self, fake_llm, server):
        fake_llm.script([tool_call("danger", cmd="x")])
        _, done = await _run(_orch(), server)
        assert server.session.calls == [("danger", {"cmd": "x"})]
        assert done[-1]["status"] == "completed"


class TestConfirmPausesTheRunLikeAnAgentStep:
    async def test_it_pauses_with_the_call_and_nothing_has_run(self, fake_llm, server):
        agent = S.make_agent(tools=["danger"], tool_policy={"destructive": "confirm"})
        await S.seed_agents([agent])
        fake_llm.script([tool_call("danger", cmd="rm -rf build")])
        events, done = await _run(_orch(agent_id=agent["id"]), server, run_id="run_tp_c")
        pause = [e for e in events if e.get("type") == "human_input_required"]
        assert pause and pause[0]["kind"] == "tool_approval"
        assert pause[0]["tool_name"] == "danger" and pause[0]["tool_args"] == {"cmd": "rm -rf build"}
        assert server.session.calls == []
        assert not done or done[-1]["status"] != "completed"

    async def test_approving_runs_the_call_once_without_asking_the_model_again(self, fake_llm, server):
        agent = S.make_agent(tools=["danger"], tool_policy={"destructive": "confirm"})
        await S.seed_agents([agent])
        fake_llm.script([tool_call("danger", cmd="rm -rf build")])
        await _run(_orch(agent_id=agent["id"]), server, run_id="run_tp_a")
        assert server.session.calls == [], "nothing runs before the answer"
        calls_before = fake_llm.call_count

        _, done = await _resume("run_tp_a", {"approve": True}, server)
        assert server.session.calls == [("danger", {"cmd": "rm -rf build"})]
        assert fake_llm.call_count == calls_before, "the approved call is the one that runs"
        assert done[-1]["status"] == "completed"
        assert done[-1]["final_state"]["result"] == "ran-danger"

    async def test_the_spent_approval_is_saved_before_the_call_runs(self, fake_llm, server, monkeypatch):
        """Otherwise a crash mid-call finds the approval again on recovery and
        runs the call a second time."""
        from core.orchestration.state import SharedState

        agent = S.make_agent(tools=["danger"], tool_policy={"destructive": "confirm"})
        await S.seed_agents([agent])
        fake_llm.script([tool_call("danger", cmd="rm -rf build")])
        await _run(_orch(agent_id=agent["id"]), server, run_id="run_tp_ck")

        order: list[str] = []
        real = SharedState.checkpoint

        async def checkpoint(self, *a, **k):
            progress = (self.run.step_progress or {}).get("call") or {}
            order.append("saved-with-approval" if progress.get("pending_call") else "saved")
            return await real(self, *a, **k)

        real_call = server.session.call_tool

        async def call_tool(name, args, **kw):
            order.append("ran")
            return await real_call(name, args, **kw)

        monkeypatch.setattr(SharedState, "checkpoint", checkpoint)
        monkeypatch.setattr(server.session, "call_tool", call_tool)
        await _resume("run_tp_ck", {"approve": True}, server)
        ran = order.index("ran")
        assert "saved" in order[:ran], "the spent approval was saved before the call"
        assert "saved-with-approval" not in order[ran:]

    async def test_refusing_fails_the_step_with_the_reason(self, fake_llm, server):
        agent = S.make_agent(tools=["danger"], tool_policy={"destructive": "confirm"})
        await S.seed_agents([agent])
        fake_llm.script([tool_call("danger", cmd="rm -rf build")])
        await _run(_orch(agent_id=agent["id"]), server, run_id="run_tp_r")

        events, done = await _resume("run_tp_r", {"approve": False, "reason": "not today"}, server)
        assert server.session.calls == []
        assert done[-1]["status"] == "failed"
        assert "not today" in _said(events)


class TestTheStepRunsOnlyItsOwnTool:
    async def test_a_model_naming_another_tool_runs_nothing(self, fake_llm, server):
        """F281: a step forced to the clock whose model answers with `danger`."""
        fake_llm.script([tool_call("danger", cmd="x")] * 3)
        events, done = await _run(_orch(tool="clock"), server, run_id="run_tp_f281")
        assert server.session.calls == []
        assert done[-1]["status"] == "failed"
        assert "clock" in _said(events)

    async def test_a_model_that_corrects_itself_runs_the_forced_tool(self, fake_llm, server):
        fake_llm.script([tool_call("danger", cmd="x"), tool_call("clock")])
        _, done = await _run(_orch(tool="clock"), server, run_id="run_tp_f281b")
        assert server.session.calls == [("clock", {})]
        assert done[-1]["status"] == "completed"
        assert "clock" in str(fake_llm.calls[1]), "the retry was told which tool to call"


    async def test_a_bare_name_for_the_same_namespaced_tool_runs_it(self, fake_llm, server):
        """A model that drops the `server__` prefix names the same tool, not another."""
        from core.tool_router import ToolRouter

        router = ToolRouter({})
        router["acme__clock"] = ("fake", "clock")
        server.tool_router = router
        server.declared["acme__clock"] = VirtualTool("acme__clock", "time", {}, CLOCK.annotations)
        fake_llm.script([tool_call("clock")])
        _, done = await _run(_orch(tool="acme__clock"), server, run_id="run_tp_f281c")
        assert server.session.calls == [("clock", {})]
        assert done[-1]["status"] == "completed"


class TestAStrangersReadOnlyClaimIsNotBelieved:
    async def test_an_external_servers_read_only_tool_is_undeclared(self):
        """`aggregate_all_tools` marks an external MCP server's tools untrusted;
        `resolve_one_tool`, which the Tool step uses, lost the mark — so a policy
        check there would believe a third party's "I only read"."""
        from core.tools import resolve_one_tool
        from core.tool_policy import decide

        ext = types.SimpleNamespace(name="lookup", description="d", inputSchema={},
                                    annotations=_ann(readOnlyHint=True, idempotentHint=True))
        server = types.SimpleNamespace(agent_sessions={}, tool_router={"acme__lookup": ("ext_mcp_acme", "lookup")},
                                       _session_tools={"ext_mcp_acme": [ext]})
        tool = await resolve_one_tool(server, "acme__lookup", {"tools": ["all"]}, [])
        assert tool is not None
        assert decide(tool, {"destructive": "allow", "open_world": "allow", "undeclared": "deny"}) == \
            ("deny", "undeclared")
