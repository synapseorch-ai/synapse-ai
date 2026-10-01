"""Web sources end to end (S1, D165): the route, a crawl against a loopback site, what is written,
indexed, kept and deleted, and the crawl's state.

The site is a loopback server named in `EGRESS_ALLOW_HOSTS` (the guard runs for
real); its pages are a dict a test changes between crawls. Pace is switched
off (`Pacer.wait`), ingest jobs are recorded and drained in order, and indexing
is faked unless a test searches. SQLite always; Postgres with
`SYNAPSE_TEST_POSTGRES_URL`.
"""
from __future__ import annotations

import asyncio
import http.server
import threading

import pytest

from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from _fakes.knowledge_env import FakeIndexer, install_fake_indexing
from core import egress_guard
from core.knowledge import dispatch, documents, seams, web
from core.knowledge import fetch as F
from core.knowledge.jobs import run_ingest_job
from core.knowledge.testing.fakes import RecordingDispatcher
from core.storage import get_blob_store

pytestmark = pytest.mark.usefixtures("knowledge_store")

PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy",
              "no_proxy")


def html(title: str, body: str, *links: str, head: str = "") -> tuple[int, dict, bytes]:
    nav = "".join(f'<a href="{href}">{href}</a>' for href in links)
    page = (f"<html><head><title>{title}</title>{head}</head><body><nav>{nav}</nav>"
            f"<main><h1>{title}</h1><p>{body}</p></main></body></html>")
    return 200, {"Content-Type": "text/html; charset=utf-8"}, page.encode()


class _Site(http.server.BaseHTTPRequestHandler):
    pages: dict[str, tuple[int, dict, bytes]] = {}
    hits: list[tuple[str, str | None]] = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).hits.append((self.path, self.headers.get("If-None-Match")))
        status, headers, body = type(self).pages.get(self.path, (404, {}, b"not here"))
        if status == 200 and headers.get("ETag") and self.headers.get("If-None-Match") == headers["ETag"]:
            status, body = 304, b""
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


@pytest.fixture
def site(server, monkeypatch):
    """The site's base URL, with the docs every test starts from."""
    for name in PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EGRESS_ALLOW_HOSTS", "127.0.0.1")
    egress_guard._resolve_cache.clear()

    async def no_pace(self, host, interval):
        return None

    monkeypatch.setattr(F.Pacer, "wait", no_pace)
    base = f"http://127.0.0.1:{server}"
    _Site.hits = []
    _Site.pages = {
        "/robots.txt": (200, {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow: /private\n"),
        "/docs/": html("Docs home", "Welcome to the docs.", "/docs/a", "/docs/b", "/docs/notes.txt", "/blog/x",
                       "https://elsewhere.test/docs/"),
        "/docs/a": html("Page A", "Alpha holds the termination clause.", "/docs/"),
        "/docs/b": html("Page B", "Bravo explains hosting fees.", "/docs/a"),
        "/docs/notes.txt": (200, {"Content-Type": "text/plain"}, b"Plain notes about renewal."),
        "/blog/x": html("Blog", "Out of scope."),
        "/private/x": html("Private", "Never fetched."),
    }
    yield base
    egress_guard._resolve_cache.clear()


@pytest.fixture(autouse=True)
def _fresh():
    dispatch.reset()
    web._last_due.clear()
    yield
    dispatch.reset()


@pytest.fixture
async def on(client):
    assert (await client.post("/api/settings", json={"knowledge_enabled": True})).status_code == 200
    return client


@pytest.fixture
def jobs():
    recorder = RecordingDispatcher()
    seams.set_ingest_dispatcher(recorder)
    return recorder


@pytest.fixture
def indexer(monkeypatch):
    fake = FakeIndexer()
    install_fake_indexing(monkeypatch, fake)
    return fake


async def _kb(client, folder="site"):
    resp = await client.post("/api/knowledge/bases", json={"name": "Site", "folder": folder})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def crawled() -> None:
    """Wait for every crawl this process started."""
    while any(not t.done() for t in web._tasks.values()):
        await asyncio.gather(*[t for t in web._tasks.values() if not t.done()], return_exceptions=True)


async def add(client, kb_id, *urls, kind="site", **kw):
    resp = await client.post(f"/api/knowledge/bases/{kb_id}/urls", json={"urls": list(urls), "kind": kind, **kw})
    assert resp.status_code == 202, resp.text
    await crawled()
    return resp.json()


async def drain(jobs) -> None:
    await jobs.drain(run_ingest_job)


async def docs(kb_id) -> dict[str, object]:
    return {row.path: row for row in await documents.list_for(kb_id)}


async def crawls(client, kb_id) -> list[dict]:
    resp = await client.get(f"/api/knowledge/bases/{kb_id}/crawls")
    assert resp.status_code == 200, resp.text
    return resp.json()


def host(base: str) -> str:
    return "127.0.0.1_" + base.rsplit(":", 1)[1]


class TestASiteCrawl:
    async def test_pages_in_scope_are_written_indexed_and_counted(self, on, site, jobs, indexer):
        kb = await _kb(on)
        accepted = await add(on, kb["id"], f"{site}/docs/")
        assert accepted["status"] == "queued" and accepted["accepted"] == 1 and accepted["crawl_id"].startswith("kcr_")
        await drain(jobs)
        h = host(site)
        rows = await docs(kb["id"])
        assert set(rows) == {f"site/_web/{h}/docs/index.md", f"site/_web/{h}/docs/a.md", f"site/_web/{h}/docs/b.md",
                             f"site/_web/{h}/docs/notes.txt"}
        assert {r.status for r in rows.values()} == {"indexed"} and {r.source for r in rows.values()} == {"web"}
        page = get_blob_store().get(f"vault/site/_web/{h}/docs/a.md")
        assert page.startswith(f"---\ntitle: Page A\nsource_url: {site}/docs/a\n---\n")
        assert "Alpha holds the termination clause." in page
        assert rows[f"site/_web/{h}/docs/a.md"].source_url == f"{site}/docs/a"
        assert {c.parsed.title for c in indexer.contexts} >= {"Page A", "Page B", "Docs home"}
        assert all(path != "/blog/x" for path, _ in _Site.hits)  # out of scope: never requested
        [state] = await crawls(on, kb["id"])
        assert state["status"] == "done" and state["kind"] == "site" and state["pages"] == 4
        assert state["counts"]["written"] == 4 and state["counts"]["not_followed"] >= 2
        assert state["next_due_at"] is not None
        base = (await on.get(f"/api/knowledge/bases/{kb['id']}")).json()
        assert [s["url"] for s in base["web"]["sources"]] == [f"{site}/docs/"]

    async def test_a_recrawl_that_finds_nothing_new_queues_nothing(self, on, site, jobs, indexer):
        kb = await _kb(on)
        await add(on, kb["id"], f"{site}/docs/")
        await drain(jobs)
        indexed = len(indexer.contexts)
        again = await add(on, kb["id"], f"{site}/docs/")  # the same URL and kind: the same source, crawled now
        assert jobs.jobs == []
        [state] = await crawls(on, kb["id"])
        assert state["crawl_id"] == again["crawl_id"] and state["counts"]["written"] == 0
        assert state["counts"]["unchanged"] == 4 and len(indexer.contexts) == indexed
        base = (await on.get(f"/api/knowledge/bases/{kb['id']}")).json()
        assert len(base["web"]["sources"]) == 1

    async def test_a_changed_page_is_written_again(self, on, site, jobs, indexer):
        kb = await _kb(on)
        await add(on, kb["id"], f"{site}/docs/")
        await drain(jobs)
        _Site.pages["/docs/b"] = html("Page B", "Bravo now explains support hours.", "/docs/a")
        await add(on, kb["id"], f"{site}/docs/")
        await drain(jobs)
        assert "support hours" in indexer.contexts[-1].parsed.text
        [state] = await crawls(on, kb["id"])
        assert state["counts"]["written"] == 1 and state["counts"]["unchanged"] == 3


class TestWhatACrawlDeletes:
    async def test_a_page_gone_or_no_longer_linked_is_deleted(self, on, site, jobs, indexer):
        kb = await _kb(on)
        await add(on, kb["id"], f"{site}/docs/")
        await drain(jobs)
        h = host(site)
        _Site.pages["/docs/"] = html("Docs home", "Welcome.", "/docs/a")  # b and the notes are unlinked
        _Site.pages["/docs/a"] = (410, {}, b"gone")
        await add(on, kb["id"], f"{site}/docs/")
        await drain(jobs)
        assert set(await docs(kb["id"])) == {f"site/_web/{h}/docs/index.md"}
        assert not get_blob_store().exists(f"vault/site/_web/{h}/docs/b.md")
        [state] = await crawls(on, kb["id"])
        assert state["status"] == "done" and state["counts"]["deleted"] == 3 and state["pages"] == 1

    async def test_a_capped_crawl_deletes_nothing(self, on, site, jobs, indexer):
        kb = await _kb(on)
        await add(on, kb["id"], f"{site}/docs/")
        await drain(jobs)
        await add(on, kb["id"], f"{site}/docs/", max_pages=1)
        await drain(jobs)
        assert len(await docs(kb["id"])) == 4
        [state] = await crawls(on, kb["id"])
        assert state["status"] == "capped" and state["counts"]["deleted"] == 0 and state["pages"] == 4

    async def test_a_crawl_with_a_failure_deletes_nothing_and_keeps_the_failed_page(self, on, site, jobs, indexer):
        kb = await _kb(on)
        await add(on, kb["id"], f"{site}/docs/")
        await drain(jobs)
        _Site.pages["/docs/"] = html("Docs home", "Welcome.", "/docs/a", "/docs/b")  # the notes are unlinked
        _Site.pages["/docs/b"] = (503, {}, b"down")
        await add(on, kb["id"], f"{site}/docs/")
        await drain(jobs)
        assert len(await docs(kb["id"])) == 4
        [state] = await crawls(on, kb["id"])
        assert state["counts"]["failed"] == 1 and state["counts"]["deleted"] == 0

    async def test_robots_now_disallowing_a_page_deletes_it(self, on, site, jobs, indexer):
        kb = await _kb(on)
        await add(on, kb["id"], f"{site}/docs/")
        await drain(jobs)
        _Site.pages["/robots.txt"] = (200, {}, b"User-agent: SynapseBot\nDisallow: /docs/b\n")
        before = len(_Site.hits)
        await add(on, kb["id"], f"{site}/docs/")
        await drain(jobs)
        assert f"site/_web/{host(site)}/docs/b.md" not in await docs(kb["id"])
        assert "/docs/b" not in [path for path, _ in _Site.hits[before:]]  # never requested once disallowed
        [state] = await crawls(on, kb["id"])
        assert state["counts"]["blocked"] == 1 and state["counts"]["deleted"] == 1

    async def test_an_unreadable_robots_txt_fails_the_crawl_and_deletes_nothing(self, on, site, jobs, indexer):
        kb = await _kb(on)
        await add(on, kb["id"], f"{site}/docs/")
        await drain(jobs)
        _Site.pages["/robots.txt"] = (503, {}, b"busy")
        await add(on, kb["id"], f"{site}/docs/")
        await drain(jobs)
        assert len(await docs(kb["id"])) == 4
        [state] = await crawls(on, kb["id"])
        assert state["status"] == "failed" and "robots.txt" in state["last_error"]

    async def test_a_noindex_page_is_not_kept(self, on, site, jobs, indexer):
        kb = await _kb(on)
        _Site.pages["/docs/b"] = html("Page B", "Secret-ish.", head='<meta name="robots" content="noindex">')
        await add(on, kb["id"], f"{site}/docs/")
        await drain(jobs)
        assert f"site/_web/{host(site)}/docs/b.md" not in await docs(kb["id"])


class TestNamedUrlsThatFail:
    async def test_a_robots_disallowed_page_is_blocked_with_its_reason_and_no_file(self, on, site, jobs, indexer):
        kb = await _kb(on)
        await add(on, kb["id"], f"{site}/private/x", kind="page")
        [row] = (await docs(kb["id"])).values()
        assert (row.status, row.status_reason, row.sha256) == ("blocked", "robots_disallowed", None)
        detail = (await on.get(f"/api/knowledge/documents/{row.id}")).json()
        stage = detail["report"]["stages"][0]
        assert stage["stage"] == "fetch" and stage["error"]["code"] == "robots_disallowed"
        assert detail["report"]["retry"]["hint"] and detail["source_url"] == f"{site}/private/x"
        assert ("/private/x", None) not in _Site.hits

    async def test_an_address_the_guard_refuses_is_blocked(self, on, site, jobs, indexer):
        kb = await _kb(on)
        await add(on, kb["id"], "http://169.254.169.254/latest/meta-data/", kind="page")
        [row] = (await docs(kb["id"])).values()
        assert (row.status, row.status_reason) == ("blocked", "egress_blocked")

    async def test_a_missing_page_fails_and_a_second_crawl_does_not_announce_it_again(self, on, site, jobs, indexer):
        kb = await _kb(on)
        await add(on, kb["id"], f"{site}/docs/nope", kind="page")
        [row] = (await docs(kb["id"])).values()
        assert (row.status, row.status_reason) == ("failed", "fetch_failed") and "404" in (row.error or "")
        first = row.updated_at
        await add(on, kb["id"], f"{site}/docs/nope", kind="page")
        [again] = (await docs(kb["id"])).values()
        assert again.id == row.id and again.updated_at == first

    async def test_a_page_that_comes_back_replaces_its_failure(self, on, site, jobs, indexer):
        kb = await _kb(on)
        await add(on, kb["id"], f"{site}/docs/later", kind="page")
        _Site.pages["/docs/later"] = html("Later", "Arrived later.")
        await add(on, kb["id"], f"{site}/docs/later", kind="page")
        await drain(jobs)
        [row] = (await docs(kb["id"])).values()
        assert row.status == "indexed" and row.sha256


class TestConditionalGet:
    async def test_a_page_source_revalidates_with_its_etag(self, on, site, jobs, indexer):
        status, headers, body = html("Tagged", "With an ETag.")
        _Site.pages["/docs/tagged"] = (status, {**headers, "ETag": '"t1"'}, body)
        kb = await _kb(on)
        await add(on, kb["id"], f"{site}/docs/tagged", kind="page")
        await drain(jobs)
        await add(on, kb["id"], f"{site}/docs/tagged", kind="page")
        assert _Site.hits[-1] == ("/docs/tagged", '"t1"')
        [state] = await crawls(on, kb["id"])
        assert state["counts"]["unchanged"] == 1 and state["counts"]["written"] == 0 and jobs.jobs == []


class TestSitemaps:
    async def test_a_sitemaps_pages_are_kept(self, on, site, jobs, indexer):
        _Site.pages["/sitemap.xml"] = (200, {"Content-Type": "application/xml"}, (
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"<sitemap><loc>{site}/pages.xml</loc></sitemap></sitemapindex>").encode())
        _Site.pages["/pages.xml"] = (200, {"Content-Type": "application/xml"}, (
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"<url><loc>{site}/docs/a</loc></url><url><loc>{site}/blog/x</loc></url>"
            "<url><loc>https://elsewhere.test/a</loc></url></urlset>").encode())
        kb = await _kb(on)
        await add(on, kb["id"], f"{site}/sitemap.xml", kind="sitemap")
        await drain(jobs)
        h = host(site)
        assert set(await docs(kb["id"])) == {f"site/_web/{h}/docs/a.md", f"site/_web/{h}/blog/x.md"}
        [state] = await crawls(on, kb["id"])
        assert state["status"] == "done" and state["counts"]["not_followed"] == 1

    async def test_an_unreadable_sitemap_fails_the_crawl(self, on, site, jobs, indexer):
        _Site.pages["/bad.xml"] = (200, {"Content-Type": "application/xml"}, b"<html>not a sitemap</html>")
        kb = await _kb(on)
        await add(on, kb["id"], f"{site}/bad.xml", kind="sitemap")
        [state] = await crawls(on, kb["id"])
        assert state["status"] == "failed" and "not a sitemap" in state["last_error"]
        assert await docs(kb["id"]) == {}


class TestSources:
    async def test_removing_a_source_deletes_what_it_alone_holds(self, on, site, jobs, indexer):
        kb = await _kb(on)
        await add(on, kb["id"], f"{site}/docs/")
        await add(on, kb["id"], f"{site}/docs/a", kind="page")
        await drain(jobs)
        base = (await on.get(f"/api/knowledge/bases/{kb['id']}")).json()
        page_only = {**base["web"], "sources": [s for s in base["web"]["sources"] if s["kind"] == "page"]}
        resp = await on.patch(f"/api/knowledge/bases/{kb['id']}", json={"revision": base["revision"], "web": page_only})
        assert resp.status_code == 200, resp.text
        await drain(jobs)
        assert set(await docs(kb["id"])) == {f"site/_web/{host(site)}/docs/a.md"}
        assert [c["kind"] for c in await crawls(on, kb["id"])] == ["page"]
        assert await web.load_state(base["web"]["sources"][0]["id"]) == {}

    async def test_metadata_reaches_every_page(self, on, site, jobs, indexer):
        kb = await _kb(on)
        await add(on, kb["id"], f"{site}/docs/a", kind="page", metadata={"team": "legal"})
        await drain(jobs)
        [row] = (await docs(kb["id"])).values()
        assert row.meta == {"team": "legal"} and indexer.contexts[-1].metadata == {"team": "legal"}


class TestTheRoute:
    async def test_bad_urls_and_too_many_sources_are_422(self, on, site, jobs):
        kb = await _kb(on)
        resp = await on.post(f"/api/knowledge/bases/{kb['id']}/urls", json={"urls": ["file:///etc/passwd"]})
        assert resp.status_code == 422
        resp = await on.post(f"/api/knowledge/bases/{kb['id']}/urls", json={"urls": ["https://exa mple.com:x/"]})
        assert resp.status_code == 422
        many = [f"https://example.test/p{i}" for i in range(51)]
        resp = await on.post(f"/api/knowledge/bases/{kb['id']}/urls", json={"urls": many, "kind": "page"})
        assert resp.status_code == 422 and "at most 50" in resp.json()["detail"]
        base = (await on.get(f"/api/knowledge/bases/{kb['id']}")).json()
        assert base["web"]["sources"] == []

    async def test_a_disabled_base_is_409_and_an_unknown_one_404(self, on, site, jobs):
        kb = await _kb(on)
        resp = await on.patch(f"/api/knowledge/bases/{kb['id']}", json={"revision": kb["revision"], "status": "disabled"})
        assert resp.status_code == 200
        resp = await on.post(f"/api/knowledge/bases/{kb['id']}/urls", json={"urls": [f"{site}/docs/"]})
        assert resp.status_code == 409
        resp = await on.post("/api/knowledge/bases/kb_ffffffffffff/urls", json={"urls": [f"{site}/docs/"]})
        assert resp.status_code == 404
        assert (await on.get("/api/knowledge/bases/kb_ffffffffffff/crawls")).status_code == 404

    async def test_the_crawls_of_a_base_with_no_sources_are_empty(self, on):
        kb = await _kb(on)
        assert await crawls(on, kb["id"]) == []


class TestSearchable:
    async def test_a_crawled_page_is_found_by_search(self, on, site, jobs):
        kb = await _kb(on)
        await add(on, kb["id"], f"{site}/docs/")
        await drain(jobs)
        resp = await on.post("/api/knowledge/search", json={"query": "termination clause", "kb_ids": [kb["id"]]})
        assert resp.status_code == 200, resp.text
        hit = resp.json()["hits"][0]
        assert hit["citation"]["source"] == "web" and hit["citation"]["source_url"] == f"{site}/docs/a"
        assert "termination clause" in hit["content"]
