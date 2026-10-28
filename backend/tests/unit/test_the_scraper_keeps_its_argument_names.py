"""The scraper's tools keep their argument names (F1157).

Callers match on them: cloud's shared scraper refuses a page script by name (`js_code`, and a `wait_for` that runs
one), and the research route sends `url`, `include_links` and `remove_overlays`. A renamed argument fails here, not
silently in a caller that no longer sees it.
"""
from __future__ import annotations

import pytest

from tools import web_scraper

ARGUMENTS = {
    "scrape_url": {"url", "wait_for", "js_code", "headers", "remove_overlays", "include_links", "include_images",
                   "cache"},
    "scrape_structured": {"url", "schema", "wait_for", "js_code", "headers"},
    "crawl_multiple": {"urls", "wait_for", "js_code", "headers", "include_links"},
    "extract_links": {"url", "wait_for", "filter_external", "filter_domains"},
    "screenshot_url": {"url", "wait_for", "output_path", "full_page", "delay"},
    "scrape_with_session": {"session_id", "steps", "headers"},
    "search_page": {"url", "scroll_count", "wait_for", "js_code", "extract_schema"},
}
STEP_ARGUMENTS = {"url", "js_code", "wait_for", "extract"}


async def _tools() -> dict:
    return {t.name: t for t in await web_scraper.list_tools()}


async def test_the_seven_tools_are_the_seven():
    assert set(await _tools()) == set(ARGUMENTS)


@pytest.mark.parametrize("name", sorted(ARGUMENTS))
async def test_each_tool_keeps_its_argument_names(name):
    assert set((await _tools())[name].inputSchema["properties"]) == ARGUMENTS[name]


async def test_a_session_step_keeps_its_argument_names():
    steps = (await _tools())["scrape_with_session"].inputSchema["properties"]["steps"]
    assert set(steps["items"]["properties"]) == STEP_ARGUMENTS
