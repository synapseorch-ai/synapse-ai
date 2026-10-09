"""F395: a workflow agent's run that waits on a person keeps the images it was sent.

Since F313 chat hands a workflow agent's images to its run, and a step with
"Sees attachments" reads them. They ride on the engine, never in shared state,
so a run that paused lost them: the resume built a new engine, and a step after
the answer saw only the answer's images — none, from a chat or the run's page.

Now a top-level run keeps references to them on its record (never the bytes),
and every resume — an answer, a Replay — reads them back; the answer's images
join them. This install keeps the images as blobs under the run; a host with
its own store (cloud's attachments) passes its own references and reads them.
"""
import pytest

from _fakes import seed as S
from _fakes.images import data_uri, png
from tests.unit.test_a_failed_child_fails_its_parent import _human
from tests.unit.test_a_workflow_agent_sees_what_chat_sends import (
    _chat, _eyes, _images_for, _look, _nest, _server, _workflow_agent,
)

PNG = data_uri(png())
PNG2 = data_uri(png(2, 2))


def _asked(events) -> str:
    [asked] = [e for e in events if e.get("type") == "human_input_required"]
    return asked["run_id"]


async def _record(run_id):
    from core.orchestration.state import SharedState
    return (await SharedState.restore(run_id)).run


class TestAnAnswerGivesThemBack:
    async def test_a_step_after_the_question_sees_what_chat_sent(self, fake_llm):
        from core.orchestration.engine import OrchestrationEngine

        agent = await _workflow_agent("orch_f395a", [_human("ask", nxt="look"), _look()], "ask")
        run_id = _asked(await _chat(agent, "what is this?", [PNG]))
        assert not _images_for(fake_llm, "Describe the picture."), "nothing looked before the answer"

        [e async for e in OrchestrationEngine.resume(run_id, {"approval": "yes"}, _server())]
        seen = _images_for(fake_llm, "Describe the picture.")
        assert seen and all(s == [PNG] for s in seen), seen

    async def test_the_answers_own_join_them(self, fake_llm):
        from core.orchestration.engine import OrchestrationEngine

        agent = await _workflow_agent("orch_f395b", [_human("ask", nxt="look"), _look()], "ask")
        run_id = _asked(await _chat(agent, "what is this?", [PNG]))
        [e async for e in OrchestrationEngine.resume(run_id, {"approval": "yes"}, _server(), images=[PNG2, PNG])]
        seen = _images_for(fake_llm, "Describe the picture.")
        assert seen and all(s == [PNG, PNG2] for s in seen), seen

    async def test_a_nested_step_after_its_question_sees_them_through_a_step_that_passes_them(self, fake_llm):
        from core.orchestration.engine import OrchestrationEngine

        inner = S.make_orchestration(id="orch_f395n_in", name="Inner", entry_step_id="ask",
                                     steps=[_human("ask", nxt="look"), _look()])
        agent = await _workflow_agent(
            "orch_f395n_out", [_nest("call", "agent_orch_f395n_in", takes=True)], "call",
            more_orchs=[inner], more_agents=[S.make_orchestrator_agent(inner["id"], id="agent_orch_f395n_in")])
        run_id = _asked(await _chat(agent, "what is this?", [PNG]))
        [e async for e in OrchestrationEngine.resume(run_id, {"approval": "yes"}, _server())]
        seen = _images_for(fake_llm, "Describe the picture.")
        assert seen and all(s == [PNG] for s in seen), seen


class TestWhatTheRunKeeps:
    async def test_references_on_the_record_never_the_bytes(self, fake_llm):
        agent = await _workflow_agent("orch_f395r", [_human("ask", nxt="look"), _look()], "ask")
        run_id = _asked(await _chat(agent, "what is this?", [PNG, PNG2]))
        run = await _record(run_id)
        assert run.step_progress["__run__"]["images"] == [f"runs/{run_id}/images/0", f"runs/{run_id}/images/1"]
        assert "base64" not in str(run.step_progress) and "base64" not in str(run.shared_state)

    async def test_a_completed_run_lets_them_go(self, fake_llm):
        from core.orchestration.engine import OrchestrationEngine
        from core.storage import get_blob_store

        agent = await _workflow_agent("orch_f395c", [_human("ask", nxt="look"), _look()], "ask")
        run_id = _asked(await _chat(agent, "what is this?", [PNG]))
        assert get_blob_store().list(f"runs/{run_id}/images/")
        [e async for e in OrchestrationEngine.resume(run_id, {"approval": "yes"}, _server())]
        assert (await _record(run_id)).status == "completed"
        assert get_blob_store().list(f"runs/{run_id}/images/") == []

    async def test_a_run_with_no_images_keeps_nothing(self, fake_llm):
        agent = await _workflow_agent("orch_f395e", [_human("ask", nxt="look"), _look()], "ask")
        run_id = _asked(await _chat(agent, "hello", []))
        assert "images" not in ((await _record(run_id)).step_progress.get("__run__") or {})

    async def test_deleting_its_log_lets_them_go(self, client, fake_llm):
        from core.storage import get_blob_store

        agent = await _workflow_agent("orch_f395d", [_human("ask", nxt="look"), _look()], "ask")
        run_id = _asked(await _chat(agent, "what is this?", [PNG]))
        await client.delete(f"/api/orchestrations/logs/{run_id}")
        assert get_blob_store().list(f"runs/{run_id}/images/") == []

    async def test_a_reference_to_another_runs_image_is_not_read(self):
        from core.orchestration import run_images
        from core.storage import get_blob_store

        get_blob_store().put("runs/run_other/images/0", PNG)
        assert await run_images.load("run_mine", ["runs/run_other/images/0"]) == []


class TestAReplay:
    async def test_a_failed_run_replayed_still_has_them(self, fake_llm):
        """A Replay pops the run's own entry (its budget starts again, F258); its images stay."""
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine

        orch = S.make_orchestration(id="orch_f395f", entry_step_id="look", steps=[_look()])
        await S.seed_orchestrations([orch])
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        engine.images = [PNG]
        [e async for e in engine.run("go", run_id="run_f395f")]   # agent_eyes is not there yet
        assert (await _record("run_f395f")).status == "failed"

        await S.seed_agents([_eyes()])
        [e async for e in OrchestrationEngine.resume_failed("run_f395f", _server())]
        seen = _images_for(fake_llm, "Describe the picture.")
        assert seen and all(s == [PNG] for s in seen), seen


class TestAHostsOwnReferences:
    @pytest.fixture
    def host(self):
        from core.orchestration import run_images

        class Host:
            stored = {"att/1": PNG, "att/2": PNG2}
            forgotten: list = []

            async def keep(self, run_id, images, refs):
                return list(refs or [])

            async def load(self, run_id, refs):
                return [self.stored[r] for r in refs if r in self.stored]

            async def forget(self, run_id):
                self.forgotten.append(run_id)

        impl = Host()
        run_images.set_run_images(impl)
        yield impl
        run_images.set_run_images(None)

    async def test_the_run_keeps_the_hosts_names_and_reads_them_back(self, host, fake_llm):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        from core.storage import get_blob_store

        orch = S.make_orchestration(id="orch_f395h", entry_step_id="ask", steps=[_human("ask", nxt="look"), _look()])
        await S.seed_orchestrations([orch])
        await S.seed_agents([_eyes()])
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        engine.images, engine.image_refs = [PNG, PNG2], ["att/1", "att/2"]
        [e async for e in engine.run("go", run_id="run_f395h")]
        assert (await _record("run_f395h")).step_progress["__run__"]["images"] == ["att/1", "att/2"]
        assert get_blob_store().list("runs/run_f395h/images/") == [], "the host stores them, not the blob store"

        [e async for e in OrchestrationEngine.resume("run_f395h", {"approval": "yes"}, _server())]
        seen = _images_for(fake_llm, "Describe the picture.")
        assert seen and all(s == [PNG, PNG2] for s in seen), seen

    async def test_chat_hands_a_workflow_agent_the_hosts_names(self, host, fake_llm):
        """The fleet's chat job sets them on its request (`run_agent_chat_job`'s `image_refs`)."""
        from core.models import ChatRequest
        from core.react_engine import run_react_loop

        agent = await _workflow_agent("orch_f395j", [_human("ask", nxt="look"), _look()], "ask")
        request = ChatRequest(message="what is this?", session_id="sess_f395j", agent_id=agent["id"], images=[PNG])
        request._image_refs = ["att/1"]
        run_id = _asked([e async for e in run_react_loop(request, _server())])
        assert (await _record(run_id)).step_progress["__run__"]["images"] == ["att/1"]

    async def test_the_worker_passes_them_from_the_chat_job_never_a_request(self):
        from core.models import ChatRequest

        request = ChatRequest.model_validate({"message": "hi", "_image_refs": ["runs/x/images/0"]})
        assert request._image_refs is None, "no request body can name a stored image"


class TestOnAFleet:
    """`WorkerEngineAdapter` — the path every fleet run and resume takes."""

    async def _adapter(self, orch, run_id, fake_redis):
        from core.models_orchestration import Orchestration
        from core.scale.pubsub import RunEventPublisher
        from core.scale.worker_engine_adapter import WorkerEngineAdapter
        from core.store import get_store

        return WorkerEngineAdapter(
            orch=Orchestration.model_validate(orch), run_id=run_id, worker_server_module=_server(),
            publisher=RunEventPublisher(fake_redis, run_id), session_factory=await get_store(),
            redis_client=fake_redis, worker_id="w-test", job_id="job-test")

    async def test_a_resume_on_another_worker_gives_them_back(self, fake_redis, fake_llm):
        orch = S.make_orchestration(id="orch_f395w", entry_step_id="ask", steps=[_human("ask", nxt="look"), _look()])
        await S.seed_orchestrations([orch])
        await S.seed_agents([_eyes()])
        first = await self._adapter(orch, "run_f395w", fake_redis)
        assert await first.run(initial_input="go", images=[PNG]) == "paused"

        second = await self._adapter(orch, "run_f395w", fake_redis)
        assert await second.resume({"approval": "yes"}) == "completed"
        seen = _images_for(fake_llm, "Describe the picture.")
        assert seen and all(s == [PNG] for s in seen), seen

    async def test_the_hosts_names_are_what_the_run_keeps(self, fake_redis, fake_llm):
        orch = S.make_orchestration(id="orch_f395k", entry_step_id="ask", steps=[_human("ask", nxt="look"), _look()])
        await S.seed_orchestrations([orch])
        adapter = await self._adapter(orch, "run_f395k", fake_redis)

        from core.orchestration import run_images

        class Host:
            async def keep(self, run_id, images, refs):
                return list(refs or [])

            async def load(self, run_id, refs):
                return []

            async def forget(self, run_id):
                pass

        run_images.set_run_images(Host())
        try:
            assert await adapter.run(initial_input="go", images=[PNG], image_refs=["orgs/o/att/1"]) == "paused"
        finally:
            run_images.set_run_images(None)
        assert (await _record("run_f395k")).step_progress["__run__"]["images"] == ["orgs/o/att/1"]
