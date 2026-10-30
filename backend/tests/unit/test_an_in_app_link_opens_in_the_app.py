"""A link to one of the app's own screens opens in the app; every other link opens in a new tab (F1265, GUIDE1).

A "can't" now ends with its screen's link, `[Evals](/evals)` (D340 R6). The kit's Markdown drew every link as an
outside one, a new tab with ↗, so the screen opened in a second copy of the app. Now an href that is one of this app's
paths opens in place. The test is on the href itself, since the same renderer draws a model's words: only one "/" and
then a character that is not a slash is the app's, so `//evil.example` (protocol-relative) and `/\\evil.example` stay
outside links, as do `https:`, `mailto:` and `javascript:`. OSS has no unit runner for its TypeScript, so the pattern is
read from the source and tried here; cloud's vitest tries the component.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

MARKDOWN = Path(__file__).resolve().parents[3] / "frontend" / "src" / "components" / "ui" / "markdown.tsx"


def _in_app() -> re.Pattern:
    found = re.search(r"const IN_APP = /(.+?)/;", MARKDOWN.read_text("utf-8"))
    assert found, "markdown.tsx names no IN_APP pattern"
    return re.compile(found.group(1).replace(r"\/", "/"))


@pytest.mark.parametrize("href", ["/evals", "/settings/models", "/browser/recordings/rec_1#steps", "/?tab=1"])
def test_one_of_the_app_s_paths_is_in_the_app(href):
    assert _in_app().search(href)


@pytest.mark.parametrize("href", ["//evil.example/evals", "/\\evil.example", "https://docs.synapseorch.com/evals",
                                  "mailto:a@b.example", "javascript:alert(1)", "evals", "", "#k1"])
def test_anything_else_is_not(href):
    assert not _in_app().search(href)


def test_an_in_app_link_is_the_app_s_own_link_and_never_a_new_tab():
    text = MARKDOWN.read_text("utf-8")
    assert re.search(r"""import NextLink from ["']next/link["']""", text)
    branch = text[text.index("IN_APP.test(href)"):]
    branch = branch[: branch.index(") : (")]
    assert "<NextLink" in branch and "target=" not in branch and "ExternalLink" not in branch
