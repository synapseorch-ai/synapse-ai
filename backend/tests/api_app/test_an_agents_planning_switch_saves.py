"""
The agent editor's Planning switch saves (F265).

Untick Planning, Save, reload: ticked again. `POST /api/agents` validates against
the engine's `Agent` model, which did not declare `planning` — nor
`skip_default_tools`, which the engine reads too — and pydantic drops what a
model does not declare. The route stores and answers `agent.dict()`, so both
were lost on every save, the editor reloaded the answer, and the box re-ticked.
A `planning: false` the AI builder's `update_agent` had written was erased by
the next save from the editor, and a duplicated agent lost both. Every agent
therefore kept `update_tasks` and the open-task refusal loop.
"""
import types

from _fakes import seed as S


async def _save(client, **fields):
    body = {**S.make_agent(id="agent_f265"), **fields}
    resp = await client.post("/api/agents", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _stored(client):
    return {a["id"]: a for a in (await client.get("/api/agents")).json()}["agent_f265"]


class TestTheSwitchesSurviveASave:
    async def test_planning_off_is_kept(self, client):
        answered = await _save(client, planning=False)
        assert answered["planning"] is False, "the editor reloads what the route answers"
        assert (await _stored(client))["planning"] is False

    async def test_planning_on_again_is_kept(self, client):
        await _save(client, planning=False)
        await _save(client, planning=True)
        assert (await _stored(client))["planning"] is True

    async def test_skip_default_tools_is_kept(self, client):
        await _save(client, skip_default_tools=True)
        assert (await _stored(client))["skip_default_tools"] is True

    async def test_an_agent_saved_before_either_reads_as_before(self, client):
        """No key sent: Planning stays on and the defaults stay in."""
        body = {k: v for k, v in S.make_agent(id="agent_f265").items()}
        await client.post("/api/agents", json=body)
        stored = await _stored(client)
        assert stored.get("planning", True) is True
        assert stored.get("skip_default_tools", False) is False


class TestTheEngineReadsWhatWasSaved:
    async def test_a_saved_planning_off_takes_the_task_list_away(self, client, fake_llm):
        from core.react_engine import run_agent_step

        await _save(client, planning=False)
        fake_llm.script(["done"])
        server = types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})
        [ev async for ev in run_agent_step(
            message="hi", agent_id="agent_f265", session_id="s_f265", server_module=server, max_turns=2,
        )]
        offered = [t["function"]["name"] for t in (fake_llm.calls[0].get("tools") or [])]
        assert "update_tasks" not in offered, offered
