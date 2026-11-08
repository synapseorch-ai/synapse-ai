"""A page read whose response is not HTML is its body, as the server sent it (F1406; BLD1's hero take5 on v2.0
c52f0775, run_orch_c070ck4_1793932843390_0973bc; Naveen 11-06: "Fix in v2.0", and the types: "JSON, text, XML").

The patio build fetched Open-Meteo's JSON with `scrape_url`, and its Transform ran
`json.loads(state['fetch_forecast']['markdown'])`: JSONDecodeError at char 0, the run failed. Chromium shows a JSON
response in a `<pre>`, and the page's Markdown made it a ``` fence, so every workflow reading a JSON API through a page
read failed the same way. A text/plain, CSV or Markdown file is shown in the same `<pre>`, and an XML feed in
Chromium's viewer, whose page adds its own header. Now the read whose own response says, by its content-type, that it
is JSON, XML or text other than HTML is that response's body, in every page read (one seam, `_read_page`): scrape_url,
the batch read, the session crawl and search_page. An HTML page reads exactly as before, a response that names no type
too, and a PDF keeps its own path.

A response Chromium downloads instead of showing (a CSV, or any file sent as an attachment) never reached that seam: it
went to the download path, which read a PDF and refused every other file, so the CSV cases failed at the first fix's
own tip in CI2's run (eb 11-06). A download whose content-type is one of these is its body too; a PDF keeps its path,
and any other file is refused as before.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from core import egress_guard
from tools import web_scraper


class TestTheRule:
    @pytest.mark.parametrize("content_type", [
        "application/json", "application/json; charset=utf-8", "Application/JSON",
        "application/geo+json", "application/ld+json",
        "text/plain", "text/plain; charset=utf-8", "text/csv", "text/markdown", "text/xml",
        "application/xml", "application/rss+xml", "application/atom+xml",
    ])
    def test_json_xml_and_text_are_read_as_sent(self, content_type):
        assert web_scraper._read_as_sent(content_type) is True

    @pytest.mark.parametrize("content_type", [
        "text/html", "text/html; charset=UTF-8", "TEXT/HTML", "application/xhtml+xml",
        None, "", "application/pdf", "image/png", "application/octet-stream",
    ])
    def test_html_a_file_and_no_type_are_not(self, content_type):
        assert web_scraper._read_as_sent(content_type) is False


# ── For real ─────────────────────────────────────────────────────────────────────────────────────────────────────

def _chromium() -> bool:
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
    return root.is_dir() and any(p.name.startswith("chromium") for p in root.iterdir())


#: Open-Meteo's answer, as compact as it sends it, a name past ASCII included.
FORECAST = json.dumps({
    "latitude": 41.147305, "longitude": -8.6404085, "timezone": "Europe/Lisbon", "place": "Porto — Ribeira",
    "daily_units": {"time": "iso8601", "precipitation_probability_max": "%", "windspeed_10m_max": "km/h"},
    "daily": {"time": ["2026-11-06", "2026-11-07"], "precipitation_probability_max": [10, 55],
              "windspeed_10m_max": [12.4, 31.0]},
}, ensure_ascii=False, separators=(",", ":"))
CSV = "date,rain_pct,wind_kmh\n2026-11-07,55,31.0\n"
NOTES = "Patio rules\n\n- tables in above 40% rain\n- or above 30 km/h wind\n"
FEED = ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Harbor news</title>'
        "<item><title>Patio opens</title></item></channel></rss>")
MENU = "<html><head><title>Menu</title></head><body><h1>Harbor menu</h1><p>Oat flat white, 3.20</p></body></html>"
#: HTML whose words are JSON: the header says what it is, never the text.
JSON_IN_HTML = '<html><body><pre>{"daily":{"time":["2026-11-07"]}}</pre></body></html>'
PAGES = {
    "/forecast": ("application/json; charset=utf-8", FORECAST),
    "/rain.csv": ("text/csv; charset=utf-8", CSV),
    "/rules.txt": ("text/plain; charset=utf-8", NOTES),
    "/feed": ("application/rss+xml; charset=utf-8", FEED),
    "/menu": ("text/html; charset=utf-8", MENU),
    "/pre": ("text/html; charset=utf-8", JSON_IN_HTML),
}
#: Sent as attachments: Chromium downloads them, whatever their type.
DOWNLOADS = {
    "/export.json": ("application/json; charset=utf-8", FORECAST),
    "/rules-download.txt": ("text/plain; charset=utf-8", NOTES),
    "/blob.bin": ("application/octet-stream", "binary \x00\x01 payload"),
}
#: scrape_url's own excluded tags, so its HTML read is the page's `_markdown`, as it was.
SCRAPE_URL_EXCLUDED = ("script", "style", "nav", "footer")


@pytest.fixture
async def site(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EGRESS_ALLOW_HOSTS", "localhost")
    egress_guard._resolve_cache.clear()
    monkeypatch.setattr(web_scraper, "_crawler", None)

    async def serve(reader, writer):
        try:
            path = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1").split(" ", 2)[1]
            attached = path in DOWNLOADS
            content_type, text = DOWNLOADS[path] if attached else PAGES[path]
            body = text.encode("utf-8")
            disposition = f"Content-Disposition: attachment; filename={path[1:]}\r\n" if attached else ""
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\n{disposition}Connection: close\r\n"
                         f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, KeyError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    yield f"http://localhost:{server.sockets[0].getsockname()[1]}"
    server.close()
    await web_scraper.close_crawler()


def _answer(out) -> dict:
    return json.loads(out[0].text)


needs_chromium = pytest.mark.skipif(not _chromium(), reason="no Playwright Chromium on this machine")


@needs_chromium
class TestEveryPageRead:
    async def test_scrape_url_reads_a_json_api_as_its_json(self, site):
        out = _answer(await web_scraper._handle_scrape_url({"url": site + "/forecast"}))
        assert out["success"] is True and out["markdown"] == FORECAST, out
        # take5's own Transform, as the builder wrote it.
        data = json.loads(out["markdown"])
        assert (data["daily"]["precipitation_probability_max"][1], data["daily"]["windspeed_10m_max"][1],
                data["daily"]["time"][1]) == (55, 31.0, "2026-11-07")

    @pytest.mark.parametrize("path,body", [("/rain.csv", CSV), ("/rules.txt", NOTES), ("/feed", FEED)])
    async def test_scrape_url_reads_text_and_xml_as_sent(self, site, path, body):
        out = _answer(await web_scraper._handle_scrape_url({"url": site + path}))
        assert out["success"] is True and out["markdown"] == body, out

    @pytest.mark.parametrize("path,body", [("/export.json", FORECAST), ("/rules-download.txt", NOTES)])
    async def test_a_file_it_downloads_that_is_json_or_text_is_its_body(self, site, path, body):
        out = _answer(await web_scraper._handle_scrape_url({"url": site + path}))
        assert out["success"] is True and out["markdown"] == body, out

    async def test_any_other_file_it_downloads_is_still_refused(self, site):
        out = _answer(await web_scraper._handle_scrape_url({"url": site + "/blob.bin"}))
        assert out["success"] is False and "not a page" in out["error"], out

    async def test_the_batch_read(self, site):
        out = _answer(await web_scraper._handle_crawl_multiple({"urls": [site + "/forecast", site + "/rain.csv"]}))
        assert [(r["success"], r["markdown"]) for r in out["results"]] == [(True, FORECAST), (True, CSV)], out

    async def test_the_session_crawl(self, site):
        out = _answer(await web_scraper._handle_scrape_with_session(
            {"session_id": "f1406", "steps": [{"url": site + "/forecast"}]}))
        assert out["success"] is True and out["results"][0]["markdown"] == FORECAST, out

    async def test_search_page(self, site):
        out = _answer(await web_scraper._handle_search_page({"url": site + "/forecast", "scroll_count": 1}))
        assert out["success"] is True and out["markdown"] == FORECAST, out


@needs_chromium
class TestAnHtmlPageAsBefore:
    @pytest.mark.parametrize("path,html", [("/menu", MENU), ("/pre", JSON_IN_HTML)])
    async def test_an_html_page_is_its_markdown(self, site, path, html):
        out = _answer(await web_scraper._handle_scrape_url({"url": site + path}))
        assert out["success"] is True, out
        assert out["markdown"] == web_scraper._markdown(html, site + path, SCRAPE_URL_EXCLUDED), out
        assert out["markdown"] != html
