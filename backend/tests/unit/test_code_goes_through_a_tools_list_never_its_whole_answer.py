"""Code that goes through a tool's answer goes through its list, never the whole answer (F1704).

A plan's Transform parsed scrape_structured's answer, an object (`success`, `url`, `count`, `data`), and went through it
(`for r in rows`): that walks the object's keys, so the 181 rows it read became 0 and the run wrote an empty CSV. Where a
tool's answer is a known object, code that goes through it as a list is sent back once with the field to go through
(scrape_structured's `data`); a second such plan is built with a line on its card, and the validator warns of it. The
planner's own words say it too. Reading the list field, or a tool whose answer is not known, is no such read.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.builder_tools import _validate_orchestration

URL = "https://en.wikipedia.org/wiki/List_of_sovereign_states"
#: S2's own Build CSV, as the planner wrote it on 2.1.1's publish check.
WHOLE = """rows = state['scrape_wiki']
if isinstance(rows, str):
    import json; rows = json.loads(rows)
import csv, io
countries = [r['name'].strip() for r in rows if isinstance(r, dict) and r.get('name','').strip()]
result = {'csv': 'Country', 'count': len(countries), 'countries': countries}
"""
#: The same, going through the answer's `data`.
THROUGH_DATA = """import json
answer = json.loads(state['scrape_wiki']) if isinstance(state['scrape_wiki'], str) else state['scrape_wiki']
countries = [r['name'] for r in answer['data'] if r.get('name')]
result = {'count': len(countries), 'countries': countries}
"""
SAID = "goes through state.scrape_wiki as a list"


def _plan(code: str, tool: str = "scrape_structured") -> dict:
    read = {"id": "scrape_wiki", "type": "tool", "name": "Fetch Wikipedia table", "does": "Reads the table",
            "tool": tool, "config": {"tool_arguments": {"url": URL, "schema": {
                "baseSelector": "table.wikitable tbody tr",
                "fields": [{"name": "name", "selector": "td:first-child a", "type": "text"}]}}},
            "next": "build_csv"}
    build = {"id": "build_csv", "type": "transform", "name": "Build CSV", "does": "Writes the rows as a CSV",
             "config": {"transform_code": code}, "next": "end"}
    return {"kind": "create", "title": "Countries", "summary": "Lists the countries.", "steps": [read, build]}


def _sent(errors: list[str]) -> list[str]:
    return [e for e in errors if SAID in e]


# ── the plan check ─────────────────────────────────────────────────────────────

async def test_code_going_through_a_whole_object_answer_is_sent_back_once_with_its_list_field():
    from core.native_builder import build_plan

    first = await build_plan.examine(_plan(WHOLE), None, ws=build_plan.Workspace(), state={"user_message": "x"})
    [said] = _sent(first.errors)
    assert "'build_csv'" in said and "scrape_structured" in said and "`data`" in said, said


async def test_a_second_such_plan_is_built_with_a_line_on_its_card():
    from core.native_builder import build_plan

    state = {"user_message": "x"}
    await build_plan.examine(_plan(WHOLE), None, ws=build_plan.Workspace(), state=state)
    again = await build_plan.examine(_plan(WHOLE), None, ws=build_plan.Workspace(), state=state)
    assert not _sent(again.errors), "never a loop: once a run"
    assert any("goes through" in n and "`data`" in n for n in again.notes), again.notes


@pytest.mark.parametrize(("code", "tool"), [
    (THROUGH_DATA, "scrape_structured"),  # through its `data`: the right read
    (WHOLE, "some_unknown_tool"),  # an answer whose shape is not known: no claim made
])
async def test_reading_the_list_field_or_an_unknown_answer_is_not_sent_back(code, tool):
    from core.native_builder import build_plan

    got = await build_plan.examine(_plan(code, tool), None, ws=build_plan.Workspace(), state={"user_message": "x"})
    assert not _sent(got.errors), got.errors


# ── the validator ──────────────────────────────────────────────────────────────

def _workflow(code: str) -> dict:
    return {"id": "orch_f1704", "name": "Countries", "entry_step_id": "scrape_wiki", "steps": [
        {"id": "scrape_wiki", "name": "Fetch Wikipedia table", "type": "tool", "forced_tool": "scrape_structured",
         "output_key": "scrape_wiki", "next_step_id": "build_csv", "tool_arguments": {"url": URL, "schema": {
             "baseSelector": "table.wikitable tbody tr",
             "fields": [{"name": "name", "selector": "td:first-child a", "type": "text"}]}}},
        {"id": "build_csv", "name": "Build CSV", "type": "transform", "transform_code": code,
         "output_key": "build_csv", "next_step_id": "end"},
        {"id": "end", "name": "End", "type": "end"}]}


def test_the_validator_warns_of_code_going_through_a_whole_object_answer():
    verdict = _validate_orchestration(_workflow(WHOLE))
    assert verdict["ok"], verdict["issues"]
    [said] = [w for w in verdict.get("warnings", []) if SAID in w]
    assert "`data`" in said, said


def test_the_validator_is_quiet_about_code_that_goes_through_data():
    verdict = _validate_orchestration(_workflow(THROUGH_DATA))
    assert not [w for w in verdict.get("warnings", []) if SAID in w]


# ── the planner's words ────────────────────────────────────────────────────────

def test_the_planners_transform_words_say_to_go_through_the_list_field():
    import core.native_builder as nb

    text = (Path(nb.__file__).parent / "prompts" / "plan_steps.md").read_text(encoding="utf-8")
    line = next(x for x in text.splitlines() if x.startswith("- `transform`"))
    assert "answers an object" in line and "`data`" in line and "never the answer itself" in line, line
