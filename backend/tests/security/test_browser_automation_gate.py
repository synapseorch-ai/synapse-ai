"""The Browser Automation server (`@playwright/mcp`, `core/server.py`) is pinned, gated, and goes through the egress
proxy.

It is on by default and its tools are called by an agent that reads pages, so what a page says can steer them. The
gate (`core.browser_automation`) offers only the tools that were reviewed for this version, and none that run code
in the server process, read local files or upload them; a `url` it is handed is http(s), and a `filename` stays
in the server's own folder, which is also its working directory. The browser's network is the egress proxy's
(`core.egress_proxy`). BRW1's browser component replaces the server; until then it is this.
"""
from __future__ import annotations

import inspect

import mcp.types as types
import pytest

from core.browser_automation import BROWSER_AUTOMATION, PLAYWRIGHT_MCP, gated

#: Every tool `@playwright/mcp@0.0.83` lists by default (read from the server itself, 2026-10-28).
LISTED_BY_0_0_83 = {
    "browser_close", "browser_resize", "browser_console_messages", "browser_handle_dialog",
    "browser_emulate_media", "browser_evaluate", "browser_file_upload", "browser_drop", "browser_find",
    "browser_fill_form", "browser_press_key", "browser_type", "browser_navigate", "browser_navigate_back",
    "browser_network_requests", "browser_network_request", "browser_run_code_unsafe", "browser_take_screenshot",
    "browser_snapshot", "browser_click", "browser_drag", "browser_hover", "browser_select_option", "browser_tabs",
    "browser_wait_for",
}
NEVER = {"browser_run_code_unsafe", "browser_evaluate", "browser_file_upload", "browser_drop"}


class FakeSession:
    def __init__(self, names):
        self.names = names
        self.calls: list[tuple[str, dict]] = []
        self.pinged = False

    async def list_tools(self):
        return types.ListToolsResult(tools=[types.Tool(name=n, inputSchema={"type": "object"}) for n in self.names])

    async def call_tool(self, name, arguments=None, **kwargs):
        self.calls.append((name, arguments))
        return types.CallToolResult(content=[types.TextContent(type="text", text="ran")])

    async def send_ping(self):
        self.pinged = True


@pytest.fixture
def session():
    return FakeSession(sorted(LISTED_BY_0_0_83 | {"browser_some_tool_a_later_version_adds"}))


def test_the_package_is_pinned_to_the_reviewed_version():
    assert PLAYWRIGHT_MCP == "@playwright/mcp@0.0.83"


async def test_it_lists_only_reviewed_tools_and_none_that_run_code_or_touch_local_files(session):
    listed = {t.name for t in (await gated(BROWSER_AUTOMATION, session).list_tools()).tools}
    assert listed == LISTED_BY_0_0_83 - NEVER


@pytest.mark.parametrize("name", sorted(NEVER) + ["browser_some_tool_a_later_version_adds"])
async def test_a_tool_it_does_not_offer_is_refused_without_reaching_the_server(session, name):
    result = await gated(BROWSER_AUTOMATION, session).call_tool(name, {"function": "() => 1", "paths": ["/etc/passwd"]})
    assert result.isError and "not available" in result.content[0].text
    assert session.calls == []


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "file:///home/someone/.ssh/id_rsa", "chrome://settings", "javascript:alert(1)",
    "view-source:http://example.com", "data:text/html,<script>fetch('http://127.0.0.1')</script>", "ftp://x/",
])
@pytest.mark.parametrize("tool", ["browser_navigate", "browser_tabs"])
async def test_a_url_that_is_not_http_is_refused(session, tool, url):
    result = await gated(BROWSER_AUTOMATION, session).call_tool(tool, {"url": url, "action": "new"})
    assert result.isError
    assert session.calls == []


@pytest.mark.parametrize("url", ["https://example.com/", "http://example.com/a?b=1", "about:blank"])
async def test_an_http_url_goes_through(session, url):
    result = await gated(BROWSER_AUTOMATION, session).call_tool("browser_navigate", {"url": url})
    assert not result.isError
    assert session.calls == [("browser_navigate", {"url": url})]


@pytest.mark.parametrize("filename", ["/home/someone/.bashrc", "../../.bashrc", "shots/../../x.png", "~/.bashrc",
                                      "C:\\Users\\x\\a.png", "\\\\server\\share\\a.png"])
async def test_a_filename_outside_the_servers_folder_is_refused(session, filename):
    result = await gated(BROWSER_AUTOMATION, session).call_tool("browser_take_screenshot", {"filename": filename})
    assert result.isError
    assert session.calls == []


@pytest.mark.parametrize("filename", ["page.png", "shots/page.png", "console.log"])
async def test_a_filename_in_its_folder_goes_through(session, filename):
    result = await gated(BROWSER_AUTOMATION, session).call_tool("browser_take_screenshot", {"filename": filename})
    assert not result.isError
    assert session.calls == [("browser_take_screenshot", {"filename": filename})]


async def test_the_session_answers_everything_else_as_before(session):
    g = gated(BROWSER_AUTOMATION, session)
    await g.send_ping()
    assert session.pinged


def test_another_servers_session_is_left_as_it_is(session):
    assert gated("Google Workspace", session) is session


async def test_gating_twice_is_gating_once(session):
    once = gated(BROWSER_AUTOMATION, session)
    assert gated(BROWSER_AUTOMATION, once) is once


# ── How the backend starts it ────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
async def browser_spec(monkeypatch, tmp_path):
    import core.server as server
    from core import egress_proxy

    async def no_google():
        return {}

    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(server, "load_settings", lambda: {"browser_automation_enabled": True})
    monkeypatch.setattr(server, "_get_google_oauth_env", no_google)

    async def spec():
        servers = {s["name"]: s for s in await server._build_native_mcp_servers()}
        return servers[BROWSER_AUTOMATION]

    yield spec
    await (await egress_proxy.shared()).close()


def _arg(args, flag):
    return args[args.index(flag) + 1]


async def test_it_runs_the_pinned_package(browser_spec):
    args = (await browser_spec())["args"]
    assert PLAYWRIGHT_MCP in args
    assert "@playwright/mcp" not in args  # never the unpinned name beside it


async def test_its_browser_goes_through_synapses_egress_proxy(browser_spec):
    from core import egress_proxy

    spec = await browser_spec()
    assert _arg(spec["args"], "--proxy-server") == (await egress_proxy.shared()).url


async def test_a_hosts_own_proxy_comes_first(browser_spec, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://egress-proxy:4750")
    spec = await browser_spec()
    assert _arg(spec["args"], "--proxy-server") == "http://egress-proxy:4750"


async def test_it_works_in_its_own_folder_not_the_home_directory(browser_spec):
    from core.vault import _vault_root

    spec = await browser_spec()
    assert spec["cwd"] == str(_vault_root() / "playwright")
    assert _arg(spec["args"], "--output-dir") == spec["cwd"]


def test_the_backend_spawns_it_in_that_folder_and_registers_the_gated_session():
    """The spawn loop honours a server's `cwd`, and the session it stores and lists from is the gated one."""
    import core.server as server

    source = inspect.getsource(server.lifespan)
    assert 'cwd=mcp_cfg.get("cwd") or str(Path.home())' in source
    gate = source.index("session = gated(mcp_name, session)")
    assert gate < source.index("agent_sessions[mcp_name] = session")
    assert gate < source.index("tools = await session.list_tools()", gate)
