"""The docs index gives each heading the anchor the docs page itself gives it, so a cited link lands on its section
(F1664).

The docs (Fumadocs, `remark-heading`) give a heading its id two ways. A heading ending in `[#id]` has that id, as
written, and the `[#id]` is not shown. Any other heading's id is github-slugger's of its words, counted over every
heading on the page: the second "Request" on a page is `request-1`. The index slugged a tagged heading's words with
its tag (`what-you-can-count-on-what-makes-synapse-different`) and showed the tag in the heading, and it gave a
repeated heading the first one's anchor, so a link to either landed on the page's top or on the wrong section.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SHIPPED = ROOT / "backend" / "core" / "native_builder" / "docs_topics.json"
DOCS_REPO = Path(os.environ.get("DOCS_REPO") or Path.home() / "docs")

_spec = importlib.util.spec_from_file_location("docs_index", ROOT / "scripts" / "docs_index.py")
docs_index = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(docs_index)


def _page(tmp_path: Path, body: str) -> list[dict]:
    docs = tmp_path / "content" / "docs"
    docs.mkdir(parents=True)
    (docs / "page.mdx").write_text(f"---\ntitle: A page\nslug: /a-page\n---\n\n{body}", encoding="utf-8")
    return docs_index.index_dir(docs)["pages"][0]["sections"]


def _anchors(sections: list[dict]) -> list[tuple[str, str]]:
    return [(s["heading"], s["anchor"]) for s in sections if s["heading"]]


class TestTheGenerator:
    def test_a_heading_tagged_with_an_id_has_that_id_and_does_not_show_the_tag(self, tmp_path):
        made = _page(tmp_path, "## What you can count on [#what-makes-synapse-different]\n\nWords.\n\n"
                               "### Which model? [#which-model]\n\nMore words.\n")
        assert _anchors(made) == [("What you can count on", "what-makes-synapse-different"),
                                  ("Which model?", "which-model")]

    def test_a_repeated_heading_is_numbered_as_the_docs_number_it(self, tmp_path):
        made = _page(tmp_path, "## Request\n\nOne.\n\n## Response\n\nTwo.\n\n## Request\n\nThree.\n\n"
                               "### Request\n\nFour.\n")
        assert _anchors(made) == [("Request", "request"), ("Response", "response"), ("Request", "request-1"),
                                  ("Request", "request-2")]

    def test_headings_the_index_leaves_out_still_count_toward_the_numbering(self, tmp_path):
        # The docs number every heading on the page, at any level; the index keeps the second and third levels.
        made = _page(tmp_path, "#### Prerequisites\n\nA small one.\n\n## Prerequisites\n\nThe section.\n")
        assert _anchors(made) == [("Prerequisites", "prerequisites-1")]

    def test_a_tagged_heading_takes_no_number_and_gives_none(self, tmp_path):
        made = _page(tmp_path, "## Setup [#install]\n\nA.\n\n## Setup\n\nB.\n\n## Setup\n\nC.\n")
        assert _anchors(made) == [("Setup", "install"), ("Setup", "setup"), ("Setup", "setup-1")]

    def test_a_heading_inside_a_code_block_is_no_heading(self, tmp_path):
        made = _page(tmp_path, "```md\n## Request\n```\n\n## Request\n\nThe real one.\n")
        assert _anchors(made) == [("Request", "request")]


class TestTheShippedIndex:
    def _pages(self) -> list[dict]:
        return json.loads(SHIPPED.read_text(encoding="utf-8"))["pages"]

    def test_no_heading_shows_an_id_tag(self):
        tagged = [(p["slug"], s["heading"]) for p in self._pages() for s in p["sections"] if "[#" in s["heading"]]
        assert tagged == []

    def test_no_page_names_one_anchor_twice(self):
        """An anchor named twice reaches only its first section."""
        twice = []
        for p in self._pages():
            anchors = [s["anchor"] for s in p["sections"] if s["anchor"]]
            twice += [(p["slug"], a) for a in sorted(set(anchors)) if anchors.count(a) > 1]
        assert twice == []


class TestEveryAnchorExistsOnItsPage:
    """Read against the docs' own files at the index's commit, where the docs repo is at hand (DOCS_REPO, else ~/docs):
    a tagged heading's anchor is the id written in its tag, and every other anchor is one the page's headings make."""

    def _files(self) -> dict[str, str]:
        commit = json.loads(SHIPPED.read_text(encoding="utf-8"))["commit"]
        if not (DOCS_REPO / ".git").exists():
            pytest.skip(f"SKIPPED, not passed: no docs repo at {DOCS_REPO} (set DOCS_REPO)")
        if subprocess.run(["git", "-C", str(DOCS_REPO), "cat-file", "-e", f"{commit}^{{commit}}"],
                          capture_output=True).returncode:
            pytest.skip(f"SKIPPED, not passed: the docs repo at {DOCS_REPO} does not have {commit[:8]}")
        names = subprocess.run(["git", "-C", str(DOCS_REPO), "ls-tree", "-r", "--name-only", commit, "content/docs"],
                               capture_output=True, text=True, check=True).stdout.split()
        out: dict[str, str] = {}
        for name in names:
            if name.endswith(".mdx"):
                text = subprocess.run(["git", "-C", str(DOCS_REPO), "show", f"{commit}:{name}"], capture_output=True,
                                      text=True, check=True).stdout
                slug = re.search(r"^slug:\s*['\"]?(/[^'\"\n]*)", text, re.M)
                if slug:
                    out[slug.group(1).strip()] = text
        return out

    def test_each_tagged_heading_s_anchor_is_the_id_its_tag_writes(self):
        files = self._files()
        pages = {p["slug"]: p for p in json.loads(SHIPPED.read_text(encoding="utf-8"))["pages"]}
        checked = 0
        for slug, text in files.items():
            tags = re.findall(r"^#{2,3}\s+(.+?)\s*\[#([^\]]+)\]\s*$", text, re.M)
            named = {s["anchor"] for s in pages[slug]["sections"]}
            for heading, tag in tags:
                assert tag in named, f"{slug}: the heading {heading!r} is tagged #{tag}, which the index never names"
                checked += 1
        assert checked >= 100, "the docs tag about a hundred headings: the files were not read"

    def test_every_anchor_the_index_names_is_a_tag_or_a_slug_of_a_heading_on_its_page(self):
        files = self._files()
        for page in json.loads(SHIPPED.read_text(encoding="utf-8"))["pages"]:
            text = files[page["slug"]]
            tags = set(re.findall(r"\[#([^\]]+)\]\s*$", text, re.M))
            slugs = {docs_index.anchor(h) for h in re.findall(r"^#{1,6}\s+(.+?)\s*#*\s*$", text, re.M)}
            for s in page["sections"]:
                if s["anchor"]:
                    base = re.sub(r"-\d+$", "", s["anchor"])
                    assert s["anchor"] in tags or s["anchor"] in slugs or base in slugs, (
                        f"{page['slug']}#{s['anchor']}: no heading on the page makes this anchor")
