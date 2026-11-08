"""A duplicated builder's workflow agent runs the copy it came with (F289).

`POST /api/builder/duplicate` copies the builder's agents and orchestration
into the workspace. The orchestrator shell among the agents had its
`orchestration_id` set to None — "fixed up below", said the comment, and
nothing below fixed it — so the copy's workflow agent was hollow and failed on
its first message. It now points at the orchestration copied with it.
"""


class TestTheCopiesWorkflowAgent:
    async def test_it_runs_the_copied_orchestration(self, client):
        from core.routes.agents import load_user_agents

        resp = await client.post("/api/builder/duplicate")

        assert resp.status_code == 200
        body = resp.json()
        copies = {a["id"]: a for a in await load_user_agents() if a["id"] in body["agent_ids"]}
        shells = [a for a in copies.values() if a.get("type") == "orchestrator"]
        assert shells, "the builder bundle carries an orchestrator shell"
        assert all(a.get("orchestration_id") == body["orchestration_id"] for a in shells)
