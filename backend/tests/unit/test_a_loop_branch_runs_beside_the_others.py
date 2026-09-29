"""A Parallel's branch holding a Loop or a nested Parallel runs beside the others (F301).

F255 let agent, tool and workflow branches run at the same time, but a branch
holding a Loop or a nested Parallel kept the old order, one after another: a
loop writes values two loops beside each other would overwrite. Those are the
engine's own view of where a step is (`current_transition`, which every body
step's prompt reads, and the loop depth) and the loop's item and index keys.

Now the engine's view is each branch's own, and a loop branch runs beside the
others unless it shares a loop key with one of them: two loops keeping the
default `loop_index` stay in order, with the reason, as two branches reading
each other's keys do. A nested Parallel keeps its branch in order only for a
reason one of its own branches has.
"""
import pytest

from _fakes import seed as S
from test_parallel_agents_run_together import DELAY, _Agents, _agent_step, _base_agents, _run, _status


class _Recording(_Agents):
    """Also keeps what each call was told about where it is."""

    def __init__(self):
        super().__init__()
        self.told: dict[str, list[str]] = {}

    async def run_agent_step(self, **kw):
        import re
        key = re.search(r"B:(\w+)", kw.get("message") or "").group(1)
        self.told.setdefault(key, []).append(
            " ".join(str(kw.get(k) or "") for k in ("message", "system_prompt_extra", "system_prompt_prefix")))
        async for ev in super().run_agent_step(**kw):
            yield ev


@pytest.fixture
async def agents(monkeypatch):
    import core.react_engine as re_mod
    fake = _Recording()
    monkeypatch.setattr(re_mod, "run_agent_step", fake.run_agent_step)
    await S.seed_agents(_base_agents())
    return fake


def _loop(sid, body, count, **keys):
    return [{"id": sid, "name": sid.upper(), "type": "loop", "loop_count": count,
             "loop_step_ids": [body["id"]], "output_key": f"o_{sid}", "next_step_id": None, **keys},
            {**body, "next_step_id": None}]


def _fanout(*branches):
    """Each branch is a list of steps; its first is the branch's entry."""
    steps = [{"id": "fan", "name": "Fan", "type": "parallel",
              "parallel_branches": [[b[0]["id"]] for b in branches], "next_step_id": None}]
    for b in branches:
        steps += b
    return S.make_orchestration(id="orch_f301", entry_step_id="fan", steps=steps)


def _start(events):
    return next(e for e in events if e.get("type") == "parallel_start")


class TestALoopBranch:
    async def test_it_runs_beside_an_agent_branch(self, agents):
        events = await _run(_fanout([_agent_step("a0")], _loop("lp", _agent_step("lb"), 2)))

        assert _status(events) == "completed"
        assert agents.start("lb") < agents.end("a0"), "the loop waited for the agent branch"
        assert "in_order" not in _start(events)
        assert len(agents.spans["lb"]) == 2

    async def test_two_loops_with_their_own_keys_run_together_and_each_knows_where_it_is(self, agents):
        events = await _run(_fanout(
            _loop("la", _agent_step("ba"), 2, loop_index_key="i_a"),
            _loop("lc", _agent_step("bc"), 3, loop_index_key="i_c"),
        ))

        assert _status(events) == "completed"
        assert abs(agents.start("ba") - agents.start("bc")) < DELAY / 2, "the loops took turns"
        assert len(agents.told["ba"]) == 2 and len(agents.told["bc"]) == 3
        for i, told in enumerate(agents.told["ba"], 1):
            assert f"Loop iteration {i} of 2" in told and '"LA"' in told, told
            assert '"LC"' not in told
        for i, told in enumerate(agents.told["bc"], 1):
            assert f"Loop iteration {i} of 3" in told and '"LC"' in told, told
            assert '"LA"' not in told

    async def test_two_loops_sharing_their_index_key_keep_their_order(self, agents):
        events = await _run(_fanout(
            _loop("la", _agent_step("ba"), 2), _loop("lc", _agent_step("bc"), 2)))

        assert _status(events) == "completed"
        assert agents.start("bc") >= agents.end("ba") - 0.01, "two loops wrote one loop_index at once"
        reasons = {r["branch"]: r["reason"] for r in _start(events)["in_order"]}
        assert "loop_index" in reasons[1]

    async def test_a_branch_reading_a_siblings_loop_key_waits_for_it(self, agents):
        reader = _agent_step("rd", prompt="B:rd at {state.i_a}")
        events = await _run(_fanout(_loop("la", _agent_step("ba"), 2, loop_index_key="i_a"), [reader]))

        assert agents.start("rd") >= agents.end("ba") - 0.01
        reasons = {r["branch"]: r["reason"] for r in _start(events)["in_order"]}
        assert "i_a" in reasons[1]

    async def test_a_body_reading_the_iteration_marker_beside_another_loop_keeps_order(self, agents):
        marker = {"id": "pm", "name": "PM", "type": "print", "print_content": "{state._loop_current_iteration}",
                  "output_key": "o_pm"}
        events = await _run(_fanout(
            _loop("la", marker, 2, loop_index_key="i_a"),
            _loop("lc", _agent_step("bc"), 2, loop_index_key="i_c")))

        assert _status(events) == "completed"
        in_order = _start(events).get("in_order") or []
        assert any("_loop_current_iteration" in r["reason"] for r in in_order), in_order

    async def test_a_loop_that_asks_a_person_keeps_its_order(self, agents):
        human = {"id": "ok", "name": "OK", "type": "human", "human_prompt": "ok?", "output_key": "h"}
        events = await _run(_fanout([_agent_step("a0")], _loop("lp", human, 2)))

        assert _start(events)["in_order"] == [{"branch": 1, "reason": "a person answers a step in it"}]

    async def test_the_iteration_markers_do_not_outlive_the_node(self, agents):
        after = {"id": "after", "name": "After", "type": "print",
                 "print_content": "[{state._loop_current_iteration}]", "output_key": "o_after",
                 "next_step_id": None}
        orch = _fanout(_loop("la", _agent_step("ba"), 2, loop_index_key="i_a"),
                       _loop("lc", _agent_step("bc"), 3, loop_index_key="i_c"))
        orch["steps"][0]["next_step_id"] = "after"
        orch["steps"].append(after)
        events = await _run(orch)

        assert _status(events) == "completed"
        from test_parallel_agents_run_together import _saved
        state = (await _saved("run_par_agents")).shared_state
        assert "1" not in state["o_after"] and "2" not in state["o_after"], state["o_after"]


class TestANestedParallel:
    async def test_it_runs_beside_the_others(self, agents):
        inner = [{"id": "inner", "name": "Inner", "type": "parallel",
                  "parallel_branches": [["n1"], ["n2"]], "next_step_id": None},
                 _agent_step("n1"), _agent_step("n2")]
        events = await _run(_fanout([_agent_step("a0")], inner))

        assert _status(events) == "completed"
        assert agents.start("n1") < DELAY / 2 and agents.start("n2") < DELAY / 2
        assert "in_order" not in _start(events)

    async def test_it_keeps_its_order_when_a_branch_of_its_own_would(self, agents):
        inner = [{"id": "inner", "name": "Inner", "type": "parallel",
                  "parallel_branches": [["n1"], ["nm"]], "next_step_id": None},
                 _agent_step("n1"), _agent_step("nm", agent_id="a_memory")]
        events = await _run(_fanout([_agent_step("a0")], inner))

        reasons = {r["branch"]: r["reason"] for r in _start(events)["in_order"]}
        assert "memory" in reasons[1]
