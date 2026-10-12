"""CE1 (D261) and F454: what each step of a run spent, attributed by time.

`core.orchestration.step_usage.sweep` gives a run's model calls to the steps whose
windows hold them. A history entry now carries an exact window (`started_ms`,
`ended_ms`) beside its whole-second stamps, so a fast step keeps its own call; an
entry written before keeps the old rule. A step that ran more than once sums its
passes, and each pass keeps its own figure.
"""
from datetime import UTC, datetime, timedelta

from core.orchestration.pauses import end_step
from core.orchestration.step_usage import LEGACY_SLACK, Call, Payment, exact_window, sweep, window

T0 = datetime(2026, 10, 12, 7, 50, 3, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def iso(seconds: float) -> str:
    """A whole-second stamp, as the engine writes `started_at`."""
    return at(seconds).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def entry(step_id, start, end, *, exact=True, step_type="llm"):
    e = {"step_id": step_id, "step_type": step_type, "started_at": iso(start), "ended_at": iso(end)}
    if exact:
        e |= {"started_ms": int(at(start).timestamp() * 1000), "ended_ms": int(at(end).timestamp() * 1000)}
    return e


def call(seconds, model="synapse.nova-pro", **kw):
    return Call(at=at(seconds), model=model, tokens=kw.pop("tokens", 100), **kw)


# Editing pass on the dev stack: Structure 0.0–2.69 s, Line edit 2.70–4.11, Changelog 4.12–6.17.
EDITING = [(0.0, 2.69), (2.70, 4.11), (4.12, 6.17)]
EDITING_CALLS = [call(2.65), call(4.06), call(6.10)]


class TestAFastStepKeepsItsOwnCall:
    def test_exact_windows_give_each_step_its_call(self):
        history = [entry(s, a, b) for s, (a, b) in zip(("structure", "line_edit", "changelog"), EDITING)]
        by_step, per_entry = sweep(history, EDITING_CALLS)
        assert {sid: f["turns"] for sid, f in by_step.items()} == {"structure": 1, "line_edit": 1, "changelog": 1}
        assert [f["turns"] for f in per_entry] == [1, 1, 1]

    def test_an_old_entry_keeps_the_old_rule(self):
        # The dev run as stored (run_01a12f17119c…): whole seconds only, so Structure's 2 s slack
        # reaches Line edit's call, as the run page has always read it.
        history = [
            {"step_id": "structure", "started_at": "2026-10-12T07:50:03Z", "ended_at": "2026-10-12T07:50:06Z"},
            {"step_id": "line_edit", "started_at": "2026-10-12T07:50:06Z", "ended_at": "2026-10-12T07:50:07Z"},
            {"step_id": "changelog", "started_at": "2026-10-12T07:50:07Z", "ended_at": "2026-10-12T07:50:10Z"},
        ]
        calls = [call(3.540451), call(4.956874), call(7.018841)]
        by_step, _ = sweep(history, calls)
        assert by_step["structure"]["turns"] == 2
        assert "line_edit" not in by_step
        assert window(history[0])[1] == at(3) + LEGACY_SLACK

    def test_a_settle_written_just_after_the_call_is_still_the_steps(self):
        history = [entry("a", 0, 3), entry("b", 3.1, 9)]
        by_step, _ = sweep(history, [call(2.9), call(8.0)],
                           [Payment(at(3.3), 0.002), Payment(at(8.2), 0.005)])
        assert by_step["a"]["cost_usd"] == 0.002
        assert by_step["b"]["cost_usd"] == 0.005


class TestEveryPassCounts:
    def test_a_step_that_ran_three_times_sums_its_passes(self):
        history = [entry("work", 0, 2), entry("work", 2.5, 4), entry("work", 4.5, 6)]
        calls = [call(1, cost=0.01, tokens=10), call(3, cost=0.02, tokens=20), call(5, cost=0.03, tokens=30)]
        by_step, per_entry = sweep(history, calls, unpriced=lambda m: False)
        assert by_step["work"]["turns"] == 3
        assert by_step["work"]["tokens"] == 60
        assert by_step["work"]["cost_usd"] == 0.06
        assert [f["cost_usd"] for f in per_entry] == [0.01, 0.02, 0.03]

    def test_an_entry_with_no_call_is_none_and_a_call_in_no_window_is_left_out(self):
        history = [entry("ask", 0, 1, step_type="human"), entry("work", 5, 6)]
        by_step, per_entry = sweep(history, [call(3), call(5.5)])
        assert per_entry[0] is None
        assert by_step["work"]["turns"] == 1

    def test_a_running_step_takes_everything_since_it_began(self):
        history = [entry("a", 0, 1), {"step_id": "b", "started_at": iso(2), "started_ms": int(at(2).timestamp() * 1000)}]
        by_step, _ = sweep(history, [call(0.5), call(30)])
        assert by_step["b"]["turns"] == 1


class TestUnknownIsNotFree:
    def test_a_step_whose_only_calls_are_unpriced_has_no_figure(self):
        by_step, _ = sweep([entry("a", 0, 2)], [call(1, model="mystery")], unpriced=lambda m: m == "mystery")
        assert by_step["a"]["cost_usd"] is None

    def test_a_paid_call_beside_an_unpriced_one_keeps_its_figure(self):
        by_step, _ = sweep([entry("a", 0, 2)], [call(1, model="mystery"), call(1.5)],
                           [Payment(at(1.5), 0.004)], unpriced=lambda m: m == "mystery")
        assert by_step["a"]["cost_usd"] == 0.004


class TestTheEngineWritesTheExactWindow:
    def test_exact_window_is_epoch_milliseconds(self):
        assert exact_window(1.2345, 3.5) == {"started_ms": 1234, "ended_ms": 3500}

    def test_a_reopened_entry_keeps_its_exact_start(self):
        history = [{"step_id": "a", "status": "running", "started_at": iso(0), "started_ms": 1000}]
        end_step(history, {"step_id": "a", "status": "completed", "started_at": iso(9),
                           "ended_at": iso(10), "started_ms": 9000, "ended_ms": 10000})
        assert history == [{"step_id": "a", "status": "completed", "started_at": iso(0),
                            "ended_at": iso(10), "started_ms": 1000, "ended_ms": 10000}]

    def test_an_entry_opened_before_the_exact_stamps_stays_on_the_old_rule(self):
        history = [{"step_id": "a", "status": "running", "started_at": iso(0)}]
        end_step(history, {"step_id": "a", "status": "completed", "started_at": iso(9),
                           "ended_at": iso(10), "started_ms": 9000, "ended_ms": 10000})
        assert history == [{"step_id": "a", "status": "completed", "started_at": iso(0), "ended_at": iso(10)}]
