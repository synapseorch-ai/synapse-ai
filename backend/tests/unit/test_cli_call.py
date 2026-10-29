"""`call_cli_provider` runs the invocation `build_cli_invocation` decides, in its
own directory, and refuses an answer the CLI reached by running a tool of its
own (CLIM1, D299, F743–F747).

The flags switch each CLI's tools off where the CLI lets them; Codex 0.137.0
keeps `view_image` with no switch at all, and a CLI update can add a tool.
What the CLI reports doing is the guard: an answer it reached through a tool
of its own is not the model's answer to Synapse's prompt, so it is an error.
"""
import json
from pathlib import Path

import pytest

from core.llm_providers import LLMError, call_cli_provider
from core.providers.cli_invocation import HOST_NOTE


class _Proc:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, on_run=None):
        self._out, self._err, self.returncode, self._on_run = stdout, stderr, returncode, on_run

    async def communicate(self):
        if self._on_run:
            self._on_run()
        return self._out, self._err

    def kill(self):
        pass

    async def wait(self):
        return self.returncode


@pytest.fixture
def cli(monkeypatch, tmp_path):
    """Every CLI binary, faked: records what it was asked, answers as told."""
    seen: dict = {"proc": None, "calls": []}

    async def fake_exec(executable, *argv, **kwargs):
        cwd = Path(kwargs["cwd"])
        call = {"argv": [Path(executable).name, *argv], "cwd": cwd, "env": kwargs["env"],
                "cwd_empty": not any(cwd.iterdir())}
        stdin = kwargs.get("stdin")
        if hasattr(stdin, "read"):
            call["stdin"] = stdin.read().decode("utf-8")
        seen["calls"].append(call)
        proc = seen["proc"]
        return proc(call) if callable(proc) else proc

    monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setenv("SYNAPSE_CLI_WORKDIR_ROOT", str(tmp_path))

    async def no_catalog(refresh=False):
        return None

    async def token(refresh=False):
        return "gho_test"

    import core.providers.cli_status as status
    monkeypatch.setattr(status, "codex_catalog", no_catalog)
    monkeypatch.setattr(status, "github_token", token)
    seen["root"] = tmp_path
    return seen


def _claude(result="hello", **extra):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": result,
                       "num_turns": 1, "permission_denials": [], **extra}).encode()


class TestItsOwnDirectory:
    async def test_it_runs_in_an_empty_directory_removed_afterwards(self, cli):
        cli["proc"] = _Proc(_claude())
        text, _, _ = await call_cli_provider("cli.claude", "hi", sys_prompt="SYS", effort="low")
        call = cli["calls"][0]
        assert text == "hello"
        assert call["cwd"].name == "cwd" and call["cwd_empty"]
        assert call["cwd"].parent.parent == cli["root"]
        assert not call["cwd"].parent.exists()

    async def test_it_is_removed_after_a_failure_too(self, cli):
        cli["proc"] = _Proc(b"", b"boom", returncode=1)
        with pytest.raises(LLMError):
            await call_cli_provider("cli.claude", "hi", effort="low")
        assert not cli["calls"][0]["cwd"].parent.exists()

    async def test_it_runs_with_the_invocations_environment(self, cli, monkeypatch):
        monkeypatch.setenv("CLAUDECODE", "1")
        cli["proc"] = _Proc(_claude())
        await call_cli_provider("cli.claude", "hi", effort="low")
        env = cli["calls"][0]["env"]
        assert "CLAUDECODE" not in env and env["CLAUDE_CODE_DISABLE_ATTACHMENTS"] == "1"


class TestAnAnswerReachedThroughItsOwnToolIsRefused:
    async def test_claude_code_taking_more_than_one_turn(self, cli):
        cli["proc"] = _Proc(_claude(num_turns=3))
        with pytest.raises(LLMError, match="its own tool"):
            await call_cli_provider("cli.claude", "hi", effort="low")

    async def test_claude_code_refused_a_tool(self, cli):
        cli["proc"] = _Proc(_claude(permission_denials=[{"tool_name": "Read"}]))
        with pytest.raises(LLMError, match="Read"):
            await call_cli_provider("cli.claude", "hi", effort="low")

    async def test_gemini_cli_calling_a_tool(self, cli):
        out = {"response": "the file says x", "stats": {"tools": {"totalCalls": 1, "byName": {"read_file": {}}}}}
        cli["proc"] = _Proc(json.dumps(out).encode())
        with pytest.raises(LLMError, match="read_file"):
            await call_cli_provider("cli.gemini", "SYS\n\nhi", sys_prompt="SYS")

    @pytest.mark.parametrize("kind", ["command_execution", "file_change", "mcp_tool_call", "web_search",
                                      "collab_tool_call", "something_new"])
    async def test_codex_using_a_tool(self, cli, kind):
        def run(call):
            out = Path(call["argv"][call["argv"].index("-o") + 1])
            return _Proc(b"\n".join(json.dumps(e).encode() for e in [
                {"type": "item.completed", "item": {"type": kind}},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
            ]), on_run=lambda: out.write_text("done", encoding="utf-8"))
        cli["proc"] = run
        with pytest.raises(LLMError, match=kind):
            await call_cli_provider("cli.codex", "hi")

    async def test_codex_planning_or_reasoning_is_not_a_tool(self, cli):
        def run(call):
            out = Path(call["argv"][call["argv"].index("-o") + 1])
            return _Proc(b"\n".join(json.dumps(e).encode() for e in [
                {"type": "item.completed", "item": {"type": "reasoning", "text": "…"}},
                {"type": "item.completed", "item": {"type": "todo_list", "items": []}},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "the answer"}},
                {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
            ]), on_run=lambda: out.write_text("the answer", encoding="utf-8"))
        cli["proc"] = run
        text, _, _ = await call_cli_provider("cli.codex", "hi")
        assert text == "the answer"


class TestGeminiCli:
    async def test_the_answer_is_its_json_response(self, cli):
        out = {"session_id": "s", "response": "hello there", "stats": {"tools": {"totalCalls": 0}}}
        cli["proc"] = _Proc(json.dumps(out).encode())
        text, _, _ = await call_cli_provider("cli.gemini", "SYS\n\nUser: hi", sys_prompt="SYS")
        assert text == "hello there"
        call = cli["calls"][0]
        assert call["stdin"].startswith("Write the assistant")

    async def test_a_sign_in_failure_says_how_to_sign_in(self, cli):
        cli["proc"] = _Proc(b"", json.dumps({"error": {"type": "Error", "message": "Please set an Auth method",
                                                       "code": 41}}).encode(), returncode=41)
        with pytest.raises(LLMError, match="not signed in"):
            await call_cli_provider("cli.gemini", "hi")

    async def test_a_failure_names_its_message_not_its_stack(self, cli):
        # Gemini CLI 0.38.0 on a free Code Assist account, 2026-10-17.
        stderr = ("Error authenticating: IneligibleTierError: This client is no longer supported for Gemini Code "
                  "Assist for individuals.\n  ineligibleTiers: [\n    {\n      reasonCode: 'UNSUPPORTED_CLIENT',\n"
                  + "".join(f"    at frame{i} (file:///x/chunk.js:{i}:1)\n" for i in range(30)))
        cli["proc"] = _Proc(b"", stderr.encode(), returncode=1)
        with pytest.raises(LLMError, match="no longer supported") as err:
            await call_cli_provider("cli.gemini", "hi")
        assert "chunk.js" not in str(err.value)

    async def test_its_error_is_not_an_answer(self, cli):
        cli["proc"] = _Proc(b"", json.dumps({"error": {"type": "Error", "message": "model not found",
                                                       "code": 1}}).encode(), returncode=1)
        with pytest.raises(LLMError, match="model not found"):
            await call_cli_provider("cli.gemini", "hi")


class TestCodex:
    async def test_a_sign_in_that_cannot_be_refreshed_says_how_to_sign_in(self, cli):
        # Codex 0.137.0's words for a stale login (measured 2026-10-17); its
        # `login status` still said "Logged in using ChatGPT".
        msg = "Your access token could not be refreshed. Please log out and sign in again."
        cli["proc"] = _Proc(b"\n".join(json.dumps(e).encode() for e in [
            {"type": "error", "message": msg}, {"type": "turn.failed", "error": {"message": msg}}]), returncode=1)
        with pytest.raises(LLMError, match="not signed in.*codex login"):
            await call_cli_provider("cli.codex", "hi")

    async def test_a_failed_turn_is_reported_with_its_message(self, cli):
        cli["proc"] = _Proc(b"\n".join(json.dumps(e).encode() for e in [
            {"type": "error", "message": "The 'gpt-x' model is not supported"},
            {"type": "turn.failed", "error": {"message": "The 'gpt-x' model is not supported"}},
        ]), returncode=1)
        with pytest.raises(LLMError, match="gpt-x"):
            await call_cli_provider("cli.codex.gpt-x", "hi")

    async def test_a_rate_limit_from_its_events(self, cli):
        cli["proc"] = _Proc(json.dumps({"type": "turn.failed", "error": {"message": "429 Too Many Requests"}}).encode(),
                            returncode=1)
        with pytest.raises(LLMError, match="rate limit"):
            await call_cli_provider("cli.codex", "hi")

    async def test_its_model_list_comes_from_codex(self, cli, monkeypatch):
        import core.providers.cli_status as status

        async def catalog(refresh=False):
            return {"models": [{"slug": "gpt-5.4-mini", "apply_patch_tool_type": "freeform"}]}
        monkeypatch.setattr(status, "codex_catalog", catalog)
        cli["proc"] = _Proc(b"", returncode=0)
        with pytest.raises(LLMError):          # empty answer
            await call_cli_provider("cli.codex", "hi")
        argv = cli["calls"][0]["argv"]
        assert any(a.startswith("model_catalog_json=") for a in argv)


def _copilot(*events):
    return b"\n".join(json.dumps(e).encode() for e in events)


_COPILOT_ANSWER = {"type": "assistant.message", "data": {"content": "hello", "toolRequests": [],
                                                         "phase": "final_answer"}}


class TestCopilotCli:
    async def test_the_answer_is_its_last_message(self, cli):
        cli["proc"] = _Proc(_copilot({"type": "user.message", "data": {"content": "hi"}},
                                     {"type": "assistant.message", "data": {"content": "thinking", "toolRequests": []}},
                                     _COPILOT_ANSWER, {"type": "result", "data": None}))
        text, _, _ = await call_cli_provider("cli.copilot", "hi")
        assert text == "hello"
        assert cli["calls"][0]["stdin"] == f"{HOST_NOTE}\n\nhi"  # F1209's note, on purpose

    async def test_a_tool_request_is_refused(self, cli):
        cli["proc"] = _Proc(_copilot({"type": "assistant.message",
                                      "data": {"content": "", "toolRequests": [{"name": "bash"}]}}, _COPILOT_ANSWER))
        with pytest.raises(LLMError, match="bash"):
            await call_cli_provider("cli.copilot", "hi")

    async def test_a_tool_running_is_refused(self, cli):
        cli["proc"] = _Proc(_copilot({"type": "tool.execution_start", "data": {"toolName": "view"}}, _COPILOT_ANSWER))
        with pytest.raises(LLMError, match="view"):
            await call_cli_provider("cli.copilot", "hi")

    async def test_the_token_reaches_it_in_its_environment(self, cli):
        cli["proc"] = _Proc(_copilot(_COPILOT_ANSWER))
        text, _, _ = await call_cli_provider("cli.copilot", "hi")
        call = cli["calls"][0]
        assert text == "hello"
        assert call["env"]["COPILOT_GITHUB_TOKEN"] == "gho_test"
        assert call["env"]["COPILOT_HOME"] == str(call["cwd"].parent / "home")


class TestAnOlderCli:
    @pytest.mark.parametrize("stderr", [b"error: unknown option '--safe-mode'",
                                        b"Unknown argument: policy",
                                        b"error: unexpected argument '--ignore-user-config' found",
                                        # Codex older than 0.163.0 has no view_image feature to switch off.
                                        b"Error: Unknown feature flag: view_image"])
    async def test_a_flag_it_does_not_know_says_to_update_it(self, cli, stderr):
        cli["proc"] = _Proc(b"", stderr, returncode=1)
        with pytest.raises(LLMError, match="[Uu]pdate"):
            await call_cli_provider("cli.claude", "hi", effort="low")
