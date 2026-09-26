"""The AI builder's conversation survives a reload, and a plan left waiting ends (F214).

The panel kept its conversation in React state only. A reload mid-answer lost
the question and the answer while the server finished the turn; a reload after
*approve* lost the summary while the build landed; and a reload while the plan
waited showed the greeting while `orch_native_builder` kept the run paused, with
no screen that lists or resumes it (builder runs are hidden, F162), for ever.

`GET /api/builder/conversation` is cloud's F148 in open source: the newest
builder conversation still open — working, or parked on a question — read back
from its run rows, so the panel can redraw it and answer the question it was
asked. A run parked more than a day is cancelled whenever the builder is next
used (Naveen, 2026-09-26).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from _fakes import seed as S
from core.native_builder import seed_native_builder
from test_builder_routes import stream


@pytest.fixture
async def builder():
    await seed_native_builder()


ASK = ["create", "What should it do?", "clarify"]  # step_intent, analyst, clarify route


async def _park(client, fake_llm, message: str = "build something", cid: str = "conv-back") -> dict:
    """A panel conversation parked on the builder's clarifying question."""
    fake_llm.script(list(ASK))
    events = await stream(client, "/api/builder/chat", {"message": message, "client_session_id": cid})
    return next(e for e in events if e["type"] == "human_input_required")


async def _open(client) -> dict | None:
    response = await client.get("/api/builder/conversation")
    assert response.status_code == 200, response.text
    return response.json()["conversation"]


async def _row(run_id: str):
    from sqlalchemy import select

    from core.store import session
    from core.store.models import OrchestrationRunDB

    async with session() as s:
        return (await s.execute(
            select(OrchestrationRunDB).where(OrchestrationRunDB.run_id == run_id))).scalar_one()


async def _set(run_id: str, **values) -> None:
    from sqlalchemy import update

    from core.store import session
    from core.store.models import OrchestrationRunDB

    async with session() as s:
        await s.execute(
            update(OrchestrationRunDB).where(OrchestrationRunDB.run_id == run_id).values(**values))


async def _paused_for(run_id: str, hours: float) -> None:
    """Move the run's pause `hours` into the past, as if nobody came back."""
    row = await _row(run_id)
    when = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    history = [dict(e) for e in row.step_history]
    for entry in history:
        if entry.get("status") == "paused":
            entry["started_at"] = when
    await _set(run_id, step_history=history,
               created_at=datetime.now(timezone.utc) - timedelta(hours=hours))


class TestAParkedPlanComesBack:
    async def test_the_question_and_what_was_asked(self, client, builder, fake_llm):
        asked = await _park(client, fake_llm)

        conversation = await _open(client)

        assert conversation is not None, "a builder parked on its question is not offered back"
        assert conversation["client_session_id"] == "conv-back"
        assert conversation["run_id"] == asked["run_id"]
        assert conversation["status"] == "paused"
        assert conversation["pending"] == {
            "run_id": asked["run_id"],
            "field": "answer",
            "step_id": asked["orch_step_id"],
            "prompt": asked["prompt"],
        }
        assert conversation["turns"] == [{"user": "build something", "assistant": ""}]

    async def test_answering_it_resumes_the_same_run(self, client, builder, fake_llm):
        asked = await _park(client, fake_llm)
        conversation = await _open(client)
        pending = conversation["pending"]

        fake_llm.reset()
        fake_llm.set_default("create")
        events = await stream(client, "/api/builder/resume", {
            "run_id": pending["run_id"], "response": {pending["field"]: "a weekly report"},
        })
        assert events, "the answer resumed nothing"
        carried = {e["run_id"] for e in events if e.get("run_id")}
        assert carried == {asked["run_id"]}, f"the answer ran as {carried}, not as the run it answered"

    async def test_the_panels_runs_are_marked_as_the_panels(self, client, builder, fake_llm):
        asked = await _park(client, fake_llm)
        from core.tenancy import get_tenant
        row = await _row(asked["run_id"])
        assert row.session_id == f"builder:{get_tenant()}:panel:conv-back"


class TestEarlierTurnsComeBack:
    async def test_a_finished_turn_keeps_what_the_builder_said(self, client, builder, fake_llm):
        first = await _park(client, fake_llm, message="first ask")
        await _set(first["run_id"], status="completed", waiting_for_human=False,
                   shared_state={**(await _row(first["run_id"])).shared_state,
                                 "summary": "Built the first thing."})
        await asyncio.sleep(0.01)  # created_at orders the turns
        await _park(client, fake_llm, message="second ask")

        conversation = await _open(client)

        assert conversation["turns"] == [
            {"user": "first ask", "assistant": "Built the first thing."},
            {"user": "second ask", "assistant": ""},
        ]

    async def test_another_conversations_turns_do_not(self, client, builder, fake_llm):
        await _park(client, fake_llm, message="elsewhere", cid="conv-other")
        await asyncio.sleep(0.01)
        await _park(client, fake_llm, message="here", cid="conv-here")

        conversation = await _open(client)

        assert conversation["client_session_id"] == "conv-here"
        assert [t["user"] for t in conversation["turns"]] == ["here"]


class TestOnlyWhatCanBeCarriedOnIsOffered:
    async def test_nothing_open_is_nothing(self, client, builder):
        assert await _open(client) is None

    async def test_a_finished_conversation_is_not_reopened(self, client, builder, fake_llm):
        asked = await _park(client, fake_llm)
        await _set(asked["run_id"], status="completed", waiting_for_human=False)
        assert await _open(client) is None

    async def test_a_running_row_with_nothing_driving_it_is_not_offered(self, client, builder, fake_llm):
        # A restart leaves a builder run `running` with no pump: following it
        # would wait for ever on a stream nothing writes to.
        asked = await _park(client, fake_llm)
        await _set(asked["run_id"], status="running", waiting_for_human=False)
        assert await _open(client) is None

    async def test_the_chat_pages_builder_agent_is_not_the_panels(self, client, builder, fake_llm):
        asked = await _park(client, fake_llm)
        from core.tenancy import get_tenant
        # What `run_builder_stream_compat` writes for a chat conversation.
        await _set(asked["run_id"], session_id=f"builder:{get_tenant()}:chat-session-1")
        assert await _open(client) is None

    async def test_another_tenants_conversation_is_not_offered(self, client, builder, fake_llm):
        asked = await _park(client, fake_llm)
        await _set(asked["run_id"], tenant_id="someone-else",
                   session_id="builder:someone-else:panel:conv-back")
        assert await _open(client) is None


class TestARunningBuildComesBack:
    @pytest.fixture
    def gated_llm(self, monkeypatch):
        started, release = asyncio.Event(), asyncio.Event()

        async def gate(*args, **kwargs):
            started.set()
            await release.wait()
            return "create"

        import core.llm_providers as _llm
        import core.react_engine as _re
        monkeypatch.setattr(_llm, "generate_response", gate, raising=False)
        monkeypatch.setattr(_re, "llm_generate_response", gate, raising=False)
        return started, release

    async def test_as_running_while_its_pump_drives_it(self, client, builder, gated_llm):
        started, release = gated_llm

        async def consume():
            async with client.stream("POST", "/api/builder/chat", json={
                "message": "build while I reload", "client_session_id": "conv-live",
            }) as response:
                async for _ in response.aiter_lines():
                    pass

        reader = asyncio.create_task(consume())
        try:
            deadline = time.monotonic() + 15
            while not started.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert started.is_set(), "the run never reached a model call"

            conversation = await _open(client)

            assert conversation is not None, "a build still working is not offered back"
            assert conversation["status"] == "running"
            assert conversation["pending"] is None
            assert conversation["client_session_id"] == "conv-live"
            assert conversation["turns"] == [{"user": "build while I reload", "assistant": ""}]
            # Where its journal stood: the panel replays from the start and
            # treats only a question past this as the one being asked now.
            from core.orchestration.journal import open_journal
            assert conversation["cursor"] == str(open_journal(conversation["run_id"]).last_id())
            assert int(conversation["cursor"]) > 0
        finally:
            release.set()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(reader, timeout=30)


class TestAPlanLeftWaitingEnds:
    async def test_a_day_later_the_panel_opening_cancels_it(self, client, builder, fake_llm):
        asked = await _park(client, fake_llm)
        await _paused_for(asked["run_id"], hours=25)

        assert await _open(client) is None
        row = await _row(asked["run_id"])
        assert row.status == "cancelled"
        assert not row.waiting_for_human

    async def test_an_hour_later_it_is_still_there(self, client, builder, fake_llm):
        asked = await _park(client, fake_llm)
        await _paused_for(asked["run_id"], hours=1)

        assert (await _open(client))["run_id"] == asked["run_id"]
        assert (await _row(asked["run_id"])).status == "paused"

    async def test_a_new_message_cancels_it_too(self, client, builder, fake_llm):
        old = await _park(client, fake_llm, cid="conv-old")
        await _paused_for(old["run_id"], hours=25)

        await _park(client, fake_llm, cid="conv-new")

        assert (await _row(old["run_id"])).status == "cancelled"

    async def test_a_workflow_waiting_on_a_person_is_never_touched(
        self, client, builder, fake_llm, seed_orchestration
    ):
        from core.orchestration.engine import OrchestrationEngine
        from core.models_orchestration import Orchestration

        orch = await seed_orchestration(
            id="orch_waits", entry_step_id="w_ask", state_schema={},
            steps=[{"id": "w_ask", "name": "Ask", "type": "human",
                    "human_prompt": "Approve?", "output_key": "ok", "next_step_id": None}],
        )
        engine = OrchestrationEngine(Orchestration.model_validate(orch), None)
        events = [e async for e in engine.run(initial_input="go", run_id="run_waits")]
        assert any(e["type"] == "human_input_required" for e in events)
        await _paused_for("run_waits", hours=48)

        await _open(client)

        assert (await _row("run_waits")).status == "paused", "the builder's sweep reached a workflow's run"
