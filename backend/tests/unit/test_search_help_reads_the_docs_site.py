"""search_help reads the docs site too (F1257, GUIDE1; D340 A, "the planner knows the product").

The "?" panel says a screen's steps in a few lines; the docs site says the rest. Neither the planner nor the answer
step could read the docs, so a question the panel did not cover was answered from what products usually do. Now
`scripts/docs_index.py` writes the docs repo's pages (`native_builder/docs_topics.json`, a snapshot regenerated at
each docs publish) and `search_help` returns the best of them after the panel's topics, each with its link.

* A page is known by its `slug`, never its file path, and each section by the anchor the docs give its heading.
* A page is for one edition by its folder (`cloud/`, `self-host/`) or both; a host cites only its own and both.
* The generated API reference pages keep their title and description, and rank below the guides.
* The snapshot is read once, on the first search, never on import, and is no part of the prompt bundle.
* It is held to the docs repo at its own commit, from git, so a docs push never turns this suite red.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

from core import builder_help as H

ROOT = Path(__file__).resolve().parents[3]
SHIPPED = ROOT / "backend" / "core" / "native_builder" / "docs_topics.json"
HELP = ROOT / "backend" / "core" / "native_builder" / "help_topics.json"
DOCS_REPO = Path(os.environ.get("DOCS_REPO") or Path.home() / "docs")

_spec = importlib.util.spec_from_file_location("docs_index", ROOT / "scripts" / "docs_index.py")
docs_index = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(docs_index)


def _shipped() -> dict:
    return json.loads(SHIPPED.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _the_shipped_docs():
    """Each test starts from the snapshot as shipped, for the self-hosted edition."""
    H.set_docs_index(None)
    H.set_docs_edition("self_hosted")
    yield
    H.set_docs_index(None)
    H.set_docs_edition("self_hosted")


# ── the generator ───────────────────────────────────────────────────────────────────────────────────────────────

def _tree(tmp_path: Path) -> Path:
    docs = tmp_path / "content" / "docs"
    files = {
        "guides/start/evals.mdx": ("---\ntitle: \"Score runs with evals\"\ndescription: Watch a workflow's quality.\n"
                                   "slug: /guides/start/evals\n---\n\nimport { Cards } from 'fumadocs-ui';\n\n"
                                   "A run that finished has not **necessarily** done its job.\n\n<Cards>\n  <Card "
                                   "title=\"x\" />\n</Cards>\n\n## Write a rubric\n\nOpen [Runs › Evals](/evals) and "
                                   "press `New rubric`.\n\n```bash\nnot prose\n```\n\n### Bind it, to the workflow!\n"
                                   "\nPick the workflow."),
        "cloud/cloud-wallet.mdx": "---\ntitle: Models wallet\nslug: /cloud/wallet\n---\n\nCredits pay for models.\n",
        "self-host/telemetry.mdx": "---\ntitle: Telemetry\nslug: /self-host/telemetry\n---\n\nWhat a local install "
                                   "sends.\n",
        "api/reference/v1/runs.mdx": ("---\ntitle: List runs\ndescription: GET /v1/runs\nslug: /api/v1/runs\n"
                                      "_openapi:\n  method: GET\n---\n\n## Response\n\nA list.\n"),
        "notes/draft.mdx": "---\ntitle: No slug yet\n---\n\nA page with no URL of its own.\n",
    }
    for rel, text in files.items():
        (docs / rel).parent.mkdir(parents=True, exist_ok=True)
        (docs / rel).write_text(text, encoding="utf-8")
    return tmp_path


class TestTheGenerator:
    def test_each_page_by_its_slug_with_its_edition_and_kind(self, tmp_path):
        made = docs_index.index_dir(_tree(tmp_path) / "content" / "docs", "abc123")
        assert made["commit"] == "abc123"
        assert [(p["slug"], p["edition"], p["kind"]) for p in made["pages"]] == [
            ("/api/v1/runs", "both", "reference"), ("/cloud/wallet", "cloud", "guide"),
            ("/guides/start/evals", "both", "guide"), ("/self-host/telemetry", "self_hosted", "guide")]

    def test_a_page_with_no_slug_has_no_url_and_is_left_out(self, tmp_path):
        made = docs_index.index_dir(_tree(tmp_path) / "content" / "docs")
        assert "No slug yet" not in [p["title"] for p in made["pages"]], "a URL is never made from a file path"

    def test_its_sections_are_prose_by_heading_with_the_docs_own_anchors(self, tmp_path):
        made = docs_index.index_dir(_tree(tmp_path) / "content" / "docs")
        evals = next(p for p in made["pages"] if p["slug"] == "/guides/start/evals")
        assert evals["sections"] == [
            {"heading": "", "anchor": "", "excerpt": "A run that finished has not necessarily done its job."},
            {"heading": "Write a rubric", "anchor": "write-a-rubric",
             "excerpt": "Open Runs › Evals and press New rubric."},
            {"heading": "Bind it, to the workflow!", "anchor": "bind-it-to-the-workflow",
             "excerpt": "Pick the workflow."}], "no import, tag or code block, and links as their words"

    def test_a_reference_page_keeps_its_title_and_description_only(self, tmp_path):
        made = docs_index.index_dir(_tree(tmp_path) / "content" / "docs")
        runs = next(p for p in made["pages"] if p["slug"] == "/api/v1/runs")
        assert runs["sections"] == [] and runs["description"] == "GET /v1/runs"

    def test_a_long_section_is_cut_at_a_word(self, tmp_path):
        docs = tmp_path / "content" / "docs"
        docs.mkdir(parents=True)
        (docs / "long.mdx").write_text("---\ntitle: Long\nslug: /long\n---\n\n" + "word " * 200, encoding="utf-8")
        [section] = docs_index.index_dir(docs)["pages"][0]["sections"]
        assert len(section["excerpt"]) <= docs_index.EXCERPT + 1 and section["excerpt"].endswith("word…")


# ── what ships ──────────────────────────────────────────────────────────────────────────────────────────────────

class TestTheSnapshot:
    def test_its_shape(self):
        made = _shipped()
        assert len(made["commit"]) == 40 and made["pages"]
        assert {p["edition"] for p in made["pages"]} <= {"both", "cloud", "self_hosted"}
        assert {p["kind"] for p in made["pages"]} <= {"guide", "reference"}
        assert all(p["slug"].startswith("/") and p["title"] for p in made["pages"])
        assert [p["slug"] for p in made["pages"]] == sorted({p["slug"] for p in made["pages"]}), "once each, by slug"

    def test_every_help_topic_s_docs_link_is_a_page_and_its_anchor_a_section(self):
        """The "?" panel's "Read the guide" links (help.ts `guide`): a page that moved, or a heading renamed under
        a link's anchor, fails here, not on a person's click."""
        pages = {p["slug"]: p for p in _shipped()["pages"]}
        links = [t["guide"] for t in json.loads(HELP.read_text()).values() if t.get("guide")]
        assert links
        for link in links:
            slug, _, at = link.partition("#")
            assert slug in pages, f"{link}: no docs page has this slug"
            if at:
                assert at in {s["anchor"] for s in pages[slug]["sections"]}, f"{link}: no section has this anchor"

    def test_it_is_no_part_of_the_prompt_bundle(self):
        from core.native_builder import seed

        assert "docs_topics" not in json.dumps(seed.bundled_definitions())


class TestTheSnapshotIsTheDocsAtItsCommit:
    """Held to the docs repo where it is at hand (DOCS_REPO, else ~/docs), at the snapshot's own commit read from
    git: the same answer on any machine, and a docs push never turns this red. Regenerating at the published commit
    is the release's own row (#4 checklist)."""

    def _repo_with_the_commit(self) -> str:
        commit = _shipped()["commit"]
        if not (DOCS_REPO / ".git").exists():
            pytest.skip(f"SKIPPED, not passed: no docs repo at {DOCS_REPO} (set DOCS_REPO) to hold the snapshot to")
        if subprocess.run(["git", "-C", str(DOCS_REPO), "cat-file", "-e", f"{commit}^{{commit}}"],
                          capture_output=True).returncode:
            pytest.skip(f"SKIPPED, not passed: the docs repo at {DOCS_REPO} does not have {commit[:8]}")
        return commit

    def test_its_pages_are_the_generator_s_at_that_commit(self):
        commit = self._repo_with_the_commit()
        assert docs_index.index_at(DOCS_REPO, commit)["pages"] == _shipped()["pages"], (
            "docs_topics.json is not what scripts/docs_index.py makes of its own commit: regenerate it")

    def test_the_docs_have_not_moved_since(self):
        commit = self._repo_with_the_commit()
        head = docs_index.head(DOCS_REPO)
        if head != commit:
            pytest.skip(f"docs moved since {commit[:8]} (now {head[:8]}); regenerate at the release")


# ── the search ──────────────────────────────────────────────────────────────────────────────────────────────────

def _edition_of(url: str) -> str:
    slug = url.removeprefix(H.DOCS_SITE).partition("#")[0]
    return next(p["edition"] for p in _shipped()["pages"] if p["slug"] == slug)


class TestTheSearch:
    async def test_evaluating_a_workflow_finds_the_panel_s_topic_then_the_docs_page(self):
        got = await H.search_help({"question": "How do I evaluate a workflow with a rubric?"}, None, [])
        assert got["topics"], "the panel's own words first"
        urls = [d["url"] for d in got["docs"]]
        assert any(u.startswith(f"{H.DOCS_SITE}/guides/start/evals") or u.startswith(f"{H.DOCS_SITE}/evals")
                   for u in urls), urls
        assert all(d["title"] and d["excerpt"] for d in got["docs"]) and len(got["docs"]) <= H.DOCS_TOP

    async def test_the_self_hosted_product_never_cites_a_cloud_only_page(self):
        for question in ("credits wallet plan", "sign up for cloud", "Google sign in on cloud", "notifications"):
            got = await H.search_help({"question": question}, None, [])
            assert "cloud" not in {_edition_of(d["url"]) for d in got.get("docs", [])}, question

    async def test_cloud_cites_its_own_pages_and_never_a_self_hosted_only_one(self):
        H.set_docs_edition("cloud")
        editions = set()
        for question in ("credits wallet plan", "telemetry a local install sends", "upgrade the install"):
            editions |= {_edition_of(d["url"]) for d in (await H.search_help({"question": question}, None, []))
                         .get("docs", [])}
        assert "cloud" in editions and "self_hosted" not in editions

    def test_a_guide_outranks_a_reference_page_on_the_same_words(self):
        H.set_docs_index([
            {"slug": "/api/runs", "title": "List runs", "description": "runs", "edition": "both",
             "kind": "reference", "sections": []},
            {"slug": "/runs", "title": "List runs", "description": "runs", "edition": "both", "kind": "guide",
             "sections": []}])
        assert [d["url"] for d in H.search_docs("list runs")] == [f"{H.DOCS_SITE}/runs", f"{H.DOCS_SITE}/api/runs"]

    async def test_only_the_docs_answer_says_so_and_nothing_says_so_as_before(self):
        H.set_docs_index([{"slug": "/zzqx", "title": "Zzqx flurble", "description": "", "edition": "both",
                           "kind": "guide", "sections": []}])
        got = await H.search_help({"question": "zzqx flurble"}, None, [])
        assert got["topics"] == [] and got["docs"][0]["url"] == f"{H.DOCS_SITE}/zzqx"
        assert "answer only from the docs pages below" in got["note"]
        H.set_docs_index([])
        got = await H.search_help({"question": "zzqx flurble"}, None, [])
        assert "docs" not in got and "do not invent a screen" in got["note"]

    def test_the_snapshot_is_read_on_the_first_search_never_on_import(self):
        assert H._docs is None, "nothing read before a search"
        H.search_docs("schedule")
        assert H._docs, "read once, on the first search"


class TestTheResearchersReadIt:
    """What this product does is product knowledge, never the web (8a, 10-30): both researchers read search_help."""

    @pytest.mark.parametrize("role", ["plan_researcher", "researcher"])
    def test_each_has_it_and_its_rule(self, role):
        from core.native_builder import seed

        agent = next(a for a in seed.bundled_definitions()["agents"] if a.get("system_role") == role)
        assert "search_help" in agent["tools"]
        prompt = " ".join(agent["system_prompt"].split())
        assert "is in `search_help`: its own help and docs pages. Read it there, never on the web." in prompt
