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
