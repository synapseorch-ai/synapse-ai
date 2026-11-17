"""A workflow's read past a tool's declared answer never refuses its save, an edit or its run (F1620, part 1b-iii).

The rule that holds each read of a Tool step's answer to what its tool declares (`builder_tools.tool_answer_misses`)
speaks only where a build is made: the builder's `validate_orchestration` tool and the plan check. A workflow saved from
the editor or the API whose template reads a field its custom tool's output schema does not declare saves and runs as
before: the run reads whatever the tool answers, and a read that needs the field fails where it reads it. An edit
whose only finding would be a warning, a read past an MCP tool's own declaration, saves too.

Each case first shows the rule does name its read (an issue for the custom tool's, a warning for the MCP tool's), so
what saves and runs here is exactly what the builder is told of.
"""
from __future__ import annotations

import types

import pytest

from _fakes import engine_events as E
from _fakes import seed as S
from core.builder_tools import _tool_answer_reads
from core.native_builder.build_plan import answers_of, tools_here
from core.orchestration.output_fields import json_schema

LEADS = [{"name": "leads", "type": "list", "of": {"fields": [{"name": "email", "type": "string"}]}},
         {"name": "total", "type": "number"}]
SCHEMA = json_schema(LEADS)


@pytest.fixture
def listed(monkeypatch):
    """The workspace's tools: `find_leads`, a custom HTTP tool with its output schema, and `crm_leads`, an MCP tool
    whose server declares the same answer."""
    def tool(name: str, schema: dict | None = None):
        return types.SimpleNamespace(name=name, description=f"{name}.", inputSchema={"type": "object"},
                                     outputSchema=schema)

    async def catalogue(server_module):
        return [tool("crm_leads", SCHEMA), tool("find_leads")]

    async def custom():
        return [{"name": "find_leads", "description": "Finds leads", "tool_type": "http", "method": "GET",
                 "url": "https://example.com/leads", "inputSchema": {"type": "object"}, "outputSchema": SCHEMA}]

    async def named(server_module):
        return frozenset()

    monkeypatch.setattr("core.builder_tools._workspace_catalogue", catalogue)
    monkeypatch.setattr("core.routes.tools.load_custom_tools", custom)
    monkeypatch.setattr("core.tools.named_only_offered", named)


def _workflow(tool: str, read: str, oid: str = "orch_f1620_reads") -> dict:
    return S.make_orchestration(id=oid, name="Leads", entry_step_id="find", steps=[
        {"id": "find", "name": "Find leads", "type": "tool", "forced_tool": tool, "output_key": "find",
         "next_step_id": "show"},
        {"id": "show", "name": "Show the lead", "type": "print", "print_content": f"{{{{ state.find.{read} }}}}",
         "output_key": "shown", "next_step_id": None}])


async def _said(orch: dict) -> tuple[list[str], list[str]]:
    return _tool_answer_reads(orch["steps"], answers_of((await tools_here(None))[0]))


async def test_a_custom_tools_read_past_its_answer_saves(client, listed):
    orch = _workflow("find_leads", "leads.0.emial")
    issues, _ = await _said(orch)
    assert issues and "emial" in issues[0], "the builder is told of this read"
    saved = await client.post("/api/orchestrations", json=orch)
    assert saved.status_code == 200, saved.text
    got = (await client.get(f"/api/orchestrations/{orch['id']}")).json()
    assert got["steps"][1]["print_content"] == "{{ state.find.leads.0.emial }}", got


async def test_an_edit_whose_only_finding_is_an_mcp_warning_saves(client, listed):
    first = _workflow("crm_leads", "total")
    assert (await client.post("/api/orchestrations", json=first)).status_code == 200
    edited = _workflow("crm_leads", "leads.0.emial")
    issues, warnings = await _said(edited)
    assert not issues and warnings and "emial" in warnings[0], (issues, warnings)
    saved = await client.post("/api/orchestrations", json=edited)
    assert saved.status_code == 200, saved.text
    got = (await client.get(f"/api/orchestrations/{edited['id']}")).json()
    assert got["steps"][1]["print_content"] == "{{ state.find.leads.0.emial }}", got


async def test_its_run_starts(client, listed, monkeypatch):
    import core.orchestration.engine as engine_mod

    started: list[dict] = []
    fake = E.fake_engine([E.orch_start(), E.orch_complete()])

    class _Recording(fake):
        def __init__(self, orch=None, server_module=None):
            super().__init__(orch, server_module)
            started.append(orch)

    monkeypatch.setattr(engine_mod, "OrchestrationEngine", _Recording)
    orch = _workflow("find_leads", "leads.0.emial", oid="orch_f1620_run")
    assert (await _said(orch))[0], "the builder is told of this read"
    assert (await client.post("/api/orchestrations", json=orch)).status_code == 200
    ran = await client.post(f"/api/orchestrations/{orch['id']}/run", json={"message": "go"})
    assert ran.status_code == 200, ran.text
    assert ran.headers["content-type"].startswith("text/event-stream")
    assert len(started) == 1, "the run started with the workflow as saved"
