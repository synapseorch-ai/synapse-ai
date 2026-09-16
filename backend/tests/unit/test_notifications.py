"""
NotificationHub: ring buffer, persistence, fan-out, run-event observation,
startup reconstruction of missed human-input pauses, and messaging dispatch.
"""
import asyncio
import types

import pytest

from core.notifications import NotificationHub


@pytest.fixture()
def hub(tmp_path):
    return NotificationHub(capacity=5, persist_path=tmp_path / "notifications.json")


class TestRing:
    def test_publish_list_after_and_capacity(self, hub):
        for n in range(7):
            hub.publish("run_completed", f"title {n}")
        items = hub.list()
        assert len(items) == 5  # capacity trim
        assert [i["id"] for i in items] == [3, 4, 5, 6, 7]
        assert hub.list(after_id=5) == items[-2:]
        assert hub.last_id() == 7

    def test_persistence_round_trip(self, tmp_path):
        # A blob key now, not a filesystem path — the tenant prefix and the
        # directory are the blob store's business, not the hub's.
        path = "test_notifications.json"
        first = NotificationHub(capacity=10, persist_path=path)
        first.publish("human_input", "needs input", body="Approve?")
        second = NotificationHub(capacity=10, persist_path=path)
        assert [i["title"] for i in second.list()] == ["needs input"]
        assert second.publish("run_completed", "done")["id"] == 2  # id continues

    async def test_fanout(self, hub):
        queue = hub.subscribe()
        hub.publish("run_failed", "boom")
        assert (queue.get_nowait())["title"] == "boom"
        hub.unsubscribe(queue)
        hub.publish("run_failed", "again")
        assert queue.empty()


class TestObserver:
    async def test_human_input_uses_cached_orch_name(self, hub):
        await hub.observe_run_event("r1", {"type": "orchestration_start",
                                     "orchestration_name": "Market Watch"})
        await hub.observe_run_event("r1", {"type": "human_input_required",
                                     "prompt": "Approve the trade?", "orch_step_id": "s3"})
        [item] = hub.list()
        assert item["kind"] == "human_input"
        assert item["title"] == "Market Watch needs your input"
        assert item["body"] == "Approve the trade?"
        assert item["run_id"] == "r1"
        assert item["data"]["step_id"] == "s3"

    async def test_terminal_dedupe_error_then_complete(self, hub):
        await hub.observe_run_event("r1", {"type": "orchestration_start", "orchestration_name": "X"})
        await hub.observe_run_event("r1", {"type": "orchestration_error", "error": "step blew up"})
        await hub.observe_run_event("r1", {"type": "orchestration_complete", "status": "failed"})
        items = hub.list()
        assert len(items) == 1
        assert items[0]["kind"] == "run_failed"
        assert "step blew up" in items[0]["body"]

    async def test_step_start_rearms_terminal_notification(self, hub):
        """A resumed run (no orchestration_start) must still notify on its
        second completion."""
        await hub.observe_run_event("r1", {"type": "orchestration_start", "orchestration_name": "X"})
        await hub.observe_run_event("r1", {"type": "orchestration_error", "error": "boom"})
        await hub.observe_run_event("r1", {"type": "step_start", "orch_step_id": "s1"})  # resume pump
        await hub.observe_run_event("r1", {"type": "orchestration_complete", "status": "completed"})
        assert [i["kind"] for i in hub.list()] == ["run_failed", "run_completed"]

    async def test_step_start_resolves_pending_human_input(self, hub):
        """Answering the input (run resumes → step_start) marks the
        notification resolved and pushes the update to live subscribers."""
        await hub.observe_run_event("r1", {"type": "orchestration_start", "orchestration_name": "X"})
        await hub.observe_run_event("r1", {"type": "human_input_required", "prompt": "Approve?"})
        queue = hub.subscribe()
        await hub.observe_run_event("r1", {"type": "step_start", "orch_step_id": "s2"})
        [item] = hub.list()
        assert item["kind"] == "human_input"
        assert item["resolved"] is True
        update = queue.get_nowait()  # same id, re-pushed as an update
        assert update["id"] == item["id"] and update["resolved"] is True

    async def test_completion_resolves_pending_human_input(self, hub):
        await hub.observe_run_event("r1", {"type": "orchestration_start", "orchestration_name": "X"})
        await hub.observe_run_event("r1", {"type": "human_input_required", "prompt": "Approve?"})
        await hub.observe_run_event("r1", {"type": "orchestration_complete", "status": "completed"})
        by_kind = {i["kind"]: i for i in hub.list()}
        assert by_kind["human_input"]["resolved"] is True
        assert "resolved" not in by_kind["run_completed"]

    async def test_completed_and_ignored_events(self, hub):
        await hub.observe_run_event("r1", {"type": "orchestration_start", "orchestration_name": "X"})
        await hub.observe_run_event("r1", {"type": "tool_execution", "tool_name": "t"})
        await hub.observe_run_event("r1", {"type": "orchestration_complete", "status": "completed"})
        await hub.observe_run_event("r1", {"type": "run_stream_end", "paused": False})
        items = hub.list()
        assert [i["kind"] for i in items] == ["run_completed"]
        assert items[0]["title"] == "X completed"


class TestReconstruction:
    async def test_missed_pause_is_republished_once(self, hub, tmp_path, monkeypatch, seed_orchestration):
        import core.orchestration.state as state_mod
        from core.models_orchestration import OrchestrationRun

        orch = await seed_orchestration(name="Paused Orch")
        run = OrchestrationRun(run_id="run_p", orchestration_id=orch["id"], status="paused",
                              waiting_for_human=True, human_prompt="Pick one")
        await state_mod.SharedState(run).checkpoint()

        await hub.reconstruct_missed()
        [item] = hub.list()
        assert item["kind"] == "human_input"
        assert item["title"] == "Paused Orch needs your input"
        assert item["body"] == "Pick one"

        await hub.reconstruct_missed()  # idempotent
        assert len(hub.list()) == 1

    async def test_startup_resolves_stale_unresolved_items(self, hub, tmp_path, monkeypatch):
        """An unresolved 'needs your input' whose run is no longer waiting
        (answered/completed while the server was down) is resolved at startup."""
        import core.orchestration.state as state_mod
        from core.models_orchestration import OrchestrationRun

        hub.publish("human_input", "Old needs your input", run_id="run_done")
        run = OrchestrationRun(run_id="run_done", orchestration_id="o1",
                              status="completed", waiting_for_human=False)
        await state_mod.SharedState(run).checkpoint()

        await hub.reconstruct_missed()
        [item] = [i for i in hub.list() if i["kind"] == "human_input"]
        assert item["resolved"] is True

    async def test_repaused_run_with_resolved_item_notifies_again(self, hub, tmp_path,
                                                            monkeypatch, seed_orchestration):
        """A run that paused again at a later step gets a fresh notification
        even though an older resolved one exists."""
        import core.orchestration.state as state_mod
        from core.models_orchestration import OrchestrationRun

        orch = await seed_orchestration(name="Twice Paused")
        first = hub.publish("human_input", "first ask", run_id="run_2p")
        hub._resolve_human_input("run_2p")
        run = OrchestrationRun(run_id="run_2p", orchestration_id=orch["id"], status="paused",
                              waiting_for_human=True, human_prompt="second ask")
        await state_mod.SharedState(run).checkpoint()

        await hub.reconstruct_missed()
        items = [i for i in hub.list() if i["kind"] == "human_input"]
        assert len(items) == 2
        assert items[0]["id"] == first["id"] and items[0]["resolved"] is True
        assert items[1]["body"] == "second ask" and not items[1].get("resolved")

    async def test_non_waiting_paused_runs_ignored(self, hub, tmp_path, monkeypatch):
        import core.orchestration.state as state_mod
        from core.models_orchestration import OrchestrationRun
        run = OrchestrationRun(run_id="run_q", orchestration_id="o1", status="paused",
                              waiting_for_human=False)
        await state_mod.SharedState(run).checkpoint()
        await hub.reconstruct_missed()
        assert hub.list() == []


class TestMessagingDispatch:
    async def test_dispatch_to_configured_channel(self, hub, monkeypatch):
        sent: list[tuple[str, str]] = []

        class Adapter:
            _last_chat_id = "fallback_chat"
            async def send_message(self, chat_id, text):
                sent.append((chat_id, text))

        manager = types.SimpleNamespace(_adapters={"ch1": Adapter()})
        hub.configure(types.SimpleNamespace(messaging_manager=manager))

        import core.config as config_mod
        monkeypatch.setattr(config_mod, "load_settings",
                            lambda: {"run_notification_channel_id": "ch1"})
        import core.messaging.store as store_mod
        async def _get_channel(cid):
            return {"id": cid, "notify_chat_id": "chat9"}

        monkeypatch.setattr(store_mod, "get_channel", _get_channel)

        hub.publish("human_input", "Orch needs your input", body="Approve?")
        await asyncio.sleep(0)  # let the dispatch task run
        await asyncio.sleep(0)
        assert sent == [("chat9", "⏸ Orch needs your input\nApprove?")]

    async def test_no_channel_configured_is_noop(self, hub, monkeypatch):
        import core.config as config_mod
        monkeypatch.setattr(config_mod, "load_settings", lambda: {})
        hub.configure(types.SimpleNamespace(messaging_manager=None))
        hub.publish("run_completed", "done")
        await asyncio.sleep(0)  # must not raise


class TestEndpoints:
    @pytest.fixture()
    def routed_hub(self, hub, monkeypatch):
        import core.routes.notifications as routes_mod
        monkeypatch.setattr(routes_mod, "hub", hub)
        return hub

    async def test_list_endpoint_with_after(self, client, routed_hub):
        routed_hub.publish("run_completed", "one")
        routed_hub.publish("run_failed", "two")
        resp = await client.get("/api/notifications")
        body = resp.json()
        assert [n["title"] for n in body["notifications"]] == ["one", "two"]
        assert body["last_id"] == 2
        resp = await client.get("/api/notifications?after=1")
        assert [n["title"] for n in resp.json()["notifications"]] == ["two"]

    async def test_stream_replays_then_tails(self, routed_hub):
        from core.routes.notifications import _stream_notifications
        routed_hub.publish("run_completed", "old")
        agen = _stream_notifications(0)
        chunk = await agen.__anext__()
        assert '"title": "old"' in chunk
        next_chunk = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0)
        routed_hub.publish("human_input", "fresh")
        chunk = await next_chunk
        assert '"title": "fresh"' in chunk
        assert chunk.startswith("id: 2\n")
        await agen.aclose()


class TestTheBuilderIsNotAnnounced:
    """A builder run is driven by the same pump as every other run, so the hub
    sees its events — and must say nothing about them.

    The panel shows the builder's question in the conversation the person is
    already looking at. A bell item and a toast for every clarifying question
    would duplicate that, and the toast lands in the one corner that already
    covers a parked run's Answer control.
    """

    async def test_a_builder_run_raises_nothing(self, hub):
        from core.native_builder import BUILDER_RUN_PREFIX

        run = f"{BUILDER_RUN_PREFIX}9f2c1a"
        await hub.observe_run_event(run, {"type": "orchestration_start",
                                          "orchestration_name": "Synapse AI Builder"})
        await hub.observe_run_event(run, {"type": "human_input_required",
                                          "prompt": "Which orchestration?"})
        await hub.observe_run_event(run, {"type": "orchestration_complete",
                                          "status": "completed"})
        assert hub.list() == []

    async def test_a_resumed_builder_run_raises_nothing(self, hub):
        """A resume emits no `orchestration_start`, which is why the run id and
        not the orchestration id is what this is keyed on."""
        from core.native_builder import BUILDER_RUN_PREFIX

        run = f"{BUILDER_RUN_PREFIX}9f2c1a"
        await hub.observe_run_event(run, {"type": "step_start", "orch_step_id": "s1"})
        await hub.observe_run_event(run, {"type": "orchestration_error", "error": "Cancelled"})
        assert hub.list() == []

    async def test_every_other_run_is_still_announced(self, hub):
        await hub.observe_run_event("run_x", {"type": "orchestration_start",
                                              "orchestration_name": "Nightly brief"})
        await hub.observe_run_event("run_x", {"type": "orchestration_complete",
                                              "status": "completed"})
        assert [i["kind"] for i in hub.list()] == ["run_completed"]
