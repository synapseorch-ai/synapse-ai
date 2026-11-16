"""Our own links to the site's pages take its final slash form (F1644).

The site serves each page at its address with a final slash (`/cloud/`), and the address without it redirects there.
Search Console kept listing those redirecting addresses as pages it could not index, and the strongest source of them
was our own: the README's link to the cloud page, which GitHub, PyPI and npm all show. Every link to one of the site's
pages in what this repository ships (its README and docs, its code and its copy) now takes the final slash. Tests and
the release notes are left as they are: test data is made up, and a published release's notes never change.

Near misses: a link to a file (`/install/v2.0.1.sh`), the site itself (`https://synapseorch.com/`), another host
of ours (`docs.synapseorch.com`), and a prefix the code joins a file name to, each named with its reason.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]

#: A link to a page of the site with no final slash: a path whose last part is not a file and has no slash after it.
NO_SLASH = re.compile(r"https?://(?:www\.)?synapseorch\.com(?:/[A-Za-z0-9_-]+)+(?![A-Za-z0-9_./-])")
#: What the repository ships and keeps as text.
_TEXT = (".md", ".mdx", ".py", ".ts", ".tsx", ".js", ".mjs", ".json", ".html", ".sh", ".ps1", ".yml", ".yaml", ".toml",
         ".txt", ".cfg")
#: Never scanned: test data is made up, and a published release's notes are immutable.
_LEFT = ("backend/tests/", "tests/", ".github/release-notes/", "frontend/src/__tests__/")
#: A prefix the code joins a file name to, never a page: (file, the line's own words, why).
_PREFIXES = (("synapse/installer/versions.py", "_RELEASE_SETUP = ",
              "joined to `/<tag>.sh` and `/<tag>.ps1`, a file, never shown as a page"),)


def no_slash_links(text: str) -> list[str]:
    return NO_SLASH.findall(text)


def _shipped() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [p for p in out.splitlines() if p.endswith(_TEXT) and not p.startswith(_LEFT) and "lock" not in p]


class TestTheDetector:
    @pytest.mark.parametrize("text", ['<a href="https://synapseorch.com/cloud">',
                                      "https://www.synapseorch.com/switch and more",
                                      "(https://synapseorch.com/features/build-with-ai)"])
    def test_a_page_with_no_final_slash_is_found(self, text):
        assert no_slash_links(text), text

    @pytest.mark.parametrize("text", ["https://synapseorch.com/cloud/", "https://synapseorch.com/",
                                      "https://synapseorch.com", "curl -LsSf https://synapseorch.com/install/v2.0.1.sh",
                                      "https://docs.synapseorch.com/getting-started/docker",
                                      "mailto:hello@synapseorch.com"])
    def test_its_near_misses_are_not(self, text):
        assert not no_slash_links(text), text


def test_every_link_we_ship_to_a_page_of_the_site_takes_its_final_slash():
    found = []
    for path in _shipped():
        try:
            lines = (ROOT / path).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for n, line in enumerate(lines, 1):
            if any(path == f and words in line for f, words, _ in _PREFIXES):
                continue
            found += [f"{path}:{n}: {link}" for link in no_slash_links(line)]
    assert not found, "add the final slash:\n" + "\n".join(found)
