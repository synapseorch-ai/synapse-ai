"""
A Tool step whose tool fails fails — it does not hand the failure on as output (F90).

A Tool step used to count three kinds of tool failure as success: a python
tool's non-zero exit came back as an error JSON string, an MCP result's
`isError` was never read, and an HTTP tool's 4xx/5xx came back as the error
body. Each was written into the step's output key and the run carried on.
A Tool step with no tool, a loop nested past the limit and a body step with no
executor were reported and skipped, and the run carried on as if they had run.

Now (Naveen, D112) a tool error is a failed call. What new arguments can fix —
a python exit, an MCP error, an HTTP 4xx — uses the step's attempts, telling the
model what failed; an HTTP 5xx fails at once and is not sent again, because the
server faulted and the request may already have had its effect. A step that
cannot run at all fails the run, as a tool that is not found already did (F70).
"""
import types

import httpx
import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call


def _server(**extra):
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}, **extra)


def _engine(orch, server=None):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    return OrchestrationEngine(Orchestration.model_validate(orch), server or _server())


def _tool_step(tool, nxt="after", max_turns=3):
    return {"id": "call", "name": "Call", "type": "tool", "forced_tool": tool,
            "prompt_template": "use it", "output_key": "result", "model": "claude-x",
            "max_turns": max_turns, "next_step_id": nxt}


_AFTER = {"id": "after", "name": "After", "type": "print", "print_content": "ran",
          "output_key": "after", "next_step_id": None}


async def _run(orch, server=None, run_id="run_f90"):
    events = [e async for e in _engine(orch, server).run("go", run_id=run_id)]
    done = [e for e in events if e.get("type") == "orchestration_complete"][-1]
    return events, done


@pytest.fixture
def custom_tools(monkeypatch):
    """The run's custom tools, as `run_custom_tools` would resolve them."""
    from core.orchestration import resolved
    tools: list[dict] = []

    async def run_custom_tools(engine):
        return tools

    monkeypatch.setattr(resolved, "run_custom_tools", run_custom_tools)
    return tools


@pytest.fixture
def python_runner():
    """A code runner whose exits are scripted, one per call."""
    from core import execution

    class Runner:
        def __init__(self):
            self.exits: list[tuple[int, str, str]] = []
            self.calls = 0

        async def run(self, req):
            self.calls += 1
            code, out, err = self.exits.pop(0) if self.exits else (0, '{"ok": true}', "")
            return types.SimpleNamespace(exit_code=code, stdout=out, stderr=err)

    runner = Runner()
    execution.register_runner(runner)
    yield runner
    execution.register_runner(None)


@pytest.fixture
def http(monkeypatch):
    """`http_tool.send`, answering with scripted statuses; records each send."""
    from core import http_tool

    class Http:
        def __init__(self):
            self.statuses: list[int] = []
            self.sent = 0

        async def send(self, client, tool, args, global_variables, timeout=None):
            self.sent += 1
            status = self.statuses.pop(0) if self.statuses else 200
            return httpx.Response(status, json={"status": status},
                                  request=httpx.Request("POST", "http://api.test/x"))

    fake = Http()
    monkeypatch.setattr(http_tool, "send", fake.send)
    return fake


def _error(events) -> str:
    return " ".join(str(e.get("response") or e.get("error") or "") for e in events
                    if e.get("type") in ("final", "step_error") and e.get("orch_step_id") == "call")


class TestAPythonToolThatExitsNonZero:
    async def test_the_step_retries_then_fails_the_run(self, fake_llm, custom_tools, python_runner):
        custom_tools.append({"name": "py", "tool_type": "python", "code": "1/0"})
        python_runner.exits = [(1, "", "ZeroDivisionError: division by zero")] * 3
        fake_llm.script([tool_call("py")] * 3)

        events, done = await _run(S.make_orchestration(entry_step_id="call", steps=[_tool_step("py"), _AFTER]))

        assert done["status"] == "failed"
        assert python_runner.calls == 3, "an exit that new arguments might fix uses the step's attempts"
        assert "ZeroDivisionError" in _error(events)
        assert "after" not in done["final_state"]
        assert "result" not in done["final_state"], "the error is not the step's output"

    async def test_a_later_attempt_that_succeeds_is_the_result(self, fake_llm, custom_tools, python_runner):
        custom_tools.append({"name": "py", "tool_type": "python", "code": "x"})
        python_runner.exits = [(1, "", "KeyError: 'city'"), (0, '{"temp": 21}', "")]
        fake_llm.script([tool_call("py"), tool_call("py", city="Pune")])

        _, done = await _run(S.make_orchestration(entry_step_id="call", steps=[_tool_step("py"), _AFTER]))

        assert done["status"] == "completed"
        assert done["final_state"]["result"] == '{"temp": 21}'
        assert done["final_state"]["after"] == "ran"


class TestAnMcpResultMarkedAsAnError:
    async def test_the_step_retries_then_fails_the_run(self, fake_llm, monkeypatch):
        import core.tools as tools_mod
        calls = []

        class Session:
            async def call_tool(self, name, args, read_timeout_seconds=None):
                calls.append(args)
                return types.SimpleNamespace(
                    content=[types.SimpleNamespace(text="permission denied")], isError=True)

        async def resolve_one_tool(server_module, name, agent, custom):
            return types.SimpleNamespace(name=name, description="d", inputSchema={"type": "object"})

        monkeypatch.setattr(tools_mod, "resolve_one_tool", resolve_one_tool)
        server = _server()
        server.agent_sessions = {"srv": Session()}
        server.tool_router = {"t": ("srv", "t")}
        fake_llm.script([tool_call("t")] * 3)

        events, done = await _run(
            S.make_orchestration(entry_step_id="call", steps=[_tool_step("t"), _AFTER]), server)

        assert done["status"] == "failed"
        assert len(calls) == 3
        assert "permission denied" in _error(events)
        assert "after" not in done["final_state"]


class TestAnHttpToolAnsweringWithAnError:
    async def test_a_4xx_is_retried_then_fails_the_run(self, fake_llm, custom_tools, http):
        custom_tools.append({"name": "h", "tool_type": "http", "url": "http://api.test/x", "method": "POST"})
        http.statuses = [404, 404, 404]
        fake_llm.script([tool_call("h")] * 3)

        events, done = await _run(S.make_orchestration(entry_step_id="call", steps=[_tool_step("h"), _AFTER]))

        assert done["status"] == "failed"
        assert http.sent == 3
        assert "404" in _error(events)
        assert "after" not in done["final_state"]

    async def test_a_5xx_fails_at_once_and_is_not_sent_again(self, fake_llm, custom_tools, http):
        custom_tools.append({"name": "h", "tool_type": "http", "url": "http://api.test/x", "method": "POST"})
        http.statuses = [502, 200, 200]
        fake_llm.script([tool_call("h")] * 3)

        events, done = await _run(S.make_orchestration(entry_step_id="call", steps=[_tool_step("h"), _AFTER]))

        assert done["status"] == "failed"
        assert http.sent == 1, "a server fault is not re-sent: the request may already have had its effect"
        assert "502" in _error(events)

    async def test_a_2xx_is_the_result(self, fake_llm, custom_tools, http):
        custom_tools.append({"name": "h", "tool_type": "http", "url": "http://api.test/x", "method": "POST"})
        http.statuses = [201]
        fake_llm.script([tool_call("h")])

        _, done = await _run(S.make_orchestration(entry_step_id="call", steps=[_tool_step("h"), _AFTER]))

        assert done["status"] == "completed"
        assert done["final_state"]["result"] == '{"status": 201}'


class TestAStepThatCannotRun:
    async def test_a_tool_step_with_no_tool_fails_the_run(self, fake_llm):
        events, done = await _run(S.make_orchestration(entry_step_id="call", steps=[
            {**_tool_step(None)}, _AFTER]))

        assert done["status"] == "failed"
        assert "no tool" in _error(events).lower()
        assert "after" not in done["final_state"]

    async def test_a_loop_nested_past_the_limit_fails_the_run(self):
        steps = [{"id": f"l{i}", "name": f"L{i}", "type": "loop", "loop_count": 1,
                  "loop_step_ids": [f"l{i + 1}"], "next_step_id": None} for i in range(4)]
        steps[0]["next_step_id"] = "after"
        steps += [{"id": "l4", "name": "Body", "type": "print", "print_content": "deep",
                   "output_key": "deep", "next_step_id": None}, _AFTER]

        events, done = await _run(S.make_orchestration(entry_step_id="l0", steps=steps))

        assert done["status"] == "failed"
        assert any("nesting exceeded" in (e.get("error") or "") for e in events)
        assert "after" not in done["final_state"]

    @pytest.mark.parametrize("container", ["loop", "parallel"])
    async def test_a_body_step_with_no_executor_fails_the_run(self, container):
        from core.models_orchestration import StepType
        if container == "loop":
            head = {"id": "c", "name": "C", "type": "loop", "loop_count": 1,
                    "loop_step_ids": ["body"], "next_step_id": "after"}
        else:
            head = {"id": "c", "name": "C", "type": "parallel", "parallel_branches": [["body"]],
                    "next_step_id": "after"}
        body = {"id": "body", "name": "Body", "type": "tool", "forced_tool": "x",
                "output_key": "b", "next_step_id": None}
        engine = _engine(S.make_orchestration(entry_step_id="c", steps=[head, body, _AFTER]))
        engine.executors = {k: v for k, v in engine.executors.items() if k != StepType.TOOL}

        events = [e async for e in engine.run("go", run_id="run_noexec")]
        done = [e for e in events if e.get("type") == "orchestration_complete"][-1]

        assert done["status"] == "failed"
        assert any("No executor" in (e.get("error") or "") for e in events)
        assert "after" not in done["final_state"]


# ── a container naming a step that is not there (F113) ──────────────────────
# The `driver` runs each case in-process and through the fleet's real job
# functions: how a failure is recorded has differed between them before (F109).
from test_human_in_containers import _run as _restored, driver, worker  # noqa: E402,F401


class TestAContainerNamingAStepThatIsNotThere:
    """A Loop body or a Parallel branch naming a step id the orchestration does
    not have was warned about and skipped, and the run went on as if the body
    had run — a loop could complete having run none of it. A missing step fails
    the run at the top level ("Step '…' not found"), and both validators call
    this an error; the runtime was the one place that disagreed (D112's rule,
    decided for this case 2026-09-21). The step that is not there cannot be
    shown on the canvas, so the failure is the container's, naming it."""

    @pytest.mark.parametrize("container", ["loop", "parallel"])
    async def test_it_fails_the_run(self, driver, container):
        if container == "loop":
            head = {"id": "c", "name": "C", "type": "loop", "loop_count": 1,
                    "loop_step_ids": ["body", "gone"], "next_step_id": "after"}
        else:
            head = {"id": "c", "name": "C", "type": "parallel",
                    "parallel_branches": [["body"], ["gone"]], "next_step_id": "after"}
        body = {"id": "body", "name": "Body", "type": "print", "print_content": "ran",
                "output_key": "b", "next_step_id": None}
        orch = S.make_orchestration(id=f"orch_f113_{container}", entry_step_id="c",
                                    steps=[head, body, _AFTER])
        await S.seed_orchestrations([orch])

        status = await driver.start(orch, f"run_f113_{container}")

        assert status == "failed"
        run = await _restored(f"run_f113_{container}")
        assert run.status == "failed"
        failed = [h for h in run.step_history if h.get("status") == "failed"]
        assert [h["step_id"] for h in failed] == ["c"]
        assert "'gone'" in failed[0]["error"]
        assert "after" not in run.shared_state
