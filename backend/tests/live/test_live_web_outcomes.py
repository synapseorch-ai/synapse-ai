"""The live web check sorts what each fetcher answered into the right outcome, and judges it against its
expectation, without touching the web (these run in the normal suite).

The near misses matter most: a page whose own words mention a block is content, and a missing page is "not found",
never a block (F1633's lesson: a detector is held to ordinary pages that look like the bad case, not only to a clean
page and a real block)."""

import json

import live_web
import pytest
from live_web import Row


def _scrape(answer: dict) -> tuple[str, str, str]:
    return live_web.scrape_outcome([json.dumps(answer)])


def test_a_scraped_page_that_talks_about_blocks_is_content():
    outcome, reason, content = _scrape({"success": True, "url": "https://example.com/",
                                        "markdown": "# Bots\nBlocked by anti-bot protection: Cloudflare challenge"})
    assert (outcome, reason) == ("content", "")
    assert "Cloudflare" in content


@pytest.mark.parametrize(("error", "outcome"), [
    ("Blocked by anti-bot protection: Cloudflare challenge", "blocked"),
    ("Blocked by anti-bot protection: HTTP 403", "blocked"),
    ("URL blocked: the egress proxy refused this address", "egress"),
    ("Page returned no extractable content. Try adding a wait_for selector", "empty"),
    ("Crawl failed: net::ERR_NAME_NOT_RESOLVED", "error"),
])
def test_a_scraper_failure_is_sorted_by_its_own_reason(error, outcome):
    assert _scrape({"success": False, "error": error})[:2] == (outcome, error)


def test_a_scraper_answer_that_is_not_json_is_an_error():
    assert live_web.scrape_outcome(["boom"])[0] == "error"


@pytest.mark.parametrize(("status", "outcome"), [(200, None), (204, None), (404, "not_found"), (410, "not_found"),
                                                  (403, "http_error"), (503, "http_error"), (301, "http_error")])
def test_the_knowledge_fetch_sorts_a_status_as_its_crawl_does(status, outcome):
    assert live_web.knowledge_outcome_for_status(status) == outcome


@pytest.mark.parametrize(("kw", "outcome"), [
    ({"status": 200, "handover": None, "is_error": False, "refused": None, "text": "- heading \"JSON\""}, "content"),
    ({"status": 200, "handover": "captcha", "is_error": False, "refused": None, "text": "Just a moment"}, "blocked"),
    ({"status": 404, "handover": None, "is_error": False, "refused": None, "text": "Not Found"}, "not_found"),
    ({"status": 403, "handover": None, "is_error": False, "refused": None, "text": "Forbidden"}, "http_error"),
    ({"status": None, "handover": None, "is_error": True, "refused": None, "text": "net::ERR_TIMED_OUT"}, "error"),
    ({"status": 200, "handover": None, "is_error": False, "refused": None, "text": "  "}, "empty"),
    ({"status": None, "handover": None, "is_error": False, "refused": "URL blocked by egress", "text": ""}, "egress"),
])
def test_a_browser_load_is_sorted_by_what_the_agent_would_see(kw, outcome):
    assert live_web.browser_outcome(**kw)[0] == outcome


def _row(outcome: str, text: str = "", reason: str = "") -> Row:
    return Row("site", "https://example.com/", "scrape", "ua", outcome, reason=reason, text=text)


def test_an_expected_outcome_passes_only_with_its_sanity_text():
    want = {"outcome": "content", "contains": "json encoder"}
    assert live_web.judge(_row("content", text="The JSON encoder and decoder"), want, {}).verdict == "pass"
    missed = live_web.judge(_row("content", text="Something else entirely"), want, {})
    assert (missed.verdict, missed.contains_ok) == ("fail", False)
    assert live_web.judge(_row("blocked"), want, {}).verdict == "fail"


def test_a_reason_check_reads_the_reason():
    want = {"outcome": "robots", "reason_has": "robots.txt disallows"}
    assert live_web.judge(_row("robots", reason="The site's robots.txt disallows it"), want, {}).verdict == "pass"
    assert live_web.judge(_row("robots", reason="something else"), want, {}).verdict == "fail"


def test_a_recorded_row_is_new_then_kept_then_changed():
    assert live_web.judge(_row("content"), "record", {}).verdict == "new"
    assert live_web.judge(_row("content"), "record", {("site", "scrape"): "content"}).verdict == "recorded"
    changed = live_web.judge(_row("blocked"), "record", {("site", "scrape"): "content"})
    assert (changed.verdict, changed.previous) == ("changed", "content")


def test_the_results_file_keeps_no_page_text(tmp_path):
    row = live_web.judge(_row("content", text="page words"), "record", {})
    out = tmp_path / "results.json"
    live_web.write_results([row], {"vantage": "local", "started": "2026-01-05T00:00:00Z"}, out)
    data = json.loads(out.read_text())
    assert "text" not in data["rows"][0] and "page words" not in out.read_text()
    assert live_web.previous_outcomes(out) == {("site", "scrape"): "content"}


def test_the_summary_puts_failures_first():
    rows = [live_web.judge(_row("content", text="ok"), {"outcome": "content"}, {}),
            live_web.judge(_row("blocked"), {"outcome": "content"}, {})]
    table = live_web.summary_markdown(rows, {"vantage": "ci", "started": "2026-01-05T00:00:00Z"})
    body = [line for line in table.splitlines() if line.startswith("| site | scrape |")]
    assert body[0].split("|")[4].strip() == "fail"


def test_a_site_list_with_a_repeated_id_or_unknown_outcome_is_refused(tmp_path):
    twice = tmp_path / "twice.json"
    twice.write_text(json.dumps({"sites": [{"id": "a", "url": "https://example.com/"},
                                           {"id": "a", "url": "https://example.org/"}]}))
    with pytest.raises(ValueError, match="twice"):
        live_web.load_sites(twice)
    odd = tmp_path / "odd.json"
    odd.write_text(json.dumps({"sites": [{"id": "a", "url": "https://example.com/",
                                          "expect": {"scrape": {"outcome": "fine"}}}]}))
    with pytest.raises(ValueError, match="unknown outcome"):
        live_web.load_sites(odd)


def test_the_public_sample_loads_and_fetches_nothing_robots_refuses():
    """A page whose robots.txt the knowledge fetch expects to refuse it, or not to read, is fetched by no other fetcher
    either."""
    sites = live_web.load_sites()
    assert sites
    for site in sites:
        knowledge = live_web.wanted(site, "knowledge")
        if isinstance(knowledge, dict) and knowledge.get("outcome") in ("robots", "robots_unreadable"):
            for other in ("scrape", "browser"):
                want = live_web.wanted(site, other)
                assert want is None or want.get("outcome") == "not_run_robots", (site["id"], other)


def test_the_reason_nothing_was_fetched_says_which_robots_answer_it_was():
    from core.knowledge.fetch import Robots

    url = "https://example.com/private/page"
    rules = Robots.parse("User-agent: *\nDisallow: /private/\n")
    assert live_web.robots_skip(rules, url) == "robots.txt does not allow this page, so the harness fetched nothing"
    assert "could not be read" in live_web.robots_skip(Robots.unreachable(), url)
    assert "refused outright" in live_web.robots_skip(None, url)


def test_a_page_robots_allows_is_fetched():
    from core.knowledge.fetch import Robots

    rules = Robots.parse("User-agent: *\nDisallow: /private/\n")
    # Near misses: a path that only starts like the disallowed one, and a host with no rules at all.
    assert live_web.robots_skip(rules, "https://example.com/privately-public") is None
    assert live_web.robots_skip(rules, "https://example.com/") is None
    assert live_web.robots_skip(Robots.allow_all(), "https://example.com/private/page") is None
