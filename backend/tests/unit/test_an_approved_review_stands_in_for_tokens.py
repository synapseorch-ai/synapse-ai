"""A person's approved review stands in for per-call confirm tokens, and covers only what they saw (F778; SAI2-P1,
DESIGN.md "Coordination"; SAI2's binding, 10-21; MCP2 reviews).

Synapse AI's `step_apply` / `step_save` (P2/P3) act through `registry.invoke` like MCP does, as the person whose
conversation it is. The assistant's acts need a token per call (`every_act`: a card the person presses); a change
or a build has already been approved, as a whole, at its review step. `confirm_policy="approved_review"` lets that
approval stand in — and only for what it covered:

* the Plan / Change is hashed at the yes, with the approver; at the call it is hashed again from state, so an
  object changed since (by anything) is not covered;
* only the calls the approved object makes, each by tool and exact arguments;
* only for the person who approved it;
* the role is still checked live at every call, and scope and audit are as for any call.

Anything not covered falls back to `every_act`: a preview and a token, nothing done. The record lives in the run's
checkpointed state, so a resume on another worker finds it.
"""
import pytest

from core.product_tools import approved, hooks, registry
from core.product_tools.caller import Caller
from core.product_tools.roles import Role

CHANGE = {"ops": [{"op": "set_schedule", "target": {"kind": "schedule", "id": "sch_1"}, "fields": {"cron": "0 9 * * 1"}}]}
CALL = ("t_set_schedule", {"schedule_id": "sch_1", "cron": "0 9 * * 1"})


def _ana(role=Role.BUILDER):
    return Caller(role=role, via="assistant", user_id="ana")


@pytest.fixture
def tool():
    done: list[dict] = []

    async def handler(caller, args):
        done.append(args)
        return {"status": "updated"}

    registry.register(registry.ProductTool(
        name="t_set_schedule", description="Move a schedule.", input_schema={"type": "object"}, handler=handler,
        kind="act", area="schedules", min_role=Role.BUILDER, scope="build", irreversible=True), replace=True)
    audits: list[hooks.AuditEvent] = []

    async def record(event):
        audits.append(event)

    hooks.set_audit(record)
    yield done, audits
    hooks.set_audit(None)
    registry.unregister("t_set_schedule")


def _review(caller=None, obj=CHANGE, calls=(CALL,)):
    return approved.approve(obj, caller or _ana(), list(calls), run_id="builder_1")


async def _call(review, *, caller=None, current=CHANGE, args=None):
    return await registry.invoke(CALL[0], caller or _ana(), dict(args or CALL[1]), confirm_policy="approved_review",
                                 review=review, reviewed=current)


class TestCovered:
    async def test_the_approved_call_is_done_without_a_token_and_audited_once(self, tool):
        done, audits = tool
        assert await _call(_review()) == {"status": "updated"}
        assert done == [CALL[1]] and len(audits) == 1

    async def test_the_assistants_own_policy_still_asks_for_a_token(self, tool):
        out = await registry.invoke(CALL[0], _ana(), dict(CALL[1]), confirm_policy="every_act")
        assert out["status"] == "needs_confirmation"


class TestNotCoveredFallsBackToAToken:
    async def _refused(self, out, tool):
        done, audits = tool
        assert out["status"] == "needs_confirmation" and "confirm_token" in out
        assert done == [] and audits == []

    async def test_no_review(self, tool):
        await self._refused(await _call(None), tool)

    async def test_the_object_changed_since_the_yes(self, tool):
        changed = {"ops": [{**CHANGE["ops"][0], "fields": {"cron": "0 7 * * 1"}}]}
        await self._refused(await _call(_review(), current=changed), tool)

    async def test_a_call_it_did_not_approve(self, tool):
        await self._refused(await _call(_review(), args={"schedule_id": "sch_1", "cron": "0 7 * * 1"}), tool)

    async def test_someone_else(self, tool):
        await self._refused(await _call(_review(), caller=Caller(role=Role.BUILDER, via="assistant",
                                                                 user_id="ben")), tool)


class TestTheRoleIsStillLive:
    async def test_a_person_demoted_since_the_yes_is_refused(self, tool):
        with pytest.raises(registry.ToolRefusal, match="builder"):
            await _call(_review(), caller=_ana(Role.VIEWER))
        assert tool[0] == []


class TestItSurvivesAMove:
    async def test_the_record_round_trips_as_json(self):
        from core.native_builder import review as review_mod

        state: dict = {}
        review_mod.record_approval(state, CHANGE, _ana(), [CALL], run_id="builder_1")
        import json

        moved = json.loads(json.dumps(state))   # what the store keeps
        assert review_mod.approved_review(moved) == _review()
        assert review_mod.approved_review({}) is None

    async def test_through_the_runs_checkpoint(self, tool):
        from core.models_orchestration import OrchestrationRun
        from core.native_builder import review as review_mod
        from core.orchestration.state import SharedState
        from core.store import get_store

        await get_store()
        run = OrchestrationRun(run_id="builder_f778", orchestration_id="orch_native_builder", status="running")
        review_mod.record_approval(run.shared_state, CHANGE, _ana(), [CALL], run_id=run.run_id)
        run.shared_state["change"] = CHANGE
        await SharedState(run).checkpoint()
        restored = (await SharedState.restore(run.run_id)).run
        assert await _call(review_mod.approved_review(restored.shared_state),
                           current=restored.shared_state["change"]) == {"status": "updated"}


class TestNothingWritesThePlanBetweenTheYesAndTheApply:
    def test_a_step_that_could_is_named(self):
        from core.native_builder.review import writes_between

        orch = {"steps": [
            {"id": "review", "type": "human", "output_key": "answer", "next_step_id": "gate"},
            {"id": "gate", "type": "if_else", "if_true_step_id": "redraft", "if_false_step_id": "apply"},
            {"id": "redraft", "type": "agent", "output_key": "change", "next_step_id": "apply"},
            {"id": "apply", "type": "tool", "output_key": "applied", "next_step_id": None},
        ]}
        assert writes_between(orch, "review", "apply", {"change", "plan"}) == ["redraft"]
        orch["steps"][2]["output_key"] = "notes"
        assert writes_between(orch, "review", "apply", {"change", "plan"}) == []

    def test_a_redraft_that_goes_back_through_the_review_is_not_between(self):
        """The change route's "change something" loop re-checks the change, then shows it again: a new yes comes
        before any apply (SAI2-P2)."""
        from core.native_builder.review import writes_between

        orch = {"steps": [
            {"id": "review", "type": "human", "output_key": "answer", "next_step_id": "gate"},
            {"id": "gate", "type": "if_else", "if_true_step_id": "approve", "if_false_step_id": "redraft"},
            {"id": "redraft", "type": "tool", "output_key": "change", "next_step_id": "review"},
            {"id": "approve", "type": "tool", "output_key": "approval", "next_step_id": "apply"},
            {"id": "apply", "type": "tool", "output_key": "applied", "next_step_id": None},
        ]}
        assert writes_between(orch, "review", "apply", {"change"}) == []
        orch["steps"][3]["output_key"] = "change"
        assert writes_between(orch, "review", "apply", {"change"}) == ["approve"]

    def test_the_shipped_change_route_writes_nothing_it_approved_after_the_review(self):
        """MCP2's point 5, on Synapse AI as shipped: from the change review to the apply, nothing writes the checked
        change, nor the approval but the approval step."""
        from core.native_builder.review import writes_between
        from core.native_builder.seed import bundled_definitions

        orch = bundled_definitions()["orchestration"]
        assert writes_between(orch, "step_manage_review", "step_apply", {"change_checked"}) == []
        assert writes_between(orch, "step_manage_review", "step_apply", {"change_approval"}) == [
            "step_change_approve"]
