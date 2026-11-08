"""The real CLI binaries, run exactly as Synapse runs them, send their model
nothing of a person's own setup (CLIM1, D299, F743–F745).

No model is called: each CLI is pointed at a local capture server that records
the request and refuses it. Each runs from a seeded home in a temp dir (its
own config: an MCP server, a memory file, a hook, a skill) — never the
person's real one — and a file named with `@` in the prompt. The test then
reads what the model would have received.

Skipped for a CLI that is not installed.
"""
from __future__ import annotations

import http.server
import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from core.providers.cli_invocation import build_cli_invocation, new_workdir

_MCP_SERVER = '''
import sys, json
for line in sys.stdin:
    try: m = json.loads(line)
    except Exception: continue
    if m.get("id") is None: continue
    meth = m.get("method")
    if meth == "initialize":
        r = {"protocolVersion": m["params"].get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}},
             "serverInfo": {"name": "probe", "version": "1"}}
    elif meth == "tools/list":
        r = {"tools": [{"name": "probe_marker_tool", "description": "MCPMARKER", "inputSchema": {"type": "object"}}]}
    else:
        r = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}) + "\\n"); sys.stdout.flush()
'''

MARKERS = ("USERMEMORYMARKER", "PROJECTMEMORYMARKER", "SKILLMARKER", "HOOKMARKER", "MCPMARKER",
           "probe_marker_tool", "ATFILEMARKER")


class _Capture:
    """A local server that records each request body and refuses it."""

    def __init__(self):
        self.bodies: list[dict] = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def _answer(self):
                n = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    outer.bodies.append({"path": self.path, "body": json.loads(raw) if raw else None})
                except ValueError:
                    outer.bodies.append({"path": self.path, "body": raw.decode("utf-8", "replace")})
                msg = b'{"type":"error","error":{"type":"invalid_request_error","message":"capture-only"}}'
                self.send_response(400)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)

            do_POST = do_GET = _answer

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def model_requests(self, needle: str) -> list[dict]:
        return [b["body"] for b in self.bodies if needle in b["path"] and isinstance(b["body"], dict)]

    def close(self):
        self.server.shutdown()


@pytest.fixture
def capture():
    c = _Capture()
    yield c
    c.close()


@pytest.fixture
def seeded(tmp_path):
    """A fake home and project: what a person's own setup would bring in."""
    home = tmp_path / "home"
    home.mkdir()
    mcp = tmp_path / "mcp_probe.py"
    mcp.write_text(_MCP_SERVER, encoding="utf-8")
    at_file = tmp_path / "secret.txt"
    at_file.write_text("ATFILEMARKER", encoding="utf-8")
    return {"tmp": tmp_path, "home": home, "mcp": [sys.executable, str(mcp)], "at": at_file,
            "hook_ran": tmp_path / "HOOK_RAN"}


def _skill(dir_: Path):
    (dir_ / "probe").mkdir(parents=True)
    (dir_ / "probe" / "SKILL.md").write_text("---\nname: probe\ndescription: SKILLMARKER\n---\nx\n", encoding="utf-8")


def _run(inv, timeout=90):
    return subprocess.run(inv.argv, input=(inv.stdin or "").encode("utf-8"), cwd=str(inv.cwd), env=inv.env,
                          capture_output=True, timeout=timeout)


def _prompt(seeded):
    return f"SYSTEM\n\nUser: what does @{seeded['at']} say? mail a@b.com"


def _leaks(body) -> list[str]:
    text = json.dumps(body)
    return [m for m in MARKERS if m in text]


@pytest.mark.skipif(not shutil.which("claude"), reason="Claude Code is not installed")
def test_claude_code_sends_nothing_of_its_own(capture, seeded, tmp_path):
    config = seeded["home"] / ".claude"
    config.mkdir()
    (config / "CLAUDE.md").write_text("USERMEMORYMARKER", encoding="utf-8")
    _skill(config / "skills")
    (config / "settings.json").write_text(json.dumps({"hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": f"touch {seeded['hook_ran']}"}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo HOOKMARKER"}]}]}}), encoding="utf-8")
    (config / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True, "mcpServers": {
        "probe": {"type": "stdio", "command": seeded["mcp"][0], "args": seeded["mcp"][1:]}}}), encoding="utf-8")
    workdir = new_workdir(tmp_path)
    (workdir.parent / "CLAUDE.md").write_text("PROJECTMEMORYMARKER", encoding="utf-8")   # above its cwd
    environ = {"PATH": shutil.os.environ["PATH"], "HOME": str(seeded["home"]), "CLAUDE_CONFIG_DIR": str(config),
               "ANTHROPIC_BASE_URL": capture.url, "ANTHROPIC_API_KEY": "sk-ant-capture-only"}
    inv = build_cli_invocation("cli.claude", "haiku", workdir=workdir, system_prompt="SYSTEM",
                               prompt=_prompt(seeded), effort="low", environ=environ)
    _run(inv)

    sent = capture.model_requests("/v1/messages")
    assert sent, "Claude Code sent no request"
    body = sent[0]
    assert body.get("tools", []) == []
    assert _leaks(body) == []
    assert "what does @" in json.dumps(body)          # the prompt itself went as written
    assert not seeded["hook_ran"].exists()
    assert not (config / "projects").exists()          # no transcript


@pytest.mark.skipif(not shutil.which("gemini"), reason="Gemini CLI is not installed")
def test_gemini_cli_sends_nothing_of_its_own(capture, seeded, tmp_path):
    gem = seeded["home"] / ".gemini"
    gem.mkdir()
    (gem / "GEMINI.md").write_text("USERMEMORYMARKER", encoding="utf-8")
    _skill(gem / "skills")
    (gem / "settings.json").write_text(json.dumps({
        "mcpServers": {"probe": {"command": seeded["mcp"][0], "args": seeded["mcp"][1:]}},
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": f"touch {seeded['hook_ran']}"}]}]},
    }), encoding="utf-8")
    workdir = new_workdir(tmp_path)
    (workdir.parent / "GEMINI.md").write_text("PROJECTMEMORYMARKER", encoding="utf-8")
    environ = {"PATH": shutil.os.environ["PATH"], "HOME": str(seeded["home"]), "GEMINI_API_KEY": "capture-only",
               "GOOGLE_GEMINI_BASE_URL": capture.url}
    inv = build_cli_invocation("cli.gemini", "gemini-2.5-flash", workdir=workdir, system_prompt="SYSTEM",
                               prompt=_prompt(seeded), environ=environ)
    _run(inv)

    sent = capture.model_requests("GenerateContent")
    assert sent, "Gemini CLI sent no request"
    body = sent[0]
    tools = [d.get("name") for t in body.get("tools", []) for d in t.get("functionDeclarations", [])]
    assert tools == []
    assert _leaks(body) == []
    assert "mail a@b.com" in json.dumps(body)          # the conversation as written, address and all
    assert not seeded["hook_ran"].exists()
    assert not (gem / "tmp").exists()                  # no chat in the person's Gemini home
    # Its error report (the refused request) went to the call's own temp dir, removed with it.
    assert list(Path(inv.env["TMPDIR"]).glob("gemini-client-error-*"))


def _codex_tools(body: dict) -> set[str]:
    """Every tool a Codex request offers: its `tools`, and from 0.163.0 the
    namespaces of an `additional_tools` input item (e.g. `functions.exec`)."""
    names: set[str] = set()

    def walk(tools):
        for t in tools or []:
            if not isinstance(t, dict):
                continue
            if t.get("type") == "namespace":
                walk(t.get("tools"))
            else:
                names.add(str(t.get("name") or t.get("type")))
    walk(body.get("tools"))
    for item in body.get("input") or []:
        if isinstance(item, dict) and item.get("type") == "additional_tools":
            walk(item.get("tools"))
    return names


def _codex_catalog() -> dict | None:
    """Codex's own catalog, as the person's Codex reads it (read only)."""
    got = subprocess.run(["codex", "debug", "models"], capture_output=True, timeout=60)
    try:
        catalog = json.loads(got.stdout)
    except ValueError:
        return None
    return catalog if isinstance(catalog, dict) and catalog.get("models") else None


# Codex (0.163.0) has no switch for request_user_input; exec mode refuses it.
_CODEX_LEFT = {"request_user_input"}


@pytest.mark.skipif(not shutil.which("codex"), reason="Codex is not installed")
def test_codex_sends_nothing_of_its_own_but_what_it_cannot_drop(capture, seeded, tmp_path):
    catalog = _codex_catalog()
    models = [m["slug"] for m in (catalog or {}).get("models", [])
              if m.get("visibility") == "list"] or ["gpt-5.4-mini"]
    codex_home = seeded["home"] / ".codex"
    _skill(codex_home / "skills")
    (codex_home / "config.toml").write_text(
        f'[mcp_servers.probe]\ncommand = {json.dumps(seeded["mcp"][0])}\nargs = {json.dumps(seeded["mcp"][1:])}\n',
        encoding="utf-8")
    environ = {"PATH": shutil.os.environ["PATH"], "HOME": str(seeded["home"]), "CODEX_HOME": str(codex_home),
               "CAPTURE_KEY": "capture-only"}
    provider = ("model_providers.capture={name=\"capture\",base_url=\"" + capture.url
                + "/v1\",wire_api=\"responses\",env_key=\"CAPTURE_KEY\"}")
    for model in models:
        capture.bodies.clear()
        workdir = new_workdir(tmp_path)
        (workdir.parent / "AGENTS.md").write_text("PROJECTMEMORYMARKER", encoding="utf-8")
        (workdir.parent / ".git").mkdir(exist_ok=True)
        inv = build_cli_invocation("cli.codex", model, workdir=workdir, system_prompt="SYSTEM",
                                   prompt=_prompt(seeded), environ=environ, codex_catalog=catalog)
        inv.argv[2:2] = ["-c", provider, "-c", "model_provider=\"capture\""]
        _run(inv)

        sent = capture.model_requests("/responses")
        assert sent, f"Codex sent no request for {model}"
        body = sent[0]
        tools = {name.split(".")[-1] for name in _codex_tools(body)}
        assert tools <= _CODEX_LEFT, (model, tools)
        assert _leaks(body) == [], model
        text = json.dumps(body)
        for block in ("<permissions", "<environment_context>", "skills_instructions"):
            assert block not in text, (model, block)
    assert not (codex_home / "sessions").exists()       # no transcript


#: What the bridge adds besides the agent's tools: Codex's helpers for reading an
#: MCP server's resources. The bridge serves none, and a call to them is still
#: refused as Codex's own tool.
_CODEX_MCP_HELPERS = {"list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"}


@pytest.mark.skipif(not shutil.which("codex"), reason="Codex is not installed")
def test_codex_is_offered_the_agents_tools_through_the_bridge(capture, seeded, tmp_path):
    """F753: the installed Codex starts the bridge and offers the agent's tools as its own."""
    catalog = _codex_catalog()
    model = next((m["slug"] for m in (catalog or {}).get("models", []) if m.get("visibility") == "list"), "gpt-5.4-mini")
    tools = [{"type": "function", "function": {"name": "get_order", "description": "Get an order by id.",
              "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}}}]
    (seeded["home"] / ".codex").mkdir(exist_ok=True)
    environ = {"PATH": shutil.os.environ["PATH"], "HOME": str(seeded["home"]),
               "CODEX_HOME": str(seeded["home"] / ".codex"), "CAPTURE_KEY": "capture-only"}
    workdir = new_workdir(tmp_path)
    inv = build_cli_invocation("cli.codex", model, workdir=workdir, system_prompt="SYSTEM", prompt=_prompt(seeded),
                               environ=environ, codex_catalog=catalog, tools=tools)
    provider = ("model_providers.capture={name=\"capture\",base_url=\"" + capture.url
                + "/v1\",wire_api=\"responses\",env_key=\"CAPTURE_KEY\"}")
    inv.argv[2:2] = ["-c", provider, "-c", "model_provider=\"capture\""]
    done = _run(inv)

    sent = capture.model_requests("/responses")
    assert sent, f"Codex sent no request: {done.stderr.decode(errors='replace')[-600:]}"
    offered = _codex_tools(sent[0])
    assert "get_order" in {n.split(".")[-1] for n in offered}, offered
    others = {n.split(".")[-1] for n in offered} - {"get_order"}
    assert others <= _CODEX_LEFT | _CODEX_MCP_HELPERS, others
