"""
A workflow deleted the way cloud deletes one is invisible to every engine reader (F423).

Cloud keeps a deleted workflow's row — its runs name it — and marks it with the
`is_active` column alone; it strips the key from the definitions it stores. The
engine's readers of workspace content all skip `is_active: false` documents
(`builder_tools._is_visible`), but the store built a document from `definition`
only, so in cloud every one of them saw deleted workflows: C2 counted 330 MCP
tools for acme, 221 of them `run_…` tools for deleted workflows, Claude Code
read "You have 294 workflows", and the builder could update one — and its save
wrote `is_active: true` back, bringing the deleted workflow back. The tests
that covered `_is_visible` seeded the flag inside the definition, as no cloud
row has it.
"""
import json
import types

import pytest

from _fakes import seed as S


async def _insert_retired(orch_id: str, name: str) -> None:
    """A row as cloud's delete leaves it: `is_active` false in the column, absent from the definition."""
    from core.store import cache, session
    from core.store.models import OrchestrationDB
    from core.tenancy import get_tenant

    definition = {"name": name, "description": "", "entry_step_id": "s1",
                  "steps": [{"id": "s1", "name": "S1", "type": "print", "print_content": "x", "output_key": "o"}]}
    async with session() as s:
        s.add(OrchestrationDB(id=orch_id, name=name, definition=definition, tenant_id=get_tenant(), is_active=False))
        await s.commit()
    cache.invalidate(OrchestrationDB.__tablename__)


async def _builder(tool: str, args: dict) -> dict:
    from core.builder_tools import execute_builder_tool
    return json.loads(await execute_builder_tool(tool, args, types.SimpleNamespace(agent_sessions={})))


class TestTheStoreSaysSo:
    async def test_a_column_only_retired_row_reads_inactive(self):
        from core.store import resources
        await _insert_retired("orch_gone", "Gone")

        assert (await resources.get_orchestration("orch_gone"))["is_active"] is False
        listed = {o["id"]: o for o in await resources.load_orchestrations()}
        assert listed["orch_gone"]["is_active"] is False, "the list stays whole: id checks and resumes read it"

    async def test_a_live_one_reads_as_it_was_saved(self):
        """No key added: an active workflow's document is still exactly what was saved."""
        from core.store import resources
        item = {"id": "orch_live", "name": "Live", "steps": []}
        await resources.save_orchestration(item)
        assert await resources.get_orchestration("orch_live") == item


class TestEveryReaderSkipsIt:
    async def test_it_is_never_an_mcp_tool(self):
        from core.mcp_server.workflows import workflow_map
        await S.seed_orchestrations([S.make_orchestration(id="orch_live", name="Invoice triage",
                                                          offer_to_mcp=True)])
        await _insert_retired("orch_gone", "Invoice triage")

        named = await workflow_map(set())

        assert [o["id"] for o in named.values()] == ["orch_live"]
        assert "run_invoice_triage" in named, "the live one keeps its plain name, no _<id> suffix"

    async def test_list_orchestrations_leaves_it_out(self):
        await S.seed_orchestrations([S.make_orchestration(id="orch_live", name="Live")])
        await _insert_retired("orch_gone", "Gone")

        listed = await _builder("list_orchestrations", {})

        ids = [o["id"] for o in (listed.get("orchestrations") if isinstance(listed, dict) else listed)]
        assert ids == ["orch_live"]

    async def test_the_builder_cannot_bring_it_back(self):
        from core.store import cache, session
        from core.store.models import OrchestrationDB
        from sqlalchemy import select
        await _insert_retired("orch_gone", "Gone")

        result = await _builder("update_orchestration", {"orch_id": "orch_gone", "name": "Back"})

        assert "error" in result
        cache.invalidate(OrchestrationDB.__tablename__)
        async with session() as s:
            assert (await s.execute(select(OrchestrationDB.is_active).where(OrchestrationDB.id == "orch_gone"))
                    ).scalar_one() is False
