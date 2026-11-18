"""A Tool step's answer is held to what its tool declares at run time, and what it lacks is said as a warning
(F1620, part 2a).

A tool can say what it answers: a custom tool's output schema, or an MCP tool's `outputSchema`. A run kept a Tool step's
answer as the tool gave it and checked nothing, so an answer without a field its tool declares was only found when a
later step read that field and found nothing there (F1616(b)). Now the step holds the answer to the declaration, to its
depth (F1618's reading, `output_fields`), as soon as the tool answers: each field missing, or not of its type, is named
by its path in one step warning, with whose word the declaration is (the tool's output schema in its settings, or its
server's own). The answer stays as the tool gave it and the step completes: a server's declaration can be wrong, and a
run that works today never fails on it.

A field the declaration does not require may be absent or empty. A declaration known only at its first level (a part
outside the answer's subset) is held there only. A value the warning quotes is masked as a step's error is (a held
secret, anything shaped like a key) and cut short.

Near misses: a tool that declares nothing says nothing; an answer that holds says nothing; the built-in page reads, whose
shape lists every key the scraper may answer, are not held at run time; an answer big enough to be kept as a file is
held from its value.
"""
from __future__ import annotations

import json
import types

import pytest

from _fakes import seed as S
from core.orchestration.output_fields import json_schema
from core.tools import VirtualTool

LEADS = [{"name": "leads", "type": "list", "of": {"fields": [{"name": "email", "type": "string"}]}},
         {"name": "total", "type": "number"}, {"name": "note", "type": "string", "may_be_empty": True}]
SCHEMA = json_schema(LEADS)
#: Known at its first level only (a group with no fields is outside the answer's subset): `hourly` and `daily` named,
#: `daily` required.
PARTLY = {"type": "object", "properties": {"hourly": {"type": "object"}, "daily": {"type": "string"}},
          "required": ["daily"]}
TAKES = {"type": "object", "properties": {"week": {"type": "string"}}}
GOOD = {"leads": [{"email": "a@example.com"}, {"email": "b@example.com"}], "total": 2}


def _ann():
    from mcp.types import ToolAnnotations
    return ToolAnnotations(readOnlyHint=True, idempotentHint=True)


def _mcp_tool(name: str, schema: dict | None) -> VirtualTool:
    tool = VirtualTool(name, "lists leads", TAKES, _ann())
    if schema is not None:
        tool.outputSchema = schema
    return tool


class _Session:
    def __init__(self):
        self.answers: dict[str, str] = {}

    async def call_tool(self, name, args, **_kw):
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=self.answers[name])], isError=False)


@pytest.fixture
def server(monkeypatch):
    """`crm_leads` (an MCP tool declaring LEADS), `crm_loose` (one declaring nothing), `crm_weather` (one declaring
    PARTLY), `scrape_url` (a page read), and `find_leads` (the person's own HTTP tool declaring LEADS)."""
    import core.orchestration.resolved as resolved_mod
    import core.tools as tools_mod
    from core.orchestration.steps import ToolStepExecutor
    from core.tools import custom_virtual_tool

    session = _Session()
    custom = [{"name": "find_leads", "description": "Finds leads", "tool_type": "http", "method": "GET",
               "url": "https://example.com/leads", "inputSchema": TAKES, "outputSchema": SCHEMA,
               "annotations": {"readOnlyHint": True, "idempotentHint": True}}]
    declared = {"crm_leads": _mcp_tool("crm_leads", SCHEMA), "crm_loose": _mcp_tool("crm_loose", None),
                "crm_weather": _mcp_tool("crm_weather", PARTLY), "scrape_url": _mcp_tool("scrape_url", None),
                "find_leads": custom_virtual_tool(custom[0])}

    async def one(server_module, name, agent, custom_tools):
        return declared.get(name)

    async def custom_tools(engine):
        return custom

    async def http(self, tool, args):
        return session.answers[tool["name"]]

    monkeypatch.setattr(tools_mod, "resolve_one_tool", one)
    monkeypatch.setattr(resolved_mod, "run_custom_tools", custom_tools)
    monkeypatch.setattr(ToolStepExecutor, "_execute_http_tool", http)
    router = {n: ("fake", n) for n in declared if n != "find_leads"}
    return types.SimpleNamespace(agent_sessions={"fake": session}, memory_store=None, tool_router=router,
                                 session=session)


async def _run(server, tool: str, answer, run_id: str = "run_f1620_held"):
    """Runs one Tool step calling `tool`, which answers `answer` (as JSON text, as a tool's text is)."""
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    server.session.answers[tool] = answer if isinstance(answer, str) else json.dumps(answer)
    orch = S.make_orchestration(id=f"orch_{run_id}", entry_step_id="find", steps=[
        {"id": "find", "name": "Find leads", "type": "tool", "forced_tool": tool, "tool_arguments": {"week": "this"},
         "output_key": "found", "model": "claude-x", "next_step_id": None}])
    await S.seed_orchestrations([orch])
    events = [e async for e in OrchestrationEngine(Orchestration.model_validate(orch), server).run("go", run_id=run_id)]
    done = [e for e in events if e.get("type") == "orchestration_complete"]
    warned = [e["message"] for e in events if e.get("type") == "step_warning" and e.get("orch_step_id") == "find"]
    return done[-1], warned


class TestWhatTheAnswerLacksIsSaid:
    async def test_a_custom_tools_answer_missing_a_field_at_depth_is_one_warning_naming_its_path(self, server):
        answer = {"leads": [{"email": "a@example.com"}, {"name": "Harbor & Pine"}], "total": 2}
        done, warned = await _run(server, "find_leads", answer)
        assert len(warned) == 1, warned
        assert "`leads[1].email` is missing" in warned[0] and "find_leads" in warned[0], warned[0]
        assert "output schema" in warned[0], "a custom tool's declaration is the person's own, in its settings"

    async def test_the_answer_is_kept_as_the_tool_gave_it_and_the_step_completes(self, server):
        answer = {"leads": [{"name": "Harbor & Pine"}], "total": 1}
        done, warned = await _run(server, "find_leads", answer)
        assert done["status"] == "completed", done
        assert json.loads(done["final_state"]["found"]) == answer
        assert warned

    async def test_an_mcp_tools_is_said_as_its_servers_word(self, server):
        done, warned = await _run(server, "crm_leads", {"leads": [{"name": "x"}], "total": 1})
        assert done["status"] == "completed" and len(warned) == 1, (done, warned)
        assert "`leads[0].email` is missing" in warned[0] and "server" in warned[0], warned[0]

    async def test_a_field_not_of_its_type_is_named_with_its_value(self, server):
        _, warned = await _run(server, "crm_leads", {"leads": [], "total": "two"})
        assert len(warned) == 1 and '`total` is "two", not a number' in warned[0], warned

    async def test_an_answer_that_is_no_object_is_said(self, server):
        _, warned = await _run(server, "crm_leads", "No leads this week.")
        assert len(warned) == 1 and "crm_leads" in warned[0], warned

    async def test_a_declaration_known_at_its_first_level_is_held_there_only(self, server):
        _, warned = await _run(server, "crm_weather", {"hourly": {"anything": [1, 2]}})
        assert len(warned) == 1 and "`daily` is missing" in warned[0], warned
        _, deep = await _run(server, "crm_weather", {"hourly": "not even a group", "daily": "sunny"},
                             run_id="run_f1620_first")
        assert deep == [], deep


class TestAQuotedValueIsMaskedAndCutShort:
    async def test_a_long_value_is_cut_short(self, server):
        _, warned = await _run(server, "crm_leads", {"leads": [], "total": "x" * 200})
        assert len(warned) == 1 and "x" * 41 not in warned[0] and "…" in warned[0], warned

    @pytest.mark.parametrize("key", ["sk-abcdefghijklmnopqrstuvwxyz123456", "ghp_" + "a1B2c3D4" * 8],
                             ids=["short", "long"])
    async def test_a_value_shaped_like_a_key_is_never_quoted(self, server, key):
        _, warned = await _run(server, "crm_leads", {"leads": [], "total": key})
        assert len(warned) == 1, warned
        assert key[:12] not in warned[0], "neither the key nor the start of it is quoted"


class TestNearMisses:
    async def test_a_tool_that_declares_nothing_says_nothing(self, server):
        done, warned = await _run(server, "crm_loose", {"anything": 1})
        assert done["status"] == "completed" and warned == []

    async def test_an_answer_that_holds_says_nothing(self, server):
        assert (await _run(server, "find_leads", GOOD))[1] == []
        assert (await _run(server, "crm_leads", {**GOOD, "note": None}, run_id="run_f1620_null"))[1] == []

    async def test_a_page_read_is_not_held_at_run_time(self, server):
        assert (await _run(server, "scrape_url", {"success": True, "url": "https://example.com"}))[1] == []

    async def test_an_answer_kept_as_a_file_is_held_from_its_value(self, server, monkeypatch):
        import core.orchestration.steps as steps_mod

        monkeypatch.setattr(steps_mod, "_kept", lambda value, run, step, engine: "vault:Run outputs/found.json")
        done, warned = await _run(server, "crm_leads", {"leads": [{"name": "x"}], "total": 1})
        assert len(warned) == 1 and "`leads[0].email` is missing" in warned[0], warned
