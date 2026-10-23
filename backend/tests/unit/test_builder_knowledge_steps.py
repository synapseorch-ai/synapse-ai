"""The AI builder knows Knowledge's Retrieve and Ingest steps — only while Knowledge is on (R1, D166).

While it is off nothing changes: `list_knowledge_bases` is in no agent's
roster, the builder's prompts carry no palette for the two steps, and a write
that holds one is refused (it would fail its run at that step). While it is on
the builder lists the bases, writes the steps' settings as the contract's own
block, and `validate_orchestration` names a base that does not exist.
"""
from __future__ import annotations

import json
import types

import pytest

from _fakes import seed as S
from _fakes.knowledge_corpus import CONTRACTS, REPORTS, save_bases
from _fakes.knowledge_env import enable_knowledge, use_saved_settings
from core.builder_tools import _validate_orchestration, execute_builder_tool
from core.knowledge.kb_settings import KnowledgeBase


async def call(tool: str, args: dict) -> dict:
    return json.loads(await execute_builder_tool(tool, args, types.SimpleNamespace(agent_sessions={})))


@pytest.fixture
async def on(monkeypatch):
    use_saved_settings(monkeypatch)
    await enable_knowledge()


@pytest.fixture
def off(monkeypatch):
    use_saved_settings(monkeypatch)  # shipped default: off


async def _steps(orch_id: str) -> dict:
    from core.routes.orchestrations import load_orchestrations
    orch = next(o for o in await load_orchestrations() if o["id"] == orch_id)
    return {s["id"]: s for s in orch["steps"]}


RETRIEVE_ARGS = {
    "orch_id": "orch_k", "step_id": "step_find", "name": "Find clauses", "type": "retrieve",
    "retrieve_kb_ids": [CONTRACTS.id], "retrieve_query": "{state.user_input}",
    "retrieve_filters_json": '{"doc_type": "contract"}', "retrieve_top_k": 5, "output_key": "found",
}


# ── list_knowledge_bases ───────────────────────────────────────────────────────

class TestListKnowledgeBases:
    async def test_it_lists_the_workspace_s_bases(self, on):
        disabled = KnowledgeBase.model_validate({**REPORTS.model_dump(), "status": "disabled"})
        await save_bases(CONTRACTS, disabled)
        assert await call("list_knowledge_bases", {}) == [
            {"id": CONTRACTS.id, "name": "Contracts", "description": "Customer contracts and their renewals.",
             "folder": "contracts", "status": "active", "top_k": CONTRACTS.retrieval.top_k},
            {"id": REPORTS.id, "name": "Reports", "description": "", "folder": "reports", "status": "disabled",
             "top_k": REPORTS.retrieval.top_k}]

    async def test_while_off_it_refuses(self, off):
        await save_bases(CONTRACTS)
        result = await call("list_knowledge_bases", {})
        assert "Knowledge is switched off" in result["error"]

    @pytest.mark.parametrize("knowledge", [False, True])
    async def test_an_agent_that_names_it_gets_it_only_while_on_and_all_never_does(self, monkeypatch, knowledge):
        """`"all"` does not reach it, on or off — so no `"all"` agent's roster or prompt cache changes with
        Knowledge (B1b's `test_an_agent_with_no_bases_is_offered_the_same_with_knowledge_on_or_off`)."""
        import core.tools as tools_mod

        use_saved_settings(monkeypatch)
        if knowledge:
            await enable_knowledge()
        server = types.SimpleNamespace(agent_sessions={}, tool_router={}, memory_store=None, _session_tools={})
        everything = {"tools": ["all"], "skip_default_tools": True}
        naming = {"tools": ["list_agents", "list_knowledge_bases"], "skip_default_tools": True}
        for agent, offered in ((everything, False), (naming, knowledge)):
            all_tools, _, _, _ = await tools_mod.aggregate_all_tools(server, agent, [])
            resolved = await tools_mod.resolve_one_tool(server, "list_knowledge_bases", agent, [])
            assert ("list_knowledge_bases" in {t.name for t in all_tools}) is offered, agent
            assert (resolved is not None) is offered, agent
            assert "list_agents" in {t.name for t in all_tools}  # every other builder tool, either way


# ── writing the steps ──────────────────────────────────────────────────────────

class TestWritingTheSteps:
    async def test_while_off_a_knowledge_step_is_refused_and_nothing_is_saved(self, off):
        await S.seed_orchestrations([S.make_orchestration(id="orch_k")])
        result = await call("add_single_step", RETRIEVE_ARGS)
        assert "Knowledge is switched off" in result["error"] and "step_find" in result["error"]
        assert list(await _steps("orch_k")) == ["step_1"]

    async def test_flat_fields_are_stored_as_the_step_s_settings_block(self, on):
        await S.seed_orchestrations([S.make_orchestration(id="orch_k")])
        assert (await call("add_single_step", RETRIEVE_ARGS))["status"] == "step_added"

        step = (await _steps("orch_k"))["step_find"]
        assert step["retrieve"] == {"kb_ids": [CONTRACTS.id], "query": "{state.user_input}",
                                    "filters": {"doc_type": "contract"}, "top_k": 5}
        assert not any(k.startswith("retrieve_") for k in step)

    async def test_a_patch_of_one_field_changes_only_that_field(self, on):
        await S.seed_orchestrations([S.make_orchestration(id="orch_k")])
        await call("add_single_step", RETRIEVE_ARGS)
        result = await call("update_step", {"orch_id": "orch_k", "step_id": "step_find",
                                            "patch_json": '{"retrieve_query": "{state.question}"}'})
        assert result["status"] == "step_updated"
        assert (await _steps("orch_k"))["step_find"]["retrieve"] == {
            "kb_ids": [CONTRACTS.id], "query": "{state.question}", "filters": {"doc_type": "contract"}, "top_k": 5}

    async def test_a_batch_and_a_steps_json_fold_the_same_way(self, on):
        await S.seed_orchestrations([S.make_orchestration(id="orch_k")])
        result = await call("add_multiple_steps", {"orch_id": "orch_k", "steps": [
            {"step_id": "step_keep", "name": "Index tickets", "type": "ingest", "ingest_kb_id": CONTRACTS.id,
             "ingest_records_from": "tickets", "ingest_id_field": "key", "ingest_metadata_fields": ["priority"]}]})
        assert result["status"] == "steps_added"
        assert (await _steps("orch_k"))["step_keep"]["ingest"] == {
            "kb_id": CONTRACTS.id, "records_from": "tickets", "id_field": "key", "metadata_fields": ["priority"]}

        created = await call("create_orchestration", {
            "name": "Ask", "entry_step_id": "step_find",
            "steps_json": json.dumps([{"id": "step_find", "name": "Find", "type": "retrieve",
                                       "retrieve_kb_ids": [CONTRACTS.id], "retrieve_query": "q",
                                       "next_step_id": "step_end"},
                                      {"id": "step_end", "name": "End", "type": "end"}])})
        found = created["orchestration"]["steps"][0]
        assert found["retrieve"] == {"kb_ids": [CONTRACTS.id], "query": "q"}

    async def test_a_filter_that_is_not_json_is_kept_for_the_validator_to_name(self, on):
        await S.seed_orchestrations([S.make_orchestration(id="orch_k")])
        await call("add_single_step", {**RETRIEVE_ARGS, "retrieve_filters_json": "doc_type = contract"})
        step = (await _steps("orch_k"))["step_find"]
        assert step["retrieve"]["filters"] == "doc_type = contract"
        issues = _validate_orchestration({"entry_step_id": "step_find", "steps": [step]})["issues"]
        assert any("Retrieve step 'step_find' is not set up: filters" in i for i in issues)


# ── validation ─────────────────────────────────────────────────────────────────

def _orch(*steps: dict, schema: dict | None = None) -> dict:
    return {"entry_step_id": steps[0]["id"], "state_schema": schema or {},
            "steps": [*steps, {"id": "e", "name": "End", "type": "end"}]}


def _retrieve(**kw) -> dict:
    settings = {"kb_ids": [CONTRACTS.id], "query": "{state.user_input}", **kw.pop("retrieve", {})}
    return {"id": "r", "name": "Find", "type": "retrieve", "retrieve": settings, "output_key": "found",
            "next_step_id": "e", **kw}


def _ingest(**kw) -> dict:
    settings = {"kb_id": CONTRACTS.id, "records_from": "tickets", **kw.pop("ingest", {})}
    return {"id": "i", "name": "Keep", "type": "ingest", "ingest": settings, "next_step_id": "e", **kw}


KBS = {CONTRACTS.id: CONTRACTS,
       REPORTS.id: KnowledgeBase.model_validate({**REPORTS.model_dump(), "status": "disabled"})}


class TestValidation:
    def test_a_well_formed_retrieve_step_has_no_issues(self):
        assert _validate_orchestration(_orch(_retrieve()), kbs=KBS) == {"ok": True, "issues": [], "warnings": []}

    @pytest.mark.parametrize("step,expected", [
        ({"id": "r", "name": "Find", "type": "retrieve", "output_key": "found", "next_step_id": "e"},
         "Retrieve step 'r' is not set up: kb_ids"),
        (_retrieve(retrieve={"kb_ids": ["kb_gone"]}), "uses knowledge base 'kb_gone', which does not exist"),
        (_retrieve(retrieve={"kb_ids": [REPORTS.id]}), f"knowledge base 'Reports' ({REPORTS.id}), which is disabled"),
        (_retrieve(retrieve={"kb_ids": [CONTRACTS.id, REPORTS.id], "snapshot": "ksnap_" + "1" * 32}),
         "search exactly one knowledge base"),
        (_retrieve(output_key=None), "Step 'r' (retrieve) has no output_key"),
        ({"id": "i", "name": "Keep", "type": "ingest", "next_step_id": "e"}, "Ingest step 'i' is not set up: kb_id"),
    ])
    def test_what_it_names(self, step, expected):
        issues = _validate_orchestration(_orch(step), kbs=KBS)["issues"]
        assert any(expected in i for i in issues), issues

    def test_an_ingest_step_reads_a_key_something_writes_or_the_schema_declares(self):
        issues = _validate_orchestration(_orch(_ingest()), kbs=KBS)["issues"]
        assert any("Ingest step 'i' reads its records from 'tickets', which nothing writes" in i for i in issues)
        made = {"id": "x", "name": "Extract", "type": "extract_json", "input_keys": ["user_input"],
                "output_key": "tickets", "next_step_id": "i"}
        assert _validate_orchestration(_orch(made, _ingest()), kbs=KBS)["ok"]
        assert _validate_orchestration(_orch(_ingest(ingest={"records_from": "state.tickets"}),
                                             schema={"tickets": {"type": "list", "default": []}}), kbs=KBS)["ok"]

    def test_without_the_bases_only_the_settings_are_checked(self):
        assert _validate_orchestration(_orch(_retrieve(retrieve={"kb_ids": ["kb_gone"]})))["ok"]

    def test_while_off_each_knowledge_step_is_named(self):
        issues = _validate_orchestration(_orch(_retrieve()), knowledge_off=True)["issues"]
        assert issues == ["Retrieve step 'r' cannot run while Knowledge is switched off in this workspace — remove it."]

    async def test_the_tool_looks_the_bases_up_while_on(self, on):
        await save_bases(CONTRACTS)
        await S.seed_orchestrations([{**S.make_orchestration(id="orch_k"),
                                      **_orch(_retrieve(retrieve={"kb_ids": ["kb_gone"]}))}])
        verdict = await call("validate_orchestration", {"orch_id": "orch_k"})
        assert any("'kb_gone', which does not exist" in i for i in verdict["issues"])

    async def test_the_tool_names_knowledge_steps_while_off(self, off):
        await S.seed_orchestrations([{**S.make_orchestration(id="orch_k"), **_orch(_retrieve())}])
        verdict = await call("validate_orchestration", {"orch_id": "orch_k"})
        assert any("cannot run while Knowledge is switched off" in i for i in verdict["issues"])


# ── the builder's prompts ──────────────────────────────────────────────────────

class TestTheBuildersPrompts:
    def _steps(self) -> dict:
        from core.native_builder import bundled_definitions
        return {s["id"]: s for s in bundled_definitions()["orchestration"]["steps"]}

    def test_the_palette_is_a_state_key_the_planning_steps_read(self):
        from core.native_builder import bundled_definitions

        orch = bundled_definitions()["orchestration"]
        assert orch["state_schema"]["knowledge_steps"]["default"] == ""
        steps = self._steps()
        # SAI2-P3: the planner writes the Plan, and code reads the knowledge bases for it, by id, into its pack.
        assert "{state.knowledge_steps}" in steps["step_plan"]["prompt_template"]
        import inspect

        from core.native_builder import build_plan
        assert "knowledge_bases" in inspect.getsource(build_plan.read_plan_context)

    @pytest.mark.parametrize("palette", [False, True])
    def test_off_the_prompt_says_nothing_of_the_two_steps(self, palette):
        from core.native_builder import KNOWLEDGE_STEPS_PALETTE
        from core.orchestration.context import _fill_task_template

        state = {"knowledge_steps": KNOWLEDGE_STEPS_PALETTE if palette else ""}
        drafted = _fill_task_template(self._steps()["step_plan"]["prompt_template"], state, {})
        assert ("**retrieve**" in drafted) is palette and ('"kb_ids"' in drafted) is palette

    async def test_the_run_starts_with_the_palette_only_while_on(self, monkeypatch):
        from core.native_builder import KNOWLEDGE_STEPS_PALETTE
        from core.routes.builder import _knowledge_steps

        use_saved_settings(monkeypatch)
        assert _knowledge_steps() == ""
        await enable_knowledge()
        assert _knowledge_steps() == KNOWLEDGE_STEPS_PALETTE
