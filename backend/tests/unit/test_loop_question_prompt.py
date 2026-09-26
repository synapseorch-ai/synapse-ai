"""
A question asked inside a Loop names its iteration, whatever the loop's mode (F212).

Only a for-each loop wrote `loop_index`, so a count or a while loop asking
`Iteration {state.loop_index}?` stored, notified and served the template text:
the person could not tell which iteration was asking. Every loop now writes its
0-based index to `loop_index_key` before the body runs (Naveen's call; the same
key and meaning as for-each).

The human prompt also renders `{state.key.nested}` the way Print does; it used
to read only top-level keys.
"""
import pytest

from _fakes import seed as S
from test_human_in_containers import _human, _loop, _print, _run, driver, worker  # noqa: F401 — fixtures


def _asks_in(mode: dict, oid: str):
    return S.make_orchestration(id=oid, entry_step_id="loop1", steps=[
        _loop("loop1", ["ask"], nxt="tail", **mode),
        _human("ask", prompt="Iteration {state.loop_index}?"),
        _print("tail"),
    ])


@pytest.mark.parametrize("mode", [
    {"loop_count": 2},
    {"loop_while_condition": "True", "loop_max_iterations": 2},
    {"loop_over": "state.items"},
], ids=["count", "while", "foreach"])
async def test_each_iteration_asks_with_its_own_index(driver, mode):
    oid = f"orch_iterq_{next(iter(mode))}"
    run_id = f"run_iterq_{next(iter(mode))}"
    orch = _asks_in(mode, oid)
    await S.seed_orchestrations([orch])

    assert await driver.start(orch, run_id, {"items": ["a", "b"]}) == "paused"
    assert (await _run(run_id)).human_prompt == "Iteration 0?"
    assert await driver.answer(run_id, "one") == "paused"
    assert (await _run(run_id)).human_prompt == "Iteration 1?"
    assert await driver.answer(run_id, "two") in ("completed",)

    state = (await _run(run_id)).shared_state
    assert state["o_tail"] == "tail", "the loop's next step ran"


async def test_a_question_reads_a_nested_key(driver):
    orch = S.make_orchestration(id="orch_nestedq", entry_step_id="ask", steps=[
        _human("ask", prompt="Ship order {state.order.id} to {state.order.city}? ({state.order.none})",
               nxt="say"),
        _print("say", "ok"),
    ])
    await S.seed_orchestrations([orch])

    assert await driver.start(orch, "run_nestedq", {"order": {"id": "A7", "city": "Pune"}}) == "paused"
    assert (await _run("run_nestedq")).human_prompt == "Ship order A7 to Pune? ({state.order.none})"
