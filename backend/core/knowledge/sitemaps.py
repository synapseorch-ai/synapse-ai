"""Reading a sitemap (sitemaps.org): the pages a site lists, or the sitemaps an index lists (S1, D165).

A sitemap is a remote file a stranger wrote, so it is read defensively:

* **XML through defusedxml**, with DTDs refused outright — no entity
  expansion (a billion laughs), no external entity (XXE).
* **Gzip decompressed against a cap** (`MAX_UNCOMPRESSED`, the protocol's own
  50 MB), never whole into memory first: a small `.gz` can expand to gigabytes.
* **At most `MAX_URLS` URLs** read from one file (the protocol's 50,000); an
  index's children are fetched by the crawl, nested at most `MAX_DEPTH` deep.
* A **plain-text sitemap** (one URL per line) is read too, as the protocol allows.

Only `http(s)` URLs are kept, with their `<lastmod>`; anything else in the
file is ignored.
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass, field

MAX_UNCOMPRESSED = 50 * 1024 * 1024
MAX_URLS = 50_000
#: An index may list sitemaps that are themselves indexes; deeper than this is not followed.
MAX_DEPTH = 2


class SitemapError(ValueError):
    """The file is not a sitemap that can be read safely. The message is safe to show."""


@dataclass(frozen=True)
class SitemapUrl:
    url: str
    lastmod: str | None = None


@dataclass
class Sitemap:
    """A sitemap's pages, or — for an index — the sitemaps it lists. `truncated`: more than `MAX_URLS`."""

    urls: list[SitemapUrl] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)
    truncated: bool = False


def decompress(data: bytes) -> bytes:
    """`data` as is, or gunzipped when it is gzip — refusing anything that grows past `MAX_UNCOMPRESSED`."""
    if not data.startswith(b"\x1f\x8b"):
        return data
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = inflater.decompress(data, MAX_UNCOMPRESSED + 1)
    if len(out) > MAX_UNCOMPRESSED or inflater.unconsumed_tail:
        raise SitemapError(f"The sitemap expands past {MAX_UNCOMPRESSED // (1024 * 1024)} MB.")
    return out


def _http(value: str | None) -> str | None:
    value = (value or "").strip()
    return value if value.lower().startswith(("http://", "https://")) and len(value) <= 2048 else None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def parse(data: bytes) -> Sitemap:
    """A sitemap, a sitemap index, or a text sitemap. Raises `SitemapError`."""
    body = decompress(data)
    if not body.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<"):  # past a BOM and blank lines
        return _text(body)
    from defusedxml import DefusedXmlException
    from defusedxml.ElementTree import fromstring

    try:
        root = fromstring(body, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except DefusedXmlException as exc:
        raise SitemapError(f"The sitemap was refused: {type(exc).__name__}.") from None
    except Exception as exc:  # noqa: BLE001 — ParseError and friends: not XML we can read
        raise SitemapError(f"The sitemap is not valid XML ({type(exc).__name__}).") from None
    kind = _local(root.tag)
    out = Sitemap()
    if kind not in ("urlset", "sitemapindex"):
        raise SitemapError("The file is XML, but not a sitemap (no <urlset> or <sitemapindex>).")
    entry = "url" if kind == "urlset" else "sitemap"
    for node in root:
        if _local(node.tag) != entry:
            continue
        loc = lastmod = None
        for child in node:
            name = _local(child.tag)
            if name == "loc":
                loc = _http(child.text)
            elif name == "lastmod":
                lastmod = (child.text or "").strip()[:40] or None
        if loc is None:
            continue
        if kind == "sitemapindex":
            out.sitemaps.append(loc)
        else:
            out.urls.append(SitemapUrl(loc, lastmod))
        if len(out.urls) + len(out.sitemaps) >= MAX_URLS:
            out.truncated = True
            break
    return out


def _text(body: bytes) -> Sitemap:
    out = Sitemap()
    for line in body.decode("utf-8", errors="replace").splitlines():
        url = _http(line)
        if url is not None:
            out.urls.append(SitemapUrl(url))
            if len(out.urls) >= MAX_URLS:
                out.truncated = True
                break
    if not out.urls:
        raise SitemapError("The file lists no http(s) URLs.")
    return out
