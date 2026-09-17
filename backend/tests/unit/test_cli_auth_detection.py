"""Unit tests for CLI provider auth-failure detection (_match_auth).

Guards against the issue #345 regression where a bare "auth" substring match
flagged normal model output ("authority", "authorization", ...) as an
authentication failure and aborted otherwise successful CLI runs.

The pure `_match_auth` helper is tested directly; `call_cli_provider` is tested
below with the subprocess faked, for what it asks the CLI to do (F65).
"""
import pytest

import core.llm_providers as _llm_mod
from core.llm_providers import _match_auth

# Captured at collection, before the autouse `fake_llm` fixture replaces it.
_REAL_GENERATE = _llm_mod.generate_response


class TestAuthDetection:
    # Real auth-failure strings emitted by CLIs — these MUST still be detected.
    @pytest.mark.parametrize("text", [
        "Invalid API key · Please run /login",
        "Not logged in",
        "Please sign in to continue",
        "401 Unauthorized",
        "Your session has expired. Please log in.",
        "Authentication required",
        "Error: authentication failed",
        "You are not authenticated. Run the CLI to log in.",
    ])
    def test_real_auth_failures_match(self, text):
        assert _match_auth(text) is True

    # Normal model output — these MUST NOT be flagged (the issue #345 regression).
    @pytest.mark.parametrize("text", [
        "The authority granted authorization to the authorized author.",
        "Use API key rotation for better security.",
        "Successfully authenticated the request earlier; here is your answer.",
        "The author writes about authorization models and authoritative sources.",
        (
            "Here is my analysis of the workflow:\n"
            "1. The transform step builds a work packet.\n"
            "2. Each reviewer has authority over its own branch.\n"
            "3. We then merge the authorized results and synthesize a summary."
        ),
    ])
    def test_normal_output_does_not_match(self, text):
        assert _match_auth(text) is False

    @pytest.mark.parametrize("text", ["", None])
    def test_empty_or_none_does_not_match(self, text):
        assert _match_auth(text) is False


# ── What `cli.claude` is asked to do (F65) ───────────────────────────────────

class _Proc:
    def __init__(self, stdout: bytes, returncode: int = 0):
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, b""


@pytest.fixture
def claude(monkeypatch):
    """The `claude` binary, faked: records its argv, answers with JSON."""
    seen: dict = {"answer": b'{"type":"result","subtype":"success","is_error":false,"result":"hello","session_id":"s-1"}'}

    async def fake_exec(executable, *argv, **kwargs):
        seen["argv"] = list(argv)
        return _Proc(seen["answer"])

    monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    return seen


def _flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


class TestClaudeCli:
    """`cli.claude.<model>` with no suffix sent `--effort low` and `-max` sent
    `medium`; no `--output-format json` was sent, so the session id the resume
    code looked for never parsed — and resuming would have sent the history
    twice, because the whole transcript is in the prompt already."""

    async def test_it_asks_for_json_and_takes_the_answer_from_it(self, claude):
        from core.llm_providers import call_cli_provider

        text, _in, _out = await call_cli_provider("cli.claude.claude-opus-4-6", "hi", effort="high")

        assert text == "hello"
        assert _flag(claude["argv"], "--output-format") == "json"
        assert _flag(claude["argv"], "--model") == "claude-opus-4-6"
        assert _flag(claude["argv"], "--effort") == "high"
        assert "--resume" not in claude["argv"]

    @pytest.mark.parametrize("effort,sent", [("low", "low"), ("medium", "medium"), ("high", "high"),
                                              ("off", "low"), (None, "low")])
    async def test_the_effort_is_the_callers(self, claude, effort, sent):
        from core.llm_providers import call_cli_provider

        await call_cli_provider("cli.claude", "hi", effort=effort)

        assert _flag(claude["argv"], "--effort") == sent
        assert "--model" not in claude["argv"]

    async def test_an_old_suffix_names_the_model_and_nothing_else(self, claude):
        from core.llm_providers import call_cli_provider

        await call_cli_provider("cli.claude.claude-opus-4-6-thinking", "hi", effort="medium")

        assert _flag(claude["argv"], "--model") == "claude-opus-4-6"
        assert _flag(claude["argv"], "--effort") == "medium"

    async def test_an_error_result_is_an_error_not_an_answer(self, claude):
        from core.llm_providers import LLMError, call_cli_provider
        claude["answer"] = b'{"type":"result","subtype":"error_max_turns","is_error":true,"result":"Reached max turns"}'

        with pytest.raises(LLMError, match="max turns"):
            await call_cli_provider("cli.claude", "hi", effort="low")

    async def test_the_dispatch_passes_the_effort_and_keeps_no_cli_session(self, monkeypatch):
        import core.llm_providers as llm
        import core.session as session
        seen = {}

        async def fake_cli(cli_model, full_prompt, **kwargs):
            seen.update(kwargs, cli_model=cli_model)
            return "ok", 1, 1

        async def no_sessions(*a, **k):
            raise AssertionError("the CLI session store is not used")

        monkeypatch.setattr(llm, "call_cli_provider", fake_cli)
        monkeypatch.setattr(session, "get_cli_session_id", no_sessions)
        monkeypatch.setattr(session, "save_cli_session_id", no_sessions)

        out = await _REAL_GENERATE("hi", "sys", "cli", "cli.claude.claude-opus-4-6", {},
                                   session_id="s1", reasoning_effort="high")

        assert out == "ok"
        assert seen["effort"] == "high"
