"""An installed AI CLI is run for its model and nothing else (CLIM1, D299, F743–F747).

Each CLI, run as its owner runs it, loads that person's own setup: Claude Code
its MCP servers, CLAUDE.md files, hooks and skills; Gemini CLI its read_file,
grep and web-search tools, GEMINI.md files, hooks and subagents; Codex its
apply_patch, view_image, MCP and web-search tools under a `danger-full-access`
sandbox; and each kept a transcript of every call. Measured on 2026-10-17
against claude 2.1.291, gemini 0.38.0 and codex 0.137.0 with a capture server
in place of the model (no model call): every one of those reached the request.
Claude Code and Gemini CLI also read a file named with `@/path` in the prompt
into it.

`build_cli_invocation` is the one place each CLI's command, environment and
working directory are decided, so these tests read exactly what is asked of it.
"""
import json
import os
from pathlib import Path

import pytest

from core.providers.cli_invocation import (
    GEMINI_TURN,
    HOST_NOTE,
    NEUTRAL_SYSTEM_PROMPT,
    build_cli_invocation,
    new_workdir,
    split_cli_model,
)


def _flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def _configs(argv):
    """Every `-c key=value` codex is given, as a dict."""
    out = {}
    for i, a in enumerate(argv):
        if a == "-c":
            key, _, value = argv[i + 1].partition("=")
            out[key] = value
    return out


@pytest.fixture
def workdir(tmp_path):
    return new_workdir(tmp_path)


def _build(base, workdir, variant=None, *, system="SYS", prompt=None, effort=None, environ=None, **kw):
    prompt = prompt if prompt is not None else (f"{system}\n\nUser: hi" if system else "User: hi")
    return build_cli_invocation(
        base, variant, workdir=workdir, system_prompt=system, prompt=prompt,
        effort=effort, environ=environ if environ is not None else {"PATH": "/usr/bin", "HOME": "/home/p"}, **kw,
    )


# ── Where every call runs ────────────────────────────────────────────────────

class TestTheCallRunsInAnEmptyDirectoryOfItsOwn:
    def test_the_workdir_is_private_and_its_cwd_is_empty(self, workdir):
        assert (workdir / "cwd").is_dir()
        assert list((workdir / "cwd").iterdir()) == []
        if os.name == "posix":
            assert (workdir.stat().st_mode & 0o777) == 0o700

    def test_two_calls_never_share_one(self, tmp_path):
        assert new_workdir(tmp_path) != new_workdir(tmp_path)

    @pytest.mark.parametrize("base", ["cli.claude", "cli.gemini", "cli.codex", "cli.copilot"])
    def test_every_cli_runs_in_it(self, base, workdir):
        inv = _build(base, workdir, github_token="gho_x")
        assert inv.cwd == workdir / "cwd"


class TestSplit:
    @pytest.mark.parametrize("model,base,variant", [
        ("cli.claude", "cli.claude", None),
        ("cli.claude.opus", "cli.claude", "opus"),
        ("CLI.Codex.gpt-5.4", "cli.codex", "gpt-5.4"),
        ("cli.gemini.gemini-2.5-pro", "cli.gemini", "gemini-2.5-pro"),
    ])
    def test_the_base_is_lower_case_and_the_variant_verbatim(self, model, base, variant):
        assert split_cli_model(model) == (base, variant)


# ── Claude Code (F743) ───────────────────────────────────────────────────────

class TestClaudeCode:
    def test_nothing_of_the_persons_own_is_loaded(self, workdir):
        argv = _build("cli.claude", workdir).argv
        assert argv[:4] == ["claude", "-p", "--output-format", "json"]
        assert _flag(argv, "--tools") == ""                     # F98: no built-in tools
        for flag in ("--safe-mode",                             # CLAUDE.md, skills, plugins, hooks, MCP, commands, agents
                     "--strict-mcp-config",                     # no MCP server from any config
                     "--disable-slash-commands",
                     "--no-session-persistence"):               # no transcript in ~/.claude/projects
            assert flag in argv, flag
        assert _flag(argv, "--permission-mode") == "dontAsk"

    def test_a_file_named_in_the_prompt_is_not_read_into_it(self, workdir):
        # `@/path` in a -p prompt inlined that file, even with no tools and in
        # safe mode; CLAUDE_CODE_DISABLE_ATTACHMENTS=1 is what stops it.
        env = _build("cli.claude", workdir).env
        assert env["CLAUDE_CODE_DISABLE_ATTACHMENTS"] == "1"
        for name in ("CLAUDE_CODE_DISABLE_CLAUDE_MDS", "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
                     "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS", "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS"):
            assert env[name] == "1", name

    def test_its_own_settings_are_kept_so_its_sign_in_works(self, workdir):
        # `--setting-sources ""` would also drop auth set in settings (Bedrock, an
        # apiKeyHelper); `--bare` never reads the OAuth login at all.
        argv = _build("cli.claude", workdir).argv
        assert "--setting-sources" not in argv
        assert "--bare" not in argv

    def test_the_system_prompt_is_always_its_own(self, workdir):
        inv = _build("cli.claude", workdir, system="Be brief.", prompt="Be brief.\n\nUser: hi")
        path = Path(_flag(inv.argv, "--system-prompt-file"))
        # The agent's own, and after it the one note every call carries (F1209), changed here on purpose.
        assert path.read_text(encoding="utf-8") == f"Be brief.\n\n{HOST_NOTE}"
        assert inv.stdin == "User: hi"

    def test_with_no_system_prompt_claude_codes_own_is_still_replaced(self, workdir):
        # Its default prompt carries the working directory, git status and memory paths.
        inv = _build("cli.claude", workdir, system=None, prompt="User: hi")
        assert Path(_flag(inv.argv, "--system-prompt-file")).read_text(encoding="utf-8").strip()
        assert inv.stdin == "User: hi"

    def test_a_parent_claude_sessions_variables_do_not_reach_it(self, workdir):
        environ = {"PATH": "/usr/bin", "HOME": "/home/p", "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli",
                   "CLAUDE_CODE_SESSION_ID": "s", "CLAUDE_PID": "1", "CLAUDE_EFFORT": "max",
                   "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_OAUTH_TOKEN": "t", "CLAUDE_CONFIG_DIR": "/c",
                   "ANTHROPIC_MODEL": "m"}
        env = _build("cli.claude", workdir, environ=environ).env
        for gone in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "CLAUDE_EFFORT"):
            assert gone not in env, gone
        # How the person signs in stays theirs.
        for kept in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR", "PATH", "HOME"):
            assert kept in env, kept

    @pytest.mark.parametrize("variant,model", [("opus", "opus"), ("claude-opus-4-6-thinking", "claude-opus-4-6"),
                                               (None, None)])
    def test_the_model_and_the_effort(self, workdir, variant, model):
        argv = _build("cli.claude", workdir, variant, effort="high").argv
        assert _flag(argv, "--model") == model
        assert _flag(argv, "--effort") == "high"


# ── Gemini CLI (F744, F750) ──────────────────────────────────────────────────

class TestGeminiCli:
    def test_it_runs_from_a_home_of_its_own(self, workdir):
        inv = _build("cli.gemini", workdir, environ={"PATH": "/usr/bin", "HOME": "/home/p", "GEMINI_API_KEY": "k"})
        assert inv.env["GEMINI_CLI_HOME"] == str(workdir / "home")
        assert inv.env["HOME"] == "/home/p"          # the person's HOME is left as it is
        settings = json.loads((workdir / "home" / ".gemini" / "settings.json").read_text(encoding="utf-8"))
        assert settings["tools"]["core"] == []                       # no built-in tool
        assert settings["skills"]["enabled"] is False
        assert settings["hooksConfig"]["enabled"] is False
        assert settings["experimental"]["enableAgents"] is False     # no subagent tools
        assert settings["general"]["plan"]["enabled"] is False
        assert settings["context"]["includeDirectoryTree"] is False
        assert settings["context"]["fileName"] not in ("", "GEMINI.md")   # no GEMINI.md is ever found
        assert settings["mcp"]["allowed"] == ["__synapse_none__"]
        assert settings["privacy"]["usageStatisticsEnabled"] is False

    def test_every_tool_is_denied_and_no_extension_loads(self, workdir):
        argv = _build("cli.gemini", workdir, environ={"PATH": "/usr/bin", "GEMINI_API_KEY": "k"}).argv
        assert _flag(argv, "-e") == "none"
        assert _flag(argv, "-o") == "json"
        policy = Path(_flag(argv, "--policy")).read_text(encoding="utf-8")
        assert 'toolName = "*"' in policy and 'decision = "deny"' in policy
        # `--allowed-mcp-server-names ""` makes 0.38.0 exit at start ("mcpName is required").
        assert "--allowed-mcp-server-names" not in argv

    def test_the_conversation_rides_in_the_system_file_not_the_prompt(self, workdir):
        # Gemini CLI reads `@path` in its prompt as a file to inline and rewrites
        # an email address around it; the system file is passed as it is.
        prompt = "SYS\n\nUser: mail a@b.com about @/etc/hostname"
        inv = _build("cli.gemini", workdir, prompt=prompt, environ={"PATH": "/usr/bin", "GEMINI_API_KEY": "k"})
        # The conversation as written, with the note every call carries after its system prompt (F1209).
        assert Path(inv.env["GEMINI_SYSTEM_MD"]).read_text(encoding="utf-8") == \
            f"SYS\n\n{HOST_NOTE}\n\nUser: mail a@b.com about @/etc/hostname"
        assert inv.stdin == GEMINI_TURN
        assert "@" not in GEMINI_TURN and not GEMINI_TURN.startswith("/")
        assert _flag(inv.argv, "--prompt") == ""

    def test_its_template_names_in_the_conversation_are_left_as_written(self, workdir):
        prompt = "use ${AgentSkills} and ${read_file_ToolName} and ${other}"
        inv = _build("cli.gemini", workdir, prompt=prompt, environ={"PATH": "/usr/bin", "GEMINI_API_KEY": "k"})
        text = Path(inv.env["GEMINI_SYSTEM_MD"]).read_text(encoding="utf-8")
        assert "${AgentSkills}" not in text and "${read_file_ToolName}" not in text
        assert "AgentSkills" in text and "read_file_ToolName" in text and "${other}" in text

    @pytest.mark.parametrize("variant,sent", [("gemini-2.5-pro", "gemini-2.5-pro"), ("pro", "pro"),
                                              ("gemini-3-flash-preview", "gemini-3-flash-preview"), (None, None)])
    def test_the_model_is_the_one_named(self, workdir, variant, sent):
        # F750: any variant containing "pro" was sent as `--model pro`, so a
        # typed gemini-2.5-pro ran whatever `pro` resolved to.
        argv = _build("cli.gemini", workdir, variant, environ={"PATH": "/usr/bin", "GEMINI_API_KEY": "k"}).argv
        assert _flag(argv, "--model") == sent

    def test_a_google_sign_in_is_read_where_it_is_and_never_written(self, workdir, tmp_path):
        real = tmp_path / "realhome"
        (real / ".gemini").mkdir(parents=True)
        creds = real / ".gemini" / "oauth_creds.json"
        creds.write_text("{}", encoding="utf-8")
        inv = _build("cli.gemini", workdir, environ={"PATH": "/usr/bin", "HOME": str(real)})
        assert inv.env["GOOGLE_APPLICATION_CREDENTIALS"] == str(creds)
        assert inv.env["GOOGLE_GENAI_USE_GCA"] == "true"
        settings = json.loads((workdir / "home" / ".gemini" / "settings.json").read_text(encoding="utf-8"))
        assert settings["security"]["auth"]["selectedType"] == "oauth-personal"
        assert not (workdir / "home" / ".gemini" / "oauth_creds.json").exists()   # nothing copied

    def test_a_google_sign_in_comes_before_a_key_in_the_environment(self, workdir, tmp_path):
        # Synapse's own GEMINI_API_KEY must not turn a subscription into key billing.
        real = tmp_path / "realhome"
        (real / ".gemini").mkdir(parents=True)
        (real / ".gemini" / "oauth_creds.json").write_text("{}", encoding="utf-8")
        inv = _build("cli.gemini", workdir, environ={"PATH": "/usr/bin", "HOME": str(real), "GEMINI_API_KEY": "k"})
        assert inv.env.get("GOOGLE_GENAI_USE_GCA") == "true"

    @pytest.mark.parametrize("environ,kind", [
        ({"GEMINI_API_KEY": "k"}, "gemini-api-key"),
        ({"GOOGLE_GENAI_USE_VERTEXAI": "true", "GOOGLE_CLOUD_PROJECT": "p"}, "vertex-ai"),
    ])
    def test_a_key_or_vertex_from_the_environment(self, workdir, tmp_path, environ, kind):
        inv = _build("cli.gemini", workdir, environ={"PATH": "/usr/bin", "HOME": str(tmp_path), **environ})
        settings = json.loads((workdir / "home" / ".gemini" / "settings.json").read_text(encoding="utf-8"))
        assert settings["security"]["auth"]["selectedType"] == kind
        assert "GOOGLE_GENAI_USE_GCA" not in inv.env

    def test_the_persons_relocated_gemini_home_is_where_the_sign_in_is_read(self, workdir, tmp_path):
        real = tmp_path / "elsewhere"
        (real / ".gemini").mkdir(parents=True)
        (real / ".gemini" / "oauth_creds.json").write_text("{}", encoding="utf-8")
        inv = _build("cli.gemini", workdir, environ={"PATH": "/usr/bin", "HOME": "/home/p", "GEMINI_CLI_HOME": str(real)})
        assert inv.env["GOOGLE_APPLICATION_CREDENTIALS"] == str(real / ".gemini" / "oauth_creds.json")
        assert inv.env["GEMINI_CLI_HOME"] == str(workdir / "home")

    def test_settings_paths_from_the_environment_do_not_reach_it(self, workdir):
        environ = {"PATH": "/usr/bin", "GEMINI_API_KEY": "k", "GEMINI_CLI_SYSTEM_SETTINGS_PATH": "/x",
                   "GEMINI_CLI_SYSTEM_DEFAULTS_PATH": "/y", "GEMINI_MODEL": "m", "GEMINI_SYSTEM_MD": "/z"}
        env = _build("cli.gemini", workdir, environ=environ).env
        assert "GEMINI_CLI_SYSTEM_SETTINGS_PATH" not in env and "GEMINI_CLI_SYSTEM_DEFAULTS_PATH" not in env
        assert "GEMINI_MODEL" not in env
        assert env["GEMINI_SYSTEM_MD"] != "/z"
        assert env["NO_BROWSER"] == "true"          # never waits on a browser sign-in
        assert env["GEMINI_CLI_NO_RELAUNCH"] == "true"
        # 0.64.0 will not run headless in an untrusted folder; the folder is the call's empty own.
        assert env["GEMINI_CLI_TRUST_WORKSPACE"] == "true"

    def test_its_error_reports_stay_in_the_calls_directory(self, workdir):
        # It writes /tmp/gemini-client-error-*.json, the request's turns, on a failure.
        env = _build("cli.gemini", workdir, environ={"PATH": "/usr/bin", "GEMINI_API_KEY": "k"}).env
        assert env["TMPDIR"] == env["TEMP"] == env["TMP"] == str(workdir / "tmp")
        assert (workdir / "tmp").is_dir()


# ── Codex (F745) ─────────────────────────────────────────────────────────────

class TestCodex:
    def test_nothing_of_the_persons_own_is_loaded(self, workdir):
        argv = _build("cli.codex", workdir, "gpt-5.4-mini").argv
        assert argv[:2] == ["codex", "exec"]
        for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check", "--json"):
            assert flag in argv, flag
        assert _flag(argv, "-C") == str(workdir / "cwd")
        assert _flag(argv, "-m") == "gpt-5.4-mini"
        assert argv[-1] == "-"

    def test_it_runs_in_a_read_only_sandbox(self, workdir):
        argv = _build("cli.codex", workdir).argv
        assert "--dangerously-bypass-approvals-and-sandbox" not in argv
        assert _flag(argv, "-s") == "read-only"

    def test_its_tools_and_extra_instructions_are_off(self, workdir):
        argv = _build("cli.codex", workdir).argv
        disabled = {argv[i + 1] for i, a in enumerate(argv) if a == "--disable"}
        # view_image: 0.163.0's feature; it read any image on the host and its
        # call shows in no --json event, so no guard could see it.
        assert {"view_image", "shell_tool", "unified_exec", "apps", "plugins", "hooks", "multi_agent", "browser_use",
                "browser_use_external", "in_app_browser", "computer_use", "image_generation", "goals",
                "tool_suggest", "memories"} <= disabled
        c = _configs(argv)
        assert c["web_search"] == '"disabled"'
        assert c["include_permissions_instructions"] == "false"
        assert c["include_environment_context"] == "false"
        assert c["include_apps_instructions"] == "false"
        assert c["skills.include_instructions"] == "false"
        assert c["project_doc_max_bytes"] == "0"                  # no AGENTS.md from the project
        assert c["history.persistence"] == '"none"'

    def test_its_own_system_prompt_is_replaced_by_the_agents(self, workdir):
        # Like Claude Code's --system-prompt-file: Codex's own 18 KB prompt (its
        # writing style, collaboration modes) gives way to the agent's, and the
        # rest of the prompt — the engine's tools and the transcript — is stdin.
        inv = _build("cli.codex", workdir, system="Be brief.", prompt="Be brief.\n\nUser: hi")
        path = Path(json.loads(_configs(inv.argv)["model_instructions_file"]))
        assert path.read_text(encoding="utf-8") == f"Be brief.\n\n{HOST_NOTE}"  # F1209's note, on purpose
        assert path.parent == workdir
        assert inv.stdin == "User: hi"
        assert "instructions" not in _configs(inv.argv)

    def test_with_no_system_prompt_its_own_is_still_replaced(self, workdir):
        # Never empty: some accounts 400 with "Instructions are required".
        inv = _build("cli.codex", workdir, system=None, prompt="User: hi")
        path = Path(json.loads(_configs(inv.argv)["model_instructions_file"]))
        assert path.read_text(encoding="utf-8").strip()

    def test_its_model_list_is_given_back_with_no_tool_a_model_entry_grants(self, workdir):
        # Codex sets a model's tools from its catalog entry: apply_patch, and in
        # 0.163.0 `tool_mode: code_mode_only` (an `exec` that runs code),
        # `multi_agent_version` (spawn_agent and its kin) and
        # `experimental_supported_tools` (clock) — none of which has a flag.
        catalog = {"models": [
            {"slug": "gpt-6-luna", "apply_patch_tool_type": "freeform", "tool_mode": "code_mode_only",
             "multi_agent_version": "v2", "experimental_supported_tools": ["send_user_message_async", "clock"],
             "include_plugin_usage_instructions": True, "include_apps_usage_instructions": True, "priority": 4},
            {"slug": "gpt-5.5", "apply_patch_tool_type": "function", "priority": 0}]}
        argv = _build("cli.codex", workdir, codex_catalog=catalog).argv
        path = json.loads(_configs(argv)["model_catalog_json"])
        written = json.loads(Path(path).read_text(encoding="utf-8"))
        assert [m["slug"] for m in written["models"]] == ["gpt-6-luna", "gpt-5.5"]
        for m in written["models"]:
            assert m["apply_patch_tool_type"] is None
            assert "tool_mode" not in m and "multi_agent_version" not in m
            assert m["experimental_supported_tools"] == []
            assert m["supports_search_tool"] is False      # tool_search, which hid the sub-agent tools
        assert catalog["models"][0]["tool_mode"] == "code_mode_only"   # the caller's copy untouched

    def test_without_a_catalog_none_is_named(self, workdir):
        assert "model_catalog_json" not in _configs(_build("cli.codex", workdir).argv)

    def test_the_answer_is_written_to_a_file_in_the_workdir(self, workdir):
        inv = _build("cli.codex", workdir)
        assert inv.output_file is not None and inv.output_file.parent == workdir
        assert _flag(inv.argv, "-o") == str(inv.output_file)
        assert inv.stdin == "User: hi"


# ── Copilot CLI (F746) ───────────────────────────────────────────────────────

class TestCopilotCli:
    def test_nothing_of_the_persons_own_is_loaded(self, workdir):
        inv = _build("cli.copilot", workdir, "gpt-5.4", github_token="gho_secret")
        argv = inv.argv
        for flag in ("--no-custom-instructions", "--disable-builtin-mcps", "--no-ask-user", "--no-auto-update",
                     "--no-remote", "--no-remote-export", "--disallow-temp-dir", "--no-color"):
            assert flag in argv, flag
        assert _flag(argv, "--log-level") == "none"
        assert _flag(argv, "--model") == "gpt-5.4"
        assert _flag(argv, "--output-format") == "json"

    def test_it_is_allowed_no_tool_at_all(self, workdir):
        # 1.0.98, measured with its own debug log: an empty --available-tools
        # still left 20 tools (bash, view, apply_patch, web_fetch, sql, …), and
        # --excluded-tools can only name the ones known today; an allow-list
        # naming no real tool left tool_count 0.
        argv = _build("cli.copilot", workdir).argv
        allowed = argv[argv.index("--available-tools") + 1]
        assert allowed == "__synapse_none__"
        assert "--excluded-tools" not in argv

    def test_the_prompt_goes_on_stdin_not_the_command_line(self, workdir):
        inv = _build("cli.copilot", workdir, prompt="SYS\n\nUser: hi")
        assert inv.stdin == f"SYS\n\n{HOST_NOTE}\n\nUser: hi"  # F1209's note, on purpose
        assert "-p" not in inv.argv and not any("User: hi" in a for a in inv.argv)

    def test_it_runs_from_a_home_of_its_own_and_signs_in_with_the_token(self, workdir):
        inv = _build("cli.copilot", workdir, github_token="gho_secret")
        assert inv.env["COPILOT_HOME"] == str(workdir / "home")
        assert inv.env["COPILOT_GITHUB_TOKEN"] == "gho_secret"
        assert not any("gho_secret" in a for a in inv.argv)          # never on the command line

    def test_switches_from_the_environment_do_not_reach_it(self, workdir):
        environ = {"PATH": "/usr/bin", "COPILOT_ALLOW_ALL": "true", "COPILOT_CUSTOM_INSTRUCTIONS_DIRS": "/x",
                   "COPILOT_MODEL": "m", "GITHUB_COPILOT_PROMPT_MODE_REPO_HOOKS": "true", "COPILOT_HOME": "/real"}
        env = _build("cli.copilot", workdir, environ=environ).env
        for gone in ("COPILOT_ALLOW_ALL", "COPILOT_CUSTOM_INSTRUCTIONS_DIRS", "COPILOT_MODEL",
                     "GITHUB_COPILOT_PROMPT_MODE_REPO_HOOKS"):
            assert gone not in env, gone
        assert env["COPILOT_HOME"] == str(workdir / "home")


# ── What the model is told about the machine (F1209, CAP3) ─────────────────────────────────────────────────────

def _read_by_model(inv) -> str:
    """Everything the model reads: its system file (whichever CLI's) and its stdin."""
    files = [inv.env.get("GEMINI_SYSTEM_MD"), _flag(inv.argv, "--system-prompt-file")]
    codex = _configs(inv.argv).get("model_instructions_file")
    files.append(json.loads(codex) if codex else None)
    return "\n".join([Path(f).read_text(encoding="utf-8") for f in files if f] + [inv.stdin or ""])


class TestTheMachineItIsToldOf:
    """Every CLI tells its model of its own working directory, OS or temp folder, and none can be stopped. CAP3's
    stage 0 (10-29): a model writing an `execute_python` script saved its chart to that directory, which the
    sandbox does not have, and the script failed. Every call is told that folder is not where any tool runs."""

    @pytest.mark.parametrize("base", ["cli.claude", "cli.gemini", "cli.codex", "cli.copilot"])
    def test_every_cli_is_told_once(self, base, workdir):
        inv = _build(base, workdir, environ={"PATH": "/usr/bin", "HOME": "/home/p", "GEMINI_API_KEY": "k"})
        assert _read_by_model(inv).count(HOST_NOTE) == 1

    @pytest.mark.parametrize("base", ["cli.claude", "cli.gemini", "cli.codex", "cli.copilot"])
    def test_with_no_system_prompt_too(self, base, workdir):
        inv = _build(base, workdir, system=None, prompt="User: hi",
                     environ={"PATH": "/usr/bin", "HOME": "/home/p", "GEMINI_API_KEY": "k"})
        assert _read_by_model(inv).count(HOST_NOTE) == 1

    def test_claude_code_without_a_system_prompt_still_gets_the_neutral_one(self, workdir):
        inv = _build("cli.claude", workdir, system=None, prompt="User: hi")
        text = Path(_flag(inv.argv, "--system-prompt-file")).read_text(encoding="utf-8")
        assert text == f"{NEUTRAL_SYSTEM_PROMPT}\n\n{HOST_NOTE}"
        assert inv.stdin == "User: hi"

    def test_it_says_no_tool_runs_there_and_where_paths_come_from(self):
        assert "No tool runs there" in HOST_NOTE
        assert "tool's own description" in HOST_NOTE


class TestTheSandboxSaysWhereCodeMayWrite:
    async def test_execute_python_names_its_writable_folder_and_how_to_keep_a_file(self):
        """The note sends a model to the tool's description for paths; the sandbox's said only what it may read."""
        from tools import sandbox
        tools = await sandbox.list_tools()
        desc = next(t.description for t in tools if t.name == "execute_python")
        assert "/tmp" in desc and "vault_write" in desc and "/data" in desc
