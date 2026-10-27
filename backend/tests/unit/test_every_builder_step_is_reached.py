"""
Every step of the builder's definition is one some route reaches (SAI2-P3c, D300).

`step_researched_before` stayed in the bundle after P4's code override took its job (findings, then a yes or a build,
is a build: F766, `classify`): nothing led to it, and nothing said so. A step no step names is dead weight a reader
takes for a live route, so the bundle holds none.
"""
from __future__ import annotations

import json

from core.native_builder import seed


def test_every_step_is_named_by_another_or_is_the_entry():
    orch = seed.bundled_definitions()["orchestration"]
    ids = {s["id"] for s in orch["steps"]}
    named = {orch["entry_step_id"]}
    for step in orch["steps"]:
        said = json.dumps({k: v for k, v in step.items() if k != "id"}, ensure_ascii=False)
        named |= {i for i in ids if i != step["id"] and f'"{i}"' in said}
    assert ids - named == set()


def test_the_already_researched_route_is_gone():
    ids = {s["id"] for s in seed.bundled_definitions()["orchestration"]["steps"]}
    assert "step_researched_before" not in ids
