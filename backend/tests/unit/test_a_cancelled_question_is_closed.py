"""
Cancelling a run that waits on a person closes its question everywhere (F193).

F141 made the run stop asking: the checkpoint says `cancelled`, the flag is
down and the open entry is closed. But the cancel of a *paused* run emitted
nothing — its pump had ended at the pause — so everything fed by events went
on believing the question stood: the bell kept its "needs your input" item
unresolved (and, after a restart, labelled it *answered*), no "cancelled"
notification came, and a reopened run replayed its journal to the question and
offered Answer again. Measured on a scratch install.

Now a paused run's cancel is announced the way a running one's is: the pump's
own `orchestration_error "Cancelled"`, then the terminator that closes a tailing
reader, through the pump's observers — so the journal, a live tail and the
notification hub all hear it. The hub resolves the question as *cancelled*.
"""
import types

from _fakes import seed as S
from test_human_in_containers import _human, _print


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _paused_through_the_pump(run_id, hub):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    from core.orchestration.runner import spawn_engine_run

    orch = S.make_orchestration(id=f"orch_{run_id}", name="Approvals", entry_step_id="ask",
                                steps=[_human("ask", prompt="Ship it?", nxt="tail"), _print("tail")])
    await S.seed_orchestrations([orch])
    engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
    task, _ = spawn_engine_run(engine.run("go", run_id=run_id), run_id)
    await task
    asking = [i for i in hub.list() if i["run_id"] == run_id and i["kind"] == "human_input"]
    assert asking and not asking[0].get("resolved"), hub.list()


class TestACancelledQuestionIsClosed:
    async def _hub(self, tmp_path, monkeypatch):
        from core.notifications import NotificationHub
        from core.orchestration import runner

        hub = NotificationHub(capacity=50, persist_path=str(tmp_path / "n.json"))
        monkeypatch.setattr(runner, "event_observers", [hub.observe_run_event])
        return hub

    async def test_the_bell_resolves_it_as_cancelled_and_says_so(self, tmp_path, monkeypatch):
        from core.orchestration.cancel import cancel_run

        hub = await self._hub(tmp_path, monkeypatch)
        await _paused_through_the_pump("run_f193_bell", hub)

        assert (await cancel_run("run_f193_bell")).status == "cancelled"

        items = [i for i in hub.list() if i["run_id"] == "run_f193_bell"]
        asking = [i for i in items if i["kind"] == "human_input"]
        assert [(i.get("resolved"), i.get("resolved_as")) for i in asking] == [(True, "cancelled")], items
        ended = [i for i in items if i["kind"] != "human_input"]
        assert [i["title"] for i in ended] == ["Approvals cancelled"], items

    async def test_the_journal_ends_with_the_cancel(self, tmp_path, monkeypatch):
        from core.orchestration.cancel import cancel_run
        from core.orchestration.journal import get_journal

        hub = await self._hub(tmp_path, monkeypatch)
        await _paused_through_the_pump("run_f193_journal", hub)
        await cancel_run("run_f193_journal")

        events = [e["event"] for e in get_journal("run_f193_journal").read()]
        types_ = [e["type"] for e in events]
        asked = types_.index("human_input_required")
        tail = events[asked + 1:]
        assert {"type": "orchestration_error", "error": "Cancelled"} in tail, types_
        assert tail[-1] == {"type": "run_stream_end", "paused": False}, tail

    async def test_a_second_cancel_announces_nothing_more(self, tmp_path, monkeypatch):
        from core.orchestration.cancel import cancel_run

        hub = await self._hub(tmp_path, monkeypatch)
        await _paused_through_the_pump("run_f193_twice", hub)
        await cancel_run("run_f193_twice")
        before = len(hub.list())
        result = await cancel_run("run_f193_twice")
        assert result.stopped is False
        assert len(hub.list()) == before


class TestTheRestartSweepSaysCancelledToo:
    async def test_a_question_left_open_across_a_restart_is_resolved_as_cancelled(self, tmp_path):
        """A cancel before this fix left the item open; the startup sweep closed
        it as *answered*. It reads the run's status now."""
        from core.notifications import NotificationHub
        from core.orchestration.cancel import cancel_run
        from core.orchestration.engine import OrchestrationEngine
        from core.models_orchestration import Orchestration

        orch = S.make_orchestration(id="orch_f193_sweep", entry_step_id="ask",
                                    steps=[_human("ask", nxt="tail"), _print("tail")])
        await S.seed_orchestrations([orch])
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        [e async for e in engine.run("go", run_id="run_f193_sweep")]
        await cancel_run("run_f193_sweep")

        hub = NotificationHub(capacity=50, persist_path=str(tmp_path / "n.json"))
        hub.publish("human_input", "X needs your input", run_id="run_f193_sweep")
        await hub.reconstruct_missed()
        item = next(i for i in hub.list() if i["run_id"] == "run_f193_sweep")
        assert (item.get("resolved"), item.get("resolved_as")) == (True, "cancelled"), item
