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


class _Provider:
    """Any resource provider opens `tenant_scope()`; these tests resolve nothing."""

    async def resolve_agent(self, agent_id):
        return None

    async def resolve_orchestration(self, orch_id):
        return None

    async def resolve_custom_tools(self):
        return []

    async def resolve_mcp_servers(self):
        return []


@pytest.fixture
def multi_tenant():
    from core.scale.context import set_resource_provider

    set_resource_provider(_Provider())
    yield
    set_resource_provider(None)


def _titles(raw) -> list[str]:
    import json

    return [i["title"] for i in json.loads(raw or "[]")]


class TestOneRingPerTenant:
    """F337: the hub was one ring per process, persisted under whichever tenant
    was current. A cloud worker serves every org, so org A's eval regression
    joined the ring and the next persist inside org B's job wrote it to B's
    `notifications.json`. Each tenant now has its own ring, saved under its own
    prefix, and its own subscribers."""

    def test_each_tenant_persists_only_its_own(self, multi_tenant):
        from core.notifications import NOTIFICATIONS_KEY
        from core.storage import get_blob_store
        from core.tenancy import tenant_scope

        hub = NotificationHub()
        with tenant_scope("acme"):
            hub.publish("eval_regression", "acme regressed")
        with tenant_scope("globex"):
            hub.publish("eval_regression", "globex regressed")

        store = get_blob_store()
        with tenant_scope("acme"):
            assert _titles(store.get(NOTIFICATIONS_KEY)) == ["acme regressed"]
        with tenant_scope("globex"):
            assert _titles(store.get(NOTIFICATIONS_KEY)) == ["globex regressed"]

    def test_each_tenant_lists_only_its_own(self, multi_tenant):
        from core.tenancy import tenant_scope

        hub = NotificationHub()
        with tenant_scope("acme"):
            hub.publish("run_completed", "acme done")
        with tenant_scope("globex"):
            hub.publish("run_completed", "globex done")
            assert [i["title"] for i in hub.list()] == ["globex done"]
            assert hub.last_id() == 1
        with tenant_scope("acme"):
            assert [i["title"] for i in hub.list()] == ["acme done"]

    def test_a_new_process_loads_each_tenants_own_ring(self, multi_tenant):
        """Constructed under one tenant (the import happens inside some org's
        job) and then used under another: nothing of the first leaks."""
        from core.tenancy import tenant_scope

        first = NotificationHub()
        with tenant_scope("acme"):
            first.publish("run_completed", "acme done")
            second = NotificationHub()
        with tenant_scope("globex"):
            assert second.list() == []
            second.publish("run_completed", "globex done")
        with tenant_scope("acme"):
            assert [i["title"] for i in second.list()] == ["acme done"]

    async def test_a_subscriber_hears_only_its_own_tenant(self, multi_tenant):
        from core.tenancy import tenant_scope

        hub = NotificationHub()
        with tenant_scope("acme"):
            queue = hub.subscribe()
        with tenant_scope("globex"):
            hub.publish("run_completed", "globex done")
        assert queue.empty()
        with tenant_scope("acme"):
            hub.publish("run_completed", "acme done")
        assert queue.get_nowait()["title"] == "acme done"

    def test_resolving_reaches_only_the_current_tenant(self, multi_tenant):
        from core.tenancy import tenant_scope

        hub = NotificationHub()
        with tenant_scope("acme"):
            hub.publish("human_input", "acme asks", run_id="r1")
        with tenant_scope("globex"):
            hub.publish("human_input", "globex asks", run_id="r1")
            hub._resolve_human_input("r1")
            assert [i.get("resolved") for i in hub.list()] == [True]
        with tenant_scope("acme"):
            assert [i.get("resolved") for i in hub.list()] == [None]

    def test_an_evicted_tenant_reloads_from_its_store(self, multi_tenant):
        """The rings are bounded; dropping one loses nothing, because every
        change was already saved."""
        from core.tenancy import tenant_scope

        hub = NotificationHub(max_tenants=2)
        for tenant in ("acme", "globex", "initech"):
            with tenant_scope(tenant):
                hub.publish("run_completed", f"{tenant} done")
        assert len(hub._rings) == 2
        with tenant_scope("acme"):
            assert [i["title"] for i in hub.list()] == ["acme done"]
            assert hub.publish("run_completed", "acme again")["id"] == 2


class TestOneRingInScaleMode:
    """F361: in scale mode each API server and each worker kept its own copy of
    the ring and wrote it whole: a worker's eval regression went over the file
    the API kept rewriting, the API's next publish wrote its own copy back over
    the worker's item, and the bell (which reads memory) never heard it. OSS's
    k8s runs three API servers, which did the same to each other. Shared, every
    process changes the one ring in turn and every API server hears it."""

    KEY = "scale_notifications.json"

    @pytest.fixture
    async def redis(self):
        from _fakes.fake_redis_stream import new_fake_redis

        r = new_fake_redis()
        yield r
        await r.aclose()

    @pytest.fixture
    async def processes(self, redis):
        made: list[NotificationHub] = []

        async def process(*, api: bool) -> NotificationHub:
            hub = NotificationHub(persist_path=self.KEY)
            hub.share(redis, listen=api)
            if api:
                await asyncio.wait_for(hub._shared.listening.wait(), 2)
            made.append(hub)
            return hub

        yield process
        for hub in made:
            await hub.unshare()

    @staticmethod
    async def _resolved(queue: asyncio.Queue) -> dict:
        while not (item := await queue.get()).get("resolved"):
            pass
        return item

    def _saved(self) -> list[tuple[int, str]]:
        return [(i["id"], i["title"]) for i in NotificationHub(persist_path=self.KEY).list()]

    async def test_a_worker_s_notice_and_the_api_s_both_survive(self, processes):
        api, worker = await processes(api=True), await processes(api=False)
        api.publish("run_completed", "A")
        await api.flush()
        assert [i["title"] for i in worker.list()] == ["A"]  # the worker has read the ring
        api.publish("run_completed", "B")
        await api.flush()
        worker.publish("eval_regression", "C")
        await worker.flush()
        assert self._saved() == [(1, "A"), (2, "B"), (3, "C")]

    async def test_every_api_server_hears_a_worker_s_notice(self, processes):
        one, two, worker = await processes(api=True), await processes(api=True), await processes(api=False)
        heard = [one.subscribe(), two.subscribe()]
        worker.publish("eval_regression", "C")
        await worker.flush()
        got = [await asyncio.wait_for(q.get(), 2) for q in heard]
        assert [(g["id"], g["title"]) for g in got] == [(1, "C"), (1, "C")]
        assert [i["title"] for i in one.list()] == [i["title"] for i in two.list()] == ["C"]

    async def test_a_worker_s_resolve_is_kept_and_heard(self, processes):
        api, worker = await processes(api=True), await processes(api=False)
        api.publish("tool_approval", "Approve?", data={"approval_id": "ap1"})
        await api.flush()
        heard = api.subscribe()
        worker.resolve_approval("ap1", "allowed")
        await worker.flush()
        update = await asyncio.wait_for(self._resolved(heard), 2)  # the item's own announcement may come first
        assert (update["id"], update["resolved_as"]) == (1, "allowed")
        assert [i.get("resolved") for i in api.list()] == [True]

    async def test_processes_writing_at_once_never_reuse_an_id(self, processes):
        api, worker = await processes(api=True), await processes(api=False)
        for n in range(5):
            api.publish("run_completed", f"api {n}")
            worker.publish("eval_regression", f"worker {n}")
        await asyncio.gather(api.flush(), worker.flush())
        saved = self._saved()
        assert [i for i, _ in saved] == list(range(1, 11)) and len({t for _, t in saved}) == 10

    async def test_a_worker_shutting_down_writes_what_it_was_given(self, redis):
        worker = NotificationHub(persist_path=self.KEY)
        worker.share(redis, listen=False)
        worker.publish("eval_regression", "last words")
        await worker.unshare()
        assert self._saved() == [(1, "last words")]

    async def test_each_tenant_has_its_own_shared_ring(self, processes, multi_tenant):
        from core.tenancy import tenant_scope

        api, worker = await processes(api=True), await processes(api=False)
        with tenant_scope("acme"):
            heard = api.subscribe()
        with tenant_scope("globex"):
            worker.publish("eval_regression", "globex regressed")
        with tenant_scope("acme"):
            worker.publish("eval_regression", "acme regressed")
        await worker.flush()
        assert (await asyncio.wait_for(heard.get(), 2))["title"] == "acme regressed" and heard.empty()
        with tenant_scope("globex"):
            assert [i["title"] for i in api.list()] == ["globex regressed"]
