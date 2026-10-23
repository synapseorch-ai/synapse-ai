"""Every engine write of a workflow moves its row's `updated_at` (F871).

The store's upsert is ON CONFLICT DO UPDATE, which runs no SQLAlchemy `onupdate`: the column kept the workflow's
creation time for ever. Cloud checks its editor's save against that column, so a change by the AI builder, an MCP key
or the assistant — all engine writes — would never have made an editor's copy stale. The document is untouched.
"""
import asyncio

from sqlalchemy import select

from core.store import resources


async def _stamp(orch_id):
    from core.store import session
    from core.store.models import OrchestrationDB

    async with session() as s:
        return (await s.execute(select(OrchestrationDB.updated_at).where(OrchestrationDB.id == orch_id))).scalar_one()


async def test_an_update_moves_the_rows_stamp_and_leaves_the_document_as_written():
    item = {"id": "orch_f871_row", "name": "Desk"}
    await resources.save_orchestration(item)
    first = await _stamp("orch_f871_row")
    await asyncio.sleep(0.01)
    await resources.save_orchestration({**item, "name": "Desk v2"})
    assert await _stamp("orch_f871_row") > first
    assert await resources.get_orchestration("orch_f871_row") == {**item, "name": "Desk v2"}


async def test_an_agents_and_a_tools_row_stamps_move_too():
    """F891: the same for the other two documents an editor saves whole; their stamp is the row's alone."""
    agent = {"id": "agent_f891_row", "name": "Dispatch"}
    tool = {"name": "f891_weather", "description": "d"}
    await resources.save_agent(agent)
    await resources.save_tools([tool])
    first = await resources.row_stamps("agents", ["agent_f891_row"]), await resources.row_stamps("tools", ["f891_weather"])
    await asyncio.sleep(0.01)
    await resources.save_agent({**agent, "name": "Dispatch v2"})
    await resources.save_tools([{**tool, "description": "v2"}])
    assert (await resources.row_stamps("agents", ["agent_f891_row"]))["agent_f891_row"] > first[0]["agent_f891_row"]
    assert (await resources.row_stamps("tools", ["f891_weather"]))["f891_weather"] > first[1]["f891_weather"]
    assert await resources.get_agent("agent_f891_row") == {**agent, "name": "Dispatch v2"}
