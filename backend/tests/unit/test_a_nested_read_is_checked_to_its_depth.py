"""A read of a model step's answer is checked to its full depth, and an answer read in depth that declares nothing is
given its shape from those reads (F1618, commit c: the builder).

A model step's answer can hold groups and lists of groups (commit a), but the builder checked a later step's read of it
only one level down: `{{ state.find.leads.0.emial }}` passed because `leads` was declared, and failed only when the run
reached it. A read whose step declared nothing was told to declare its first field alone, as text. Now every read, in a
template, a condition or a Loop's body through its item, is checked against the declaration to its end, with the fix
named: the field it meant, the field to declare in which group, or that a list is read one item at a time. And an
answer read in depth with nothing declared is given the whole shape its readers use: a field read by an index or looped
over is a list, its item's reads are that list's fields, a field compared with a number is a number.

Near misses: a plan whose reads are one level deep is told what it was told before, word for word; a read that holds
is never named; a plan that reads nothing of a model step's answer has nothing to fix.
"""
from __future__ import annotations

import json

from core.builder_tools import _fields_never_written

LEADS = [{"name": "leads", "type": "list", "of": {"fields": [
    {"name": "email", "type": "text"}, {"name": "score", "type": "number", "may_be_empty": True}]}},
    {"name": "total", "type": "number"}]


def _find(fields=None) -> dict:
    step = {"id": "find", "type": "llm", "prompt_template": "Find the leads.", "output_key": "find"}
    if fields is not None:
        step["output_fields"] = fields
    return step


def _print(sid: str, text: str) -> dict:
    return {"id": sid, "type": "print", "print_content": text, "output_key": f"o_{sid}"}


def _loop(body: list[str], over: str = "state.find.leads") -> dict:
    return {"id": "each", "type": "loop", "loop_over": over, "loop_item_key": "lead", "loop_step_ids": body}


class TestEveryReadIsCheckedToItsEnd:
    def test_a_template_read_that_holds_is_never_named(self):
        assert _fields_never_written([_find(LEADS), _print("p", "First: {{ state.find.leads.0.email }}")]) == []

    def test_a_misspelt_field_is_named_with_the_one_it_meant(self):
        issues = _fields_never_written([_find(LEADS), _print("p", "First: {{ state.find.leads.0.emial }}")])
        assert len(issues) == 1 and "emial" in issues[0] and "`email`" in issues[0], issues

    def test_a_condition_reads_to_its_end(self):
        good = {"id": "hot", "type": "if_else", "if_condition": "state.find.leads[0].score > 0.5",
                "if_true_step_id": "p", "if_false_step_id": "p"}
        bad = {**good, "id": "cold", "if_condition": "state.find.leads[0].scor > 0.5"}
        assert _fields_never_written([_find(LEADS), good, _print("p", "x")]) == []
        issues = _fields_never_written([_find(LEADS), bad, _print("p", "x")])
        assert issues and "scor" in issues[0] and "`score`" in issues[0], issues

    def test_a_loops_body_reads_its_item_as_one_of_the_list(self):
        ok = _fields_never_written([_find(LEADS), _loop(["p"]), _print("p", "Mail {{ state.lead.email }}")])
        assert ok == []
        issues = _fields_never_written([_find(LEADS), _loop(["p"]), _print("p", "Call {{ state.lead.phone }}")])
        assert issues and "`phone`" in issues[0] and "leads" in issues[0], issues

    def test_a_list_read_as_one_group_says_to_read_one_item(self):
        issues = _fields_never_written([_find(LEADS), _print("p", "{{ state.find.leads.email }}")])
        assert issues and "list" in issues[0] and "leads.0.email" in issues[0], issues


class TestTheShapeIsProposedFromItsReaders:
    def test_an_answer_read_in_depth_with_nothing_declared_gets_its_whole_shape(self):
        steps = [_find(), _loop(["p"]), _print("p", "Mail {{ state.lead.email }} ({{ state.lead.score }})"),
                 {"id": "many", "type": "if_else", "if_condition": "state.find.total > 3", "if_true_step_id": "p",
                  "if_false_step_id": "p"}]
        issues = _fields_never_written(steps)
        proposed = [i for i in issues if "output_fields" in i and '"of"' in i]
        assert len(proposed) == 1, issues
        said = proposed[0]
        shape = json.loads(said[said.index("["):said.rindex("]") + 1])
        by = {f["name"]: f for f in shape}
        assert by["leads"]["type"] == "list" and {f["name"] for f in by["leads"]["of"]["fields"]} == {"email", "score"}
        assert by["total"]["type"] == "number", shape


class TestNearMisses:
    def test_one_level_reads_are_told_what_they_were_told_before(self):
        issues = _fields_never_written([_find(), _print("p", "{{ state.find.summary }}")])
        assert len(issues) == 1 and issues[0].startswith("Step 'p' reads `summary` from state.find"), issues
        assert "declare `summary` in 'find's output_fields" in issues[0], issues

    def test_nothing_read_is_nothing_to_fix(self):
        assert _fields_never_written([_find(), _print("p", "{{ state.find }}")]) == []

    def test_a_flat_declaration_read_flat_holds(self):
        fields = [{"name": "summary", "type": "text"}]
        assert _fields_never_written([_find(fields), _print("p", "{{ state.find.summary }}")]) == []


def test_the_planner_is_told_the_nested_shapes_once():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "core/native_builder/prompts/plan_steps.md").read_text()
    llm_line = next(line for line in text.splitlines() if line.startswith("- `llm`:"))
    assert '"of"' in llm_line and '"object"' in llm_line, llm_line
