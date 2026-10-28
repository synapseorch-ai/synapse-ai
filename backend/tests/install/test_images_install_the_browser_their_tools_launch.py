"""An image that runs a tool which launches a browser installs that browser (F1159).

The worker image (Dockerfile.worker) runs the web scraper on every worker (`WORKER_NATIVE_TOOLS`), and the scraper
launches Playwright's Chromium, but the image installed none: every scrape on a scale-mode worker failed with
"Executable doesn't exist". The class: whichever native tool an image runs, if its module drives Playwright, the
image runs `playwright install` for Chromium.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.tools_registry import ALL_NATIVE_TOOLS, WORKER_NATIVE_TOOLS

REPO = Path(__file__).resolve().parents[3]

#: Each image, and the native tools it runs.
IMAGES = {
    "Dockerfile": set(ALL_NATIVE_TOOLS),
    "Dockerfile.backend": set(ALL_NATIVE_TOOLS),
    "Dockerfile.worker": WORKER_NATIVE_TOOLS,
}


def _launches_a_browser(tool: str) -> bool:
    source = Path(ALL_NATIVE_TOOLS[tool]).read_text(encoding="utf-8")
    return re.search(r"^\s*(from|import) playwright\b", source, re.M) is not None


def _installs_chromium(dockerfile: str) -> bool:
    text = (REPO / dockerfile).read_text(encoding="utf-8").replace("\\\n", " ")
    return re.search(r"^RUN .*playwright install\b.*\bchromium", text, re.M) is not None


def test_the_scraper_is_a_tool_that_launches_a_browser():
    assert _launches_a_browser("web_scraper"), "the scan below must see the scraper's Playwright import"


@pytest.mark.parametrize("dockerfile", sorted(IMAGES))
def test_an_image_installs_the_browser_its_tools_launch(dockerfile):
    launching = sorted(t for t in IMAGES[dockerfile] if _launches_a_browser(t))
    if launching:
        assert _installs_chromium(dockerfile), f"{dockerfile} runs {launching} but installs no Chromium"
