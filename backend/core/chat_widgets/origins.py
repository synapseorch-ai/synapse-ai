"""
Which sites may embed a widget.

This is the credential. The public key says *which* widget; this says whether
the page asking for it is allowed to have it, and it is the only thing standing
between a scraped key and somebody else's model bill.

Two enforcement points, deliberately, because they fail in opposite ways:

* **`Content-Security-Policy: frame-ancestors`**, set on the embed page. The
  *browser* refuses to paint the frame, so a key pasted onto a disallowed site
  shows nothing at all — no request is even made. This is the strong one: it
  cannot be spoofed by the embedding page, because the page is not the one
  enforcing it.
* **The `Referer` check here**, run before a token is minted. This is the one
  that catches a scraped key used outside a browser, where no CSP applies.

Neither is sufficient alone. A browser with CSP disabled defeats the first; a
forged `Referer` defeats the second; nothing defeats both.

**Matching is exact, on origin.** Scheme, host and port, compared as strings
after normalisation — never a substring or a suffix test. `https://acme.com` does
not admit `https://acme.com.evil.net`, and `https://acme.com` is not
`http://acme.com`. The one wildcard allowed is a single leading `*.` for
subdomains, which matches labels and not dots, so `*.acme.com` admits
`https://shop.acme.com` and refuses `https://evil.net/?x=.acme.com`.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

#: Localhost is allowed to be http; anything else must be https. A customer
#: testing against their dev server is the ordinary case, and refusing it would
#: send them to paste their key somewhere public to try it.
_LOCAL_HOSTS = frozenset(("localhost", "127.0.0.1", "[::1]", "::1"))

#: A hostname, checked rather than assumed. `urlsplit` is a parser, not a
#: validator: it will hand back `"not a url"` as a hostname, spaces and all. An
#: allow-list holding a value no browser can ever send is worse than an empty one
#: — it reads as configured and admits nothing, and the owner has no way to tell.
#: Labels of alphanumerics and hyphens, hyphens not at either end, or an IPv6
#: literal in brackets.
_HOST_RE = re.compile(
    r"^(?:\[[0-9a-f:]+\]|(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*)$"
)


def normalise(value: str | None) -> str | None:
    """`https://Acme.com:443/path?q` → `https://acme.com`. `None` if unusable.

    A port that is the scheme's default is dropped, so a customer who writes
    `https://acme.com:443` and a browser that sends `https://acme.com` agree.
    """
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or raw == "null":
        # `Origin: null` is what a sandboxed iframe or a `file://` page sends.
        # It is not an origin and must never match one.
        return None
    if "//" not in raw:
        raw = f"https://{raw}"

    try:
        parts = urlsplit(raw)
    except ValueError:
        return None

    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host or not _HOST_RE.match(host):
        return None
    if scheme == "http" and host not in _LOCAL_HOSTS:
        return None

    try:
        port = parts.port
    except ValueError:
        return None
    default = 443 if scheme == "https" else 80
    if port is None or port == default:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def normalise_pattern(value: str | None) -> str | None:
    """Like `normalise`, but keeps a single leading `*.` label."""
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if raw.startswith("*."):
        rest = normalise(raw[2:])
        return None if rest is None else _reinsert_star(rest)
    return normalise(raw)


def _reinsert_star(origin: str) -> str:
    scheme, _, hostport = origin.partition("://")
    return f"{scheme}://*.{hostport}"


def clean_list(values: object) -> list[str]:
    """The stored allow-list: normalised, de-duplicated, order kept.

    Anything unparseable is dropped rather than stored, so a typo cannot sit in
    the list looking like it works.
    """
    if not isinstance(values, (list, tuple)):
        return []
    out: list[str] = []
    for value in values:
        pattern = normalise_pattern(value if isinstance(value, str) else None)
        if pattern and pattern not in out:
            out.append(pattern)
    return out


def matches(origin: str | None, allowed: object) -> bool:
    """Whether `origin` is admitted by the allow-list.

    An **empty allow-list admits nothing.** That is the opposite of the usual
    "unset means unrestricted" convention in this codebase, and it is deliberate:
    every other such default guards a workspace the caller already reached, while
    this one faces the open internet. A widget nobody has configured is a widget
    nobody can embed.
    """
    candidate = normalise(origin)
    if candidate is None:
        return False

    scheme, _, host = candidate.partition("://")
    for pattern in clean_list(allowed):
        if pattern == candidate:
            return True
        p_scheme, _, p_host = pattern.partition("://")
        if not p_host.startswith("*.") or p_scheme != scheme:
            continue
        # `*.acme.com` matches one-or-more labels in front, and matching is on
        # the dot boundary — so it admits `shop.acme.com` and never `acme.com`
        # itself, nor `evilacme.com`.
        suffix = p_host[1:]                     # ".acme.com"
        if host.endswith(suffix) and len(host) > len(suffix):
            return True
    return False


def frame_ancestors(allowed: object) -> str:
    """The CSP directive for an embed page.

    `'none'` when the list is empty, which is what makes an unconfigured widget
    unembeddable rather than universally embeddable.
    """
    patterns = clean_list(allowed)
    if not patterns:
        return "frame-ancestors 'none'"
    return "frame-ancestors " + " ".join(patterns)
