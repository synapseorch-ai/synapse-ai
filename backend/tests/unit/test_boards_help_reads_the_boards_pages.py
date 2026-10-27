"""The Boards help reads the docs' Boards pages, not the workflows guide (GUIDE1, D318, F1044).

Boards had no page of their own when the "?" topics were written, so all three sent people to the workflows guide.
DOC5's first publish has them: /boards (what a board is), /boards/tasks (working on one) and /boards/agents (what an
agent reads and claims). That each URL exists is the docs' own check (check.mjs's HELP_GUIDES); this holds the
topics to them, and the search_help copy (`help_topics.json`) to the same.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HELP = ROOT / "frontend" / "src" / "lib" / "help.ts"
TOPICS_JSON = ROOT / "backend" / "core" / "native_builder" / "help_topics.json"

BOARDS_GUIDES = {"boards": "/boards", "boards.board": "/boards/tasks", "orchestrations.board": "/boards/agents"}


def _guide_in_help(key: str) -> str:
    text = HELP.read_text("utf-8")
    head = re.search(rf"(?m)^    '?{re.escape(key)}'?: \{{", text)
    assert head, f"no {key} topic in help.ts"
    block_end = re.search(r"(?m)^    \},?$", text[head.end():])
    block = text[head.end(): head.end() + block_end.start()]
    found = re.search(r"guide: '([^']+)'", block)
    assert found, f"{key} has no guide"
    return found.group(1)


def test_each_boards_topic_reads_its_boards_page():
    assert {key: _guide_in_help(key) for key in BOARDS_GUIDES} == BOARDS_GUIDES


def test_the_search_help_copy_says_the_same():
    data = json.loads(TOPICS_JSON.read_text("utf-8"))
    assert {key: data[key].get("guide") for key in BOARDS_GUIDES} == BOARDS_GUIDES
