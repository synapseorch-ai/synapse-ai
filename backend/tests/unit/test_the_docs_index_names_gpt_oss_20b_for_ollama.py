"""The docs index Synapse AI searches names gpt-oss:20b for Ollama, never a pull of mistral (F1653).

`search_help` reads a snapshot of the docs site's pages (`native_builder/docs_topics.json`, made by
`scripts/docs_index.py`). The docs' self-host quick start said to pull mistral, a dense 7B that reads 32K: Synapse AI
needs 128K, so a person following it got a model Synapse AI refuses. The docs now say `ollama pull gpt-oss:20b`, a
model that reads 128K, and the snapshot is made again from them.
"""
from __future__ import annotations

import json
from pathlib import Path

INDEX = Path(__file__).resolve().parents[2] / "core" / "native_builder" / "docs_topics.json"


def _pages() -> list[dict]:
    return json.loads(INDEX.read_text("utf-8"))["pages"]


def test_the_index_quotes_no_pull_of_mistral():
    said = [p["slug"] for p in _pages() if "ollama pull mistral" in json.dumps(p)]
    assert not said, said


def test_the_self_host_quick_start_names_gpt_oss_20b():
    quick = [p for p in _pages() if p.get("edition") == "self_hosted" or "quick" in p.get("slug", "")]
    named = [p["slug"] for p in quick if "ollama pull gpt-oss:20b" in json.dumps(p)]
    assert named, sorted(p["slug"] for p in quick)
