"""A Parallel's join reads what every branch wrote: none of its keys is unset there (F1621, part 2).

A Parallel runs every branch before it goes on, so the step it goes on to has each branch's keys. Its branches are
usually wired to the join (each branch step's next is the Parallel's own next), and the walk of the ways a run takes
followed that next: it reached the join through one branch with the other's key unset. So a Transform at the join
that read both branches' keys was refused ("a run reaches it from 'both' by its branch 2, through 'b', without 'x'
set"), and a prompt there was warned that it reads one empty, though every run had both.

A step in a branch now goes on only within its branch; the Parallel goes on to the join once every branch has run.
Near misses: a key no branch writes, written only after the join, is still read before any step writes it; a key
nothing writes is still said.
"""
from __future__ import annotations

from core.builder_tools import _validate_orchestration
from core.orchestration.branch_keys import missing_key_reads, unset_reads


def _branch(sid: str, key: str, nxt: str | None = "join") -> dict:
    return {"id": sid, "name": sid, "type": "transform", "transform_code": f"result = '{key}'", "output_key": key,
            "next_step_id": nxt}


def _orch(join: dict, *more: dict, wired: bool = True) -> dict:
    nxt = "join" if wired else None
    return {"id": "orch_join", "name": "Harbor & Pine join", "entry_step_id": "both", "state_schema": {},
            "steps": [{"id": "both", "name": "both", "type": "parallel", "parallel_branches": [["a"], ["b"]],
                       "next_step_id": "join"},
                      _branch("a", "x", nxt), _branch("b", "y", nxt), join, *more,
                      {"id": "end", "name": "End", "type": "end"}]}


CODE = {"id": "join", "name": "join", "type": "transform", "transform_code": "result = state.x + state.y",
        "output_key": "z", "next_step_id": "end"}
PROMPT = {"id": "join", "name": "join", "type": "llm", "prompt_template": "Compare {{ state.x }} with {{ state.y }}.",
          "output_key": "z", "next_step_id": "end"}


class TestTheJoinHasEveryBranchsKeys:
    def test_code_at_the_join_reading_both_branches_is_not_refused(self):
        assert missing_key_reads(_orch(CODE)) == []
        issues = _validate_orchestration(_orch(CODE))["issues"]
        assert not [i for i in issues if "state.x" in i or "state.y" in i], issues

    def test_a_prompt_at_the_join_is_not_warned(self):
        assert unset_reads(_orch(PROMPT)) == []

    def test_branches_not_wired_to_the_join_read_the_same(self):
        assert missing_key_reads(_orch(CODE, wired=False)) == [] and unset_reads(_orch(PROMPT, wired=False)) == []


class TestNearMisses:
    def test_a_key_written_only_after_the_join_is_still_read_before_it(self):
        reads_w = {**CODE, "transform_code": "result = state.w", "next_step_id": "later"}
        later = _branch("later", "w", "end")
        said = missing_key_reads(_orch(reads_w, later))
        assert len(said) == 1 and "state.w" in said[0] and "before any step writes it" in said[0], said

    def test_a_key_nothing_writes_is_still_said(self):
        reads_v = {**CODE, "transform_code": "result = state.v"}
        said = missing_key_reads(_orch(reads_v))
        assert len(said) == 1 and "which no step writes" in said[0], said
