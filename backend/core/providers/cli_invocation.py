"""How Synapse runs an installed AI CLI as a model and nothing else (CLIM1, D299).

Claude Code, Gemini CLI, Codex and Copilot CLI are agents in their own right.
Run as their owner runs them, each loads that person's setup: built-in tools
that read, write and run things, MCP servers, hooks, skills, extensions,
memory files (CLAUDE.md, GEMINI.md, AGENTS.md), the project around its working
directory, and it keeps a transcript of the call. Synapse wants only the
model: the agent's tools are written into the prompt and run by the engine.

`build_cli_invocation` decides, per CLI, the command, the environment and the
working directory of one call. Every call runs in a fresh private directory of
its own (`new_workdir`), removed afterwards; Gemini CLI and Copilot CLI also get
a throwaway home there, because neither has a switch to stop saving the chat.
The flags were each measured on 2026-10-17 against claude 2.1.291, gemini
0.38.0 and codex 0.137.0 with a capture server in place of the model.

What none of them lets Synapse switch off, said plainly:
- Claude Code: an environment block (the empty working directory, the OS and
  the date); admin-managed policy settings.
- Gemini CLI: the date, OS and temp-directory lines of its session context;
  admin settings fetched from Google for enterprise accounts.
- Codex (0.163.0): `request_user_input` stays in its tool list (refused in
  exec mode), and a global `$CODEX_HOME/AGENTS.md` is still read (moving
  CODEX_HOME would move its sign-in). The sandbox is read-only, and an answer
  reached through any tool is refused (`_read_cli_answer`).
- Copilot CLI: hooks installed as admin policy; managed (organization) settings.
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

CLI_BASES = ("cli.claude", "cli.gemini", "cli.codex", "cli.copilot")

#: The CLIs given the agent's tools as real tools through the bridge (F753): run
#: with no tools of their own, newer Codex models would not use the tools written
#: into the prompt. Claude Code uses them as text (CLIM1's measure, 14/14 on Sonnet).
BRIDGED_CLIS = ("cli.codex",)

#: The bridge server the CLI starts for a call (standard library only).
BRIDGE_SERVER = Path(__file__).resolve().with_name("cli_bridge_server.py")
#: The name the bridge's MCP server goes by in the CLI's config and events.
BRIDGE_SERVER_NAME = "synapse"

#: The binary each CLI base runs.
CLI_BINARIES = {"cli.claude": "claude", "cli.gemini": "gemini", "cli.codex": "codex", "cli.copilot": "copilot"}

#: Gemini CLI's prompt: fixed, so its `@path` and `/command` handling never
#: sees the conversation, which rides in the system file instead.
GEMINI_TURN = "Write the assistant's next reply to the conversation above."

#: Used when an agent has no system prompt, so Claude Code's own (the working
#: directory, git status, memory paths) is never sent in its place.
NEUTRAL_SYSTEM_PROMPT = "You are a helpful assistant."

#: What every call is told about the machine its CLI says it runs on (F1209, CAP3). None of the CLIs can be stopped
#: from telling its model of its own working directory, OS or temp folder (the module's note), and a model writing
#: code for a tool took that directory for the tool's: CAP3's stage 0 saw an `execute_python` script save its chart to
#: `/tmp/synapse-cli-…/cwd`, which the sandbox does not have, and fail. The correction goes where the false fact comes
#: in, on every call: a Tool step's call carries its tool in the prompt, not as `tools`.
HOST_NOTE = ("About the machine you may be told you run on (a working directory, an operating system, a temp "
             "folder): that is an empty folder made for this one reply and deleted after it. No tool runs there, and "
             "nothing written there is kept. When a tool needs a path, take it from that tool's own description or "
             "from the task, never from that folder.")

# Claude Code's variables that say how it signs in or reaches its provider.
# Everything else under CLAUDE_CODE_* is dropped: a Synapse started from inside
# a Claude Code session inherits that session's variables (its id, its entry
# point, its messaging socket), and a person's own switches could turn back on
# what the flags below turn off.
_CLAUDE_KEEP = frozenset({
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH", "CLAUDE_CODE_SKIP_VERTEX_AUTH", "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_API_KEY_HELPER_TTL_MS",
    "CLAUDE_CODE_CLIENT_CERT", "CLAUDE_CODE_CLIENT_KEY", "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE",
})
_CLAUDE_DROP = frozenset({"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT"})

_CLAUDE_ENV = {
    # `@/path` in a -p prompt reads that file into it, with no tools and in safe mode.
    "CLAUDE_CODE_DISABLE_ATTACHMENTS": "1",
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": "1",
    "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}

_GEMINI_DROP = frozenset({
    "GEMINI_CLI_SYSTEM_SETTINGS_PATH", "GEMINI_CLI_SYSTEM_DEFAULTS_PATH", "GEMINI_SYSTEM_MD",
    "GEMINI_WRITE_SYSTEM_MD", "GEMINI_MODEL", "GEMINI_CLI_HOME", "GEMINI_FORCE_ENCRYPTED_FILE_STORAGE",
})

#: Every built-in Gemini CLI tool is off, and so is everything that brings more.
_GEMINI_SETTINGS = {
    "tools": {"core": []},
    "skills": {"enabled": False},
    "hooksConfig": {"enabled": False},
    "experimental": {"enableAgents": False},
    "general": {"plan": {"enabled": False}},
    # An empty fileName is ignored and GEMINI.md read; a name no file has is not.
    "context": {"fileName": "__synapse_none__.md", "includeDirectoryTree": False},
    "mcp": {"allowed": ["__synapse_none__"]},
    "privacy": {"usageStatisticsEnabled": False},
}

# GEMINI_SYSTEM_MD substitutes these names; anything else in it is passed as written.
_GEMINI_TEMPLATE = re.compile(r"\$\{(AgentSkills|SubAgents|AvailableTools|\w+_ToolName)\}")

_CODEX_DISABLE = (
    # 0.163.0: it read any image on the host, and its call shows in no --json
    # event. An older Codex rejects the name ("Unknown feature flag") and is
    # asked to update: before it, view_image had no switch at all.
    "view_image",
    "shell_tool", "unified_exec", "apps", "plugins", "hooks", "multi_agent", "browser_use",
    "browser_use_external", "in_app_browser", "computer_use", "image_generation", "goals",
    "tool_suggest", "memories",
)

#: What each Codex model entry is given back with: nothing that grants a tool
#: (0.163.0). `supports_search_tool` is tool_search, which also hid the
#: sub-agent tools behind it; `experimental_supported_tools` held `clock`.
_CODEX_MODEL_TOOLS_OFF = {
    "apply_patch_tool_type": None,
    "experimental_supported_tools": [],
    "supports_search_tool": False,
}
#: Left out of each entry: `tool_mode: code_mode_only` is an `exec` that runs
#: code, `multi_agent_version` the sub-agent tools.
_CODEX_MODEL_TOOL_KEYS = ("tool_mode", "multi_agent_version")

_COPILOT_DROP = frozenset({
    "COPILOT_ALLOW_ALL", "COPILOT_CUSTOM_INSTRUCTIONS_DIRS", "COPILOT_MODEL", "COPILOT_HOME",
})

#: Copilot CLI's tool allow-list: a name no tool has. 1.0.98, measured with its
#: own debug log: an empty --available-tools left all 20 of its tools (bash,
#: view, apply_patch, web_fetch, sql, run_dynamic_workflow, …) and
#: --excluded-tools can only name the ones known today; this left none.
COPILOT_NO_TOOL = "__synapse_none__"


@dataclass
class CliInvocation:
    """One CLI call: what runs, with what, where, and where its answer is."""
    argv: list[str]
    env: dict[str, str]
    cwd: Path
    #: Fed on stdin; None feeds nothing.
    stdin: str | None
    #: Codex writes its last message here.
    output_file: Path | None = None
    #: Where the bridge records the tool calls the CLI makes (F753); None unbridged.
    bridge_calls: Path | None = None


def split_cli_model(cli_model: str) -> tuple[str, str | None]:
    """`cli.codex.gpt-5.4` → (`cli.codex`, `gpt-5.4`). The base is matched
    case-insensitively; the variant is kept verbatim (model names can be
    case-sensitive) with its own dots."""
    parts = cli_model.split(".", 2)
    base = f"{parts[0]}.{parts[1]}".lower() if len(parts) >= 2 else cli_model.lower()
    return base, (parts[2] if len(parts) > 2 else None)


def new_workdir(root: Path | None = None) -> Path:
    """A fresh private directory for one call: an empty `cwd/` to run in and a
    `home/` for a CLI that gets a throwaway one. The caller removes it.

    Outside any repository by default (the system temp dir), so no project
    file above it can be found either.
    """
    path = Path(tempfile.mkdtemp(prefix="synapse-cli-", dir=str(root) if root else None))
    os.chmod(path, 0o700)
    (path / "cwd").mkdir()
    (path / "home").mkdir()
    return path


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _strip_system(prompt: str, system: str | None) -> str:
    """The prompt after the system prompt it starts with (`_build_cli_prompt`
    joins them), or all of it."""
    prefix = (system or "").strip()
    if prefix and prompt.startswith(prefix):
        return prompt[len(prefix):].lstrip("\n")
    return prompt


def _claude(variant, *, workdir, system_prompt, prompt, effort, environ, **_):
    from core import reasoning as _reasoning

    env = {k: v for k, v in environ.items()
           if k not in _CLAUDE_DROP and not k.startswith("CLAUDE_AGENT_SDK_")
           and (not k.startswith("CLAUDE_CODE_") or k in _CLAUDE_KEEP)}
    env.update(_CLAUDE_ENV)
    system_file = _write(workdir / "system.md", (system_prompt or "").strip() or NEUTRAL_SYSTEM_PROMPT)
    # The CLI has no "off", so off and None are low (F65).
    level = _reasoning.normalise(effort, fallback="low")
    argv = ["claude", "-p", "--output-format", "json",
            "--tools", "",                      # F98: no built-in tool
            "--safe-mode",                      # CLAUDE.md, skills, plugins, hooks, MCP, commands, agents
            "--strict-mcp-config",              # no MCP server from any config
            "--disable-slash-commands",
            "--no-session-persistence",         # no transcript
            "--permission-mode", "dontAsk",
            "--effort", "low" if level == "off" else level,
            "--system-prompt-file", str(system_file)]
    if variant:
        # A stored id with an old suffix still names its model (F65).
        model = variant
        for suffix in ("-thinking", "-max", "-high"):
            if model.endswith(suffix):
                model = model[: -len(suffix)]
        argv += ["--model", model]
    return CliInvocation(argv, env, workdir / "cwd", _strip_system(prompt, system_prompt))


def gemini_sign_in(environ: Mapping[str, str]) -> tuple[str | None, dict[str, str]]:
    """How Gemini CLI signs in from a throwaway home: (auth type, variables).

    A Google sign-in is read where it is (`oauth_creds.json` in the person's
    Gemini home) through GOOGLE_APPLICATION_CREDENTIALS, which the CLI falls back
    to; a refreshed token is written to the throwaway home, never to theirs. It
    comes first, so a GEMINI_API_KEY in Synapse's own environment cannot turn a
    subscription into key billing. None when no sign-in can be found.
    """
    real_home = Path(environ.get("GEMINI_CLI_HOME") or environ.get("HOME") or Path.home())
    creds = real_home / ".gemini" / "oauth_creds.json"
    if creds.is_file():
        return "oauth-personal", {"GOOGLE_GENAI_USE_GCA": "true", "GOOGLE_APPLICATION_CREDENTIALS": str(creds)}
    if environ.get("GEMINI_API_KEY"):
        return "gemini-api-key", {}
    if (environ.get("GOOGLE_GENAI_USE_VERTEXAI") or "").lower() in ("true", "1"):
        return "vertex-ai", {}
    return None, {}


def _gemini(variant, *, workdir, prompt, environ, **_):
    env = {k: v for k, v in environ.items()
           if k not in _GEMINI_DROP and not k.startswith("GEMINI_PROMPT_")}
    kind, auth_env = gemini_sign_in(environ)
    env.update(auth_env)
    settings = copy.deepcopy(_GEMINI_SETTINGS)
    if kind:
        settings["security"] = {"auth": {"selectedType": kind}}
    home = workdir / "home"
    _write(home / ".gemini" / "settings.json", json.dumps(settings, indent=2))
    policy = _write(workdir / "deny-all.toml", '[[rule]]\ntoolName = "*"\ndecision = "deny"\npriority = 999\n')
    # Its prompt is fixed; the conversation is the system file, read as written
    # except for its own template names, which are broken so they stay text.
    system_file = _write(workdir / "system.md", _GEMINI_TEMPLATE.sub(lambda m: "$ {" + m.group(1) + "}", prompt))
    # Its error reports (the request's turns) go to the temp dir: this call's own.
    tmp = workdir / "tmp"
    tmp.mkdir(exist_ok=True)
    env.update({
        "TMPDIR": str(tmp), "TEMP": str(tmp), "TMP": str(tmp),
        "GEMINI_CLI_HOME": str(home),
        "GEMINI_SYSTEM_MD": str(system_file),
        "NO_BROWSER": "true",              # a missing sign-in fails instead of waiting on a browser
        # 0.64.0 refuses to run headless in an untrusted folder; this one is the
        # call's own, empty. A variable, not --skip-trust, which 0.38.0 rejects.
        "GEMINI_CLI_TRUST_WORKSPACE": "true",
        "GEMINI_CLI_NO_RELAUNCH": "true",
    })
    argv = ["gemini", "--prompt", "", "-o", "json", "-e", "none", "--policy", str(policy)]
    if variant:
        argv += ["--model", variant]      # F750: verbatim; `pro` and `flash` are its own aliases
    return CliInvocation(argv, env, workdir / "cwd", GEMINI_TURN)


def _bridge_tools(tools: list[dict]) -> list[dict]:
    """The agent's tools as the bridge lists them: a name the model API takes
    (letters, digits, `_`, `-`), mapped back to the engine's own name."""
    out, used = [], set()
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) else None
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        engine_name = str(fn["name"])
        name = re.sub(r"[^A-Za-z0-9_-]", "_", engine_name)[:64] or "tool"
        base, n = name, 2
        while name in used:
            name = f"{base[:60]}_{n}"
            n += 1
        used.add(name)
        out.append({"name": name, "engine_name": engine_name, "description": fn.get("description") or "",
                    "inputSchema": fn.get("parameters") or {"type": "object", "properties": {}}})
    return out


def _codex(variant, *, workdir, system_prompt, prompt, environ, codex_catalog=None, tools=None, **_):
    env = dict(environ)
    # Its own system prompt (18 KB: writing style, collaboration modes) gives
    # way to the agent's, as Claude Code's does; never empty, since some
    # accounts 400 with "Instructions are required".
    system_file = _write(workdir / "system.md", (system_prompt or "").strip() or NEUTRAL_SYSTEM_PROMPT)
    cwd = workdir / "cwd"
    out = workdir / "answer.txt"
    argv = ["codex", "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check",
            "-s", "read-only", "-C", str(cwd), "--json"]
    for feature in _CODEX_DISABLE:
        argv += ["--disable", feature]
    for setting in ('web_search="disabled"',
                    "include_permissions_instructions=false",
                    "include_environment_context=false",
                    "include_apps_instructions=false",
                    "skills.include_instructions=false",
                    "project_doc_max_bytes=0",
                    'history.persistence="none"',
                    f"model_instructions_file={json.dumps(str(system_file))}"):
        argv += ["-c", setting]
    if codex_catalog and isinstance(codex_catalog.get("models"), list):
        # A model's catalog entry grants tools no flag reaches: apply_patch, and
        # in 0.163.0 code mode (`exec`, which runs code), sub-agents and clock.
        catalog = copy.deepcopy(codex_catalog)
        for model in catalog["models"]:
            if isinstance(model, dict):
                model.update(_CODEX_MODEL_TOOLS_OFF)
                for key in _CODEX_MODEL_TOOL_KEYS:
                    model.pop(key, None)
        path = _write(workdir / "models.json", json.dumps(catalog))
        argv += ["-c", f"model_catalog_json={json.dumps(str(path))}"]
    bridge_calls = None
    listed = _bridge_tools(tools)
    if listed:
        # The agent's tools as real tools; the bridge records a call and runs nothing (F753).
        spec = _write(workdir / "bridge-tools.json", json.dumps(listed))
        bridge_calls = workdir / "bridge-calls.jsonl"
        server = (f"{{command={json.dumps(sys.executable)}, "
                  f"args=[{json.dumps(str(BRIDGE_SERVER))}, {json.dumps(str(spec))}, {json.dumps(str(bridge_calls))}]}}")
        argv += ["-c", f"mcp_servers.{BRIDGE_SERVER_NAME}={server}"]
    if variant:
        argv += ["-m", variant]
    argv += ["-o", str(out), "-"]
    return CliInvocation(argv, env, cwd, _strip_system(prompt, system_prompt), output_file=out,
                         bridge_calls=bridge_calls)


def _copilot(variant, *, workdir, prompt, environ, github_token=None, **_):
    env = {k: v for k, v in environ.items()
           if k not in _COPILOT_DROP and not k.startswith("GITHUB_COPILOT_PROMPT_MODE_")
           and not k.startswith("COPILOT_PLUGIN")}
    env.update({"COPILOT_HOME": str(workdir / "home"), "COPILOT_AUTO_UPDATE": "false"})
    if github_token:
        env["COPILOT_GITHUB_TOKEN"] = github_token
    # The prompt goes on stdin (it runs non-interactive when stdin is piped),
    # off the command line and its length limit.
    argv = ["copilot", "--output-format", "json", "--no-color", "--no-custom-instructions",
            "--disable-builtin-mcps",           # github-mcp-server, githubiq
            "--no-ask-user", "--no-auto-update", "--no-remote", "--no-remote-export",
            "--disallow-temp-dir", "--log-level", "none",
            "--available-tools", COPILOT_NO_TOOL]
    if variant:
        argv += ["--model", variant]
    return CliInvocation(argv, env, workdir / "cwd", prompt)


#: The CLIs whose system prompt is a file of its own (the rest read it all as their prompt).
SYSTEM_FILE_CLIS = ("cli.claude", "cli.codex")


def _with_host_note(base: str, system_prompt: str | None, prompt: str) -> tuple[str | None, str]:
    """`HOST_NOTE` where each CLI reads it once, after the system prompt (`_build_cli_prompt` puts that first). A CLI
    with a system file: the note ends its system prompt (the neutral one when the agent has none, so Claude Code's own
    is still never sent). A CLI that reads it all as its prompt: the note follows the system prompt in the prompt, or
    opens it when there is none."""
    system = (system_prompt or "").strip()
    starts = bool(system) and prompt.startswith(system)
    if base in SYSTEM_FILE_CLIS:
        noted = f"{system or NEUTRAL_SYSTEM_PROMPT}\n\n{HOST_NOTE}"
        return noted, (noted + prompt[len(system):]) if starts else prompt
    if starts:
        return system_prompt, f"{system}\n\n{HOST_NOTE}{prompt[len(system):]}"
    return system_prompt, f"{HOST_NOTE}\n\n{prompt}"


_BUILDERS = {"cli.claude": _claude, "cli.gemini": _gemini, "cli.codex": _codex, "cli.copilot": _copilot}


def build_cli_invocation(base: str, variant: str | None, *, workdir: Path, system_prompt: str | None,
                         prompt: str, effort: str | None = None, environ: Mapping[str, str] | None = None,
                         codex_catalog: dict | None = None, github_token: str | None = None,
                         tools: list[dict] | None = None) -> CliInvocation:
    """The command, environment and working directory of one call to `base`.

    `prompt` is the whole flat prompt (`_build_cli_prompt`: the system prompt,
    the engine's tools, the transcript); `workdir` comes from `new_workdir`.
    Writes only inside `workdir`.
    """
    builder = _BUILDERS.get(base)
    if builder is None:
        raise ValueError(f"Unknown CLI base '{base}'. Supported: {list(_BUILDERS)}")
    system_prompt, prompt = _with_host_note(base, system_prompt, prompt)
    return builder(variant, workdir=workdir, system_prompt=system_prompt, prompt=prompt, effort=effort,
                   environ=dict(os.environ if environ is None else environ),
                   codex_catalog=codex_catalog, github_token=github_token,
                   tools=tools if base in BRIDGED_CLIS else None)
