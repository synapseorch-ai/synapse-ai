"""A build's approval covers its save calls, ids it makes included (F940, SAI2-P3, D300; Naveen 10-23, Q1; MCP2
reviews, as F778's own).

F778 covers each call by tool and exact arguments, which a change knows at its review. A build does not: the workflow
names the agents it makes and its inner workflows, and the schedule names the workflow, by ids that only exist once
each is made. So the build is approved with code-minted placeholders for those ids (`@made:agent:1`), and its save
hands `registry.invoke` the ids it has made so far, which map back to their placeholders before the coverage check.
The map is host code's (`made=`), never a model's argument, and maps only what this pass made: an id that maps to no
placeholder is just an id, and a call naming another one is not covered.
"""
import pytest

from core.product_tools import approved, confirm, registry
from core.product_tools.caller import Caller
from core.product_tools.roles import Role

PLAN = {"plan": {"title": "Wind warning"}}
STEPS = '[{"id": "tell", "type": "agent", "agent_id": "@made:agent:1"}]'
CALL = ("t_create_workflow", {"name": "Wind warning", "steps_json": STEPS})


def _ana():
    return Caller(role=Role.BUILDER, via="assistant", user_id="ana")


@pytest.fixture(autouse=True)
def tool():
    done: list[dict] = []
    confirm.set_claim_store(None)

    async def handler(caller, args):
        done.append(args)
        return {"status": "created"}

    registry.register(registry.ProductTool(
        name="t_create_workflow", description="Make a workflow.", input_schema={"type": "object"}, handler=handler,
        kind="act", area="build", min_role=Role.BUILDER, scope="build"), replace=True)
    yield done
    registry.unregister("t_create_workflow")


async def _invoke(args, made=None):
    review = approved.approve(PLAN, _ana(), [CALL], run_id="builder_1")
    return await registry.invoke(CALL[0], _ana(), args, confirm_policy="approved_review", review=review,
                                 reviewed=PLAN, made=made)


async def test_the_id_the_save_made_maps_back_to_the_placeholder_it_was_approved_with(tool):
    made = {"agent_q8d1kz0x4w": "@made:agent:1"}
    out = await _invoke({**CALL[1], "steps_json": STEPS.replace("@made:agent:1", "agent_q8d1kz0x4w")}, made)
    assert out == {"status": "created"} and tool[0]["steps_json"].count("agent_q8d1kz0x4w") == 1


async def test_without_the_map_the_real_id_is_not_what_was_approved(tool):
    out = await _invoke({**CALL[1], "steps_json": STEPS.replace("@made:agent:1", "agent_q8d1kz0x4w")})
    assert out["status"] == "needs_confirmation" and tool == []


async def test_an_id_this_pass_did_not_make_is_not_covered(tool):
    made = {"agent_q8d1kz0x4w": "@made:agent:1"}
    out = await _invoke({**CALL[1], "steps_json": STEPS.replace("@made:agent:1", "agent_someone_elses")}, made)
    assert out["status"] == "needs_confirmation" and tool == []


async def test_an_id_maps_back_only_as_a_whole_id(tool):
    made = {"agent_q8": "@made:agent:1"}
    out = await _invoke({**CALL[1], "steps_json": STEPS.replace("@made:agent:1", "agent_q8d1kz0x4w")}, made)
    assert out["status"] == "needs_confirmation"


def test_the_map_is_a_host_argument_and_never_a_model_s():
    import inspect

    assert "made" in inspect.signature(registry.invoke).parameters
    assert inspect.signature(registry.invoke).parameters["made"].kind is inspect.Parameter.KEYWORD_ONLY


async def test_a_call_still_holding_a_placeholder_is_never_covered(tool):
    """MCP2's finding 2: a placeholder the save did not fill (an ordering slip) hashes as approved, and would save a
    workflow naming an agent "@made:agent:1". A call is never made with one."""
    out = await _invoke(dict(CALL[1]))
    assert out["status"] == "needs_confirmation" and tool == []
    assert "placeholder" in approved.uncovered(approved.approve(PLAN, _ana(), [CALL]), _ana(), CALL[0],
                                               dict(CALL[1]), PLAN)
