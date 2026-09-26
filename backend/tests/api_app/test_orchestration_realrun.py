"""
Real orchestration runs through the HTTP routes (no engine patching): the
internal SSE run endpoint and the v1 sync endpoint execute an actual print-step
orchestration end to end, covering the route handlers + engine + step executors.
"""
import pytest

from _fakes import seed as S
from _fakes.fake_redis_stream import data_events


def _sse_json(text):
    return data_events(text.split("\n\n"))


class TestRealAppRun:
    async def test_internal_run_streams_real_engine(self, client, seed_orchestration):
        orch = await seed_orchestration()  # single print step
        resp = await client.post(f"/api/orchestrations/{orch['id']}/run", json={"message": "go"})
        assert resp.status_code == 200
        types_seen = [e.get("type") for e in _sse_json(resp.text)]
        assert "orchestration_start" in types_seen
        assert "orchestration_complete" in types_seen
        assert types_seen[-1] == "done"


class TestRealChatStreamRun:
    async def test_chat_stream_orchestrator_real_engine(self, client, seed_orchestration):
        """Regression for #356: launching an orchestration from the home command
        bar (POST /chat/stream against an orchestrator agent) used to crash with
        "Attempted to exit cancel scope in a different task" once the step's
        timeout scope was exited, while the editor's run endpoint worked.

        The print step yields a `final` event from inside that scope, which is
        what suspends the engine mid-scope — no LLM needed to reproduce.
        """
        orch = await seed_orchestration()  # single print step
        agent = S.make_orchestrator_agent(orch["id"])
        await S.seed_agents([agent])

        resp = await client.post("/chat/stream",
                                 json={"message": "go", "agent_id": agent["id"]})
        assert resp.status_code == 200
        events = _sse_json(resp.text)
        types_seen = [e.get("type") for e in events]

        assert "orchestration_start" in types_seen
        assert "step_complete" in types_seen
        assert "orchestration_complete" in types_seen
        # The crash surfaced as a step_error / orchestration_error, never as a
        # transport failure — assert on the payload, not just the status code.
        assert "error" not in types_seen
        assert "orchestration_error" not in types_seen
        assert "step_error" not in types_seen


class TestOrchestratorAgentFailure:
    """A failing orchestrator agent reports `orchestration_error`, not `error`.
    Every chat surface has to recognise it or the failure is swallowed."""

    @staticmethod
    async def _seed_broken(seed_orchestration):
        # entry_step_id points at a step that does not exist -> the engine
        # yields orchestration_error on its first loop iteration.
        orch = await seed_orchestration(
            entry_step_id="missing",
            steps=[{"id": "s", "name": "Never", "type": "print",
                    "print_content": "x", "output_key": "out",
                    "next_step_id": None}],
        )
        agent = S.make_orchestrator_agent(orch["id"])
        await S.seed_agents([agent])
        return agent

    async def test_chat_stream_surfaces_the_failure(self, client, seed_orchestration):
        agent = await self._seed_broken(seed_orchestration)
        resp = await client.post("/chat/stream",
                                 json={"message": "go", "agent_id": agent["id"]})
        assert resp.status_code == 200
        errors = [e for e in _sse_json(resp.text) if e.get("type") == "orchestration_error"]
        assert errors and "missing" in errors[0]["error"]

    async def test_sync_chat_surfaces_the_failure(self, client, seed_orchestration):
        agent = await self._seed_broken(seed_orchestration)
        resp = await client.post("/chat", json={"message": "go", "agent_id": agent["id"]})
        assert resp.status_code == 200
        assert "missing" in resp.json()["response"]

    async def test_v1_chat_surfaces_the_failure(self, client, api_key, seed_orchestration):
        agent = await self._seed_broken(seed_orchestration)
        resp = await client.post("/api/v1/chat",
                                 json={"message": "go", "agent": agent["id"]},
                                 headers=api_key["headers"])
        assert resp.status_code == 500

    async def test_v1_chat_stream_surfaces_the_failure(self, client, api_key, seed_orchestration):
        agent = await self._seed_broken(seed_orchestration)
        resp = await client.post("/api/v1/chat/stream",
                                 json={"message": "go", "agent": agent["id"]},
                                 headers=api_key["headers"])
        assert resp.status_code == 200
        assert "error" in [e.get("type") for e in _sse_json(resp.text)]

    async def test_chat_stream_tells_the_member_why_a_step_failed(
        self, client, monkeypatch, seed_orchestration
    ):
        """The chat page is a member's: its step failure carries the step's
        own words beside the sanitised sentence (F195)."""
        from core.orchestration import steps as steps_mod

        async def boom(self, step, run, engine):
            raise KeyError("missing_config_key")
            yield  # pragma: no cover — makes this an async generator

        monkeypatch.setattr(steps_mod.PrintStepExecutor, "execute", boom)
        orch = await seed_orchestration()  # single print step
        agent = S.make_orchestrator_agent(orch["id"])
        await S.seed_agents([agent])

        resp = await client.post("/chat/stream",
                                 json={"message": "go", "agent_id": agent["id"]})
        failed = [e for e in _sse_json(resp.text) if e.get("type") == "step_error"]
        assert failed, [e.get("type") for e in _sse_json(resp.text)]
        assert failed[0]["error"] == "An internal error occurred while executing this step."
        assert failed[0]["reason"] == "KeyError: 'missing_config_key'"


class TestChatLaunchedRunParity:
    async def test_command_bar_run_is_registered_and_cancellable_mid_step(
        self, client, monkeypatch, seed_orchestration
    ):
        """A run launched from the command bar goes through the same runner as
        the editor's, so it is registered for cancellation and can be
        interrupted mid-step rather than only at the next step boundary."""
        import asyncio
        import core.llm_providers as llm
        import core.server as server
        from core.models import ChatRequest
        from core.orchestration.runner import _active_tasks
        from core.react_engine import run_react_loop

        in_llm_call = asyncio.Event()

        async def _blocking_llm(*_args, **_kwargs):
            in_llm_call.set()
            await asyncio.sleep(30)
            return "never reached"  # pragma: no cover

        monkeypatch.setattr(llm, "generate_response", _blocking_llm, raising=False)

        orch = await seed_orchestration(
            entry_step_id="s",
            steps=[{"id": "s", "name": "Think", "type": "llm",
                    "prompt_template": "hi", "output_key": "out",
                    "next_step_id": None}],
        )
        agent = S.make_orchestrator_agent(orch["id"])
        await S.seed_agents([agent])

        stream = run_react_loop(
            ChatRequest(message="go", agent_id=agent["id"]), server
        )
        try:
            run_id = None
            async for event in stream:
                if event.get("type") == "orchestration_start":
                    run_id = event["run_id"]
                if event.get("type") == "step_start":
                    break
            assert run_id, "orchestration_start never arrived"

            await asyncio.wait_for(in_llm_call.wait(), timeout=2.0)
            assert run_id in _active_tasks, "chat-launched run was not registered"

            resp = await client.post(f"/api/orchestrations/runs/{run_id}/cancel")
            assert resp.status_code == 200

            # The blocking LLM call is interrupted, not left to run to term.
            tail = [ev async for ev in stream]
            assert {"type": "orchestration_error", "error": "Cancelled"} in tail
        finally:
            await stream.aclose()


class TestRealV1Run:
    async def test_v1_sync_run_real_engine(self, client, api_key, seed_orchestration):
        orch = await seed_orchestration(
            entry_step_id="p",
            steps=[{"id": "p", "name": "Say", "type": "print",
                    "print_content": "hello {state.user_input}",
                    "output_key": "out", "next_step_id": None}],
        )
        resp = await client.post(f"/api/v1/orchestrations/{orch['id']}/run",
                                 json={"message": "world"}, headers=api_key["headers"])
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] in ("completed", "running")

    async def test_v1_run_stream_real_engine(self, client, api_key, seed_orchestration):
        orch = await seed_orchestration()
        resp = await client.post(f"/api/v1/orchestrations/{orch['id']}/run/stream",
                                 json={"message": "go"}, headers=api_key["headers"])
        assert resp.status_code == 200
        types_seen = [e.get("type") for e in _sse_json(resp.text)]
        assert "orchestration_complete" in types_seen
