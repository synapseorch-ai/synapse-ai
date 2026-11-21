"""A page extract whose only lines are skip or jump links holds no real text (F1660).

A page that renders with JavaScript gives a fetcher little more than its accessibility skip link ("Skip to main
content"). Indexed, that link became a knowledge base's whole "content" for the page. The rule counts what is left once
skip and jump links are set aside; it never counts characters, so short real text stays real text.
"""
from __future__ import annotations

import pytest

from core.knowledge.parse.html import has_real_text


@pytest.mark.parametrize("extract", [
    "Skip to main content",
    "skip to content",
    "Skip to main content\n\nJump to navigation\n",
    "[Skip to main content](https://a.test/docs#content-area)",
    "Skip Navigation\n---\n",
    "Jump to content",
    "",
    "   \n\n",
])
def test_an_extract_of_only_skip_links_has_no_real_text(extract):
    assert not has_real_text(extract)


@pytest.mark.parametrize("extract", [
    "Free delivery on orders over $45. Shop tools, appliances and garden supplies.",
    "Skip to main content\n\nWelcome to the docs.",
    "# Skip to content: how skip links help",
    "Main content",
    "Skip",
    "OK",
])
def test_any_real_line_is_real_text_however_short(extract):
    assert has_real_text(extract)
