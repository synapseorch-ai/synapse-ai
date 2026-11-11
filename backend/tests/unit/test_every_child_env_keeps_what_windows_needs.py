"""Every child process started with a cut-down environment keeps what Windows needs to start it (F1533, BRW5).

The browser host's allow-list was written for Linux and macOS (PATH, HOME, LANG, TMPDIR), and on Windows the host never
started, from 2.0.0 to 2.0.3, while host_runner's and the parse runner's lists already kept SYSTEMROOT and the rest
("Windows needs these to start a process at all"). One list now holds those names (`core.process_env`), and:

1. every env allow-list in backend/core keeps them, and still no secret;
2. every allow-list literal in backend/core is in a module that takes them from `core.process_env`;
3. every call in backend/core that starts something with `env=` is listed below with what it passes: a new one fails
   here until it is read for Windows and listed;
4. host_runner and the parse runner keep every name they kept before F1533.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[2] / "core"

#: What Windows needs to start a process (BRW5's probe B: the host started with exactly these added).
WINDOWS_START = {
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "PROGRAMDATA", "PROGRAMFILES", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "TEMP", "TMP",
}
SECRETS = ("OPENAI_API_KEY", "SYNAPSE_INTERNAL_TOKEN", "DATABASE_URL", "SESSION_SECRET", "AWS_SECRET_ACCESS_KEY",
           "MODEL_GATEWAY_SIGNING_KEY")

#: Every call in backend/core given `env=`: (file, what is called, what it is given) -> how many, and what that is.
LAUNCHES: dict[tuple[str, str, str], tuple[int, str]] = {
    ("browser/client.py", "asyncio.create_subprocess_exec", "self._env()"): (1, "the browser host: an allow-list"),
    ("browser/host.py", "pw.chromium.launch_persistent_context", "_clean_env()"): (1, "Chromium: an allow-list"),
    ("browser/host.py", "StdioServerParameters", "_clean_env()"): (2, "Playwright MCP: an allow-list"),
    ("execution/host_runner.py", "asyncio.create_subprocess_exec", "child_env()"): (1, "a host transform: an allow-list"),
    ("knowledge/parse/runner.py", "asyncio.create_subprocess_exec", "child_env()"): (1, "a parse: an allow-list"),
    ("llm_providers.py", "_run_cli", "inv.env"): (1, "a model CLI: the whole environment less its drop list"),
    ("llm_providers.py", "subprocess.Popen", "env"): (1, "the same model CLI call"),
    ("mcp_client.py", "StdioServerParameters", "env"): (1, "an MCP server: the whole environment, and its own"),
    ("routes/tools.py", "_server.mcp_manager.add_server", "req.env"): (1, "an added MCP server's own variables"),
    ("scale/worker_server_module.py", "StdioServerParameters", "env"):
        (1, "a worker's tool server: shared_tool_environment (an allow-list) or the whole environment"),
    ("scale/worker_server_module.py", "StdioServerParameters", "env or None"):
        (1, "a configured server's own variables; the MCP SDK adds its Windows defaults"),
    ("server.py", "StdioServerParameters", "env"): (3, "the app's own MCP servers: the whole environment"),
}

#: The allow-lists themselves: (module, how its environment is built).
ALLOW_LIST_MODULES = {"browser/client.py", "browser/host.py", "execution/host_runner.py", "knowledge/parse/runner.py",
                      "scale/worker_server_module.py"}

#: host_runner's and the parse runner's lists in 2.0.3, before F1533: none of these may go.
HOST_RUNNER_BEFORE = {
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "TZ", "TMPDIR", "TEMP", "TMP",
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "PROGRAMDATA", "PROGRAMFILES", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    "PYTHONIOENCODING", "PYTHONUTF8", "PYTHONHASHSEED",
    "CUDA_VISIBLE_DEVICES", "CUDA_HOME", "LD_LIBRARY_PATH", "HF_HOME", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE",
    "TRANSFORMERS_CACHE", "TORCH_HOME", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
    "TORCH_NUM_THREADS",
}
PARSE_RUNNER_BEFORE = {
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "TZ", "TMPDIR", "TEMP", "TMP",
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "PROGRAMDATA", "PROGRAMFILES", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    "DOCLING_ARTIFACTS_PATH", "HF_HOME", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "OMP_NUM_THREADS",
    "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "TORCH_NUM_THREADS",
    "SYNAPSE_SOFFICE", "PYTHONIOENCODING", "PYTHONUTF8",
}


def _modules():
    for path in sorted(CORE.rglob("*.py")):
        yield str(path.relative_to(CORE)), ast.parse(path.read_text("utf-8"))


def _builders():
    from core.browser import client as browser_client
    from core.browser import host as browser_host
    from core.execution import host_runner
    from core.knowledge.parse import runner as parse_runner
    from core.scale import worker_server_module as workers
    return {
        "the browser host (client._Host._env)": lambda environ: browser_client._Host()._env(),
        "Chromium and Playwright MCP (host._clean_env)": lambda environ: browser_host._clean_env(),
        "a host transform (host_runner.child_env)": lambda environ: host_runner.child_env(),
        "a parse (parse.runner.child_env)": lambda environ: parse_runner.child_env(),
        "a worker's shared tool server (shared_tool_environment)": workers.shared_tool_environment,
    }


@pytest.mark.parametrize("what", list(_builders()))
def test_each_allow_list_keeps_what_windows_needs_and_no_secret(what, monkeypatch):
    environ = {name: f"value-of-{name.lower()}" for name in WINDOWS_START}
    environ.update({name: f"secret-{name.lower()}" for name in SECRETS})
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    env = _builders()[what](environ)
    assert {k: env.get(k) for k in WINDOWS_START} == {k: environ[k] for k in WINDOWS_START}, \
        f"{what} drops {sorted(WINDOWS_START - set(env))}"
    assert not set(SECRETS) & set(env), f"{what} passes {sorted(set(SECRETS) & set(env))}"


def test_every_allow_list_takes_the_windows_names_from_one_place():
    found: set[str] = set()
    takes: set[str] = set()
    for rel, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, (ast.Tuple, ast.Set, ast.List)):
                names = {e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
                if "PATH" in names and names & {"HOME", "LANG", "TMPDIR", "USER"}:
                    found.add(rel)
            if isinstance(node, ast.ImportFrom) and node.module and (
                    node.module == "core.process_env" or (node.module == "core" and any(
                        a.name == "process_env" for a in node.names))):
                takes.add(rel)
    assert found == ALLOW_LIST_MODULES, \
        f"an env allow-list is new or gone: {sorted(found ^ ALLOW_LIST_MODULES)}; list it, and take core.process_env"
    assert not found - takes, f"these build an env allow-list without core.process_env: {sorted(found - takes)}"


def test_every_launch_with_its_own_environment_is_listed():
    seen: Counter = Counter()
    for rel, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "env":
                        seen[(rel, ast.unparse(node.func), ast.unparse(kw.value))] += 1
    want = Counter({key: count for key, (count, _what) in LAUNCHES.items()})
    assert seen == want, (
        "a call given env= is new, moved or gone: read what it passes for Windows (core.process_env) and list it "
        f"here. New: {sorted((seen - want).elements())}; gone: {sorted((want - seen).elements())}")


def test_the_transform_and_parse_lists_keep_every_name_they_kept():
    from core.execution import host_runner
    from core.knowledge.parse import runner as parse_runner
    assert HOST_RUNNER_BEFORE <= set(host_runner.ENV_ALLOW), sorted(HOST_RUNNER_BEFORE - set(host_runner.ENV_ALLOW))
    assert PARSE_RUNNER_BEFORE <= set(parse_runner.ENV_ALLOW), sorted(PARSE_RUNNER_BEFORE - set(parse_runner.ENV_ALLOW))
