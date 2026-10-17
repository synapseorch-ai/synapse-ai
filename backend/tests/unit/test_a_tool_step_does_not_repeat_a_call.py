"""A Tool step does not make a call again when the first may have gone through and the tool is not safe to repeat
(TAG1, D296, F737; Naveen 2026-10-17: "safe to repeat" stops risky retries).

A Tool step retried any exception with new arguments. An HTTP tool's POST that timed out waiting for its answer had
reached the server, so the step's next attempt could book the room, send the invoice or charge the card a second
time. A 5xx was already never retried for that reason (F90); a timeout was. Now:

- a call that may have reached the server (a read timeout, a dropped answer, an MCP request timeout) is made again
  only when the tool reads, or says it is safe to repeat; otherwise the step ends with "it may have run";
- a tool with no tags: an HTTP tool's method decides (PUT and DELETE are safe to repeat), and any other keeps the
  retry it had;
- a call that never left (a refused connection) and a 4xx (the server said no) are retried as before.
"""
from __future__ import annotations

import types

import httpx
import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from core.tools import VirtualTool, method_hints


@pytest.fixture
def sent(monkeypatch):
    """Every request an HTTP tool sends, answered by `sent.outcome`: an exception to raise, or a status code."""
    import core.http_tool as http_tool

    record = types.SimpleNamespace(calls=[], outcome=None)

    async def send(client, tool, args, variables, **kw):
        record.calls.append((tool["name"], args))
        if isinstance(record.outcome, BaseException):
            raise record.outcome
        return httpx.Response(record.outcome or 200, text="{}", request=httpx.Request("POST", tool["url"]))

    monkeypatch.setattr(http_tool, "send", send)
    return record


async def _tool(name, method, annotations=None):
    from core.routes.tools import _upsert_custom_tools

    tool = {"name": name, "description": "d", "tool_type": "http", "method": method,
            "url": "https://api.example.com/x", "inputSchema": {"type": "object", "properties": {}}}
    if annotations is not None:
        tool["annotations"] = annotations
    await _upsert_custom_tools([tool])


def _orch(tool):
    return S.make_orchestration(id=f"orch_{tool}", entry_step_id="call", steps=[
        {"id": "call", "name": "Call", "type": "tool", "forced_tool": tool, "prompt_template": "use it",
         "output_key": "result", "model": "claude-x", "max_turns": 3, "next_step_id": None}])


async def _run(tool, server=None):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    server = server or types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})
    orch = _orch(tool)
    await S.seed_orchestrations([orch])
    events = [e async for e in OrchestrationEngine(Orchestration.model_validate(orch), server).run("go",
                                                                                                    run_id=f"r_{tool}")]
    done = [e for e in events if e.get("type") == "orchestration_complete"]
    said = " ".join(str(e.get("response") or e.get("error") or "") for e in events)
    return done[-1]["status"], said


def _timeout():
    return httpx.ReadTimeout("timed out", request=httpx.Request("POST", "https://api.example.com/x"))


async def test_a_post_that_timed_out_is_not_sent_again(fake_llm, sent):
    await _tool("book_room", "POST")
    sent.outcome = _timeout()
    fake_llm.script([tool_call("book_room")] * 3)
    status, said = await _run("book_room")
    assert len(sent.calls) == 1, "the room may already be booked"
    assert status == "failed" and "may have run" in said


@pytest.mark.parametrize("method", ["PUT", "GET", "DELETE"])
async def test_a_call_safe_to_repeat_is_tried_again(fake_llm, sent, method):
    await _tool(f"t_{method.lower()}", method)
    sent.outcome = _timeout()
    fake_llm.script([tool_call(f"t_{method.lower()}")] * 3)
    await _run(f"t_{method.lower()}")
    assert len(sent.calls) == 3


async def test_the_persons_safe_to_repeat_is_believed(fake_llm, sent):
    await _tool("upsert_contact", "POST", {**method_hints("POST"), "idempotentHint": True})
    sent.outcome = _timeout()
    fake_llm.script([tool_call("upsert_contact")] * 3)
    await _run("upsert_contact")
    assert len(sent.calls) == 3


async def test_a_tool_saved_before_its_tags_is_judged_by_its_method(fake_llm, sent):
    from core.store.resources import save_tools

    await save_tools([{"name": "old_post", "description": "d", "tool_type": "http", "method": "POST",
                       "url": "https://api.example.com/x"}])
    sent.outcome = _timeout()
    fake_llm.script([tool_call("old_post")] * 3)
    await _run("old_post")
    assert len(sent.calls) == 1


async def test_a_call_that_never_left_is_tried_again(fake_llm, sent):
    await _tool("book_room2", "POST")
    sent.outcome = httpx.ConnectError("refused", request=httpx.Request("POST", "https://api.example.com/x"))
    fake_llm.script([tool_call("book_room2")] * 3)
    await _run("book_room2")
    assert len(sent.calls) == 3


async def test_a_refusal_is_still_tried_with_other_arguments(fake_llm, sent):
    await _tool("book_room3", "POST")
    sent.outcome = 422
    fake_llm.script([tool_call("book_room3")] * 3)
    await _run("book_room3")
    assert len(sent.calls) == 3


class _TimingOutSession:
    def __init__(self):
        self.calls: list[str] = []

    async def call_tool(self, name, args, **_kw):
        from mcp.shared.exceptions import McpError
        from mcp.types import ErrorData

        self.calls.append(name)
        raise McpError(ErrorData(code=408, message="Timed out while waiting for response"))


@pytest.mark.parametrize("annotations,calls", [
    ({"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}, 1),
    ({"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}, 3),
    ({"readOnlyHint": True}, 3),
    (None, 3),
])
async def test_a_servers_tool_that_timed_out(fake_llm, monkeypatch, annotations, calls):
    import core.tools as tools_mod

    tool = VirtualTool("send_mail", "d", {}, annotations=annotations)

    async def one(server_module, name, agent, custom_tools):
        return tool if name == "send_mail" else None

    monkeypatch.setattr(tools_mod, "resolve_one_tool", one)
    session = _TimingOutSession()
    server = types.SimpleNamespace(agent_sessions={"mail": session}, memory_store=None,
                                   tool_router={"send_mail": ("mail", "send_mail")})
    fake_llm.script([tool_call("send_mail")] * 3)
    await _run("send_mail", server)
    assert len(session.calls) == calls


async def test_a_tagged_python_tool_that_ran_is_not_run_again(fake_llm, monkeypatch):
    import core.execution as execution
    from core.routes.tools import _upsert_custom_tools

    ran = []

    class _Runner:
        async def run(self, request):
            ran.append(request.code)
            return types.SimpleNamespace(exit_code=1, stdout="", stderr="boom")

    monkeypatch.setattr(execution, "get_runner", lambda: _Runner())
    await _upsert_custom_tools([
        {"name": "charge", "description": "d", "tool_type": "python", "code": "charge()",
         "inputSchema": {"type": "object", "properties": {}},
         "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}},
        {"name": "untagged", "description": "d", "tool_type": "python", "code": "untagged()",
         "inputSchema": {"type": "object", "properties": {}}}])
    fake_llm.script([tool_call("charge")] + [tool_call("untagged")] * 3)
    status, said = await _run("charge")
    assert status == "failed" and "may have run" in said
    await _run("untagged")
    assert [("charge()" in c, "untagged()" in c) for c in ran] == [(True, False)] + [(False, True)] * 3


@pytest.mark.parametrize("method,warned", [("POST", True), ("PUT", False)])
async def test_an_agent_is_told_a_call_may_have_run(fake_llm, sent, method, warned):
    from core.react_engine import run_agent_step

    await _tool("book_it", method)
    sent.outcome = _timeout()
    fake_llm.script([tool_call("book_it"), "done"])
    agent = S.make_agent(tools=["book_it"], skip_default_tools=True)
    server = types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})
    [ev async for ev in run_agent_step(message="go", agent_id=agent["id"], session_id="s1", server_module=server,
                                       agent_override=agent, max_turns=3)]
    assert sent.calls, "the tool was called"
    told = fake_llm.calls[-1]["prompt_msg"]
    assert "Error executing custom tool book_it" in told
    assert ("may have run" in told) is warned
