"""A web source's HTTP side: who we say we are, what robots.txt allows, how fast, and what came back (S1, D165).

**One client per crawl, httpx's defaults.** `trust_env` stays on and no
`transport=` is passed, so the environment's proxies apply (an explicit
transport would turn them off). Every hop — robots.txt and sitemaps included —
goes through `core.safe_fetch.fetch`, which runs the egress guard on each address.

**Cloud: through the egress proxy, or not at all** (S1c, D181; Naveen's call 3).
`set_proxy_required(True)` makes every client take the environment's HTTP and
HTTPS proxies *explicitly*, so `NO_PROXY` — which names the platform's own
hosts (S3, the internal namespace, `localhost`) — never applies to a customer's
URL; with either proxy missing, nothing is fetched and the crawl or document
says so (`egress_refusal`). I1's Smokescreen then refuses, after resolving the
name, any address that is not public: the in-process guard's second layer.

**SynapseBot** (Naveen, D165): `USER_AGENT` on every request; robots.txt rules
for `SynapseBot` apply, else those for `*`. robots.txt is read per host per
crawl and matched as RFC 9309 says: the longest matching rule wins, `Allow`
on a tie, `*` and `$` in patterns. A robots.txt that answers 4xx allows
everything; one that cannot be read (5xx, no answer) disallows everything.

**Politeness.** One request at a time per host, at most `rps` per second, and
slower when robots.txt asks (`Crawl-delay`, honoured up to `MAX_CRAWL_DELAY_S`).

**Conditional GET.** A document's `etag` column keeps its page's validator:
the `ETag`, or `lm:<Last-Modified>` when the server sends no ETag — never the
blob's mtime (`source_modified_at`, which reconcile compares).

**What came back.** HTML is decoded by its declared charset (the header, then
the page's own `<meta>`, then a sniff) and read twice: once for its content
(A2's `parse.html.html_to_markdown`, which keeps only the content root's
links) and once here, over the whole document, for the links a crawl follows —
a docs site's pages are linked from its navigation, which the content leaves
out. `rel="nofollow"`, a `nofollow` meta robots or `X-Robots-Tag`, and
`noindex` are honoured.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urljoin, urlsplit

from core.log import get_logger

if TYPE_CHECKING:  # pragma: no cover
    import httpx

    from core.safe_fetch import Fetched

_log = get_logger(__name__)

BOT = "SynapseBot"
BOT_VERSION = "1.0"
USER_AGENT = f"Mozilla/5.0 (compatible; {BOT}/{BOT_VERSION}; +https://synapseorch.com/bot/)"

MIB = 1024 * 1024
#: An HTML page larger than this is not read: its text would be cut anyway, and parsing it costs memory.
HTML_MAX_BYTES = 5 * MIB
#: RFC 9309 asks a crawler to read at least 500 KiB of robots.txt.
ROBOTS_MAX_BYTES = 512 * 1024
#: The longest `Crawl-delay` honoured; a site asking for more is crawled at this pace.
MAX_CRAWL_DELAY_S = 30.0
#: One request's whole deadline: every hop, headers and body.
REQUEST_TIMEOUT_S = 60.0

HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})


#: What a crawl or a fetched document says when this deployment requires a proxy and has none.
NO_PROXY_MESSAGE = ("This deployment fetches web pages only through its egress proxy, and none is configured; "
                    "nothing was fetched.")

_proxy_required = False


def set_proxy_required(required: bool) -> None:
    """Cloud's knowledge worker: every web fetch goes through the environment's egress proxy, or none is made."""
    global _proxy_required
    _proxy_required = required


def _environment_proxies() -> tuple[str, str] | None:
    """The environment's (http, https) proxies — both, or None. Both cases of the names, as httpx reads them."""
    import urllib.request

    found = urllib.request.getproxies_environment()
    http, https = found.get("http"), found.get("https")
    return (http, https) if http and https else None


def egress_refusal() -> str | None:
    """Why no web page may be fetched here, or None: a proxy is required and the environment names none."""
    if _proxy_required and _environment_proxies() is None:
        return NO_PROXY_MESSAGE
    return None


class EgressUnavailable(Exception):
    """A proxy is required and none is configured: nothing is fetched."""


class RobotsDisallowed(Exception):
    """robots.txt disallows the URL for SynapseBot. A refusal, not a failure."""

    def __init__(self, url: str):
        super().__init__(f"The site's robots.txt disallows {url} for {BOT}.")
        self.url = url


class RobotsUnreachable(Exception):
    """The host's robots.txt could not be read (a server error, no answer), so nothing on it is fetched
    now (RFC 9309). A failure, not a refusal: it says nothing about what the site allows, so pages
    already held are kept."""

    def __init__(self, url: str):
        super().__init__(f"The site's robots.txt could not be read, so {url} was not fetched; it is tried again "
                         "at the next crawl.")
        self.url = url


# ── robots.txt (RFC 9309) ──────────────────────────────────────────────────────

@dataclass
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[bool, str]] = field(default_factory=list)  # (allow, pattern)
    crawl_delay: float | None = None


@dataclass
class Robots:
    """One host's robots.txt, as it applies to SynapseBot."""

    rules: list[tuple[bool, str]] = field(default_factory=list)
    crawl_delay: float | None = None
    disallow_all: bool = False
    sitemaps: list[str] = field(default_factory=list)
    unreachable_: bool = False  # disallow_all because it could not be read, not because the site said so

    @classmethod
    def allow_all(cls) -> Robots:
        return cls()

    @classmethod
    def unreachable(cls) -> Robots:
        return cls(disallow_all=True, unreachable_=True)

    @classmethod
    def parse(cls, text: str) -> Robots:
        groups: list[_Group] = []
        current: _Group | None = None
        last_was_agent = False
        sitemaps: list[str] = []
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if current is None or not last_was_agent:
                    current = _Group()
                    groups.append(current)
                current.agents.append(value.lower())
                last_was_agent = True
                continue
            last_was_agent = False
            if key == "sitemap" and value:
                sitemaps.append(value)
            elif current is None:
                continue
            elif key in ("allow", "disallow"):
                if value:  # an empty Disallow allows everything: no rule at all
                    current.rules.append((key == "allow", value))
            elif key == "crawl-delay":
                try:
                    current.crawl_delay = max(0.0, float(value))
                except ValueError:
                    pass
        token = BOT.lower()  # matched case-insensitively against the product token ("SynapseBot/1.0" too)
        mine = [g for g in groups if any(a.split("/", 1)[0].strip() == token for a in g.agents)]
        chosen = mine or [g for g in groups if "*" in g.agents]  # every matching group's rules, combined
        rules = [rule for g in chosen for rule in g.rules]
        delays = [g.crawl_delay for g in chosen if g.crawl_delay is not None]
        return cls(rules=rules, crawl_delay=max(delays) if delays else None, sitemaps=sitemaps)

    def allows(self, url: str) -> bool:
        if self.disallow_all:
            return False
        parts = urlsplit(url)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        if target == "/robots.txt":
            return True
        best: tuple[int, bool] | None = None
        for allow, pattern in self.rules:
            if _matches(pattern, target):
                key = (len(pattern), allow)  # the longest pattern wins; on a tie, Allow
                if best is None or key > best:
                    best = key
        return True if best is None else best[1]


def _matches(pattern: str, target: str) -> bool:
    if not pattern:
        return True
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.match(regex + ("$" if anchored else ""), target) is not None


# ── pace ───────────────────────────────────────────────────────────────────────

class HostPacer(Protocol):
    """Spaces requests to one host, `interval` seconds apart (at least), across every crawl that shares it."""

    async def wait(self, host: str, interval: float) -> None: ...


class Pacer:
    """One request at a time per host, `interval` seconds apart (at least) — in this process. Every
    `Fetcher` shares one per event loop (`shared_pacer`), so two crawls of one site take turns at its pace
    (F355); cloud installs a pacer shared across its processes (`set_pacer`)."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self._clock = clock
        self._sleep = sleep
        self._next: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def wait(self, host: str, interval: float) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = self._clock()
            at = max(now, self._next.get(host, now))
            if at > now:
                await self._sleep(at - now)
            self._next[host] = at + interval


_pacer: HostPacer | None = None
_local_pacers: dict[int, Pacer] = {}


def set_pacer(pacer: HostPacer | None) -> None:
    """Install the pace every `Fetcher` shares (cloud: one in Redis, across its knowledge workers). None:
    this process's own, one per event loop."""
    global _pacer
    _pacer = pacer
    _local_pacers.clear()


def shared_pacer() -> HostPacer:
    """The installed pacer, else this event loop's own (an `asyncio.Lock` belongs to its loop)."""
    if _pacer is not None:
        return _pacer
    try:
        loop = id(asyncio.get_running_loop())
    except RuntimeError:
        loop = 0
    pacer = _local_pacers.get(loop)
    if pacer is None:
        pacer = Pacer()
        _local_pacers.clear()  # one loop at a time serves crawls; a stale loop's pacer goes with it
        _local_pacers[loop] = pacer
    return pacer


# ── the fetcher ────────────────────────────────────────────────────────────────

def host_key(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def conditional_headers(validator: str | None) -> dict[str, str]:
    """`If-None-Match` / `If-Modified-Since` from a stored validator (a document's `etag` column)."""
    if not validator:
        return {}
    if validator.startswith("lm:"):
        return {"If-Modified-Since": validator[3:]}
    return {"If-None-Match": validator}


def validator_of(headers) -> str | None:
    """What to keep for the next conditional GET: the ETag, else `lm:<Last-Modified>`."""
    etag = (headers.get("etag") or "").strip()
    if etag:
        return etag[:255]
    modified = (headers.get("last-modified") or "").strip()
    return f"lm:{modified}"[:255] if modified else None


def media_type(headers) -> str:
    return (headers.get("content-type") or "").split(";", 1)[0].strip().lower()


class Fetcher:
    """A crawl's HTTP: one client, robots per host, and the pace per host every crawl shares (F355). Use as
    `async with Fetcher(...)`."""

    def __init__(self, *, rps: float = 1.0, client: httpx.AsyncClient | None = None,
                 pacer: HostPacer | None = None):
        self.rps = max(rps, 1e-3)
        self._client = client
        self._own = client is None
        self._pacer = pacer
        self._robots: dict[str, Robots] = {}

    @property
    def pacer(self) -> HostPacer:
        """Its own pacer if it was given one (a test), else the shared one — read when it waits, so an
        installed pacer applies to a fetcher made before it."""
        return self._pacer if self._pacer is not None else shared_pacer()

    async def __aenter__(self) -> Fetcher:
        if self._client is None:
            import httpx

            from core.safe_fetch import CONNECT_TIMEOUT_SECONDS

            timeout = httpx.Timeout(REQUEST_TIMEOUT_S, connect=CONNECT_TIMEOUT_SECONDS)
            if _proxy_required:
                proxies = _environment_proxies()
                if proxies is None:
                    raise EgressUnavailable(NO_PROXY_MESSAGE)
                http_proxy, https_proxy = proxies
                # The proxies passed explicitly, the environment's NO_PROXY ignored: it names the platform's own
                # hosts, never a customer's.
                self._client = httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT}, trust_env=False,
                                                 mounts={"http://": httpx.AsyncHTTPTransport(proxy=http_proxy),
                                                         "https://": httpx.AsyncHTTPTransport(proxy=https_proxy)})
            else:
                # httpx's defaults: trust_env on, no transport= (the environment's proxies apply).
                self._client = httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT})
        return self

    async def __aexit__(self, *exc) -> None:
        if self._own and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _interval(self, host: str) -> float:
        delay = self._robots.get(host).crawl_delay if host in self._robots else None
        return max(1.0 / self.rps, min(delay or 0.0, MAX_CRAWL_DELAY_S))

    async def _get(self, url: str, *, headers: dict[str, str] | None = None, max_bytes: int) -> Fetched:
        from core import safe_fetch

        host = host_key(url)
        await self.pacer.wait(host, self._interval(host))
        assert self._client is not None, "use the Fetcher as an async context manager"
        return await safe_fetch.fetch(url, client=self._client, headers={"User-Agent": USER_AGENT, **(headers or {})},
                                      max_bytes=max_bytes, timeout_s=REQUEST_TIMEOUT_S)

    async def robots(self, url: str) -> Robots:
        """`url`'s host's robots.txt, read once per crawl."""
        from core import safe_fetch

        host = host_key(url)
        found = self._robots.get(host)
        if found is not None:
            return found
        try:
            got = await self._get(f"{host}/robots.txt", max_bytes=ROBOTS_MAX_BYTES)
        except safe_fetch.FetchBlocked:
            raise  # the host itself is refused: every page on it is
        except safe_fetch.FetchTooLarge:
            found = Robots.unreachable()
        except safe_fetch.FetchRefused:
            found = Robots.unreachable()
        else:
            if 200 <= got.status < 300:
                found = Robots.parse(got.body.decode("utf-8", errors="replace"))
            elif 400 <= got.status < 500:
                found = Robots.allow_all()
            else:
                found = Robots.unreachable()
        self._robots[host] = found
        return found

    async def get(self, url: str, *, validator: str | None = None, max_bytes: int = HTML_MAX_BYTES) -> Fetched:
        """GET a page robots.txt allows, conditionally when `validator` is given.

        Raises `RobotsDisallowed`, `RobotsUnreachable`, or `safe_fetch`'s `FetchBlocked` / `FetchTooLarge` /
        `FetchRefused`."""
        robots = await self.robots(url)
        if robots.unreachable_:
            raise RobotsUnreachable(url)
        if not robots.allows(url):
            raise RobotsDisallowed(url)
        return await self._get(url, headers=conditional_headers(validator), max_bytes=max_bytes)

    async def get_resource(self, url: str, *, max_bytes: int) -> Fetched:
        """GET a sitemap: robots.txt governs pages, and a sitemap is where a site lists them."""
        return await self._get(url, max_bytes=max_bytes)


# ── what came back ─────────────────────────────────────────────────────────────

_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-:.]+)""", re.IGNORECASE)


def decode_html(body: bytes, content_type: str | None) -> str:
    """The page as text: its declared charset (the header, then its `<meta>`), else a sniff."""
    from bs4 import UnicodeDammit

    declared = []
    match = re.search(r"charset\s*=\s*\"?([A-Za-z0-9_\-:.]+)", content_type or "", re.IGNORECASE)
    if match:
        declared.append(match.group(1))
    meta = _META_CHARSET.search(body[:4096])
    if meta:
        declared.append(meta.group(1).decode("ascii", errors="ignore"))
    dammit = UnicodeDammit(body, known_definite_encodings=declared, is_html=True)
    if dammit.unicode_markup is not None:
        return dammit.unicode_markup
    return body.decode("utf-8", errors="replace")


@dataclass
class PageLinks:
    """What a page says about crawling it: its links (absolute, fragments dropped) and its robots meta."""

    links: list[str] = field(default_factory=list)
    noindex: bool = False
    nofollow: bool = False
    canonical: str | None = None


def robots_directives(value: str | None) -> set[str]:
    """`noindex`, `nofollow`, … from a meta robots or an `X-Robots-Tag` value (bare or `synapsebot:`)."""
    out: set[str] = set()
    for part in (value or "").lower().split(","):
        part = part.strip()
        if ":" in part:
            agent, _, part = part.partition(":")
            if agent.strip() not in (BOT.lower(), "*"):
                continue
        part = part.strip()
        if part == "none":
            out |= {"noindex", "nofollow"}
        elif part:
            out.add(part)
    return out


def page_links(html: str, url: str, *, header_robots: str | None = None) -> PageLinks:
    """Every followable link in the whole document — navigation included — and the page's directives."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    directives = robots_directives(header_robots)
    for tag in soup.find_all("meta", attrs={"name": True, "content": True}):
        if str(tag["name"]).strip().lower() in ("robots", BOT.lower()):
            directives |= robots_directives(str(tag["content"]))
    base = url
    base_tag = soup.find("base", href=True)
    if base_tag is not None:
        base = urljoin(url, str(base_tag["href"]).strip())
    out = PageLinks(noindex="noindex" in directives, nofollow="nofollow" in directives)
    canonical = soup.find("link", rel="canonical", href=True)
    if canonical is not None:
        out.canonical = urljoin(url, str(canonical["href"]).strip()).split("#", 1)[0] or None
    if out.nofollow:
        return out
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        rel = a.get("rel") or []
        rels = {r.lower() for r in (rel.split() if isinstance(rel, str) else rel)}
        if "nofollow" in rels:
            continue
        href = str(a["href"]).strip()
        if not href or href.startswith("#"):
            continue
        absolute = urljoin(base, href).split("#", 1)[0]
        if urlsplit(absolute).scheme.lower() not in ("http", "https"):
            continue
        if absolute not in seen:
            seen.add(absolute)
            out.links.append(absolute)
    return out
