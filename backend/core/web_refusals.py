"""Why the page a fetcher got is not the page the person asked for (F1632).

The scraper and Knowledge's crawl both follow redirects, so a private page arrives as the site's sign-in page, and
both handed that sign-in page back as the page asked for: a run summarised a login form as someone's settings, and a
knowledge base indexed it. A sign-in wall is named here once, for every fetcher, in words that say what happened.

The rule reads addresses and the status only, never the page's words or a password field, so a page that merely talks
about signing in, or links to its sign-in page, is never taken for a wall.
"""
from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit

#: Path segments only a sign-in page uses: one of them alone marks the page.
_SIGN_IN = frozenset({"login", "log-in", "logon", "signin", "sign-in", "sign_in", "signon"})
#: Segments a sign-in page shares with other pages ("/docs/auth/…"): they mark one only beside a return address.
_SIGN_IN_WITH_RETURN = frozenset({"session", "sessions", "auth", "authenticate", "authorize", "sso", "account",
                                  "accounts", "users"})
#: The query names a sign-in page carries the page to come back to in.
_RETURN = frozenset({"return_to", "returnto", "return", "returnurl", "return_url", "next", "redirect", "redirect_to",
                     "redirect_uri", "redirect_url", "continue", "destination", "dest", "goto", "service", "from"})


def is_sign_in_page(url: str) -> bool:
    """Whether `url` is a sign-in page by its address: a sign-in segment, or a shared one beside a return address."""
    parts = urlsplit(url)
    segments = [s for s in parts.path.lower().split("/") if s]
    if any(s in _SIGN_IN for s in segments):
        return True
    returns = any(k.lower() in _RETURN for k, _ in parse_qsl(parts.query, keep_blank_values=True))
    return returns and any(s in _SIGN_IN_WITH_RETURN for s in segments)


def sign_in_wall(asked: str, landed: str, status: int | None) -> str | None:
    """Why `asked` came back as a sign-in wall, or None.

    A 401 is one whatever the address. Otherwise it is one when the read landed on a sign-in page although the person
    asked for another page; a sign-in page asked for by its own address is the page asked for."""
    host = urlsplit(asked).hostname or "the site"
    if status == 401:
        return f"Needs a sign-in: {host} answered 401 and shows this page only to someone signed in."
    if is_sign_in_page(asked) or not is_sign_in_page(landed):
        return None
    where = urlsplit(landed)
    page = f"{where.hostname or host}{where.path}" if where.hostname != urlsplit(asked).hostname else where.path
    return f"Needs a sign-in: {host} sent this page to its sign-in page ({page or '/'})."
