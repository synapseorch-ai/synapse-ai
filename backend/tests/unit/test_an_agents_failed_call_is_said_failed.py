"""An agent's tool call that failed is said as failed, never handed on as what the tool returned (F1751).

A workflow's Tool step fails on a tool that reports failure (F90): an HTTP tool's 4xx or 5xx, an MCP result marked
`isError`, one of the product's own servers' failure shape, a Python tool's non-zero exit. An agent's own call read
each of them as `Tool 'X' returned: <body>`, with no status, so an agent told the person "the email was successfully
delivered" over a 405's HTML page. Now each is `Tool 'X' FAILED: <why>`, the why in plain words (an HTML page reads as
its words, never its markup), and the call's record and its event carry `failed` and the reason. A failure that may
still have had its effect (a 5xx, or a Python tool's exit, on a tool not safe to repeat) says it may or may not have
gone through; any other says it did not do what was asked. Either way the model is told never to say it was sent or
done. A Tool step's HTTP failure names the same words, never the page's markup.

Near misses read as before: an HTTP 200 or 201, a custom tool's answer whose JSON has an "error" key (its data), a
third-party server's answer not marked `isError` that mentions an error, a Python tool's exit 0, a refused call.
A 5xx on a read-only GET is a certain failure: a read had no effect to doubt.
"""
from __future__ import annotations

import json
import types

import httpx
import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from tests.unit.test_tool_step_errors import custom_tools  # noqa: F401 — the run's custom tools

ERROR_PAGE = ('<!doctype html><html lang="en"><head><title>Example Domain</title><style>body{background:#eee}'
              '</style></head><body><div><h1>Example Domain</h1><p>Method not allowed.</p></div></body></html>')
CERTAIN = "It did not do what was asked: tell the person it failed, and never say it was sent or done."
MAYBE = ("It may or may not have gone through: tell the person you couldn't confirm it, and never say it was sent "
         "or done.")


@pytest.fixture
def answered(monkeypatch):
    """Every request an HTTP tool sends, answered with `answered.status` and `answered.body`."""
    import core.http_tool as http_tool

    record = types.SimpleNamespace(calls=[], status=200, body='{"ok": true}', ctype="application/json")

    async def send(client, tool, args, variables, **kw):
        record.calls.append((tool["name"], args))
        return httpx.Response(record.status, text=record.body, headers={"content-type": record.ctype},
                              request=httpx.Request(tool.get("method") or "POST", tool["url"]))

    monkeypatch.setattr(http_tool, "send", send)
    return record


async def _tools(*tools):
    from core.routes.tools import _upsert_custom_tools

    await _upsert_custom_tools([dict(t) for t in tools])


NOTE = {"name": "send_note", "description": "Sends a note to the warehouse.", "tool_type": "http", "method": "POST",
        "url": "https://relay.harborpine.example/send",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}}
STOCK = {"name": "read_stock", "description": "Reads the stock count.", "tool_type": "http", "method": "GET",
         "url": "https://stock.harborpine.example/count",
         "inputSchema": {"type": "object", "properties": {"item": {"type": "string"}}}}


def _python(name: str, *, safe: bool = True) -> dict:
    return {"name": name, "description": "Counts the day's orders.", "tool_type": "python", "code": "print(1)",
            "inputSchema": {"type": "object", "properties": {"day": {"type": "string"}}},
            **({} if safe else {"annotations": {"readOnlyHint": False, "idempotentHint": False,
                                                "destructiveHint": False, "openWorldHint": True}})}


async def _turn(fake_llm, script, *tools, server=None, agent=None):
    from core.react_engine import run_agent_step

    fake_llm.script(script)
    agent = agent or S.make_agent(name="Order desk", tools=list(tools), skip_default_tools=True)
    server = server or types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})
    return [ev async for ev in run_agent_step(message="Tell the warehouse Tuesday's order ships late.",
                                              agent_id=agent["id"], session_id="s_f1751", server_module=server,
                                              agent_override=agent, max_turns=4)]


def _told(fake_llm) -> str:
    return fake_llm.calls[-1].get("prompt_msg") or ""


def _results(events):
    return [e for e in events if e.get("type") == "tool_result"]


class TestAnHttpFailure:
    async def test_a_405_is_said_failed_in_words_not_markup(self, fake_llm, answered):
        await _tools(NOTE)
        answered.status, answered.body, answered.ctype = 405, ERROR_PAGE, "text/html"
        events = await _turn(fake_llm, [tool_call("send_note", text="Ships Wednesday."), "It failed."], "send_note")
        told = _told(fake_llm)
        assert "Tool 'send_note' FAILED: HTTP 405: Example Domain" in told and CERTAIN in told, told
        assert "Tool 'send_note' returned" not in told and "<title>" not in told and "background:#eee" not in told
        [result] = _results(events)
        assert result.get("failed") is True and result["reason"].startswith("HTTP 405: Example Domain")

    async def test_a_502_on_a_send_says_it_may_have_gone_through(self, fake_llm, answered):
        await _tools(NOTE)
        answered.status, answered.body, answered.ctype = 502, "Bad gateway", "text/plain"
        await _turn(fake_llm, [tool_call("send_note", text="Ships Wednesday."), "Not confirmed."], "send_note")
        told = _told(fake_llm)
        assert "Tool 'send_note' FAILED: HTTP 502: Bad gateway" in told and MAYBE in told and CERTAIN not in told

    async def test_the_saved_turn_marks_the_call_failed(self, fake_llm, answered):
        from core.store import sessions as store

        await _tools(NOTE)
        answered.status, answered.body, answered.ctype = 405, ERROR_PAGE, "text/html"
        agent = S.make_agent(name="Order desk", tools=["send_note"], skip_default_tools=True)
        await _turn(fake_llm, [tool_call("send_note", text="Ships Wednesday."), "It failed."], agent=agent)
        saved = await store.get("s_f1751", agent["id"])
        [call] = [c for m in saved["messages"] for c in (m.get("calls") or [])]
        assert call.get("failed") is True and call["reason"].startswith("HTTP 405"), call
        assert call["result"].startswith("Tool 'send_note' FAILED: HTTP 405")

    def test_a_stopped_turns_calls_keep_failed_from_the_events(self):
        from core.session import calls_from_events

        calls = calls_from_events([
            {"type": "tool_execution", "tool_name": "send_note", "args": {"text": "x"}},
            {"type": "tool_result", "tool_name": "send_note", "preview": "Tool 'send_note' FAILED: HTTP 405: no",
             "failed": True, "reason": "HTTP 405: no"}])
        assert calls[0].get("failed") is True and calls[0]["reason"] == "HTTP 405: no"


class _Session:
    def __init__(self, text: str, *, is_error: bool):
        self.text, self.is_error = text, is_error

    async def send_ping(self):
        return None

    async def call_tool(self, name, args, **_kw):
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=self.text)], isError=self.is_error)


def _mcp(monkeypatch, server_name: str, tool: str, text: str, *, is_error: bool):
    import core.react_engine as re_mod
    from core.tools import VirtualTool

    held = VirtualTool(tool, "Updates an order.", {"type": "object", "properties": {"order": {"type": "string"}}}, None)

    async def aggregated(server_module, agent, custom_tools, **_):
        ollama = [{"type": "function", "function": {"name": held.name, "description": held.description,
                                                    "parameters": held.inputSchema}}]
        return [held], {held.name: held.inputSchema}, ollama, ""

    monkeypatch.setattr(re_mod, "aggregate_all_tools", aggregated)
    return types.SimpleNamespace(agent_sessions={server_name: _Session(text, is_error=is_error)}, memory_store=None,
                                 tool_router={tool: (server_name, tool)})


class TestAnMcpFailure:
    async def test_is_error_is_said_failed_with_its_text(self, fake_llm, monkeypatch):
        server = _mcp(monkeypatch, "orders_app", "update_order", "Order HP-2041 is locked.", is_error=True)
        events = await _turn(fake_llm, [tool_call("update_order", order="HP-2041"), "Locked."], "update_order",
                             server=server)
        told = _told(fake_llm)
        assert "Tool 'update_order' FAILED: Order HP-2041 is locked." in told and CERTAIN in told, told
        assert _results(events)[0].get("failed") is True

    async def test_a_built_in_servers_failure_shape_is_said_failed(self, fake_llm, monkeypatch):
        from core.tools_registry import ALL_NATIVE_TOOLS

        native = next(iter(ALL_NATIVE_TOOLS))
        server = _mcp(monkeypatch, native, "update_order", json.dumps({"error": "the store is read-only"}),
                      is_error=False)
        await _turn(fake_llm, [tool_call("update_order", order="HP-2041"), "Read-only."], "update_order",
                    server=server)
        assert "Tool 'update_order' FAILED: the store is read-only" in _told(fake_llm)


class TestAPythonFailure:
    @pytest.fixture
    def runner(self, monkeypatch):
        from core import execution

        out = types.SimpleNamespace(exit_code=2, stdout="", stderr="KeyError: 'day'", timed_out=False)

        class Runner:
            async def run(self, req):
                return out

        monkeypatch.setattr(execution, "get_runner", lambda: Runner())
        return out

    async def test_an_exit_2_is_said_failed_with_what_it_printed(self, fake_llm, runner):
        await _tools(_python("count_orders"))
        events = await _turn(fake_llm, [tool_call("count_orders", day="Tuesday"), "It failed."], "count_orders")
        told = _told(fake_llm)
        assert "Tool 'count_orders' FAILED: exited with code 2: KeyError: 'day'" in told and CERTAIN in told, told
        assert _results(events)[0].get("failed") is True

    async def test_an_exit_on_a_tool_not_safe_to_repeat_may_have_gone_through(self, fake_llm, runner):
        await _tools(_python("post_orders", safe=False))
        await _turn(fake_llm, [tool_call("post_orders", day="Tuesday"), "Not confirmed."], "post_orders")
        assert MAYBE in _told(fake_llm)


class TestNearMisses:
    @pytest.mark.parametrize("status", [200, 201])
    async def test_a_2xx_reads_as_before(self, fake_llm, answered, status):
        await _tools(NOTE)
        answered.status = status
        events = await _turn(fake_llm, [tool_call("send_note", text="Ships Wednesday."), "Sent."], "send_note")
        told = _told(fake_llm)
        assert "Tool 'send_note' returned:" in told and "FAILED" not in told
        assert not _results(events)[0].get("failed")

    async def test_a_custom_tools_error_key_is_its_data(self, fake_llm, answered):
        await _tools(NOTE)
        answered.body = json.dumps({"error": None, "queued": True})
        await _turn(fake_llm, [tool_call("send_note", text="Ships Wednesday."), "Sent."], "send_note")
        assert "FAILED" not in _told(fake_llm)

    async def test_a_5xx_on_a_read_only_get_is_a_certain_failure(self, fake_llm, answered):
        await _tools(STOCK)
        answered.status, answered.body, answered.ctype = 503, "Service unavailable", "text/plain"
        await _turn(fake_llm, [tool_call("read_stock", item="beans"), "Couldn't read it."], "read_stock")
        told = _told(fake_llm)
        assert "Tool 'read_stock' FAILED: HTTP 503: Service unavailable" in told and CERTAIN in told
        assert MAYBE not in told and "it may have run" not in told

    async def test_a_third_party_answer_not_marked_is_error_reads_as_before(self, fake_llm, monkeypatch):
        server = _mcp(monkeypatch, "orders_app", "update_order", json.dumps({"error": "none", "status": "updated"}),
                      is_error=False)
        await _turn(fake_llm, [tool_call("update_order", order="HP-2041"), "Done."], "update_order", server=server)
        told = _told(fake_llm)
        assert "Tool 'update_order' returned:" in told and "FAILED" not in told

    async def test_a_python_exit_0_reads_as_before(self, fake_llm, monkeypatch):
        from core import execution

        class Runner:
            async def run(self, req):
                return types.SimpleNamespace(exit_code=0, stdout='{"count": 12}', stderr="", timed_out=False)

        monkeypatch.setattr(execution, "get_runner", lambda: Runner())
        await _tools(_python("count_orders"))
        await _turn(fake_llm, [tool_call("count_orders", day="Tuesday"), "12."], "count_orders")
        told = _told(fake_llm)
        assert "Tool 'count_orders' returned:" in told and "FAILED" not in told

    async def test_a_refused_call_stays_refused(self, fake_llm, answered):
        await _tools(NOTE)
        events = await _turn(fake_llm, [tool_call("send_note", text="x"), "Refused."], "read_stock")
        refused = [e for e in _results(events) if e.get("refused")]
        assert refused and not refused[0].get("failed")


class TestTheToolStepSaysItInWords:
    async def test_a_steps_405_names_its_words_not_markup(self, fake_llm, answered, custom_tools):
        from tests.unit.test_tool_step_errors import _AFTER, _error, _run, _tool_step

        custom_tools.append(dict(NOTE))
        answered.status, answered.body, answered.ctype = 405, ERROR_PAGE, "text/html"
        fake_llm.script([tool_call("send_note", text="Ships Wednesday.")] * 3)
        events, done = await _run(S.make_orchestration(entry_step_id="call",
                                                       steps=[_tool_step("send_note"), _AFTER]))
        said = _error(events)
        assert done["status"] == "failed"
        assert "HTTP 405 from 'send_note': Example Domain Method not allowed." in said and "<title>" not in said, said
