"""
One Synapse AI chat writes a workflow at a time (TABS2, D327; F1063).

Several Synapse AI chats (F1061) can set out to change one workflow. Each route checked only that the workflow had not
moved since it read it, and the builder's writes take no lock, so two chats approved close together both wrote, the
later over the earlier. Naveen, 2026-10-27: never two silent writers. The second chat is told the workflow is open in
another Synapse AI chat — with a way to go to it when the chat is the person's own, and nothing of it when it is
someone else's — and offers nothing to approve; a race at the write is refused in the same words.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import one_writer as W
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks
from core.product_tools.roles import Role


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
def fresh():
    confirm.set_claim_store(None)
    builder_access.forget_runs()
    held = {"role": Role.OWNER}

    async def source(user_id):
        return held["role"]

    hooks.set_role_source(source)
    yield
    hooks.set_role_source(None)
    builder_access.forget_runs()


def _chat(cid: str) -> str:
    from core.tenancy import get_tenant
    return f"builder:{get_tenant()}:panel:{cid}"


def _ready_rename(wid: str, name: str = "Morning digest") -> dict:
    return {"outcome": "review", "ops": [{"op": "rename_workflow", "kind": "write", "status": "ready",
                                          "target": {"kind": "workflow", "id": wid, "name": "Digest"},
                                          "fields": {"name": name}}]}


async def _turn(run_id: str, cid: str, *, user: str | None = "u1", status: str = "paused", waiting: bool = True,
                asked: str = "rename the digest", **state) -> OrchestrationRun:
    """A builder turn of chat `cid`, as its run row holds it."""
    run = OrchestrationRun(run_id=run_id, orchestration_id="orch_native_builder", session_id=_chat(cid),
                           status=status, waiting_for_human=waiting and status == "paused",
                           shared_state={builder_access.CALLER_KEY: {"user_id": user}, "user_message": asked,
                                         **state})
    await SharedState(run).checkpoint()
    return run


async def _aged(run_id: str, hours: float) -> None:
    from sqlalchemy import update

    from core.store import session
    from core.store.models import OrchestrationRunDB

    async with session() as s:
        await s.execute(update(OrchestrationRunDB).where(OrchestrationRunDB.run_id == run_id).values(
            created_at=datetime.now(timezone.utc) - timedelta(hours=hours)))


class TestWhatATurnWillWrite:
    def test_a_ready_change_to_a_workflow(self):
        assert W.targets({"change_checked": json.dumps(_ready_rename("orch_a"))}) == {"orch_a"}

    def test_not_a_run_a_create_or_an_op_not_ready(self):
        ops = [{"op": "run_workflow", "status": "card", "target": {"kind": "workflow", "id": "orch_a"}},
               {"op": "rename_workflow", "status": "refused", "target": {"kind": "workflow", "id": "orch_b"}},
               {"op": "rename_agent", "status": "ready", "target": {"kind": "agent", "id": "agent_c"}}]
        assert W.targets({"change_checked": {"ops": ops}}) == set()

    def test_a_build_updating_a_workflow(self):
        state = {"plan_checked": {"outcome": "review", "plan": {"kind": "update"}}, "existing_orch": {"id": "orch_u"}}
        assert W.targets(state) == {"orch_u"}

    def test_not_a_build_making_a_new_one(self):
        state = {"plan_checked": {"outcome": "review", "plan": {"kind": "create"}}, "existing_orch": {"id": "orch_u"}}
        assert W.targets(state) == set()


class TestWhoHoldsAWorkflow:
    async def test_a_change_waiting_on_its_yes_in_another_chat(self, client):
        await _turn("run_a", "chat-a", change_checked=_ready_rename("orch_x"))
        holder = await W.held_by("orch_x", _chat("chat-b"))
        assert holder is not None and holder.session_id == _chat("chat-a")
        assert holder.title == "rename the digest" and holder.user_id == "u1"

    @pytest.mark.parametrize("orchestration_id, holds", [
        ("orch_0192f3a1c4d07b2e9a6f5d8c3b1e0a47", True),
        # An ordinary run named into a chat's session, with a ready op in its state, holds nothing: it is no turn.
        ("orch_digest", False),
    ])
    async def test_a_hosts_builder_turn_holds(self, client, orchestration_id, holds):
        """F1126: cloud's builder turns carry its per-workspace builder id, so a read keyed on `orch_native_builder`
        found no hold there and two chats both wrote."""
        from core.native_builder import identity

        run = await _turn("run_a", "chat-a", change_checked=_ready_rename("orch_x"))
        run.orchestration_id = orchestration_id
        await SharedState(run).checkpoint()

        async def ids():
            return ["orch_0192f3a1c4d07b2e9a6f5d8c3b1e0a47"]

        identity.set_builder_ids(ids)
        try:
            holder = await W.held_by("orch_x", _chat("chat-b"))
        finally:
            identity.set_builder_ids(None)
        assert (holder is not None) is holds

    async def test_never_the_asking_chat_itself(self, client):
        await _turn("run_a", "chat-a", change_checked=_ready_rename("orch_x"))
        assert await W.held_by("orch_x", _chat("chat-a")) is None

    async def test_another_workflow_is_free(self, client):
        await _turn("run_a", "chat-a", change_checked=_ready_rename("orch_x"))
        assert await W.held_by("orch_y", _chat("chat-b")) is None

    async def test_a_turn_still_working_holds_once_it_has_a_target(self, client):
        await _turn("run_a", "chat-a", status="running", change_checked=_ready_rename("orch_x"))
        assert await W.held_by("orch_x", _chat("chat-b")) is not None

    async def test_a_new_turn_still_classifying_holds_nothing(self, client):
        # Two new chats must never refuse each other.
        await _turn("run_a", "chat-a", status="running")
        assert await W.held_by("orch_x", _chat("chat-b")) is None

    @pytest.mark.parametrize("status", ["completed", "cancelled", "failed"])
    async def test_an_ended_turn_holds_nothing(self, client, status):
        # Applied, stopped, set aside (the panel cancels a plan it sets aside) or expired after a day.
        await _turn("run_a", "chat-a", status=status, waiting=False, change_checked=_ready_rename("orch_x"))
        assert await W.held_by("orch_x", _chat("chat-b")) is None

    async def test_a_working_row_a_restart_left_holds_nothing(self, client):
        await _turn("run_a", "chat-a", status="running", change_checked=_ready_rename("orch_x"))
        await _aged("run_a", hours=3)
        assert await W.held_by("orch_x", _chat("chat-b")) is None

    async def test_at_the_write_only_an_earlier_turn_holds(self, client):
        await _turn("run_a", "chat-a", change_checked=_ready_rename("orch_x"))
        await _aged("run_a", hours=0.5)
        await _turn("run_b", "chat-b", change_checked=_ready_rename("orch_x"))
        # Both raced past their cards: the earlier goes first, and the two never refuse each other.
        assert await W.held_by("orch_x", _chat("chat-a"), run_id="run_a") is None
        held = await W.held_by("orch_x", _chat("chat-b"), run_id="run_b")
        assert held is not None and held.run_id == "run_a"


class TestWhatThePersonIsTold:
    def test_their_own_chat_is_named_with_a_way_to_it(self):
        holder = W.Holder(session_id=_chat("chat-a"), run_id="run_a", user_id="u1", title="rename the digest")
        said = W.refusal(holder, "u1")
        assert said["say"].startswith('This workflow is open in another Synapse AI chat ("rename the digest")')
        assert said["open_chat"] == {"chat_id": "chat-a", "title": "rename the digest"}
        assert W.open_chat_event(said) == {"type": "open_chat", "chat_id": "chat-a", "title": "rename the digest"}

    def test_someone_elses_chat_is_never_named(self):
        holder = W.Holder(session_id=_chat("their-chat"), run_id="run_a", user_id="u2", title="their private words")
        said = W.refusal(holder, "u1")
        assert "another person's Synapse AI chat" in said["say"]
        assert "their private words" not in said["say"] and "their-chat" not in said["say"]
        assert said["open_chat"] is None and W.open_chat_event(said) is None


class TestTheWayToTheChatReachesThePanel:
    def test_open_chat_is_a_public_event(self):
        # Cloud's fleet passes only public events (`core.scale.pubsub`): an unknown type never reaches the panel.
        from core.events_public import public_event

        event = W.open_chat_event({"open_chat": {"chat_id": "chat-a", "title": "rename the digest"}})
        assert public_event(event) == event


class TestTheWriteIsOneAtATime:
    async def test_two_writers_of_one_workflow_take_turns(self, client):
        inside, most = 0, 0

        async def write():
            nonlocal inside, most
            async with W.writing("orch_x", run_id="none", session_id=None) as held:
                assert held is None
                inside += 1
                most = max(most, inside)
                await asyncio.sleep(0.05)
                inside -= 1

        await W.held_by("orch_warm", None)  # the store's tables are made on first use, once
        await asyncio.gather(write(), write(), write())
        assert most == 1

    async def test_two_workflows_are_written_side_by_side(self, client):
        inside, most = 0, 0

        async def write(wid):
            nonlocal inside, most
            async with W.writing(wid, run_id="none", session_id=None):
                inside += 1
                most = max(most, inside)
                await asyncio.sleep(0.05)
                inside -= 1

        await W.held_by("orch_warm", None)  # the store's tables are made on first use, once
        await asyncio.gather(write("orch_x"), write("orch_y"))
        assert most == 2


# ── the change route (SAI2's check_change / apply_change) ──────────────────────────────────────────────────────────

async def _call(tool, args, run):
    with live(run):
        raw, events = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return json.loads(raw), events


async def _workflow(name="Digest"):
    from core.store.resources import save_orchestration
    from tests._fakes.seed import make_orchestration

    orch = make_orchestration(name=name)
    await save_orchestration(orch)
    return orch


def _rename_op(wid: str, name: str = "Morning digest") -> list[dict]:
    return [{"op": "rename_workflow", "target": {"kind": "workflow", "id": wid}, "fields": {"name": name}}]


class TestTheChangeRouteAsks:
    async def test_a_workflow_open_in_another_chat_is_not_offered(self, client):
        orch = await _workflow()
        await _turn("run_a", "chat-a", asked="move the digest to 8am", change_checked=_ready_rename(orch["id"]))
        run = await _turn("run_b", "chat-b", status="running", asked='rename it to "Morning digest"')

        out, events = await _call("check_change", {"draft": {"finish": "submit_change", "ops": _rename_op(orch["id"])}},
                                  run)

        assert out["outcome"] == "said", out
        assert 'open in another Synapse AI chat ("move the digest to 8am")' in out["say"]
        assert not out.get("calls")
        assert {"type": "open_chat", "chat_id": "chat-a", "title": "move the digest to 8am"} in events

    async def test_only_the_held_op_is_left_out(self, client):
        held, free = await _workflow("Digest"), await _workflow("Report")
        await _turn("run_a", "chat-a", change_checked=_ready_rename(held["id"]))
        run = await _turn("run_b", "chat-b", status="running",
                          asked='rename it to "Morning digest" and rename it to "Weekly report"')
        ops = _rename_op(held["id"]) + _rename_op(free["id"], "Weekly report")

        out, _ = await _call("check_change", {"draft": {"finish": "submit_change", "ops": ops}}, run)

        assert out["outcome"] == "review", out
        assert [o["status"] for o in out["ops"]] == ["refused", "ready"]
        assert "another Synapse AI chat" in out["ops"][0]["say"]

    async def test_steps_handed_to_the_build_route_are_asked_too(self, client):
        orch = await _workflow()
        await _turn("run_a", "chat-a", change_checked=_ready_rename(orch["id"]))
        run = await _turn("run_b", "chat-b", status="running", asked="change the steps of Digest")
        ops = [{"op": "edit_steps", "target": {"kind": "workflow", "id": orch["id"]}, "fields": {}}]

        out, _ = await _call("check_change", {"draft": {"finish": "submit_change", "ops": ops}}, run)

        assert out["outcome"] == "said" and "another Synapse AI chat" in out["say"], out


class TestTheChangeRouteWritesOneAtATime:
    async def test_a_race_past_both_cards_writes_once(self, client):
        from core.store.resources import get_orchestration

        orch = await _workflow()
        run_b = await _turn("run_b", "chat-b", status="running", asked='rename it to "Morning digest"')
        out, _ = await _call("check_change", {"draft": {"finish": "submit_change", "ops": _rename_op(orch["id"])}},
                             run_b)
        assert out["outcome"] == "review", out
        run_b.shared_state["change_checked"] = json.dumps(out)
        # Chat A reached its own review of the same workflow first, and still waits on its yes.
        await _turn("run_a", "chat-a", change_checked=_ready_rename(orch["id"], "Their name"))
        await _aged("run_a", hours=0.5)

        run_b.shared_state["manage_review"] = {"feedback": "approve"}
        await _call("approve_change", {}, run_b)
        applied, _ = await _call("apply_change", {}, run_b)

        assert applied["applied"] == [], applied
        assert "another Synapse AI chat" in " ".join(applied["failed"])
        assert (await get_orchestration(orch["id"]))["name"] == "Digest"
