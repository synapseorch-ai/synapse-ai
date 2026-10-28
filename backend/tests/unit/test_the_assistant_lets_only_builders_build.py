"""Building and changing go on only for a person who may (ASSIST1, D291, F682).

Every role may talk to the assistant now. A viewer or an approver who asks it to build a workflow, change one, or
change a tool, skill, schedule or agent is told so in one sentence, before the builder plans anything: a plan they
could not have carried out would spend their workspace's credits for nothing. The tool chokepoint
(`builder_access`, F681) is what keeps a write from happening; this is the door before it.

In the builder's own definition, in code: the router's create, update and manage routes go to `step_may_change`, an
if-else on the run's `may_change` (written by the host from the person's role); yes goes on to where the route went
before (`step_change_route`, a switch on the route taken), no to `step_not_yours`, which says why and ends. A host
that writes no `may_change` (an older one) is let through, as before.
"""
from __future__ import annotations

import pytest

from core.models_orchestration import Orchestration, OrchestrationRun
from core.native_builder.seed import bundled_definitions
from core.orchestration.engine import OrchestrationEngine

#: Where each route went before the gate.
BEFORE = {"create": "step_plan_room", "update": "step_fetch_existing", "manage": "step_change_context"}


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture
def engine() -> OrchestrationEngine:
    return OrchestrationEngine(Orchestration.model_validate(bundled_definitions()["orchestration"]), _Server())


async def _next(engine, step_id: str, state: dict) -> str | None:
    """Where the step goes from `state`: its executor runs (an if-else or a switch decides there), then the engine
    resolves the next step, as a run does."""
    step = engine.step_map[step_id]
    run = OrchestrationRun(run_id="r", orchestration_id="orch_native_builder", shared_state=dict(state))
    if step.type.value in ("if_else", "switch"):
        async for _ in engine.executors[step.type].execute(step, run, engine):
            pass
    target, _ = engine._resolve_next(step, run)
    return target


def test_the_routes_that_change_things_go_through_the_gate(engine):
    """The front door (SAI2-P4): classify's route for a build or a change, and a change's pick read in code, go
    through the door; an answer, research and the refusal never do. Code checks the role before it too
    (`classify.override`)."""
    routes = engine.step_map["step_classify_route"].switch_cases
    for entry in BEFORE.values():
        assert routes[entry] == "step_may_change", entry
    for entry in ("step_answer", "step_research", "step_not_yours"):
        assert routes[entry] != "step_may_change", entry
    assert engine.step_map["step_classify_route"].switch_default_step_id == "step_answer"
    assert engine.step_map["step_turn_route"].switch_cases["change"] == "step_may_change"


@pytest.mark.parametrize("label", list(BEFORE))
async def test_a_person_who_may_goes_where_the_route_went_before(engine, label):
    assert await _next(engine, "step_may_change", {"intent": label, "may_change": True}) == "step_change_route"
    went = await _next(engine, "step_change_route", {"intent": label, "may_change": True})
    if label != "manage":
        # A build passes its effort check first (F1153), which goes on to the same entry when it offers nothing.
        assert went == "step_effort"
        went = await _next(engine, "step_effort_route", {"effort": {"next": BEFORE[label]}})
    assert went == BEFORE[label]


@pytest.mark.parametrize("label", list(BEFORE))
async def test_a_person_who_may_not_is_told_why_and_nothing_is_planned(engine, label):
    assert await _next(engine, "step_may_change", {"intent": label, "may_change": False, "may_act": False}) == \
        "step_not_yours"
    refusal = engine.step_map["step_not_yours"]
    assert refusal.type.value == "print" and refusal.output_key == "summary"
    assert "{state.asker_role}" in refusal.print_content and "builder" in refusal.print_content
    assert await _next(engine, "step_not_yours", {}) == "step_end"


async def test_an_approver_reaches_the_change_route_and_only_it(engine):
    """Below builder, a change keeps only the ops the role may do (SAI2-P2): an approver may answer what waits for
    them, so `manage` lets them through to the change route, where each op's role is checked; building or editing
    a workflow's steps stays the builder's."""
    approver = {"may_change": False, "may_act": True}
    assert await _next(engine, "step_may_change", {"intent": "manage", **approver}) == "step_change_route"
    assert await _next(engine, "step_change_route", {"intent": "manage", **approver}) == "step_change_context"
    for label in ("create", "update"):
        assert await _next(engine, "step_may_change", {"intent": label, **approver}) == "step_not_yours"


async def test_a_host_that_says_nothing_is_let_through_as_before(engine):
    assert await _next(engine, "step_may_change", {"intent": "create"}) == "step_change_route"


def test_the_self_hosted_entry_says_its_one_person_may():
    import inspect

    from core.routes import builder
    source = inspect.getsource(builder.prepare_builder_run)
    assert '"may_change": True' in source and '"may_act": True' in source and '"asker_role": "owner"' in source


@pytest.mark.parametrize("outcome, intent, then", [("steps", "update", "step_fetch_existing")])
async def test_a_hand_over_goes_back_through_the_door_as_its_own_route(engine, outcome, intent, then):
    """A change the change route hands over (a workflow's steps; something new) goes back through the door as the
    route it is (SAI2-P2's review): an approver who reached the change route is told why, and never runs the build's
    plan and review; a builder goes on to it with `intent` set, which its saver reads."""
    import json

    checked = json.dumps({"object": "change", "outcome": outcome})
    assert await _next(engine, "step_check_route", {"change_checked": checked}) == "step_may_change"
    approver = {"intent": intent, "may_change": False, "may_act": True}
    assert await _next(engine, "step_may_change", approver) == "step_not_yours"
    builder = {"intent": intent, "may_change": True, "may_act": True}
    assert await _next(engine, "step_may_change", builder) == "step_change_route"
    went = await _next(engine, "step_change_route", builder)
    if intent in ("create", "update"):
        assert went == "step_effort"  # a build's effort check first (F1153), then the same entry
        went = await _next(engine, "step_effort_route", {"effort": {"next": then}})
    assert went == then
