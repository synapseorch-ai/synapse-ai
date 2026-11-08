"""Deploying an orchestration as an agent — the one way a workflow agent is made (F264).

An orchestrator agent is only as good as the orchestration it points at. The
Deploy button made it from an orchestration that exists; the AI builder made
one from nothing (`orchestration_id: None`) and wired steps to it, so the first
run failed. The button and the builder's `deploy_orchestration` share this.
"""
from __future__ import annotations


async def deploy_as_agent(orch_id: str) -> dict:
    """The orchestration's workflow agent: made now, or the one already made.

    Returns `{"status": "deployed", "agent_id", "agent"}`,
    `{"status": "already_deployed", "agent_id"}`, or `{"status": "not_found"}`.
    """
    from core.routes.agents import load_user_agents
    from core.store.resources import get_orchestration, save_agent

    orch_data = await get_orchestration(orch_id)
    if not orch_data or orch_data.get("system"):
        # Synapse AI's own orchestration is never an agent of the workspace's (F1315): not found, as everywhere else.
        return {"status": "not_found"}

    existing = next((a for a in await load_user_agents() if a.get("orchestration_id") == orch_id), None)
    if existing:
        return {"status": "already_deployed", "agent_id": existing["id"]}

    # Where the store mints ids (F431), the agent's is minted like any other: a
    # derived one is an id the caller chose. Where it does not, the derived id
    # makes two deploys at once one upsert of the same row.
    from core.store.ids import minter_installed, new_id

    agent_id = new_id("agent") if minter_installed() else f"orch_agent_{orch_id}"
    agent = {
        "id": agent_id,
        "name": orch_data["name"],
        "description": orch_data.get("description", ""),
        "avatar": orch_data.get("avatar", "default"),
        "type": "orchestrator",
        "tools": [],
        "repos": [],
        "system_prompt": f"This is an orchestrator agent for '{orch_data['name']}'. It runs automatically.",
        "orchestration_id": orch_id,
    }
    await save_agent(agent)
    return {"status": "deployed", "agent_id": agent_id, "agent": agent}
