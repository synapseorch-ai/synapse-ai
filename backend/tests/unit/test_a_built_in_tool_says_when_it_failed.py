"""
A built-in tool that failed says so, and a Tool step on it fails (F1208, CAP3).

The product's own tool servers (`tools/sandbox.py`, `core/vault_tools.py`, `tools/web_scraper.py`) told a failure in
their content only — `{"error": …}`, or `{"exit_code": 1, …}` for a script that failed — and never with MCP's
`isError`. A Tool step fails only on `isError` (F90), so CAP3's stage 0 (10-29) saw an `execute_python` step whose
script exited 1 with a traceback reported "completed", and its run with it.

Now a built-in server sets `isError` on its own failures, at the source, so F90's path does the work everywhere; and a
Tool step also reads the built-ins' known failure shape, for a server that predates this. A third-party MCP server's
JSON may carry an "error" key as data, so the shape is never read from one. An agent is still handed the failure as
its answer (F90's rule): it reads the text, not the flag.
"""
import json
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call

from tests.unit.test_tool_step_errors import _AFTER, _error, _run, _server, _tool_step


# ── at the source: the built-in servers ──────────────────────────────────────────────────────────────────────────
class TestTheSourceSaysItFailed:
    def test_a_failure_is_still_the_content_its_callers_index(self):
        from core.tool_result import failed, is_failed
        out = failed('{"error": "nope"}')
        assert is_failed(out)
        assert out[0].text == '{"error": "nope"}', "callers that read result[0].text keep working"

    def test_a_failure_reaches_mcp_as_is_error(self):
        from mcp import types as mcp_types
        from core.tool_result import as_result, failed
        res = as_result(failed('{"error": "nope"}'))
        assert isinstance(res, mcp_types.CallToolResult) and res.isError
        assert res.content[0].text == '{"error": "nope"}'

    def test_a_success_passes_through_untouched(self):
        from mcp.types import TextContent
        from core.tool_result import as_result
        ok = [TextContent(type="text", text='{"ok": true}')]
        assert as_result(ok) is ok

    @pytest.mark.parametrize("module", ["tools.sandbox", "core.vault_tools", "tools.web_scraper"])
    def test_each_servers_err_is_a_failure(self, module):
        import importlib
        from core.tool_result import is_failed
        mod = importlib.import_module(module)
        assert is_failed(mod._err("nope"))
        assert not is_failed(mod._ok({"fine": True}))

    async def test_a_script_that_exits_non_zero_is_a_failure(self, monkeypatch):
        from core import execution
        from core.tool_result import is_failed
        from tools import sandbox

        class Runner:
            async def run(self, req):
                return types.SimpleNamespace(exit_code=1, stdout="", stderr="Traceback …", timed_out=False)

        monkeypatch.setattr(execution, "get_runner", lambda: Runner())
        monkeypatch.setattr(sandbox, "_vault_root", lambda: "/tmp")
        out = await sandbox._handle_execute({"code": "1/0"})
        assert is_failed(out)
        assert json.loads(out[0].text)["exit_code"] == 1, "the person and the agent still read what happened"

    async def test_a_script_that_exits_zero_is_not(self, monkeypatch):
        from core import execution
        from core.tool_result import is_failed
        from tools import sandbox

        class Runner:
            async def run(self, req):
                return types.SimpleNamespace(exit_code=0, stdout="42", stderr="", timed_out=False)

        monkeypatch.setattr(execution, "get_runner", lambda: Runner())
        monkeypatch.setattr(sandbox, "_vault_root", lambda: "/tmp")
        assert not is_failed(await sandbox._handle_execute({"code": "print(42)"}))

    async def test_the_sandbox_server_answers_is_error(self, monkeypatch):
        import core.tool_server as tool_server
        from tools import sandbox

        async def ready():
            return None

        monkeypatch.setattr(tool_server, "ready", ready)
        res = await sandbox.answer_tool("no_such_tool", {})
        assert getattr(res, "isError", False) is True

    async def test_the_vault_server_answers_is_error(self, monkeypatch):
        import core.tool_server as tool_server
        from core import vault_tools
        from tools import vault_fs

        async def ready():
            return None

        async def call(name, arguments):
            return vault_tools._err("File not found")

        monkeypatch.setattr(tool_server, "ready", ready)
        monkeypatch.setattr(vault_tools, "call", call)
        res = await vault_fs.answer_tool("vault_read", {"path": "missing.md"})
        assert getattr(res, "isError", False) is True
        assert json.loads(res.content[0].text)["error"] == "File not found"

    async def test_the_web_reader_answers_is_error(self, monkeypatch):
        from tools import web_scraper

        async def dispatch(name, arguments):
            return web_scraper._err("could not read it")

        monkeypatch.setattr(web_scraper, "_dispatch", dispatch)
        res = await web_scraper.answer_tool("scrape_url", {"url": "https://example.com"})
        assert getattr(res, "isError", False) is True


# ── the Tool step: the built-ins' shape is read, a third party's never is ────────────────────────────────────────
def _session(text: str, is_error: bool = False, calls: list | None = None):
    class Session:
        async def call_tool(self, name, args, read_timeout_seconds=None, **kw):
            if calls is not None:
                calls.append(args)
            return types.SimpleNamespace(content=[types.SimpleNamespace(text=text)], isError=is_error)
    return Session()


@pytest.fixture
def one_tool(monkeypatch):
    import core.tools as tools_mod

    async def resolve_one_tool(server_module, name, agent, custom):
        return types.SimpleNamespace(name=name, description="d", inputSchema={"type": "object"})

    monkeypatch.setattr(tools_mod, "resolve_one_tool", resolve_one_tool)

    def server_for(server_name: str, session):
        server = _server()
        server.agent_sessions = {server_name: session}
        server.tool_router = {"t": (server_name, "t")}
        return server
    return server_for


class TestAToolStepOnABuiltIn:
    async def test_a_script_that_exited_non_zero_fails_the_step_and_the_run(self, fake_llm, one_tool):
        calls: list = []
        text = json.dumps({"exit_code": 1, "stdout": "", "stderr": "Traceback (most recent call last): …"})
        fake_llm.script([tool_call("t")] * 3)
        events, done = await _run(S.make_orchestration(entry_step_id="call", steps=[_tool_step("t"), _AFTER]),
                                  one_tool("vault_sandbox", _session(text, calls=calls)))
        assert done["status"] == "failed", "CAP3's stage 0: the run said completed"
        assert len(calls) == 3, "new code might fix it: the step's attempts are used, as F90's python tool does"
        assert "exit" in _error(events).lower()
        assert "after" not in done["final_state"]

    async def test_an_error_answer_fails_the_step(self, fake_llm, one_tool):
        fake_llm.script([tool_call("t")] * 3)
        _, done = await _run(S.make_orchestration(entry_step_id="call", steps=[_tool_step("t"), _AFTER]),
                             one_tool("vault", _session(json.dumps({"error": "File not found"}))))
        assert done["status"] == "failed"

    @pytest.mark.parametrize("text", [json.dumps({"exit_code": 0, "stdout": "42", "stderr": ""}),
                                      json.dumps({"error": None, "rows": 3}), "plain words", ""])
    async def test_a_success_is_the_result(self, fake_llm, one_tool, text):
        fake_llm.script([tool_call("t")])
        _, done = await _run(S.make_orchestration(entry_step_id="call", steps=[_tool_step("t"), _AFTER]),
                             one_tool("vault_sandbox", _session(text)))
        assert done["status"] == "completed"
        assert done["final_state"]["after"] == "ran"


class TestAToolStepOnAThirdPartyServer:
    async def test_an_error_key_in_its_data_still_completes(self, fake_llm, one_tool):
        """A tracker's ticket, a payment's last error: data, not a failure. Only `isError` says a call failed."""
        text = json.dumps({"ticket": "T-301", "error": "customer reported a double charge", "exit_code": 2})
        fake_llm.script([tool_call("t")])
        _, done = await _run(S.make_orchestration(entry_step_id="call", steps=[_tool_step("t"), _AFTER]),
                             one_tool("ext_mcp_tracker", _session(text)))
        assert done["status"] == "completed"
        assert done["final_state"]["result"] == text

    async def test_its_is_error_still_fails_the_step(self, fake_llm, one_tool):
        fake_llm.script([tool_call("t")] * 3)
        _, done = await _run(S.make_orchestration(entry_step_id="call", steps=[_tool_step("t"), _AFTER]),
                             one_tool("ext_mcp_tracker", _session("denied", is_error=True)))
        assert done["status"] == "failed"
