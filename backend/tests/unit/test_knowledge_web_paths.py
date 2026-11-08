"""Where a fetched page lives, which URLs a crawl follows, and what a page file holds (S1, D165)."""
from __future__ import annotations

import pytest

from core.knowledge import web
from core.knowledge.kb_settings import WebSource


class TestNormalize:
    @pytest.mark.parametrize("raw, want", [
        ("HTTPS://Docs.Example.COM", "https://docs.example.com/"),
        ("https://example.com:443/a/./b/../c", "https://example.com/a/c"),
        ("http://example.com:8080/a/", "http://example.com:8080/a/"),
        ("https://user:secret@example.com/x#frag", "https://example.com/x"),
        ("https://example.com/search?q=a&b=2#top", "https://example.com/search?q=a&b=2"),
        ("https://bücher.example/katalog", "https://xn--bcher-kva.example/katalog"),
        ("https://example.com//a//b", "https://example.com/a/b"),
    ])
    def test_one_spelling_per_page(self, raw, want):
        assert web.normalize_url(raw) == want

    @pytest.mark.parametrize("raw", ["ftp://example.com/x", "file:///etc/passwd", "https:///nohost", "not a url",
                                     "https://example.com:99999/"])
    def test_anything_else_is_refused(self, raw):
        with pytest.raises(ValueError):
            web.normalize_url(raw)


class TestPaths:
    @pytest.mark.parametrize("url, want", [
        ("https://example.com/", "_web/example.com/index.md"),
        ("https://example.com/docs/", "_web/example.com/docs/index.md"),
        ("https://example.com/docs/intro", "_web/example.com/docs/intro.md"),
        ("https://example.com/docs/intro.html", "_web/example.com/docs/intro.md"),
        ("http://example.com:8080/a", "_web/example.com_8080/a.md"),
        ("https://example.com/a%20b/c%5Bd%5D", "_web/example.com/a b/c_d_.md"),
        ("https://example.com/.well-known/x", "_web/example.com/_.well-known/x.md"),
    ])
    def test_a_page_is_markdown_under_its_host(self, url, want):
        assert web.page_rel(url) == want

    def test_a_query_is_its_own_file(self):
        one, two = web.page_rel("https://example.com/list?page=1"), web.page_rel("https://example.com/list?page=2")
        assert one != two and one.startswith("_web/example.com/list__q") and one.endswith(".md")

    def test_a_document_keeps_its_extension(self):
        assert web.page_rel("https://example.com/files/report.pdf", ext=".pdf") == "_web/example.com/files/report.pdf"
        assert web.page_rel("https://example.com/download?id=7", ext=".pdf").endswith(".pdf")

    def test_long_names_are_shortened_and_long_paths_folded(self):
        long_name = web.page_rel("https://example.com/" + "a" * 400)
        assert len(long_name.rsplit("/", 1)[1].encode()) <= 255 and long_name.endswith(".md")
        deep = web.page_rel("https://example.com/" + "/".join(["segment"] * 150))
        assert deep.startswith("_web/example.com/_long/") and deep.endswith(".md")

    def test_every_segment_is_a_vault_name(self):
        from core.knowledge.ingest import _segment

        for url in ("https://example.com/%00evil/..%2F../x", "https://example.com/ spaced /x ",
                    "https://example.com/a​b", "https://example.com/" + "é" * 200):
            for segment in web.page_rel(url).split("/"):
                assert _segment(segment) == segment


class TestScope:
    def _site(self, url="https://example.com/docs/intro", **kw) -> WebSource:
        return WebSource(id="kws_000000000001", url=url, kind="site", **kw)

    def test_same_host_under_the_start_urls_directory(self):
        site = self._site()
        assert web.in_scope(site, "https://example.com/docs/guide")
        assert web.in_scope(site, "http://example.com/docs/guide")  # http ↔ https on the same host
        assert not web.in_scope(site, "https://example.com/blog/post")
        assert not web.in_scope(site, "https://api.example.com/docs/x")  # subdomains are other sites
        assert not web.in_scope(site, "https://example.org/docs/x")

    def test_globs(self):
        site = self._site(url="https://example.com/", include=["/docs/*"], exclude=["*/draft/*", "*?print=*"])
        assert web.in_scope(site, "https://example.com/docs/a")
        assert not web.in_scope(site, "https://example.com/blog/a")
        assert not web.in_scope(site, "https://example.com/docs/draft/a")
        assert not web.in_scope(site, "https://example.com/docs/a?print=1")

    def test_a_sitemaps_pages_need_only_the_same_host(self):
        sitemap = WebSource(id="kws_000000000002", url="https://example.com/docs/sitemap.xml", kind="sitemap")
        assert web.in_scope(sitemap, "https://example.com/blog/a") and not web.in_scope(sitemap, "https://x.test/a")


class TestPageFile:
    def test_the_front_matter_names_the_page_and_is_stable(self):
        one = web.page_file("# Hello\n\nBody.", url="https://example.com/a", title="Hello: a page\nnewline",
                            published="2026-09-30T10:00:00Z", hidden=2)
        assert one.startswith("---\ntitle: Hello: a page newline\ndate: 2026-09-30\nsource_url: https://example.com/a\n")
        assert "hidden_text_removed: 2" in one and one.endswith("# Hello\n\nBody.\n")
        assert one == web.page_file("# Hello\n\nBody.", url="https://example.com/a", title="Hello: a page\nnewline",
                                    published="2026-09-30T10:00:00Z", hidden=2)
        assert web.fetched_counts(one) == {"hidden_text_removed": 2}
        assert web.fetched_counts("# no front matter") == {}

    def test_the_markdown_reader_takes_its_title_and_date(self):
        from core.knowledge.parse.text import parse_markdown

        _, found = parse_markdown(web.page_file("Body text.", url="https://example.com/a", title="The title",
                                                published="2026-09-30"))
        assert found == {"title": "The title", "doc_date": "2026-09-30"}
