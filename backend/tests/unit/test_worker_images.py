"""Attachments reaching a run that is executed by a worker.

`OrchestrationEngine` grew `images` when the builder learned to take them, and
the in-process route sets `engine.images` directly. The queued path did not:
`run_orchestration_job` → `WorkerEngineAdapter.run` → `engine.run()` had nowhere
for an attachment to travel, so on a shared fleet — where every run is queued —
a builder conversation with a picture in it ran without the picture.

`initial_state` is not the way. It becomes `shared_state`, which is echoed to
the browser twice per run, checkpointed at every step boundary, returned by the
run-detail endpoint and fed to the memory summariser. `test_builder_images.py`
pins that argument; this pins the seam that avoids it.
"""
import base64
import types

from core.scale.worker_engine_adapter import WorkerEngineAdapter

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
URI = "data:image/png;base64," + base64.b64encode(PNG).decode()


class _Engine:
    """Just enough engine to record what the adapter put on it."""

    def __init__(self):
        self.images: list[str] = []
        self.step_map = {}
        self.logger = None
        self.seen: dict = {}

    async def run(self, initial_input, run_id, session_id=None, initial_state=None):
        self.seen = {"initial_input": initial_input, "initial_state": initial_state}
        yield {"type": "orchestration_complete", "status": "completed"}


class _Publisher:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)

    async def publish_done(self):
        self.events.append({"type": "done"})

    async def publish_paused(self):
        self.events.append({"type": "paused"})


def _adapter(engine, monkeypatch):
    adapter = WorkerEngineAdapter(
        orch=types.SimpleNamespace(id="orch_1", name="Builder", definition={}),
        run_id="run_1",
        worker_server_module=object(),
        publisher=_Publisher(),
        session_factory=object(),
        redis_client=None,
    )
    monkeypatch.setattr(adapter, "_build_engine", lambda: engine)

    async def _sync_final_state(_status):
        return None

    monkeypatch.setattr(adapter, "_sync_final_state", _sync_final_state)
    return adapter


class TestARunCarriesItsAttachments:
    async def test_they_are_set_on_the_engine(self, monkeypatch):
        engine = _Engine()
        adapter = _adapter(engine, monkeypatch)

        await adapter.run(initial_input="what is this?", images=[URI])

        assert engine.images == [URI]

    async def test_they_are_not_in_shared_state(self, monkeypatch):
        """The whole reason the parameter exists rather than a state key."""
        engine = _Engine()
        adapter = _adapter(engine, monkeypatch)

        await adapter.run(
            initial_input="what is this?",
            initial_state={"user_message": "what is this?"},
            images=[URI],
        )

        assert URI not in str(engine.seen["initial_state"])

    async def test_a_run_with_no_attachments_gets_an_empty_list_not_none(
        self, monkeypatch
    ):
        """`steps.py` reads `getattr(engine, "images", None) or None`, so either
        works — but a run that leaves the attribute at whatever the last engine
        had is the bug this rules out."""
        engine = _Engine()
        engine.images = ["stale"]
        adapter = _adapter(engine, monkeypatch)

        await adapter.run(initial_input="hello")

        assert engine.images == []
