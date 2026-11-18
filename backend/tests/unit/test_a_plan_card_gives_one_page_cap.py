"""A plan's card gives one page cap for a crawl: the one the crawl is held to (F1669).

A plan for a docs assistant crawled a site for its knowledge base. The step said "it crawls https://docs.netlify.com/,
up to 500 pages", code's own figure from Knowledge's crawl limit, and the planner's "What I assumed" said "up to 50
pages from that site", its own guess. The card gave two caps for one crawl. Now a line of what the plan assumed that
gives a page count for a base's crawl, named by the base or its start address, says code's cap.

Near misses: an assumption with the same number, one about something else that has a number ("up to 50 rows"), one
about another base, and a base that reads only the pages it is given (no cap) are left as they are.
"""
from __future__ import annotations

from core.native_builder import objects as O

SITE = "https://docs.netlify.com/"


def _base(**over) -> O.KnowledgePart:
    part = O.KnowledgePart.model_validate({"name": "Netlify Docs", "urls": [SITE], "kind": "site", **over})
    part.pages_cap = over.get("pages_cap", 500)
    return part


def _said(assumed, bases):
    from core.native_builder.build_plan import caps_as_code_says

    return caps_as_code_says(assumed, bases)


def test_an_assumed_cap_for_the_crawl_is_codes():
    line = "The knowledge base crawls from https://docs.netlify.com/ — up to 50 pages from that site."
    out = _said([line], [_base()])
    assert out == ["The knowledge base crawls from https://docs.netlify.com/ — up to 500 pages from that site."], out


def test_named_by_the_base_too():
    out = _said(["“Netlify Docs” reads about 50 pages."], [_base()])
    assert out == ["“Netlify Docs” reads about 500 pages."], out


def test_near_misses_are_left_as_they_are():
    kept = ["The knowledge base crawls from https://docs.netlify.com/, up to 500 pages.",
            "Each run reads up to 50 rows of the sheet.",
            "“Help Center” reads up to 50 pages.",
            "You check the answers before they go out."]
    assert _said(kept, [_base()]) == kept
    pages_only = _base(kind="page", urls=[SITE + "a", SITE + "b"], pages_cap=0)
    line = "“Netlify Docs” reads 2 pages, up to 50 pages each week."
    assert _said([line], [pages_only]) == [line]


def test_the_card_says_its_assumptions_through_it():
    import inspect

    from core.native_builder import build_plan

    assert "caps_as_code_says(" in inspect.getsource(build_plan)
