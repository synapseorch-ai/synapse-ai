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
BEFORE = {"create": "step_plan_room", "update": "step_fetch_existing", "manage": "step_manage_plan"}


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
    routes = engine.step_map["step_intent"].route_map
    for label in BEFORE:
        assert routes[label] == "step_may_change", label
    for label in ("answer", "howto", "connect", "cantdo", "research"):
        assert routes[label] != "step_may_change", label


@pytest.mark.parametrize("label", list(BEFORE))
async def test_a_person_who_may_goes_where_the_route_went_before(engine, label):
    assert await _next(engine, "step_may_change", {"intent": label, "may_change": True}) == "step_change_route"
    assert await _next(engine, "step_change_route", {"intent": label, "may_change": True}) == BEFORE[label]


@pytest.mark.parametrize("label", list(BEFORE))
async def test_a_person_who_may_not_is_told_why_and_nothing_is_planned(engine, label):
    assert await _next(engine, "step_may_change", {"intent": label, "may_change": False}) == "step_not_yours"
    refusal = engine.step_map["step_not_yours"]
    assert refusal.type.value == "print" and refusal.output_key == "summary"
    assert "{state.asker_role}" in refusal.print_content and "builder" in refusal.print_content
    assert await _next(engine, "step_not_yours", {}) == "step_end"


async def test_a_host_that_says_nothing_is_let_through_as_before(engine):
    assert await _next(engine, "step_may_change", {"intent": "create"}) == "step_change_route"


def test_the_self_hosted_entry_says_its_one_person_may():
    import inspect

    from core.routes import builder
    source = inspect.getsource(builder.prepare_builder_run)
    assert '"may_change": True' in source and '"asker_role": "owner"' in source
