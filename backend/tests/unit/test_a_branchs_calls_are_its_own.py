"""A model call carries the step it ran for, so concurrent branches each keep their own (F461).

C9 measured it on the dev fleet: a Parallel of four agent branches on one nova-micro agent, four calls at once
(2.3–2.7 s each), and the run's step usage held one key, `w0`, with all four turns and the whole spend; `w1`–`w3`
held nothing. `sweep` gives each history entry the calls inside its window, steps in order, each consuming what it
claims — and a branch's entry is written when it ends, so the first branch to finish holds a window around all four.
`usage_logs` had no step.

Now the engine stamps a call with the step it ran for (`step_scope`, set where every executor runs a step: the
engine's loop, a Parallel's branch task, a Loop's body), `log_usage` writes it in the row's `details`, and `sweep`
gives a stamped call to its step first. What carries no stamp — an older row, a nested run's call (its own run's
step) — is swept by time as before. A wallet's settle (cloud) follows the stamped call it paid for: same tokens, a
moment before the call is logged.
"""
import asyncio
from datetime import UTC, datetime, timedelta

from core.orchestration.step_usage import Call, Payment, current_step, step_scope, sweep

T0 = datetime(2026, 10, 13, 5, 11, 10, tzinfo=UTC)
RUN = "run_f461"


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def entry(step_id, start, end):
    return {"step_id": step_id, "step_type": "agent", "started_at": at(start).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ended_at": at(end).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "started_ms": int(at(start).timestamp() * 1000), "ended_ms": int(at(end).timestamp() * 1000)}


# C9's run: four branches started together; history in the order they finished, the Parallel's entry last.
HISTORY = [entry("w0", 0.0, 2.3), entry("w2", 0.0, 2.5), entry("w1", 0.0, 2.6), entry("w3", 0.0, 2.7),
           entry("par", 0.0, 2.8)]
OUTPUT = {"w0": 390, "w1": 326, "w2": 314, "w3": 492}
DONE = {"w0": 2.2, "w1": 2.25, "w2": 2.26, "w3": 2.69}


def calls(stamped=True):
    return [Call(at=at(DONE[s]), model="synapse.nova-micro", input_tokens=192, output_tokens=OUTPUT[s],
                 cache_read_tokens=1296, tokens=192 + OUTPUT[s] + 1296,
                 step=(RUN, s) if stamped else None) for s in ("w0", "w1", "w2", "w3")]


def settles():
    # Each a few ms before its call is logged, as the gateway settles as the call returns.
    return [Payment(at=at(DONE[s] - 0.03), usd=OUTPUT[s] / 1_000_000, input_tokens=192, output_tokens=OUTPUT[s],
                    cache_read_tokens=1296, cache_write_tokens=0) for s in ("w0", "w1", "w2", "w3")]


def test_unstamped_the_first_branch_takes_every_call():
    # The rule calls without a stamp still read by (an older row): what C9 measured.
    by_step, _ = sweep(HISTORY, calls(stamped=False), run_id=RUN)
    assert by_step["w0"]["turns"] == 4 and "w1" not in by_step


def test_stamped_each_branch_keeps_its_own_call():
    by_step, per_entry = sweep(HISTORY, calls(), run_id=RUN)
    assert {s: f["turns"] for s, f in by_step.items()} == {"w0": 1, "w1": 1, "w2": 1, "w3": 1}
    assert {s: f["output_tokens"] for s, f in by_step.items()} == OUTPUT
    assert [f and f["turns"] for f in per_entry] == [1, 1, 1, 1, None]


def test_a_settle_follows_the_call_it_paid_for():
    by_step, _ = sweep(HISTORY, [c for c in calls()], settles(), run_id=RUN)
    assert {s: f["cost_usd"] for s, f in by_step.items()} == {s: OUTPUT[s] / 1_000_000 for s in OUTPUT}


def test_a_settle_with_no_matching_call_is_swept_by_time():
    odd = Payment(at=at(1.0), usd=0.5, input_tokens=7, output_tokens=7, cache_read_tokens=0, cache_write_tokens=0)
    by_step, _ = sweep(HISTORY, calls(), [*settles(), odd], run_id=RUN)
    # Inside every branch's window; the first entry swept takes it, as an unpaired settle always went.
    assert round(by_step["w0"]["cost_usd"], 6) == round(OUTPUT["w0"] / 1_000_000 + 0.5, 6)


def test_another_runs_stamp_is_swept_by_time():
    # A nested run's call carries its own run's step: the parent gives it to the step that ran the child (F284).
    nested = Call(at=at(1.0), model="synapse.nova-micro", tokens=10, step=("run_f461__child", "inner"))
    by_step, _ = sweep([entry("call_child", 0.0, 2.0)], [nested], run_id=RUN)
    assert by_step["call_child"]["turns"] == 1


def test_a_stamp_for_a_step_with_no_entry_yet_is_swept_by_time():
    by_step, _ = sweep(HISTORY, [Call(at=at(1.0), tokens=1, step=(RUN, "still_running"))], run_id=RUN)
    assert by_step["w0"]["turns"] == 1


def test_without_a_run_id_stamps_are_not_read():
    by_step, _ = sweep(HISTORY, calls())
    assert by_step["w0"]["turns"] == 4


async def test_the_scope_is_the_tasks_own():
    seen = {}

    async def branch(step_id):
        with step_scope(RUN, step_id):
            await asyncio.sleep(0.01)
            seen[step_id] = current_step()

    with step_scope(RUN, "par"):
        await asyncio.gather(*(branch(f"w{i}") for i in range(4)))
        assert current_step() == {"run_id": RUN, "step_id": "par"}
    assert current_step() is None
    assert seen == {f"w{i}": {"run_id": RUN, "step_id": f"w{i}"} for i in range(4)}


async def test_a_logged_call_carries_its_step(monkeypatch):
    import core.usage_tracker as ut

    rows = []

    async def append(values):
        rows.append(values)

    monkeypatch.setattr(ut.usage_store, "append", append)
    with step_scope(RUN, "w2"):
        await ut.log_usage(model="m", provider="p", input_tokens=1, output_tokens=1, context_chars=1, run_id=RUN)
    await ut.log_usage(model="m", provider="p", input_tokens=1, output_tokens=1, context_chars=1)
    assert rows[0]["details"] == {"step": {"run_id": RUN, "step_id": "w2"}}
    assert "details" not in rows[1]


async def test_a_parallels_branches_are_stamped_by_the_engine(monkeypatch, fake_llm):
    """End to end in the engine: four agent branches, each's model call logged under its own step."""
    import types

    import core.usage_tracker as ut
    from _fakes import seed as S

    stamps = []

    async def append(values):
        stamps.append((values.get("details") or {}).get("step"))

    monkeypatch.setattr(ut.usage_store, "append", append)
    real_llm = fake_llm.__call__

    async def logged(*args, **kwargs):
        reply = await real_llm(*args, **kwargs)
        await ut.log_usage(model="m", provider="p", input_tokens=1, output_tokens=1, context_chars=1,
                           run_id=kwargs.get("run_id"))
        return reply

    import core.react_engine as re_mod
    monkeypatch.setattr(re_mod, "llm_generate_response", logged)
    fake_llm.set_default("done")
    await S.seed_agents([S.make_agent(id="a_plain_f461", tools=[], skip_default_tools=True)])
    steps = [{"id": "par", "name": "Par", "type": "parallel", "parallel_branches": [[f"w{i}"] for i in range(4)],
              "next_step_id": None}]
    steps += [{"id": f"w{i}", "name": f"W{i}", "type": "agent", "agent_id": "a_plain_f461",
               "prompt_template": f"write {i}", "output_key": f"o{i}"} for i in range(4)]
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    orch = S.make_orchestration(id="orch_f461", entry_step_id="par", steps=steps)
    engine = OrchestrationEngine(Orchestration.model_validate(orch),
                                 types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}))
    [ev async for ev in engine.run("go", run_id=RUN)]
    assert sorted(s["step_id"] for s in stamps if s) == ["w0", "w1", "w2", "w3"]
    assert all(s and s["run_id"] == RUN for s in stamps)
