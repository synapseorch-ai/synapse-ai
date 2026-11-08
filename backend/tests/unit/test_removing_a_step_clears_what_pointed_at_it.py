"""Removing a step clears every field that named it, as the canvas does — F1458 (ISS1, GitHub #376).

#376, 2.0.0-rc2 over MCP: `remove_step b` on a saved `a -> b` answered `step_removed`, left `a.next_step_id = "b"`,
and the next run ran `a` and then failed on "Step 'b' not found" — with a real tool step, after its side effect.
The canvas's delete (`removeStepFromGraph`, frontend/src/components/orchestration/graph.ts) already cleared every
field naming the removed step, moved the entry to the first step left, and cleared a result_key only that step
wrote (F249). The builder's `remove_step` now does the same, and says what it cleared.

`update_step` pointing a step at one that does not exist is still saved — the builder may retarget before it adds
the step (Naveen 11-08) — and its answer says so; the run is what refuses it (F1459).
"""
from __future__ import annotations

import json

import pytest

from _fakes import seed as S
from core.builder_tools import execute_builder_tool


class _Server:
    agent_sessions: dict = {}


async def _call(tool: str, args: dict) -> dict:
    return json.loads(await execute_builder_tool(tool, args, _Server()))


async def _stored(oid: str) -> dict:
    from core.store.resources import get_orchestration
    return await get_orchestration(oid)


def _print(sid: str, nxt: str | None = None, key: str | None = None) -> dict:
    return {"id": sid, "name": sid.upper(), "type": "print", "print_content": sid, "output_key": key or f"o_{sid}",
            "next_step_id": nxt}


def _by_id(orch: dict) -> dict:
    return {s["id"]: s for s in orch["steps"]}


async def _seed(oid: str, steps: list[dict], entry: str, **extra) -> None:
    await S.seed_orchestrations([S.make_orchestration(id=oid, entry_step_id=entry, steps=steps, **extra)])


class TestTheIssuesWorkflow:
    async def test_the_step_before_no_longer_points_at_the_removed_one(self):
        await _seed("orch_two", [_print("a", "b"), _print("b")], "a")

        said = await _call("remove_step", {"orch_id": "orch_two", "step_id": "b"})

        assert said["status"] == "step_removed", said
        assert not _by_id(await _stored("orch_two"))["a"].get("next_step_id")

    async def test_the_answer_says_what_it_cleared(self):
        await _seed("orch_two_said", [_print("a", "b"), _print("b")], "a")

        said = await _call("remove_step", {"orch_id": "orch_two_said", "step_id": "b"})

        text = json.dumps(said)
        assert "'a'" in text or '"a"' in text, said
        assert "next_step_id" in text, said

    async def test_the_validator_finds_nothing_left_pointing_at_it(self):
        from core.builder_tools import _validate_orchestration

        await _seed("orch_two_valid", [_print("a", "b"), _print("b")], "a")
        await _call("remove_step", {"orch_id": "orch_two_valid", "step_id": "b"})

        issues = _validate_orchestration(await _stored("orch_two_valid"), allow_dead_ends=True)["issues"]
        assert not [i for i in issues if "'b'" in i], issues


class TestEveryFieldThatNamesAStep:
    """The fields `removeStepFromGraph` clears, one each."""

    async def test_a_route_map_target(self):
        await _seed("orch_rm", [
            {"id": "judge", "name": "Judge", "type": "evaluator", "evaluator_prompt": "ok?", "output_key": "v",
             "route_map": {"yes": "b", "no": "c"}},
            _print("b"), _print("c")], "judge")
        await _call("remove_step", {"orch_id": "orch_rm", "step_id": "b"})

        route = _by_id(await _stored("orch_rm"))["judge"]["route_map"]
        assert route.get("yes") in (None, ""), route
        assert route["no"] == "c"

    async def test_an_if_else_branch(self):
        await _seed("orch_if", [
            {"id": "gate", "name": "Gate", "type": "if_else", "if_condition": "state.x == 1",
             "if_true_step_id": "b", "if_false_step_id": "c"},
            _print("b"), _print("c")], "gate")
        await _call("remove_step", {"orch_id": "orch_if", "step_id": "b"})

        gate = _by_id(await _stored("orch_if"))["gate"]
        assert not gate.get("if_true_step_id"), gate
        assert gate["if_false_step_id"] == "c"

    async def test_a_switch_case_and_its_default(self):
        await _seed("orch_sw", [
            {"id": "pick", "name": "Pick", "type": "switch", "switch_expression": "state.kind",
             "switch_cases": {"x": "b", "y": "c"}, "switch_default_step_id": "b"},
            _print("b"), _print("c")], "pick")
        await _call("remove_step", {"orch_id": "orch_sw", "step_id": "b"})

        pick = _by_id(await _stored("orch_sw"))["pick"]
        assert pick["switch_cases"].get("x") in (None, ""), pick
        assert pick["switch_cases"]["y"] == "c"
        assert not pick.get("switch_default_step_id"), pick

    async def test_a_loop_body_entry(self):
        await _seed("orch_loop", [
            {"id": "each", "name": "Each", "type": "loop", "loop_over": "state.items", "loop_step_ids": ["b", "c"],
             "output_key": "done", "next_step_id": None},
            _print("b"), _print("c")], "each")
        await _call("remove_step", {"orch_id": "orch_loop", "step_id": "b"})

        assert _by_id(await _stored("orch_loop"))["each"]["loop_step_ids"] == ["c"]

    async def test_a_parallel_branch_entry(self):
        await _seed("orch_par", [
            {"id": "fan", "name": "Fan", "type": "parallel", "parallel_branches": [["b"], ["c"]],
             "next_step_id": "join"},
            _print("b"), _print("c"),
            {"id": "join", "name": "Join", "type": "merge", "input_keys": ["o_b", "o_c"], "output_key": "m"}], "fan")
        await _call("remove_step", {"orch_id": "orch_par", "step_id": "b"})

        branches = _by_id(await _stored("orch_par"))["fan"]["parallel_branches"]
        assert all("b" not in br for br in branches), branches
        assert ["c"] in branches

    async def test_where_a_step_goes_at_its_limit(self):
        await _seed("orch_spent", [
            {**_print("a", "c"), "on_exhausted_step_id": "b"}, _print("b"), _print("c")], "a")
        await _call("remove_step", {"orch_id": "orch_spent", "step_id": "b"})

        assert not _by_id(await _stored("orch_spent"))["a"].get("on_exhausted_step_id")

    async def test_the_entry_moves_to_the_first_step_left(self):
        await _seed("orch_entry", [_print("a", "b"), _print("b")], "a")
        await _call("remove_step", {"orch_id": "orch_entry", "step_id": "a"})

        assert (await _stored("orch_entry"))["entry_step_id"] == "b"

    async def test_a_result_key_only_the_removed_step_wrote_is_cleared(self):
        await _seed("orch_result", [_print("a", "b", key="first"), _print("b", key="answer")], "a",
                    result_key="answer")
        await _call("remove_step", {"orch_id": "orch_result", "step_id": "b"})

        assert not (await _stored("orch_result")).get("result_key")

    async def test_a_result_key_another_step_writes_stays(self):
        await _seed("orch_result_kept", [_print("a", "b", key="answer"), _print("b", key="answer")], "a",
                    result_key="answer")
        await _call("remove_step", {"orch_id": "orch_result_kept", "step_id": "b"})

        assert (await _stored("orch_result_kept"))["result_key"] == "answer"


class TestUpdateStep:
    async def test_pointing_at_a_step_that_does_not_exist_saves_and_says_so(self):
        await _seed("orch_ghost", [_print("a")], "a")

        said = await _call("update_step", {"orch_id": "orch_ghost", "step_id": "a",
                                           "patch_json": json.dumps({"next_step_id": "ghost"})})

        assert said["status"] == "step_updated", said
        warned = " ".join(said.get("warnings") or [])
        assert "ghost" in warned and "next_step_id" in warned, said
        assert _by_id(await _stored("orch_ghost"))["a"]["next_step_id"] == "ghost"

    async def test_pointing_at_a_step_that_exists_says_nothing_more(self):
        await _seed("orch_real", [_print("a"), _print("b")], "a")

        said = await _call("update_step", {"orch_id": "orch_real", "step_id": "a",
                                           "patch_json": json.dumps({"next_step_id": "b"})})

        assert said["status"] == "step_updated", said
        assert not said.get("warnings"), said


@pytest.mark.parametrize("missing", ["nope"])
async def test_removing_a_step_that_is_not_there_is_still_an_error(missing):
    await _seed("orch_same", [_print("a")], "a")
    said = await _call("remove_step", {"orch_id": "orch_same", "step_id": missing})
    assert "error" in said and missing in said["error"], said
