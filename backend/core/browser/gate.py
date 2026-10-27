"""What the browser's agent may do, and what its pages may send.

Two gates, both code, so neither depends on what the model was told:

* **Tools.** Playwright MCP sits behind the component; only `ALLOWED` reaches it.
  `browser_evaluate` and `browser_run_code_unsafe` run arbitrary code (Playwright's
  own docs call the second RCE-equivalent), the file, storage, network and devtools
  tools reach past the page, and screenshots are the component's own.
* **The network.** Every request of every page passes `verdict`: http(s) only, a
  public address only (`core.egress_guard`, the scraper's rule), and in read-only
  mode nothing that changes state. The check sits on the browser's own network
  layer, so redirects, scripts and in-page fetches pass it too. A page's WebSocket
  passes `socket_verdict`, its own check, before it opens.
"""
from __future__ import annotations

from urllib.parse import urlparse

from core.egress_guard import check_host

#: Tools the agent may call, with the tags the component vouches for (TAG1).
#: Clicking and typing are not read-only, but what one click *does* is decided at
#: the network, not by a tag.
ALLOWED: dict[str, dict[str, bool]] = {
    "browser_navigate": {"read_only": True, "open_world": True},
    "browser_navigate_back": {"read_only": True, "open_world": True},
    "browser_snapshot": {"read_only": True, "open_world": True, "idempotent": True},
    "browser_find": {"read_only": True, "open_world": True, "idempotent": True},
    "browser_wait_for": {"read_only": True, "open_world": True, "idempotent": True},
    "browser_tabs": {"read_only": True, "open_world": True},
    "browser_hover": {"read_only": True, "open_world": True},
    "browser_click": {"open_world": True},
    "browser_type": {"open_world": True},
    "browser_fill_form": {"open_world": True},
    "browser_select_option": {"open_world": True},
    "browser_press_key": {"open_world": True},
    "browser_handle_dialog": {"open_world": True},
}

#: Actions after which the page may look different: each gets a screenshot.
CHANGES_PAGE = frozenset(ALLOWED) - {"browser_snapshot", "browser_find", "browser_wait_for"}

#: Methods that read. Anything else changes state somewhere.
_READS = frozenset({"GET", "HEAD", "OPTIONS"})

READ_ONLY = "read_only"
CO_DRIVE = "co_drive"
MODES = frozenset({READ_ONLY, CO_DRIVE})


def refusal(name: str) -> str | None:
    """Why the agent may not call `name` here, or None when it may."""
    if name in ALLOWED:
        return None
    return f"{name} is not available in Synapse's browser."


async def verdict(url: str, method: str, mode: str) -> str | None:
    """Why this request must not leave the browser, or None to let it go.

    Never fails open: a resolution failure or a bug here refuses the request.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return "a malformed address"
    if parsed.scheme not in {"http", "https"}:
        return f"{parsed.scheme or 'a schemeless'} address"
    host = parsed.hostname or ""
    try:
        await check_host(host)
    except Exception:
        return f"{host or 'that host'} is not a public address"
    if mode != CO_DRIVE and method.upper() not in _READS:
        return f"a {method.upper()} to {host}: this browser is read-only"
    return None


async def socket_verdict(url: str, mode: str) -> str | None:
    """Why a page's WebSocket must not open, or None to let it (F1122).

    Once open, a socket carries anything both ways with no request to check, so a
    read-only browser opens none, and co-drive opens one only to a public address.
    Never fails open, as `verdict`.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return "a malformed address"
    if parsed.scheme not in {"ws", "wss"}:
        return f"{parsed.scheme or 'a schemeless'} socket"
    host = parsed.hostname or ""
    try:
        await check_host(host)
    except Exception:
        return f"{host or 'that host'} is not a public address"
    if mode != CO_DRIVE:
        return f"a WebSocket to {host}: this browser is read-only"
    return None
