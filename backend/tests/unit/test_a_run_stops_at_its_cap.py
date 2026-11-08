"""A workflow's cap stops a run before the step that would pass it, in its own words (CAP1, D272).

The engine asks before every step — at the top of the run, in a Loop's body, in a
Parallel's branches and in a nested run — what the run has spent
(`run_cap.set_spend_reader`; self-hosted reads its own keys' `usage_logs`), and a
run at its cap stops with "Stopped: this run reached its cap of …". The cap of
the workflow that was started covers everything it calls; a called workflow's own
cap is for when it runs on its own. No cap reads nothing; a reader that fails
stops nothing (F109).

Run both in-process and on the fleet's adapter (`driver`).
"""
import pytest

from _fakes import seed as S
from tests.unit.test_a_failed_child_fails_its_parent import (  # noqa: F401 — fixtures
    _delegate, _human, _print, _run, _server, driver, worker,
)
from tests.unit.test_nested_runs_can_be_told_apart import _spend


def reader(per_step: float = 1.0, usd_per_step: float = 0.0):
    """A host that charges `per_step` credits and `usd_per_step` dollars a step.

    Each ask is a step about to start: what was used is the steps asked before it.
    Returns what it was asked, `(run_id, step_type, cap)`.
    """
    from core.orchestration import run_cap

    asked: list[tuple[str, str, object]] = []

    async def read(run_id: str, step_type: str, cap):
        before = len(asked)
        asked.append((run_id, step_type, cap))
        return run_cap.Spend(
            usd=before * usd_per_step,
            credits=before * per_step,
            credits_after=(before + 1) * per_step,
            step_credits=per_step,
        )

    run_cap.set_spend_reader(read)
    return asked


def _chain(orch_id, n, **cap):
    steps = [_print(f"p{i}", f"step {i}", nxt=f"p{i + 1}" if i + 1 < n else None, key=f"k{i}")
             for i in range(n)]
    return S.make_orchestration(id=orch_id, entry_step_id="p0", steps=steps, **cap)


def _ran(run, keys):
    return [k for k in keys if k in run.shared_state]


def _reason(run):
    from core.orchestration.run_error import failure_reason

    return failure_reason(run.status, run.step_history, run.step_progress)


CAP_3 = ("Stopped: this run reached its cap of 3 credits. It had used about 3 credits. "
         "Raise the cap in the workflow's settings, then Replay.")


class TestTheCapStopsTheRun:
    async def test_at_the_top_before_the_step_that_would_pass_it(self, driver):
        asked = reader()
        orch = _chain("orch_cap_top", 5, max_run_credits=3)
        await S.seed_orchestrations([orch])

        assert await driver.start(orch, "run_cap_top") == "failed"

        run = await _run("run_cap_top")
        assert _ran(run, ["k0", "k1", "k2", "k3", "k4"]) == ["k0", "k1", "k2"]
        assert [t for _, t, _ in asked] == ["print"] * 4
        from core.orchestration.run_cap import RunCap
        from core.orchestration.run_error import run_error

        assert {c for _, _, c in asked} == {RunCap(credits=3.0)}
        assert run_error(run) == CAP_3
        assert _reason(run) == CAP_3

    async def test_in_a_loop_it_stops_mid_body(self, driver):
        asked = reader()
        orch = S.make_orchestration(id="orch_cap_loop", entry_step_id="loop", max_run_credits=4, steps=[
            {"id": "loop", "name": "Loop", "type": "loop", "loop_count": 10,
             "loop_step_ids": ["body"], "next_step_id": "after"},
            _print("body", "iteration {state.loop_index}", key="last"),
            _print("after", "done", key="after"),
        ])
        await S.seed_orchestrations([orch])

        assert await driver.start(orch, "run_cap_loop") == "failed"

        run = await _run("run_cap_loop")
        assert "after" not in run.shared_state
        assert [t for _, t, _ in asked] == ["loop"] + ["print"] * 4
        assert sum(1 for e in run.step_history if e.get("step_id") == "body") == 3
        assert _reason(run).startswith("Stopped: this run reached its cap of 4 credits.")

    async def test_in_a_parallel_branch_it_stops_the_node(self, driver):
        reader()
        orch = S.make_orchestration(id="orch_cap_par", entry_step_id="par", max_run_credits=1, steps=[
            {"id": "par", "name": "Both", "type": "parallel",
             "parallel_branches": [["a"], ["b"]], "next_step_id": "after"},
            _print("a", "a", key="ka"),
            _print("b", "b", key="kb"),
            _print("after", "done", key="after"),
        ])
        await S.seed_orchestrations([orch])

        assert await driver.start(orch, "run_cap_par") == "failed"

        run = await _run("run_cap_par")
        assert _ran(run, ["ka", "kb", "after"]) == []
        assert _reason(run).startswith("Stopped: this run reached its cap of 1 credit.")

    async def test_in_dollars_once_the_run_has_spent_it(self, driver):
        reader(per_step=0, usd_per_step=0.01)
        orch = _chain("orch_cap_usd", 5, max_run_usd=0.02)
        await S.seed_orchestrations([orch])

        assert await driver.start(orch, "run_cap_usd") == "failed"

        run = await _run("run_cap_usd")
        assert _ran(run, ["k0", "k1", "k2"]) == ["k0", "k1"]
        assert _reason(run) == (
            "Stopped: this run reached its cap of $0.02 in model costs. It had spent about $0.02. "
            "Raise the cap in the workflow's settings, then Replay.")


class TestANestedRunIsItsRootsWork:
    def _nest(self, tag, *, parent_cap=None, child_cap=None, child_steps=3):
        parent = S.make_orchestration(id=f"orch_cap_{tag}", entry_step_id="delegate", steps=[
            _delegate(f"agent_cap_{tag}", "tail"), _print("tail", "parent", key="tail"),
        ], **({"max_run_credits": parent_cap} if parent_cap else {}))
        child = S.make_orchestration(
            id=f"orch_cap_{tag}_child", entry_step_id="c0",
            steps=[_print(f"c{i}", f"child {i}", nxt=f"c{i + 1}" if i + 1 < child_steps else None,
                          key=f"c{i}") for i in range(child_steps)],
            **({"max_run_credits": child_cap} if child_cap else {}))
        return parent, child

    async def _seed(self, tag, parent, child):
        await S.seed_agents([S.make_orchestrator_agent(child["id"], id=f"agent_cap_{tag}")])
        await S.seed_orchestrations([parent, child])

    async def test_the_roots_cap_stops_it_inside_the_child(self, driver):
        asked = reader()
        parent, child = self._nest("root", parent_cap=2)
        await self._seed("root", parent, child)

        assert await driver.start(parent, "run_cap_root") == "failed"

        run = await _run("run_cap_root")
        assert "tail" not in run.shared_state
        # The child's steps are asked under its own id, with its root's cap.
        from core.orchestration.run_cap import RunCap

        assert any(rid.startswith("run_cap_root__delegate_") for rid, _, _ in asked)
        assert {c for _, _, c in asked} == {RunCap(credits=2.0)}
        assert "Stopped: this run reached its cap of 2 credits." in _reason(run)

    async def test_a_called_workflows_own_cap_is_not_read(self, driver):
        asked = reader()
        parent, child = self._nest("own", child_cap=1)
        await self._seed("own", parent, child)

        assert await driver.start(parent, "run_cap_own") == "completed"
        assert asked == []  # nothing in the tree has a cap of its own to apply

    async def test_run_on_its_own_its_cap_applies(self, driver):
        reader()
        _, child = self._nest("alone", child_cap=1)
        await S.seed_orchestrations([child])

        assert await driver.start(child, "run_cap_alone") == "failed"
        assert _ran(await _run("run_cap_alone"), ["c0", "c1", "c2"]) == ["c0"]

    async def test_after_a_person_answered_the_child_it_keeps_the_roots_cap(self, driver):
        asked = reader()
        parent = S.make_orchestration(id="orch_cap_ask", entry_step_id="delegate", max_run_credits=3, steps=[
            _delegate("agent_cap_ask", "tail"), _print("tail", "parent", key="tail"),
        ])
        child = S.make_orchestration(id="orch_cap_ask_child", entry_step_id="ask", max_run_credits=50, steps=[
            _human("ask", nxt="c0"),
            _print("c0", "a", nxt="c1", key="c0"), _print("c1", "b", nxt="c2", key="c1"),
            _print("c2", "c", key="c2"),
        ])
        await self._seed("ask", parent, child)
        assert await driver.start(parent, "run_cap_ask") == "paused"

        assert await driver.answer("run_cap_ask", "yes") == "failed"

        from core.orchestration.run_cap import RunCap

        assert {c for _, _, c in asked} == {RunCap(credits=3.0)}
        assert "Stopped: this run reached its cap of 3 credits." in _reason(await _run("run_cap_ask"))


class TestNothingElseChanges:
    async def test_with_no_cap_nothing_is_read(self, driver):
        asked = reader()
        orch = _chain("orch_cap_none", 3)
        await S.seed_orchestrations([orch])

        assert await driver.start(orch, "run_cap_none") == "completed"
        assert asked == []

    async def test_the_old_max_cost_field_is_no_cap(self, driver):
        """F76's `max_total_cost_usd`, still in old documents, never becomes one."""
        asked = reader(usd_per_step=1.0)
        orch = {**_chain("orch_cap_old", 3), "max_total_cost_usd": 0.000001}
        await S.seed_orchestrations([orch])

        assert await driver.start(orch, "run_cap_old") == "completed"
        assert asked == []

    async def test_a_reader_that_fails_stops_nothing(self, driver):
        from core.orchestration import run_cap

        async def broken(run_id, step_type, cap):
            raise RuntimeError("the ledger is down")

        run_cap.set_spend_reader(broken)
        orch = _chain("orch_cap_broken", 3, max_run_credits=1, max_run_usd=0.01)
        await S.seed_orchestrations([orch])
        assert await driver.start(orch, "run_cap_broken") == "completed"

    async def test_a_cap_that_is_not_one_reads_as_none(self, driver):
        """Written past the model (a bundle, the builder): the workflow still runs."""
        asked = reader(usd_per_step=1.0)
        orch = _chain("orch_cap_junk", 3, max_run_credits=0.5, max_run_usd="lots")
        await S.seed_orchestrations([orch])

        assert await driver.start(orch, "run_cap_junk") == "completed"
        assert asked == []

    async def test_at_its_cap_and_out_of_credits_it_says_its_cap(self, driver):
        """Both stop the same step: the run's own limit is the one it reached."""
        from core.orchestration import run_gate

        reader(per_step=2)
        run_gate.set_run_gate(_out_of_credits)
        orch = _chain("orch_cap_first", 3, max_run_credits=1)
        await S.seed_orchestrations([orch])

        assert await driver.start(orch, "run_cap_first") == "failed"
        assert _reason(await _run("run_cap_first")).startswith("Stopped: this run reached its cap of 1 credit.")

    async def test_under_its_cap_the_hosts_gate_still_stops_it(self, driver):
        from core.orchestration import run_gate

        reader()
        run_gate.set_run_gate(_out_of_credits)
        orch = _chain("orch_cap_gate", 3, max_run_credits=10)
        await S.seed_orchestrations([orch])

        assert await driver.start(orch, "run_cap_gate") == "failed"
        assert _reason(await _run("run_cap_gate")) == OUT_OF_CREDITS


OUT_OF_CREDITS = "Stopped: this workspace is out of execution credits."


async def _out_of_credits(run_id, step_type):
    return OUT_OF_CREDITS


class TestSelfHostedCountsItsOwnKeys:
    """No host reader: `usage_logs`' estimates of the run's calls, nested runs folded in."""

    async def test_a_run_that_has_spent_its_cap_stops(self, driver):
        await _spend("run_cap_keys", 0.02)
        await _spend("run_cap_keys__delegate_d1_aaaaaa", 0.015)
        orch = _chain("orch_cap_keys", 2, max_run_usd=0.03)
        await S.seed_orchestrations([orch])

        assert await driver.start(orch, "run_cap_keys") == "failed"
        assert _reason(await _run("run_cap_keys")) == (
            "Stopped: this run reached its cap of $0.03 in model costs. It had spent about $0.035. "
            "Raise the cap in the workflow's settings, then Replay.")

    async def test_another_runs_calls_are_not_its(self, driver):
        await _spend("run_cap_mine", 0.02)
        await _spend("run_cap_mine2", 5.0)          # shares the prefix, not the run
        await _spend("run_cap_mine_x__d1", 5.0)     # nor is this nested under it
        orch = _chain("orch_cap_mine", 2, max_run_usd=0.03)
        await S.seed_orchestrations([orch])

        assert await driver.start(orch, "run_cap_mine") == "completed"

    async def test_credits_are_not_counted_here(self, driver):
        orch = _chain("orch_cap_nocredits", 3, max_run_credits=1)
        await S.seed_orchestrations([orch])
        assert await driver.start(orch, "run_cap_nocredits") == "completed"


class TestTheCapsWords:
    def test_a_wrapped_sentence_stays_whole_under_the_engines_300(self):
        from core.orchestration import run_cap

        worst = run_cap.credits_sentence(
            12345.5, run_cap.Spend(credits=12345.4, credits_after=12349.4, step_credits=4))
        wrapped = "Sub-orchestration 'A long nested workflow' › 'Another long one' failed: " + worst
        assert len(wrapped) < 300
        assert '"' not in worst
        assert '"' not in run_cap.usd_sentence(1234.5, 1240.25)

    def test_the_numbers_read_as_the_run_page_reads_them(self):
        from core.orchestration import run_cap

        assert run_cap.credits_words(1) == "1 credit"
        assert run_cap.credits_words(2.5) == "2.5 credits"
        assert run_cap.credits_words(4.62) == "4.6 credits"
        assert run_cap.usd_words(0.5) == "$0.50"
        assert run_cap.usd_words(0.0042) == "$0.0042"
        assert run_cap.usd_words(12) == "$12.00"

    def test_its_next_step_is_named_when_that_is_what_passes_it(self):
        from core.orchestration import run_cap

        sentence = run_cap.credits_sentence(
            5, run_cap.Spend(credits=4.6, credits_after=5.6, step_credits=1))
        assert sentence == (
            "Stopped: this run reached its cap of 5 credits. It had used about 4.6 credits, and its "
            "next step costs 1. Raise the cap in the workflow's settings, then Replay.")


class TestWhereACapIsSaved:
    def test_the_model_reads_a_nonsense_cap_as_none(self):
        from core.models_orchestration import Orchestration

        orch = Orchestration.model_validate(
            {**S.make_orchestration(), "max_run_credits": 0.5, "max_run_usd": -1})
        assert orch.max_run_credits is None and orch.max_run_usd is None
        orch = Orchestration.model_validate(
            {**S.make_orchestration(), "max_run_credits": "5", "max_run_usd": 0.25})
        assert orch.max_run_credits == 5.0 and orch.max_run_usd == 0.25

    @pytest.mark.parametrize("field,value", [
        ("max_run_credits", 0.5), ("max_run_credits", -2), ("max_run_usd", 0), ("max_run_usd", "lots"),
    ])
    def test_a_person_saving_one_is_told(self, field, value):
        from pydantic import ValidationError

        from core.models_orchestration import OrchestrationIn

        with pytest.raises(ValidationError, match=field):
            OrchestrationIn.model_validate({**S.make_orchestration(), field: value})

    def test_unset_is_no_cap(self):
        from core.models_orchestration import OrchestrationIn

        orch = OrchestrationIn.model_validate(
            {**S.make_orchestration(), "max_run_credits": None, "max_run_usd": ""})
        assert orch.max_run_credits is None and orch.max_run_usd is None
