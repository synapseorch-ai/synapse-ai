#!/usr/bin/env python3
"""The docs site's pages as search_help reads them (F1257, GUIDE1; D340 A): backend/core/native_builder/docs_topics.json.

The docs live in their own repo (docs.synapseorch.com, Fumadocs). The product never reads it at run time: this writes
a snapshot of its pages that ships with the backend, offline and with no egress, beside help_topics.json (the "?"
panel's). It is regenerated at each docs publish (the #4 checklist's row) and a test holds it to the docs repo
whenever that repo is at hand.

Each page: its `slug` (its URL, never its file path), title, description, edition and kind, and its sections, each a
heading with its anchor and a short excerpt of its prose.

* edition: what the page is for, by its folder — `cloud/` is cloud, `self-host/` is self_hosted, the rest both. A
  host's planner never cites a page for the other edition.
* kind: `reference` for the generated API pages (their frontmatter carries `_openapi`), which keep only their title
  and description and rank below `guide`, every other page.

    python scripts/docs_index.py <docs repo> [out.json]
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "backend" / "core" / "native_builder" / "docs_topics.json"
#: The longest excerpt a section keeps, in characters.
EXCERPT = 400
EDITIONS = {"cloud": "cloud", "self-host": "self_hosted"}

_FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_HEADING = re.compile(r"^(#{2,3})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _front(text: str) -> tuple[dict[str, str], bool, str]:
    """The frontmatter's top-level string keys, whether it is a generated API page, and the body after it."""
    m = _FRONT.match(text)
    if not m:
        return {}, False, text
    keys: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if line[:1] in (" ", "\t") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        keys[key.strip()] = value
    return keys, "_openapi" in keys, text[m.end():]


def anchor(heading: str) -> str:
    """The id the docs give a heading (github-slugger, as Fumadocs does): lower case, punctuation gone, spaces as
    hyphens."""
    words = re.sub(r"[^\w\- ]", "", _plain(heading).lower(), flags=re.UNICODE)
    return words.replace(" ", "-")


def _plain(text: str) -> str:
    """Markdown and MDX as words: links as their text, no emphasis, code marks or tags."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[*_`]{1,3}", "", text)
    text = re.sub(r"\{[^{}]*\}", " ", text)
    return " ".join(text.split())


def _excerpt(lines: list[str]) -> str:
    words = _plain(" ".join(lines))
    if len(words) <= EXCERPT:
        return words
    cut = words[:EXCERPT].rsplit(" ", 1)[0]
    return cut.rstrip(",;:") + "…"


def _sections(body: str) -> list[dict[str, str]]:
    """The page's prose by heading: the words before the first heading as the page's own (no heading, no anchor)."""
    out: list[dict[str, str]] = []
    heading, lines, fenced = "", [], False
    for raw in body.splitlines():
        if _FENCE.match(raw):
            fenced = not fenced
            continue
        if fenced or raw.lstrip().startswith(("import ", "export ")):
            continue
        m = _HEADING.match(raw)
        if m:
            if heading or _plain(" ".join(lines)):
                out.append({"heading": heading, "anchor": anchor(heading) if heading else "",
                            "excerpt": _excerpt(lines)})
            heading, lines = _plain(m.group(2)), []
            continue
        lines.append(raw)
    if heading or _plain(" ".join(lines)):
        out.append({"heading": heading, "anchor": anchor(heading) if heading else "", "excerpt": _excerpt(lines)})
    return [s for s in out if s["heading"] or s["excerpt"]]


def page(path: Path, docs: Path) -> dict | None:
    keys, reference, body = _front(path.read_text(encoding="utf-8"))
    slug = keys.get("slug", "")
    if not slug.startswith("/"):
        return None  # a page with no slug has no URL of its own: never derived from its path
    folder = path.relative_to(docs).parts[0] if len(path.relative_to(docs).parts) > 1 else ""
    return {"slug": slug, "title": keys.get("title", ""), "description": keys.get("description", ""),
            "edition": EDITIONS.get(folder, "both"), "kind": "reference" if reference else "guide",
            "sections": [] if reference else _sections(body)}


def index_dir(docs: Path, commit: str = "") -> dict:
    """Every page under `docs` (a tree's `content/docs`), by slug."""
    pages = [p for p in (page(f, docs) for f in sorted(docs.rglob("*.mdx"))) if p]
    return {"commit": commit, "pages": sorted(pages, key=lambda p: p["slug"])}


def head(repo: Path) -> str:
    """The docs repo's checked-out commit, or "" when it has none."""
    try:
        return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def index(repo: Path) -> dict:
    """Every page of the docs repo as checked out, stamped with its commit."""
    return index_dir(repo / "content" / "docs", head(repo))


def index_at(repo: Path, commit: str) -> dict:
    """Every page of the docs repo at `commit`, read from git (`git archive`), whatever is checked out: the same
    answer on any machine that has the commit. Raises when the repo does not have it."""
    import io
    import tarfile
    import tempfile

    tar = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", commit, "content/docs"],
                         capture_output=True, check=True).stdout
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=io.BytesIO(tar)) as archive:
            archive.extractall(tmp, filter="data")
        return index_dir(Path(tmp) / "content" / "docs", commit)


def write(made: dict, out: Path = OUT) -> None:
    out.write_text(json.dumps(made, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    made = index(Path(sys.argv[1]))
    write(made, Path(sys.argv[2]) if len(sys.argv) > 2 else OUT)
    print(f"{len(made['pages'])} pages from {made['commit'][:8] or 'a tree with no commit'}")
