"""Reading a sitemap safely (S1): XML, an index, gzip, text, and the files that try something."""
from __future__ import annotations

import gzip

import pytest

from core.knowledge import sitemaps as S

URLSET = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://a.test/docs/one</loc><lastmod>2026-09-30</lastmod></url>
  <url><loc> https://a.test/docs/two </loc></url>
  <url><loc>ftp://a.test/no</loc></url>
  <url><lastmod>2026-01-01</lastmod></url>
</urlset>"""


def test_a_urlset_lists_its_http_pages_with_lastmod():
    got = S.parse(URLSET)
    assert got.urls == [S.SitemapUrl("https://a.test/docs/one", "2026-09-30"), S.SitemapUrl("https://a.test/docs/two")]
    assert got.sitemaps == [] and not got.truncated


def test_an_index_lists_its_sitemaps():
    got = S.parse(b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                  b"<sitemap><loc>https://a.test/s1.xml</loc></sitemap>"
                  b"<sitemap><loc>https://a.test/s2.xml.gz</loc></sitemap></sitemapindex>")
    assert got.sitemaps == ["https://a.test/s1.xml", "https://a.test/s2.xml.gz"] and got.urls == []


def test_gzip_and_a_bom_are_read():
    assert len(S.parse(gzip.compress(b"\xef\xbb\xbf" + URLSET)).urls) == 2


def test_a_text_sitemap_is_read():
    got = S.parse(b"https://a.test/a\n\nnot a url\nhttps://a.test/b\n")
    assert [u.url for u in got.urls] == ["https://a.test/a", "https://a.test/b"]


def test_a_gzip_bomb_is_refused_before_it_is_expanded():
    bomb = gzip.compress(b"<" + b" " * (S.MAX_UNCOMPRESSED + 10))
    with pytest.raises(S.SitemapError, match="expands past"):
        S.parse(bomb)


@pytest.mark.parametrize("doc", [
    b'<?xml version="1.0"?><!DOCTYPE lol [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
    b"<urlset><url><loc>https://a.test/&b;</loc></url></urlset>",
    b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
    b"<urlset><url><loc>https://a.test/&xxe;</loc></url></urlset>",
])
def test_entities_and_dtds_are_refused(doc):
    with pytest.raises(S.SitemapError, match="refused"):
        S.parse(doc)


def test_not_a_sitemap_says_so():
    with pytest.raises(S.SitemapError, match="not a sitemap"):
        S.parse(b"<html><body>hello</body></html>")
    with pytest.raises(S.SitemapError):
        S.parse(b"<urlset><url>")
    with pytest.raises(S.SitemapError, match="no http"):
        S.parse(b"just words\n")


def test_a_huge_sitemap_is_cut_and_says_so(monkeypatch):
    monkeypatch.setattr(S, "MAX_URLS", 3)
    body = b"<urlset>" + b"".join(b"<url><loc>https://a.test/%d</loc></url>" % i for i in range(10)) + b"</urlset>"
    got = S.parse(body)
    assert len(got.urls) == 3 and got.truncated
