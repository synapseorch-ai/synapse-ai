"""The bundled template packs say what the product says (F515).

D209 retired "Orchestration(s)" for Workflows and "MCP servers" for Apps, and F511 took them off every screen; the
Templates page was left out of ui-smoke's `oldwords` because its packs carry their own names and descriptions in
`examples/index.json` ("Simple Orchestration Pack"). Everything the page shows of a pack is checked here: its name, its
description, and each of its workflows' names. Prompts are not shown, so they are not read.
"""

import json
import re
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
#: ui-smoke's `_OLD_WORDS` (scripts/ui-smoke.py, F511).
OLD_WORDS = re.compile(r"\borchestrations?\b|\bMCP servers?\b", re.I)

PACKS = json.loads((EXAMPLES / "index.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("pack", PACKS, ids=[p["id"] for p in PACKS])
def test_a_packs_name_and_description_say_todays_words(pack):
    for field in ("name", "description"):
        assert not OLD_WORDS.search(pack[field]), f"{pack['id']}'s {field} says {pack[field]!r}"


@pytest.mark.parametrize("pack", PACKS, ids=[p["id"] for p in PACKS])
def test_a_packs_workflow_names_say_todays_words(pack):
    bundle = json.loads((EXAMPLES / pack["file"]).read_text(encoding="utf-8"))
    for workflow in bundle.get("orchestrations") or []:
        assert not OLD_WORDS.search(workflow.get("name") or ""), f"{pack['id']} shows {workflow['name']!r}"
