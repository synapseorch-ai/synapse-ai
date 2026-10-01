"""Pipeline presets (B2b, D167): named starting points that set only the fields they name, and the
settings screen's mirror of them, of the index-affecting settings and of the languages."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from core.knowledge import presets
from core.knowledge.kb_settings import INDEX_AFFECTING, KnowledgeBase, Rerank
from core.knowledge.textsearch import TS_LANGUAGES

FRONTEND = Path(__file__).resolve().parents[3] / "frontend" / "src" / "lib" / "knowledge.ts"
KB = KnowledgeBase(id="kb_00000000000a", name="Policies", folder="policies")


def test_balanced_is_the_defaults():
    assert presets.matches(KB) == "balanced"
    assert presets.apply(KB, "balanced") == KB


@pytest.mark.parametrize("preset_id", [p.id for p in presets.PRESETS])
def test_each_preset_is_a_valid_base_that_matches_itself(preset_id):
    applied = presets.apply(KB, preset_id)
    assert presets.matches(applied) == preset_id
    assert applied.revision == KB.revision and applied.folder == KB.folder


def test_the_presets_name_the_same_fields_and_differ():
    shapes = {p.id: {b: sorted(f) for b, f in p.fields.items()} for p in presets.PRESETS}
    assert len({json.dumps(s, sort_keys=True) for s in shapes.values()}) == 1, "switching presets is clean"
    assert len({json.dumps(p.fields, sort_keys=True) for p in presets.PRESETS}) == len(presets.PRESETS)


def test_a_preset_keeps_what_it_does_not_name():
    mine = KB.model_copy(update={"rerank": Rerank(model="cohere.rerank-v3.5"), "budget_monthly_usd": 5.0,
                                 "language": "english", "visitor_access": True})
    applied = presets.apply(mine, "high_recall")
    assert applied.rerank.model == "cohere.rerank-v3.5" and applied.rerank.candidates == 100
    assert (applied.budget_monthly_usd, applied.language, applied.visitor_access) == (5.0, "english", True)
    assert applied.embedding == mine.embedding


def test_no_preset_picks_a_model_or_turns_rerank_on():
    for preset in presets.PRESETS:
        assert "embedding" not in preset.fields and "language" not in preset.fields
        assert "model" not in preset.fields.get("rerank", {})
        assert preset.fields["rerank"]["enabled"] in (None, False)


def test_custom_settings_match_no_preset():
    assert presets.matches(KB.model_copy(update={"retrieval": KB.retrieval.model_copy(update={"top_k": 9})})) is None
    with pytest.raises(ValueError, match="cheap"):
        presets.get("fast")


# ── the settings screen's mirrors ──────────────────────────────────────────────

def _ts() -> str:
    return FRONTEND.read_text("utf-8")


def test_the_screen_mirrors_the_presets():
    block = re.search(r"export const PRESETS: PipelinePreset\[\] = (\[.*?\n\]);", _ts(), re.DOTALL)
    assert block, "PRESETS is gone from frontend/src/lib/knowledge.ts"
    assert json.loads(block.group(1)) == presets.as_json()


def test_the_screen_mirrors_what_rebuilds_the_index_and_the_languages():
    affecting = re.search(r"export const INDEX_AFFECTING = \[(.*?)\] as const;", _ts(), re.DOTALL)
    languages = re.search(r"export const LANGUAGES = \[(.*?)\] as const;", _ts(), re.DOTALL)
    assert affecting and languages
    assert tuple(re.findall(r"'([a-z_]+)'", affecting.group(1))) == INDEX_AFFECTING
    assert tuple(re.findall(r"'([a-z_]+)'", languages.group(1))) == TS_LANGUAGES
