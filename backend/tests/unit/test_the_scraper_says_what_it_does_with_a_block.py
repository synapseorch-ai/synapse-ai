"""The scraper's tools describe what they do with a site that says no, never more (F1702).

scrape_url's description said it "handles anti-bot protections", and a model reading it plans a scrape as a way past
a block. The scraper takes no bot check and solves no CAPTCHA: a page behind a bot check, a sign-in, or one that is
not there fails the call with the reason (F1157, F1633, F1632). No tool's description claims to handle, evade or get
past a site's protection, and scrape_url's says what happens instead.
"""
from __future__ import annotations

import asyncio
import re

from tools import web_scraper

#: A claim to get past a site's protection, in the words a description would use.
_CLAIM = re.compile(r"\b(?:handles?|evad\w*|evasion|bypass\w*|gets? (?:past|around)|circumvent\w*|defeat\w*|"
                    r"stealth)\b[^.]{0,40}\b(?:anti-?bot|bot (?:check|protection|detection)|captcha|block\w*|"
                    r"protection)", re.I)


def _descriptions() -> dict[str, str]:
    return {t.name: t.description or "" for t in asyncio.run(web_scraper.list_tools())}


def test_no_tool_claims_to_get_past_a_sites_protection():
    claims = {name: d for name, d in _descriptions().items() if _CLAIM.search(d)}
    assert not claims, claims


def test_scrape_url_says_what_happens_to_a_page_behind_a_check_a_sign_in_or_gone():
    said = _descriptions()["scrape_url"]
    assert "JavaScript-rendered" in said
    assert re.search(r"bot check", said) and "sign-in" in said and "404" in said and "fails with the reason" in said


def test_an_honest_sentence_about_a_bot_check_is_not_a_claim():
    """The near miss: naming a bot check as what fails the call is no claim to get past one."""
    honest = "A page behind a bot check, a sign-in or a 404 fails with the reason."
    assert not _CLAIM.search(honest)
    assert _CLAIM.search("Handles JavaScript-rendered pages, anti-bot protections, and dynamic content.")
