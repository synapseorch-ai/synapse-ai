"""The builder sees the Vault's write wherever the host offers it, and a step can only reach it by naming it (F1625).

`OPT_IN_TOOLS` keeps an agent made with `"all"` from holding the Vault's write on cloud's fleet (F1289: a write it never
named). The builder asked for its own picture of the workspace with that same `"all"`, so where the write is served by
the `vault` server it never saw it: a plan that saves to the Vault, or draws a chart (a CSV written by `vault_write`,
F1288), was told the tool does not exist. Now the builder's catalogue names each per-tool opt-in beside `"all"`, so it
lists the ones the host serves, marked as held only by naming them; and a saved workflow whose Tool step reaches such a
tool through an agent that holds only `"all"` is refused, since that run would not find it. An `"all"` agent still
never holds it, and self-hosted, the sandbox server's own write is as it was.
"""
from __future__ import annotations

import inspect
import types

import pytest

from core.tool_router import ToolRouter
from core.tools import aggregate_all_tools


class _Tool:
    def __init__(self, name):
        self.name = name
        self.description = f"{name}."
        self.inputSchema = {"type": "object", "properties": {}}


class _Session:
    def __init__(self, *names):
        self._tools = [_Tool(n) for n in names]

    async def list_tools(self):
        return type("Result", (), {"tools": self._tools})()


#: Cloud's fleet with a writer set serves the write on `vault` beside the reads; self-hosted, the sandbox serves its own.
_SERVED = {"vault": ("list_files", "read_file", "vault_write"), "vault_sandbox": ("vault_write", "vault_read"),
           "vault_reads_only": ("list_files", "read_file")}


def _module(*sessions: str):
    built = {("vault" if name == "vault_reads_only" else name): _Session(*_SERVED[name]) for name in sessions}
    router = ToolRouter()
    for name, session in built.items():
        for tool in session._tools:
            router.register(name, tool.name, session_key=name, alias=True)
    return types.SimpleNamespace(agent_sessions=built, tool_router=router, memory_store=None, _session_tools={})


@pytest.fixture(autouse=True)
def _no_custom_tools(monkeypatch):
    async def none():
        return []

    monkeypatch.setattr("core.routes.tools.load_custom_tools", none)


def _names(catalogue) -> set[str]:
    return {getattr(t, "name", None) or (t.get("function", {}) or {}).get("name") for t in catalogue}


class TestTheBuilderSeesIt:
    async def test_the_catalogue_lists_the_write_the_fleet_serves(self):
        from core.builder_tools import _workspace_catalogue

        names = _names(await _workspace_catalogue(_module("vault")))
        assert {"list_files", "read_file"} <= names
        assert "vault_write" in names, "the builder's catalogue leaves out the Vault's write the host serves"

    async def test_the_plans_workspace_has_it_held_only_by_naming(self):
        from core.native_builder import build_plan

        ws = await build_plan.workspace(_module("vault"))
        entry = next((t for t in ws.tools if t["name"] == "vault_write"), None)
        assert entry is not None, "the planner's tool list leaves out the Vault's write the host serves"
        assert entry.get("named_only") is True, entry

    async def test_the_guards_suggestions_know_it(self):
        from core.builder_guard import _known_names

        assert "vault_write" in await _known_names(_module("vault"))


class TestAsItWas:
    async def test_with_no_writer_nothing_new_is_listed(self):
        from core.builder_tools import _workspace_catalogue

        assert "vault_write" not in _names(await _workspace_catalogue(_module("vault_reads_only")))

    async def test_self_hosted_the_sandboxs_write_is_listed_and_needs_no_naming(self):
        from core.native_builder import build_plan

        ws = await build_plan.workspace(_module("vault_sandbox"))
        entry = next((t for t in ws.tools if t["name"] == "vault_write"), None)
        assert entry is not None and not entry.get("named_only"), entry

    async def test_an_all_agent_still_never_holds_it(self):
        declared, *_ = await aggregate_all_tools(_module("vault"), {"tools": ["all"]}, [])
        assert "vault_write" not in {t.name for t in declared}


def _orch(agent_id: str | None) -> dict:
    step = {"id": "save", "name": "Save the report", "type": "tool", "forced_tool": "vault_write",
            "tool_arguments": {"path": "reports/sales.csv", "content": "{{ state.user_input }}"}, "output_key": "save",
            "next_step_id": "done"}
    if agent_id:
        step["agent_id"] = agent_id
    return {"id": "orch_f1625", "name": "Sales report", "entry_step_id": "save",
            "steps": [step, {"id": "done", "name": "Done", "type": "end"}]}


class TestAStepReachesItOnlyByNamingIt:
    """A Tool step with an agent finds its tool through that agent's list (steps.py): a tool held only by naming it,
    reached through an agent that holds only `"all"`, is not found, and the run fails at that step."""

    @staticmethod
    def _issues(orch: dict, agents: dict, named_only: frozenset[str]) -> list[str]:
        from core.builder_tools import _validate_orchestration

        assert "named_only" in inspect.signature(_validate_orchestration).parameters, \
            "the validator is not told which tools a step reaches only by naming them"
        return _validate_orchestration(orch, agents=agents, named_only=named_only)["issues"]

    def test_through_an_all_agent_it_is_refused_with_the_fix(self):
        said = [i for i in self._issues(_orch("ag_writer"), {"ag_writer": {"id": "ag_writer", "name": "Writer",
                                                                           "tools": ["all"]}},
                                        frozenset({"vault_write"})) if "vault_write" in i]
        assert len(said) == 1, said
        assert "Writer" in said[0] and "name" in said[0].lower(), said[0]

    def test_an_agent_that_names_it_passes(self):
        assert not [i for i in self._issues(_orch("ag_writer"), {"ag_writer": {"id": "ag_writer", "name": "Writer",
                                                                               "tools": ["all", "vault_write"]}},
                                            frozenset({"vault_write"})) if "vault_write" in i]

    def test_a_step_with_no_agent_names_its_own_tool_and_passes(self):
        assert not [i for i in self._issues(_orch(None), {}, frozenset({"vault_write"})) if "vault_write" in i]

    def test_self_hosted_nothing_needs_naming_so_nothing_is_refused(self):
        assert not [i for i in self._issues(_orch("ag_writer"), {"ag_writer": {"id": "ag_writer", "name": "Writer",
                                                                               "tools": ["all"]}},
                                            frozenset()) if "vault_write" in i]
