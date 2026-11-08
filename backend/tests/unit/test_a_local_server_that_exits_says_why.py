"""A local (stdio) MCP server that exits as it starts says why — the last line it wrote — F1461 (ISS1, GitHub #377).

#377, 2.0.0-rc2: the commonest first-run failure, an `npx` server missing its token, printed
`bad config: GITHUB_TOKEN is not set` to its stderr and exited. The card said "Connection closed" (or "Config saved.
Use Retry to reconnect."), and Retry "Could not reconnect: Connection closed." or "…: BrokenResourceError." — the
line that explained it went to the backend's own stderr: `stdio_client(params)` had no `errlog` (mcp_client.py), and
the reason fell back to the exception's text.

The child's stderr is now kept per connect, and a connect that fails takes its last non-empty line as the reason,
through the one wording every view reads (`connect_failure` → `last_error`, F1306): the add's answer, Retry's, the
server list. A value from the server's own env (a token it was given) never shows in it. A server that writes to
stderr and starts is connected as before.
"""
from __future__ import annotations

import sys
import textwrap
from contextlib import AsyncExitStack

import pytest

from core.mcp_client import MCPClientManager

SAID = "bad config: GITHUB_TOKEN is not set"
SECRET = "ghp_test0value0not0real0123456789"


@pytest.fixture
async def manager(monkeypatch):
    import core.server as server
    from core import mcp_client

    monkeypatch.setattr(mcp_client, "stdio_mcp_allowed", lambda: True)
    stack = AsyncExitStack()
    mgr = MCPClientManager(stack)
    monkeypatch.setattr(server, "mcp_manager", mgr)
    try:
        yield mgr
    finally:
        await stack.aclose()


def _exits_saying(*lines: str) -> list[str]:
    body = "import sys\n" + "".join(f"sys.stderr.write({line!r} + '\\n')\n" for line in lines) + "sys.exit(1)\n"
    return ["-c", body]


def _crashy(name: str = "crashy", *lines: str, env: dict | None = None) -> dict:
    return {"name": name, "label": "Crashy", "server_type": "stdio", "command": sys.executable,
            "args": _exits_saying(*(lines or (SAID,))), "env": env or {}}


class TestItSaysWhy:
    async def test_the_add_says_the_line_it_wrote(self, client, manager):
        resp = await client.post("/api/mcp/servers", json=_crashy())

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["connected"] is False
        assert SAID in body["message"], body["message"]
        assert manager.last_error("crashy") == SAID

    async def test_retry_and_the_list_say_the_same(self, client, manager):
        await client.post("/api/mcp/servers", json=_crashy())

        retried = (await client.post("/api/mcp/servers/crashy/reconnect")).json()
        assert retried["connected"] is False
        assert SAID in retried["message"], retried["message"]
        [listed] = (await client.get("/api/mcp/servers")).json()
        assert listed["last_error"] == SAID

    async def test_the_last_line_is_the_reason_not_the_first(self, client, manager):
        await client.post("/api/mcp/servers", json=_crashy("chatty_crash", "starting up…", "loading config", SAID, ""))

        assert manager.last_error("chatty_crash") == SAID

    async def test_a_long_line_is_cut_to_a_readable_length(self, client, manager):
        await client.post("/api/mcp/servers", json=_crashy("long_crash", "x" * 5000))

        said = manager.last_error("long_crash") or ""
        assert said.startswith("xxxx") and len(said) <= 300, said[:80]

    async def test_a_value_from_its_own_env_never_shows(self, client, manager):
        resp = await client.post("/api/mcp/servers", json=_crashy(
            "leaky_crash", f"token {SECRET} was refused", env={"GITHUB_TOKEN": SECRET}))

        # Its reason, wherever it is said: the add's message, the config the page gets, the manager's. (The config
        # carries the command's own args, which here are this test's script, the token in it.)
        body = resp.json()
        assert SECRET not in body["message"] and SECRET not in (body["config"].get("last_error") or ""), body
        assert SECRET not in (manager.last_error("leaky_crash") or "")
        assert "was refused" in (manager.last_error("leaky_crash") or "")


class TestAServerThatStarts:
    async def test_writing_to_stderr_does_not_stop_it_connecting(self, client, manager, tmp_path):
        server = tmp_path / "talks_then_serves.py"
        server.write_text(textwrap.dedent("""
            import sys
            from mcp.server.fastmcp import FastMCP
            sys.stderr.write("starting: a line on stderr is not a failure\\n" * 200)
            app = FastMCP("talks")

            @app.tool()
            def two_texts() -> str:
                return "a b"

            app.run()
        """))
        resp = await client.post("/api/mcp/servers", json={
            "name": "talks", "label": "Talks", "server_type": "stdio", "command": sys.executable,
            "args": ["-I", str(server)], "env": {}})

        assert resp.status_code == 200, resp.text
        assert resp.json()["connected"] is True, resp.text
        assert not manager.last_error("talks")
