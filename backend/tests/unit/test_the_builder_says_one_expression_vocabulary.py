"""
What an expression may use is said from one source (F957's line, SAI2-P3, D300; BF11's `vocabulary()`).

BF11 made the engine's tables say it once (`core.expressions.vocabulary_sentence()`) for the save's refusal. The
builder still said its own list in three tool descriptions ("str/int/float/bool/list/dict/len/abs/min/max/round, and
the common string methods"), and the planner's steps passage said none. Now:
- the planner's steps passage names `{{EXPRESSION_VOCABULARY}}`, expanded at seed like `{{STEP_TYPE_CHEATSHEET}}`;
- builder_tools' if_condition / switch_expression descriptions are built from the same sentence;
so a function or method the engine adds or refuses is said everywhere at once.
"""
from __future__ import annotations

import json

from core import builder_tools
from core.expressions import vocabulary_sentence
from core.native_builder import seed

OLD = "the functions str/int/float/bool/list/dict/len/abs/min/max/round, and the common string methods"


def _properties(schema: dict) -> list[dict]:
    """Every property schema under `schema`, nested items included."""
    found: list[dict] = []
    if isinstance(schema, dict):
        for name, value in (schema.get("properties") or {}).items():
            found.append({"name": name, **value})
            found += _properties(value)
        if schema.get("items"):
            found += _properties(schema["items"])
    return found


def _described(name: str) -> list[str]:
    return [p["description"] for t in builder_tools.BUILDER_TOOL_SCHEMAS
            for p in _properties(t["function"].get("parameters") or {})
            if p["name"] == name and "description" in p]


def test_the_token_expands_to_the_engines_sentence():
    assert seed.expand_prompts("Conditions: {{EXPRESSION_VOCABULARY}}") == f"Conditions: {vocabulary_sentence()}"


def test_the_planners_steps_passage_says_the_engines_vocabulary():
    assert "{{EXPRESSION_VOCABULARY}}" in seed.prompt_fragment("plan_steps")
    planner = next(d for d in seed.bundled_definitions()["agents"] if d["id"] == "agent_native_builder_planner")
    assert vocabulary_sentence() in planner["system_prompt"]


def test_no_expression_token_is_left_in_the_bundle():
    assert "{{EXPRESSION_VOCABULARY}}" not in json.dumps(seed.bundled_definitions(), ensure_ascii=False)


def test_the_builders_condition_descriptions_are_built_from_the_same_sentence():
    said = _described("if_condition") + _described("switch_expression")
    assert len(_described("if_condition")) >= 2 and _described("switch_expression"), said
    for text in said:
        assert OLD not in text
    # Each says the sentence itself, or points to if_condition's, which does.
    assert all(vocabulary_sentence() in t or "Same restricted expression syntax as if_condition" in t for t in said)
    assert all(vocabulary_sentence() in t for t in _described("if_condition"))
