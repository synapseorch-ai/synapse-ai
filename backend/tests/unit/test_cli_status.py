"""Which model CLIs are installed and signed in, and their models (CLIM1, D299, F748, F749, F751).

Settings → Models listed Claude Code's and Gemini CLI's models from a list typed
into the route (claude-sonnet-4-6, …), and Copilot's from GitHub Models'
catalog, which is another product. Each CLI is now asked what it can tell
without a model call: its version, whether it is signed in, and — where it can
say — its models; the rest come from a list kept per CLI version.
"""
import json

import pytest

import core.providers.cli_status as status


class _Proc:
    def __init__(self, stdout=b"", returncode=0, stderr=b""):
        self._out, self.returncode, self._err = stdout, returncode, stderr

    async def communicate(self):
        return self._out, self._err

    def kill(self):
        pass

    async def wait(self):
        return self.returncode


CODEX_CATALOG = {"models": [
    {"slug": "gpt-5.4", "visibility": "list", "supported_in_api": True, "priority": 2},
    {"slug": "gpt-5.5", "visibility": "list", "supported_in_api": True, "priority": 0},
    {"slug": "codex-auto-review", "visibility": "hide", "supported_in_api": True, "priority": 29},
    {"slug": "gpt-old", "visibility": "list", "supported_in_api": False, "priority": 5},
]}


@pytest.fixture
def host(monkeypatch, tmp_path):
    """A host with the CLIs named in `installed`, each answering as told."""
    state = {"installed": {"claude", "gemini", "codex", "copilot"}, "answers": {}, "runs": []}

    def which(cmd):
        return f"/usr/bin/{cmd}" if cmd in state["installed"] else None

    async def fake_exec(executable, *argv, **kwargs):
        name = executable.rsplit("/", 1)[-1]
        key = " ".join([name, *argv])
        state["runs"].append(key)
        for prefix, answer in state["answers"].items():
            if key.startswith(prefix):
                return answer() if callable(answer) else answer
        return _Proc(b"", 1)

    monkeypatch.setattr(status.shutil, "which", which)
    monkeypatch.setattr(status, "_binary_key", lambda path: (path, 1.0))
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    for name in ("GEMINI_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "GEMINI_CLI_HOME", "COPILOT_GITHUB_TOKEN",
                 "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    status.clear_cache()
    state["home"] = tmp_path
    state["answers"].update({
        "claude --version": _Proc(b"2.1.291 (Claude Code)\n"),
        "claude auth status": _Proc(json.dumps({"loggedIn": True, "authMethod": "claude.ai"}).encode()),
        "gemini --version": _Proc(b"0.38.0\n"),
        "codex --version": _Proc(b"codex-cli 0.137.0\n"),
        "codex login status": _Proc(b"Logged in using ChatGPT\n"),
        "codex debug models": _Proc(json.dumps(CODEX_CATALOG).encode()),
        "copilot --version": _Proc(b"1.0.98\n"),
        "copilot --help": _Proc(b"Usage: copilot [options]\n  --model <model>  Set the AI model\n"),
    })
    yield state
    status.clear_cache()


class TestInstalled:
    async def test_a_cli_not_on_the_path_is_not_installed(self, host):
        host["installed"] = {"claude"}
        got = await status.cli_statuses()
        assert got["anthropic_cli"].installed is True
        for provider in ("gemini_cli", "codex_cli", "github_copilot_cli"):
            assert got[provider].installed is False
            assert got[provider].models == []
            assert got[provider].signed_in is None

    async def test_its_version(self, host):
        got = await status.cli_statuses()
        assert got["anthropic_cli"].version == "2.1.291"
        assert got["codex_cli"].version == "0.137.0"


class TestSignedIn:
    async def test_claude_code_says_so_itself(self, host):
        assert (await status.cli_status("anthropic_cli")).signed_in is True
        status.clear_cache()
        host["answers"]["claude auth status"] = _Proc(json.dumps({"loggedIn": False}).encode(), 1)
        got = await status.cli_status("anthropic_cli")
        assert got.signed_in is False
        assert "claude" in got.sign_in_hint

    async def test_codex_says_so_on_stderr_too(self, host):
        # 0.137.0 prints "Logged in using ChatGPT" to stderr, not stdout.
        host["answers"]["codex login status"] = _Proc(b"", 0, stderr=b"Logged in using ChatGPT\n")
        assert (await status.cli_status("codex_cli")).signed_in is True

    async def test_codex_says_so_itself(self, host):
        host["answers"]["codex login status"] = _Proc(b"Not logged in\n", 1)
        got = await status.cli_status("codex_cli")
        assert got.signed_in is False and "codex login" in got.sign_in_hint

    async def test_gemini_cli_by_where_its_sign_in_is(self, host, monkeypatch):
        assert (await status.cli_status("gemini_cli")).signed_in is False
        status.clear_cache()
        (host["home"] / ".gemini").mkdir()
        (host["home"] / ".gemini" / "oauth_creds.json").write_text("{}", encoding="utf-8")
        assert (await status.cli_status("gemini_cli")).signed_in is True
        status.clear_cache()
        (host["home"] / ".gemini" / "oauth_creds.json").unlink()
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        assert (await status.cli_status("gemini_cli")).signed_in is True

    async def test_copilot_cli_by_its_github_token(self, host, monkeypatch):
        monkeypatch.setattr(status, "_read_github_token", _none)
        assert (await status.cli_status("github_copilot_cli")).signed_in is False
        status.clear_cache()
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "gho_x")
        assert (await status.cli_status("github_copilot_cli")).signed_in is True
        assert await status.github_token() == "gho_x"

    async def test_an_unreadable_answer_is_unknown_not_signed_out(self, host):
        host["answers"]["claude auth status"] = _Proc(b"something else", 0)
        assert (await status.cli_status("anthropic_cli")).signed_in is None


async def _none():
    return None


class TestModels:
    async def test_codex_lists_its_own_selectable_models_in_its_order(self, host):
        got = await status.cli_status("codex_cli")
        assert got.models == ["gpt-5.5", "gpt-5.4"]
        assert got.models_source == "cli"
        assert await status.codex_catalog() == CODEX_CATALOG

    async def test_codex_falls_back_to_its_cache_file(self, host):
        host["answers"]["codex debug models"] = _Proc(b"", 1)
        (host["home"] / ".codex").mkdir()
        (host["home"] / ".codex" / "models_cache.json").write_text(json.dumps(CODEX_CATALOG), encoding="utf-8")
        got = await status.cli_status("codex_cli")
        assert got.models == ["gpt-5.5", "gpt-5.4"]
        assert got.models_source == "cli"

    async def test_claude_code_aliases_first_then_this_versions_ids(self, host):
        got = await status.cli_status("anthropic_cli")
        assert got.models[:4] == ["fable", "opus", "sonnet", "haiku"]
        assert "claude-opus-5-5" in got.models and "claude-sonnet-4-6" not in got.models
        assert got.models_source == "list"
        assert "2.1" in got.models_note

    async def test_gemini_cli_aliases_first_then_this_versions_ids(self, host):
        got = await status.cli_status("gemini_cli")
        assert got.models[:4] == ["auto", "pro", "flash", "flash-lite"]
        assert "gemini-2.5-pro" in got.models and "gemini-3-flash-preview" in got.models

    async def test_each_gemini_version_gets_its_own_list(self, host):
        host["answers"]["gemini --version"] = _Proc(b"0.64.0\n")
        got = await status.cli_status("gemini_cli")
        assert "gemini-3.8-flash" in got.models and "gemini-2.5-flash" not in got.models
        assert got.models_note.endswith("0.64")

    async def test_a_newer_cli_gets_the_newest_list_and_says_whose_it_is(self, host):
        host["answers"]["claude --version"] = _Proc(b"3.0.1 (Claude Code)\n")
        got = await status.cli_status("anthropic_cli")
        assert got.models[:4] == ["fable", "opus", "sonnet", "haiku"]
        assert "2.1" in got.models_note

    async def test_copilot_cli_lists_the_models_its_config_help_names(self, host):
        # 1.0.98: `copilot help config` lists the `model` setting's choices.
        host["answers"]["copilot help config"] = _Proc(
            b"  `banner`: x\n    - \"on\"\n\n  `model`: AI model to use for Copilot CLI; can be changed with /model.\n"
            b'    - "claude-sonnet-5.5"\n    - "gpt-5.4-mini"\n  `contextTier`: tier\n    - "default"\n')
        got = await status.cli_status("github_copilot_cli")
        assert got.models == ["auto", "claude-sonnet-5.5", "gpt-5.4-mini"]
        assert got.models_source == "cli"

    async def test_copilot_cli_lists_the_models_its_help_names(self, host):
        host["answers"]["copilot --help"] = _Proc(
            b'  --model <model>  Set the AI model to use (choices: "claude-sonnet-4.5", "gpt-5", "auto")\n')
        got = await status.cli_status("github_copilot_cli")
        assert got.models == ["claude-sonnet-4.5", "gpt-5", "auto"]
        assert got.models_source == "cli"

    async def test_copilot_cli_never_asks_github_models(self, host, monkeypatch):
        # F751: api.github.com/catalog/models is GitHub Models, not Copilot.
        import httpx

        def refuse(*a, **k):
            raise AssertionError("GitHub Models is not asked")
        monkeypatch.setattr(httpx.AsyncClient, "get", refuse)
        got = await status.cli_status("github_copilot_cli")
        assert "auto" in got.models


class TestCache:
    async def test_probes_are_kept_and_refresh_asks_again(self, host):
        await status.cli_statuses()
        first = len(host["runs"])
        await status.cli_statuses()
        assert len(host["runs"]) == first
        await status.cli_statuses(refresh=True)
        assert len(host["runs"]) == 2 * first

    async def test_a_new_binary_is_asked_again(self, host, monkeypatch):
        await status.cli_status("anthropic_cli")
        first = len(host["runs"])
        monkeypatch.setattr(status, "_binary_key", lambda path: (path, 2.0))
        await status.cli_status("anthropic_cli")
        assert len(host["runs"]) > first


class TestNotes:
    async def test_codex_says_its_newer_models_follow_an_agents_tools_less(self, host):
        # Measured 2026-10-17 (Naveen: ship isolated, a tool bridge follows):
        # with none of its own tools, gpt-6-luna emitted the engine's tool call
        # 0/5 and gpt-5.6-luna 5/8, against 3/3 and 8/8 with today's flags.
        got = await status.cli_status("codex_cli")
        assert "tools" in (got.note or "")

    async def test_a_global_agents_md_for_codex_is_named(self, host):
        (host["home"] / ".codex").mkdir()
        (host["home"] / ".codex" / "AGENTS.md").write_text("x", encoding="utf-8")
        got = await status.cli_status("codex_cli")
        assert "AGENTS.md" in (got.note or "")

    async def test_as_dict_has_no_catalog(self, host):
        d = (await status.cli_status("codex_cli")).as_dict()
        assert set(d) == {"provider", "base", "label", "installed", "version", "signed_in", "sign_in_hint",
                          "models", "models_source", "models_note", "note"}
