"""D340 F (BLD1; Naveen 10-30): a workflow's runs are judged on a sample, its first run always.

Each judged run is one more model call, so a workflow a schedule runs often would pay more for its eval than for its
own runs. A workflow's `judge_every` is how often: 1 judges every run (the default, nothing changed); N judges its
first finished run, so a new eval has a score at once, then one in N (manager 64). A golden set's runs are always
judged. The count is the workflow's own, kept in the evals store and read and written once a run: never a listing
of its runs (TRI1). A plan's eval says its sample and what a judged run costs; Evals' tools set it.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from _fakes import seed as S
from core import builder_access, builder_tools  # noqa: F401 — the builder's modules, loaded as at boot
from core.evals import scoring, service, store as evals_store
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan, models_menu as MM
from core.native_builder.build_save import save_calls
from core.native_builder.render import render
from core.orchestration.engine import OrchestrationEngine, live
from core.orchestration.state import SharedState
from core.product_tools import areas, evals as E
from core.product_tools.caller import Caller
from core.product_tools.errors import ToolRefusal
from core.product_tools.roles import Role


@pytest.fixture
def judged(monkeypatch):
    seen: list[str] = []

    async def score(run, orch, settings, *, suite_run_id=None):
        seen.append(run.run_id)

    monkeypatch.setattr(scoring, "score_run", score)
    return seen


def _engine(every: int, *, suite: str | None = None, depth: int = 0):
    orch = SimpleNamespace(id="orch_hourly", rubric_id="rub_x", judge_every=every)
    return SimpleNamespace(orch=orch, depth=depth, eval_suite_run_id=suite)


async def _finish(engine, n: int) -> None:
    for i in range(1, n + 1):
        await OrchestrationEngine._score_run(engine, SimpleNamespace(run_id=f"run_{i}"))


class TestTheSample:
    async def test_one_judges_every_run_and_counts_nothing(self, client, judged):
        await _finish(_engine(1), 3)
        assert judged == ["run_1", "run_2", "run_3"]
        from core.store import collections

        assert await collections.load(evals_store.JUDGE_COUNTS) == []

    async def test_n_judges_the_first_then_every_nth_and_never_the_others(self, client, judged):
        await _finish(_engine(3), 7)
        assert judged == ["run_1", "run_3", "run_6"]

    async def test_the_count_is_kept_in_the_store(self, client, judged):
        await _finish(_engine(3), 2)
        from core.store import collections

        assert await collections.load(evals_store.JUDGE_COUNTS) == [{"id": "orch_hourly", "count": 2}]
        assert await evals_store.next_run_number("orch_hourly") == 3, "a restart reads it from the store"

    async def test_a_golden_set_s_runs_are_always_judged_and_a_nested_run_never(self, client, judged):
        await _finish(_engine(5, suite="srun_1"), 3)
        assert judged == ["run_1", "run_2", "run_3"]
        await _finish(_engine(5, depth=1), 2)
        assert judged == ["run_1", "run_2", "run_3"]


class TestSettingIt:
    async def test_attach_sets_it_and_says_it(self, client):
        areas.ensure_loaded()
        await S.seed_orchestrations([S.make_orchestration(id="orch_posts", name="Hourly digest")])
        rubric = await service.save_rubric({"name": "Digest quality", "criteria": [{"name": "Clear"}]})
        caller = Caller(role=Role.BUILDER, via="assistant")
        preview = await E._preview_attach_rubric(caller, {"workflow": "orch_posts", "rubric": rubric["id"],
                                                          "judge_every": 12})
        assert preview["with_it"] == "its first finished run is judged, then 1 run in 12 by it, a judge call each"
        out = await E._attach_rubric(caller, {"workflow": "orch_posts", "rubric": rubric["id"], "judge_every": 12})
        assert out["judge_every"] == 12
        with pytest.raises(ToolRefusal, match="from 1 \\(every run\\) to 100"):
            await E._attach_rubric(caller, {"workflow": "orch_posts", "rubric": rubric["id"], "judge_every": 0})


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture
async def run():
    from core.store import get_store

    await get_store()
    builder_access.forget_runs()
    run = OrchestrationRun(run_id="builder_sample", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": None}, "intent": "create"})
    await SharedState(run).checkpoint()
    yield run
    builder_access.forget_runs()


def _plan(**part) -> dict:
    return {"kind": "create", "title": "Hourly digest", "summary": "Writes an hourly digest.",
            "steps": [{"id": "write", "type": "llm", "name": "Write it", "does": "Writes the digest",
                       "prompt": "Write the digest.", "next": "end"}],
            "schedule": {"cron": "0 * * * *", "says": "every hour"},
            "eval": {"name": "Digest quality", "criteria": [{"name": "Clear"}], **part}}


class TestThePlan:
    async def test_its_card_says_the_sample_and_what_a_judged_run_costs(self, client, run, monkeypatch):
        monkeypatch.setattr(MM, "workspace_model", lambda: "claude-haiku-4-5")

        async def menu():
            return [MM.ModelEntry(id="claude-haiku-4-5", name="Claude Haiku 4.5", provider="anthropic", source="key",
                                  tools="native", vision=True, reasoning=False, price_in=1.0, price_out=5.0,
                                  can_pay=True)]

        monkeypatch.setattr(MM, "menu", menu)
        with live(run):
            found = await build_plan.examine(_plan(judge_every=12), _Server(), state=run.shared_state)
        assert found.errors == [], found.errors
        assert ("- Its first finished run is judged, then 1 run in 12: one more model call per judged run, to "
                "claude-haiku-4-5 (about $0.0060 a judged run), on top of the run's own") in render(found.plan)
        [attach] = [c for c in save_calls(found.plan, {}, run.shared_state) if c["tool"] == "attach_rubric"]
        assert attach["args"]["judge_every"] == 12

    async def test_a_sample_out_of_range_goes_back(self, client, run):
        with live(run):
            found = await build_plan.examine(_plan(judge_every=0), _Server(), state=run.shared_state)
        assert any(e.startswith("eval.judge_every: a whole number from 1") for e in found.errors)
