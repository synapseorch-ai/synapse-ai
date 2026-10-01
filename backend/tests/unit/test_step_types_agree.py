"""Every step type the engine runs is one the editor and the builder know, and the other way round (R1, D166).

A type added on one side only is a step the engine refuses, or one nobody can
pick: the editor's union, its type picker, the builder's two enums and the
engine's registry are each a list someone has to remember.
"""
from __future__ import annotations

import re
from pathlib import Path

from core.models_orchestration import StepType

BACKEND = Path(__file__).resolve().parents[2]
FRONTEND = BACKEND.parent / "frontend" / "src"
ENGINE = {t.value for t in StepType}


def _quoted(text: str) -> list[str]:
    return re.findall(r"'([a-z_]+)'", text)


def test_the_editor_s_step_types_are_the_engine_s():
    source = (FRONTEND / "types" / "orchestration.ts").read_text("utf-8")
    union = re.search(r"export type StepType = ([^;]+);", source)
    assert union, "StepType is gone from frontend/src/types/orchestration.ts"
    assert set(_quoted(union.group(1))) == ENGINE


def test_the_type_picker_offers_every_type():
    source = (FRONTEND / "components" / "orchestration" / "StepConfigPanel.tsx").read_text("utf-8")
    picker = re.search(r"const STEP_TYPES: StepType\[\] = \[([^\]]+)\];", source)
    assert picker, "STEP_TYPES is gone from StepConfigPanel.tsx"
    assert sorted(_quoted(picker.group(1))) == sorted(ENGINE)


def test_the_builder_can_write_every_type():
    from core.builder_tools import BUILDER_TOOL_SCHEMAS

    by_name = {s["function"]["name"]: s["function"]["parameters"] for s in BUILDER_TOOL_SCHEMAS}
    single = by_name["add_single_step"]["properties"]["type"]["enum"]
    batch = by_name["add_multiple_steps"]["properties"]["steps"]["items"]["properties"]["type"]["enum"]
    assert set(single) == set(batch) == ENGINE


def test_knowledge_s_step_types_are_named_the_same_everywhere():
    from core.builder_tools import KNOWLEDGE_STEP_TYPES
    from core.knowledge.api_models import STEP_TYPES

    source = (FRONTEND / "types" / "orchestration.ts").read_text("utf-8")
    listed = re.search(r"export const KNOWLEDGE_STEP_TYPES: readonly StepType\[\] = \[([^\]]+)\];", source)
    assert listed, "KNOWLEDGE_STEP_TYPES is gone from frontend/src/types/orchestration.ts"
    assert set(_quoted(listed.group(1))) == set(KNOWLEDGE_STEP_TYPES) == set(STEP_TYPES) <= ENGINE
