"""Codex gets the agent's tools as real tools, and the engine runs every call (F753).

Run with none of its own tools, newer Codex models would not call the tools written
into the prompt as text ("the order tools aren't available in this chat"):
gpt-6-luna emitted the engine's call 0 times in 5, gpt-5.6-luna 5 in 8 (CLIM1,
2026-10-17). Naveen: give the CLI the agent's tools as real tools, routed back to the
engine. The bridge offers them through an MCP server the CLI starts for the call,
records a call instead of running it, and Synapse hands it to the engine like any
model's tool call — policy, approvals, guards, memory and boards unchanged.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from core.providers.cli_invocation import BRIDGED_CLIS, build_cli_invocation, new_workdir
from tests._fakes.cli_process import fake_cli_process

SERVER = Path(__file__).resolve().parents[2] / "core" / "providers" / "cli_bridge_server.py"

ENGINE_TOOLS = [
    {"type": "function", "function": {"name": "get_order", "description": "Get an order by id.",
                                      "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}},
                                                     "required": ["order_id"]}}},
    {"type": "function", "function": {"name": "github.create_issue", "description": "Open an issue.",
                                      "parameters": {"type": "object", "properties": {"title": {"type": "string"}}}}},
]


def _configs(argv):
    out = {}
    for i, a in enumerate(argv):
        if a == "-c":
            k, _, v = argv[i + 1].partition("=")
            out[k] = v
    return out


def _rpc(*msgs):
    return "".join(json.dumps(m) + "\n" for m in msgs)


@pytest.fixture
def workdir(tmp_path):
    return new_workdir(tmp_path)


class TestTheBridgeServer:
    """The server the CLI starts: it lists the agent's tools and only records calls."""

    def _serve(self, tmp_path, *msgs):
        tools = [{"name": "get_order", "engine_name": "get_order", "description": "Get an order by id.",
                  "inputSchema": {"type": "object", "properties": {"order_id": {"type": "string"}}}},
                 {"name": "github_create_issue", "engine_name": "github.create_issue", "description": "Open an issue.",
                  "inputSchema": {"type": "object"}}]
        spec, calls = tmp_path / "tools.json", tmp_path / "calls.jsonl"
        spec.write_text(json.dumps(tools), encoding="utf-8")
        out = subprocess.run([sys.executable, str(SERVER), str(spec), str(calls)], input=_rpc(*msgs),
                             capture_output=True, text=True, timeout=30)
        replies = {r["id"]: r for r in (json.loads(l) for l in out.stdout.splitlines() if l.strip())}
        recorded = [json.loads(l) for l in calls.read_text().splitlines()] if calls.exists() else []
        return replies, recorded

    def test_it_lists_the_agents_tools(self, tmp_path):
        replies, _ = self._serve(tmp_path, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                                 {"jsonrpc": "2.0", "method": "notifications/initialized"},
                                 {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert replies[1]["result"]["serverInfo"]["name"] == "synapse"
        assert [t["name"] for t in replies[2]["result"]["tools"]] == ["get_order", "github_create_issue"]

    def test_its_tools_say_they_change_nothing(self, tmp_path):
        """A bridge call only records: it is read-only, truthfully. Codex 0.163.0 asked
        to approve an unmarked one, which exec mode cannot do ("MCP tool call requires
        approval, but approval policy is never"); the engine applies the agent's real
        policy when it runs the call."""
        replies, _ = self._serve(tmp_path, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        for tool in replies[2]["result"]["tools"]:
            assert tool["annotations"] == {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}

    def test_a_call_is_recorded_under_the_engines_name_and_run_by_nobody(self, tmp_path):
        replies, recorded = self._serve(
            tmp_path, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "github_create_issue", "arguments": {"title": "Broken"}}})
        assert recorded == [{"tool": "github.create_issue", "arguments": {"title": "Broken"}}]
        text = replies[1]["result"]["content"][0]["text"]
        assert "Synapse runs this call" in text and replies[1]["result"]["isError"] is False

    def test_an_unknown_tool_is_refused_and_not_recorded(self, tmp_path):
        replies, recorded = self._serve(
            tmp_path, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "rm_rf", "arguments": {}}})
        assert recorded == [] and replies[1]["result"]["isError"] is True


class TestCodexIsGivenTheBridge:
    def test_only_codex_is_bridged(self):
        assert BRIDGED_CLIS == ("cli.codex",)

    def test_codex_with_tools_starts_the_bridge(self, workdir):
        inv = build_cli_invocation("cli.codex", "gpt-5.6-luna", workdir=workdir, system_prompt="S",
                                   prompt="S\n\nUser: hi", environ={"PATH": "/usr/bin"}, tools=ENGINE_TOOLS)
        server = _configs(inv.argv)["mcp_servers.synapse"]
        assert json.dumps(sys.executable) in server and json.dumps(str(SERVER)) in server
        spec = json.loads((workdir / "bridge-tools.json").read_text(encoding="utf-8"))
        assert [(t["name"], t["engine_name"]) for t in spec] == [("get_order", "get_order"),
                                                                ("github_create_issue", "github.create_issue")]
        assert spec[0]["inputSchema"]["required"] == ["order_id"]
        assert inv.bridge_calls == workdir / "bridge-calls.jsonl"

    def test_codex_without_tools_has_no_bridge(self, workdir):
        inv = build_cli_invocation("cli.codex", None, workdir=workdir, system_prompt="S", prompt="S",
                                   environ={"PATH": "/usr/bin"})
        assert "mcp_servers.synapse" not in _configs(inv.argv) and inv.bridge_calls is None

    @pytest.mark.parametrize("base", ["cli.claude", "cli.gemini", "cli.copilot"])
    def test_the_others_are_not_bridged(self, workdir, base):
        inv = build_cli_invocation(base, None, workdir=workdir, system_prompt="S", prompt="S",
                                   environ={"PATH": "/usr/bin"}, tools=ENGINE_TOOLS, github_token="t")
        assert inv.bridge_calls is None
        assert not any("synapse" in a and "mcp" in a for a in inv.argv)


class _Proc:
    def __init__(self, stdout=b"", returncode=0, on_run=None):
        self._out, self.returncode, self._on_run = stdout, returncode, on_run

    async def communicate(self):
        if self._on_run:
            self._on_run()
        return self._out, b""

    def kill(self):
        pass

    async def wait(self):
        return self.returncode


@pytest.fixture
def codex(monkeypatch, tmp_path):
    seen = {"calls": []}

    async def fake_exec(executable, *argv, **kwargs):
        call = {"argv": [Path(executable).name, *argv]}
        seen["calls"].append(call)
        return seen["proc"](call)

    monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
    fake_cli_process(monkeypatch, fake_exec)  # the CLI runs off the loop (F1320)
    monkeypatch.setenv("SYNAPSE_CLI_WORKDIR_ROOT", str(tmp_path))

    async def no_catalog(refresh=False):
        return None
    import core.providers.cli_status as status
    monkeypatch.setattr(status, "codex_catalog", no_catalog)
    return seen


def _events(*items):
    return b"\n".join(json.dumps({"type": "item.completed", "item": i}).encode() for i in items)


def _codex_run(items, recorded=None, answer=""):
    def run(call):
        argv = call["argv"]
        out = Path(argv[argv.index("-o") + 1])
        workdir = out.parent

        def on_run():
            out.write_text(answer, encoding="utf-8")
            if recorded:
                (workdir / "bridge-calls.jsonl").write_text(
                    "".join(json.dumps(c) + "\n" for c in recorded), encoding="utf-8")
        return _Proc(_events(*items), on_run=on_run)
    return run


class TestABridgedCallIsTheEnginesToRun:
    async def test_a_recorded_call_comes_back_as_the_engines_call(self, codex):
        from core.llm_providers import call_cli_provider
        from core.react_engine import parse_all_tool_calls

        codex["proc"] = _codex_run(
            [{"type": "mcp_tool_call", "server": "synapse", "tool": "get_order", "arguments": {"order_id": "A-1"}},
             {"type": "agent_message", "text": "Waiting for the result."}],
            recorded=[{"tool": "get_order", "arguments": {"order_id": "A-1"}}], answer="Waiting for the result.")
        text, _, _ = await call_cli_provider("cli.codex.gpt-5.6-luna", "hi", sys_prompt="S", tools=ENGINE_TOOLS)
        assert parse_all_tool_calls(text) == [{"tool": "get_order", "arguments": {"order_id": "A-1"}}]
        assert "Waiting for the result" not in text

    async def test_two_calls_are_two_calls(self, codex):
        from core.llm_providers import call_cli_provider
        from core.react_engine import parse_all_tool_calls

        calls = [{"tool": "get_order", "arguments": {"order_id": "A-1"}},
                 {"tool": "github.create_issue", "arguments": {"title": "x"}}]
        codex["proc"] = _codex_run(
            [{"type": "mcp_tool_call", "server": "synapse", "tool": "get_order", "arguments": {}},
             {"type": "mcp_tool_call", "server": "synapse", "tool": "github_create_issue", "arguments": {}}],
            recorded=calls)
        text, _, _ = await call_cli_provider("cli.codex.gpt-5.6-luna", "hi", sys_prompt="S", tools=ENGINE_TOOLS)
        assert parse_all_tool_calls(text) == calls

    async def test_the_same_call_made_again_in_one_turn_is_one_call(self, codex):
        """Codex 0.163.0 in a loop step called get_order for one order three times in one
        turn, though each call was answered "stop now"; the engine ran three identical
        calls and its repeat guard failed the step. A call repeated as written is one."""
        from core.llm_providers import call_cli_provider
        from core.react_engine import parse_all_tool_calls

        same = {"tool": "get_order", "arguments": {"order_id": "A-2001"}}
        other = {"tool": "get_order", "arguments": {"order_id": "A-2003"}}
        codex["proc"] = _codex_run(
            [{"type": "mcp_tool_call", "server": "synapse", "tool": "get_order", "arguments": {}}] * 4,
            recorded=[same, same, other, same])
        text, _, _ = await call_cli_provider("cli.codex.gpt-5.6-luna", "hi", sys_prompt="S", tools=ENGINE_TOOLS)
        assert parse_all_tool_calls(text) == [same, other]

    async def test_another_mcp_server_is_still_its_own_tool(self, codex):
        from core.llm_providers import LLMError, call_cli_provider

        codex["proc"] = _codex_run(
            [{"type": "mcp_tool_call", "server": "codex", "tool": "list_mcp_resources", "arguments": {}}],
            answer="done")
        with pytest.raises(LLMError, match="its own tool"):
            await call_cli_provider("cli.codex.gpt-5.6-luna", "hi", sys_prompt="S", tools=ENGINE_TOOLS)

    async def test_an_answer_without_a_call_is_the_answer(self, codex):
        from core.llm_providers import call_cli_provider

        codex["proc"] = _codex_run([{"type": "agent_message", "text": "It shipped."}], answer="It shipped.")
        text, _, _ = await call_cli_provider("cli.codex.gpt-5.6-luna", "hi", sys_prompt="S", tools=ENGINE_TOOLS)
        assert text == "It shipped."


class TestTheDispatchHandsTheToolsOver:
    async def test_a_cli_models_turn_passes_its_tools(self, monkeypatch):
        import core.llm_providers as llm
        seen = {}

        async def fake_cli(cli_model, full_prompt, **kwargs):
            seen.update(kwargs)
            return "ok", 1, 1

        monkeypatch.setattr(llm, "call_cli_provider", fake_cli)
        call = type("Call", (), {"model": "cli.codex.gpt-5.6-luna", "system": "S", "tools": ENGINE_TOOLS,
                                 "reasoning_effort": None, "messages": lambda self: [{"role": "user", "content": "hi"}]})()
        await llm._chat_cli(call, None)
        assert seen["tools"] == ENGINE_TOOLS
