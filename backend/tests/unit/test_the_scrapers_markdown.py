"""What a page's Markdown keeps and leaves out (F1157).

The research route reads it: `_section` finds a heading by its own anchor link (`[](…#sendmessage)`), and the words
are a model's to pay for. An inline `data:` image is its alt text, never its payload: crawl4ai blanked them, and the
Playwright scraper wrote example.com's base64 icon into the page's first 400 characters (the image check, 10-28).
"""
from __future__ import annotations

from tools import web_scraper

PAYLOAD = "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciLz4="


def _md(html: str) -> str:
    return web_scraper._markdown(html, "https://docs.example.com/api", ("script", "style"))


def test_an_inline_image_is_its_alt_text_not_its_payload():
    out = _md(f'<p>Logo: <img alt="the logo" src="data:image/svg+xml;base64,{PAYLOAD}"></p>')
    assert PAYLOAD not in out and "base64" not in out
    assert "the logo" in out


def test_an_image_at_an_address_keeps_it():
    assert "https://docs.example.com/i.png" in _md('<p><img alt="chart" src="/i.png"></p>')


def test_a_headings_own_anchor_is_kept_for_research():
    assert "[](https://docs.example.com/api#sendmessage)" in _md('<h4><a href="#sendmessage"></a>sendMessage</h4>')


def test_brackets_in_the_text_are_not_escaped():
    assert "auth is [Bearer x]" in _md("<p>auth is [Bearer x]</p>")
