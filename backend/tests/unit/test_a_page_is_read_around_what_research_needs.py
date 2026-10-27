"""A page is read around what research reads it for, in 3000 characters (SAI2-P5, D300).

Research read about 150k tokens a persona after F765: up to 20 pages, each its first 6000 characters, mostly menus
and intros, while what decides a verdict (who may use the API, how one signs in, what it costs) is often further
down. A model now sees the page's title and the paragraphs that hold those words or the provider's, each with the
paragraph after it (a heading's body), in the page's order, at most 3000 characters; a page with no such words gives
its head.
"""
from __future__ import annotations

from core.builder_research import _PAGE_CHARS, page_extract

FILLER = "\n\n".join(f"Our story, part {i}. We love our customers and our team very much." for i in range(300))


def test_what_decides_a_verdict_deep_in_a_long_page_is_kept():
    page = ("# Booking.com for Business\n\n" + FILLER + "\n\n## Connectivity\n\nOur API is open only to approved "
            "partners. Apply to become a connectivity partner.\n\n" + FILLER)
    assert len(page) > 20_000
    got = page_extract(page)
    assert got.startswith("# Booking.com for Business")
    assert "open only to approved partners" in got and "Apply to become" in got
    assert len(got) <= _PAGE_CHARS == 3000
    assert "Our story, part 150" not in got


def test_a_heading_keeps_the_paragraph_after_it():
    page = "# Docs\n\n" + FILLER + "\n\n## Authentication\n\nSend your key in the X-Secret header.\n\n" + FILLER
    got = page_extract(page)
    assert "## Authentication\n\nSend your key in the X-Secret header." in got
    assert "…" in got, "a gap in the page is marked"


def test_the_providers_own_words_are_hits_too():
    page = "# Home\n\n" + FILLER + "\n\nHenry Schein orders are placed by phone.\n\n" + FILLER
    assert "placed by phone" in page_extract(page, ["henry", "schein"])


def test_a_page_with_nothing_to_find_gives_its_head():
    page = "# About\n\n" + FILLER
    got = page_extract(page)
    assert got == page[:3000]


def test_a_short_page_is_given_whole():
    assert page_extract("# Short\n\nAn API.") == "# Short\n\nAn API."


def test_the_cap_holds_however_many_hits():
    page = "\n\n".join(f"The API key {i} is sent in a header, with OAuth for partners." for i in range(500))
    assert len(page_extract(page)) <= 3000


def test_who_may_use_it_survives_sixty_api_sections():
    """SAI2's probe: the gating sentence after sixty sections of API reference was cut."""
    sections = "\n\n".join(f"## Endpoint {i}\n\nThe API reference for endpoint {i}, with OAuth." for i in range(60))
    page = "# Hotel Connectivity API\n\n" + sections + "\n\nAvailable to approved partners only. Apply here."
    got = page_extract(page)
    assert "approved partners only" in got and len(got) <= 3000


def test_a_page_with_no_api_word_keeps_its_gate():
    page = "# Integrations\n\n" + FILLER + "\n\nIntegrations are not offered to the public; contact our sales team."
    assert "not offered to the public" in page_extract(page)


def test_json_and_read_only_words_never_crowd_out_the_partner_line():
    """SAI2's review, S2: "application/json", "private key" and "read-only" are not who may use it."""
    body = "\n\n".join(f"Send `Content-Type: application/json` with a private key; field {i} is read-only."
                         for i in range(80))
    page = "# API reference\n\n" + body + "\n\nThis API is available to approved partners only."
    got = page_extract(page)
    assert "approved partners only" in got and len(got) <= 3000
