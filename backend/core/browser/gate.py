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

import re
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


async def verdict(url: str, method: str, mode: str, *, sites: list[str] | None = None) -> str | None:
    """Why this request must not leave the browser, or None to let it go.

    `sites`, given for the agent's top-level navigation only, is the agent's scope:
    a page elsewhere is refused, redirects included. Never fails open: a
    resolution failure or a bug here refuses the request.
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
    if sites and not site_allowed(host, sites):
        return f"{host} is not one of this agent's sites"
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


def site_allowed(host: str, sites: list[str]) -> bool:
    """`host` is one of `sites` ("portal.example.com", or "*.example.com" for it and its subdomains)."""
    host = (host or "").lower().rstrip(".")
    for site in sites:
        if site.startswith("*."):
            base = site[2:]
            if host == base or host.endswith("." + base):
                return True
        elif host == site:
            return True
    return False


def _site_of(host: str) -> str:
    """The registrable part, near enough: the last two labels (three under a two-letter country's
    second level, as in example.co.uk)."""
    labels = (host or "").lower().rstrip(".").split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in {"co", "com", "net", "org", "gov", "ac", "edu"}:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def same_site(a: str, b: str) -> bool:
    return bool(a) and bool(b) and _site_of(a) == _site_of(b)


def is_send(method: str, url: str, *, navigation: bool, page_url: str) -> bool:
    """A request that changes something: a form submission anywhere, or any non-read to the page's own
    site. A third-party beacon is not one; the page's own scripts already send what they will."""
    if method.upper() in _READS:
        return False
    if navigation:
        return True
    return same_site(urlparse(url).hostname or "", urlparse(page_url).hostname or "")


def send_key(method: str, url: str) -> tuple[str, str]:
    """What "don't ask again for this address" remembers: the method and the address without its query."""
    parsed = urlparse(url)
    return method.upper(), f"{parsed.scheme}://{(parsed.hostname or '').lower()}{parsed.path or '/'}"


#: Tools that put the agent's text into a page.
TYPES_TEXT = frozenset({"browser_type", "browser_fill_form", "browser_press_key", "browser_select_option"})

#: Frames that are a CAPTCHA or a bot check: the person's to solve (DESIGN.md §3.6).
CAPTCHA_HOSTS = ("google.com/recaptcha", "recaptcha.net", "hcaptcha.com", "challenges.cloudflare.com")


def is_captcha_frame(url: str) -> bool:
    return any(marker in (url or "") for marker in CAPTCHA_HOSTS)


#: What makes a field the person's: a password, a card, a one-time code (DESIGN.md §2.4).
SENSITIVE_AUTOCOMPLETE = ("cc-", "one-time-code", "current-password", "new-password")


#: What the box says when an answer holds a card number (DESIGN.md §2.4, F1100).
CARD_WORDS = "Card details go in the browser; you'll be asked there."

_CARD_RUN = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


def looks_like_card(text: str) -> bool:
    """A 13–19 digit run (spaces or dashes allowed) that passes the Luhn check: a payment card's number."""
    for match in _CARD_RUN.finditer(text or ""):
        digits = [int(c) for c in match.group(0) if c.isdigit()]
        if not 13 <= len(digits) <= 19:
            continue
        total = 0
        for i, d in enumerate(reversed(digits)):
            if i % 2:
                d *= 2
                d -= 9 if d > 9 else 0
            total += d
        if total % 10 == 0:
            return True
    return False


def strings_in(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings_in(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in strings_in(v)]
    return []


def is_sensitive_field(input_type: str, autocomplete: str) -> bool:
    if (input_type or "").lower() == "password":
        return True
    tokens = (autocomplete or "").lower().split()
    return any(t.startswith(SENSITIVE_AUTOCOMPLETE) for t in tokens)
