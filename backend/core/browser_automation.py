"""What the Browser Automation server may be asked to do.

The server is Playwright's MCP server (`@playwright/mcp`), started by `core/server.py`, on by default, and called
by agents that read web pages, so what a page says can steer it. Its tools are offered through `gated()`:

* **Only the tools reviewed for the pinned version.** None that run code in the server's own process
  (`browser_run_code_unsafe`; Playwright's own description calls it RCE-equivalent), run a model's script in the
  page (`browser_evaluate`), or read local files into a page (`browser_file_upload`, `browser_drop`). A tool a
  later version adds is not offered until someone reviews it and adds it here.
* **A `url` is http(s)**, or `about:blank`: never `file:`, `chrome:`, `data:` and the like.
* **A `filename` stays in the server's folder**: relative, no `..`. The server is started with that folder as its
  working directory and output directory, so it is where its own files land.

Where the browser may connect is not decided here: its network goes through the egress proxy
(`core.egress_proxy`). BRW1's browser component replaces this server and its gate (`core/browser/gate.py`).
"""
from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

#: The server's name in `core/server.py`'s list, and the package it runs, pinned: `npx -y @playwright/mcp` ran
#: whatever was newest the first time, and a new version's new tools reached agents unreviewed.
BROWSER_AUTOMATION = "Browser Automation"
PLAYWRIGHT_MCP = "@playwright/mcp@0.0.83"

#: The tools of the pinned version that are offered. Reviewed 2026-10-28 against what 0.0.83 lists.
OFFERED = frozenset({
    "browser_navigate", "browser_navigate_back", "browser_tabs", "browser_snapshot", "browser_find",
    "browser_wait_for", "browser_take_screenshot", "browser_console_messages", "browser_network_requests",
    "browser_network_request", "browser_click", "browser_type", "browser_fill_form", "browser_select_option",
    "browser_press_key", "browser_hover", "browser_drag", "browser_handle_dialog", "browser_resize",
    "browser_emulate_media", "browser_close",
})

_URL_ARGS = ("url",)
_FILE_ARGS = ("filename",)


def refusal(name: str, arguments: dict | None) -> str | None:
    """Why this call is not made, or None when it may be."""
    if name not in OFFERED:
        return f"{name} is not available in Synapse's browser."
    args = arguments or {}
    for key in _URL_ARGS:
        url = args.get(key)
        if url is None:
            continue
        scheme = urlsplit(str(url)).scheme.lower()
        if scheme not in ("http", "https") and str(url) != "about:blank":
            return f"{name} opens http and https addresses only."
    for key in _FILE_ARGS:
        filename = args.get(key)
        if filename is None:
            continue
        text = str(filename)
        if (text.startswith(("/", "\\", "~")) or PureWindowsPath(text).drive
                or ".." in PurePosixPath(text.replace("\\", "/")).parts):
            return f"{name} saves only inside its own folder: give a file name, not a path."
    return None


class _Gated:
    """The server's session, offering only `OFFERED` and refusing what `refusal` refuses. Anything else (a ping,
    initialize) is the session's own."""

    def __init__(self, session: Any) -> None:
        self._session = session

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    async def list_tools(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._session.list_tools(*args, **kwargs)
        return result.model_copy(update={"tools": [t for t in result.tools if t.name in OFFERED]})

    async def call_tool(self, name: str, arguments: dict | None = None, *args: Any, **kwargs: Any) -> Any:
        reason = refusal(name, arguments)
        if reason is not None:
            import mcp.types as types

            return types.CallToolResult(content=[types.TextContent(type="text", text=reason)], isError=True)
        return await self._session.call_tool(name, arguments, *args, **kwargs)


def gated(server_name: str, session: Any) -> Any:
    """`session`, gated when it is the Browser Automation server's; any other server's as it is."""
    if server_name != BROWSER_AUTOMATION or isinstance(session, _Gated):
        return session
    return _Gated(session)
