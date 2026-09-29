"""`/api/status` says what kind each agent is, and which workflow a workflow agent runs (U64).

Chat's header showed "Model: <the workspace default>" for a workflow
(orchestrator) agent: such an agent has no model of its own — each step runs
on the model set on it — so the status fell back to the default, and the page
had nothing to tell it apart by. The header now reads "Workflow · N steps",
from the agent's type and its orchestration.
"""
from tests._fakes import seed as _seed

PLAIN = "agent_u64_plain"
FLOW = "orch_agent_orch_u64"


async def test_each_agent_carries_its_type_and_its_workflow(client):
    await _seed.seed_agents([
        _seed.make_agent(id=PLAIN, name="Plain", skip_default_tools=True),
        _seed.make_agent(id=FLOW, name="Flow", type="orchestrator", orchestration_id="orch_u64",
                         skip_default_tools=True),
    ])

    resp = await client.get("/api/status")

    assert resp.status_code == 200
    agents = resp.json()["agents"]
    assert agents[PLAIN]["type"] == "conversational"
    assert agents[PLAIN]["orchestration_id"] is None
    assert agents[FLOW]["type"] == "orchestrator"
    assert agents[FLOW]["orchestration_id"] == "orch_u64"
