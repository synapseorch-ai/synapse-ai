"""A run says when what it made is empty, built from a cut answer, or a CSV with no rows (F1690).

A run read Wikipedia's list of countries with the web reader, which keeps a page's first 50,000 characters and marks
the rest as cut; the list's rows were in the cut part, so the parse found none and the run wrote a CSV holding only its
header. It said "completed" and nothing more. Now the step whose answer was cut, the step that made nothing and the
step that wrote a CSV with no rows each warn, in the run's log and on the step's own record, and the run reads
"completed with warnings".

Near misses: a page that merely quotes the cut marker, a CSV with rows, an append, a file that is not a CSV, and a
typed answer whose empty field the plan says may be empty are all said as they are; a clean run warns of nothing.
"""
from __future__ import annotations

import json
import types

import pytest


def RH():
    from core.orchestration import result_honesty

    return result_honesty


MARK = "\n\n[...truncated — 374479 chars omitted]"
PAGE = "| Afghanistan | Kabul |" * 100


class TestACutAnswer:
    def test_a_reader_answer_cut_short_is_said_with_how_much_was_read(self):
        said = RH().cut_said("Fetch page", {"success": True, "url": "https://example.com", "markdown": PAGE + MARK})
        assert said and "Fetch page" in said and f"{len(PAGE):,}" in said and f"{len(PAGE) + 374479:,}" in said, said
        assert "partial" in said, said

    def test_an_answer_as_json_text_is_read_too(self):
        said = RH().cut_said("Fetch page", json.dumps({"markdown": PAGE + MARK}, ensure_ascii=False))
        assert said and f"{len(PAGE):,}" in said, said

    @pytest.mark.parametrize("value", [
        {"markdown": PAGE},
        {"markdown": 'The log said "[...truncated — 5 chars omitted]" and the job went on.'},
        "a page that ends [...truncated — 12 chars omitted] here, mid-sentence, as a quote",
        [],
    ])
    def test_an_answer_that_was_not_cut_says_nothing(self, value):
        assert RH().cut_said("Fetch page", value) is None


class TestAnEmptyResult:
    @pytest.mark.parametrize("value", ["", "   \n", [], {}, {"rows": [], "note": ""}])
    def test_a_step_that_made_nothing_says_so(self, value):
        said = RH().empty_said(types.SimpleNamespace(name="Parse countries", output_fields=None), value)
        assert said and "Parse countries" in said and "empty" in said, said

    @pytest.mark.parametrize("value", ["name\nFrance\n", [0], {"count": 0}, {"rows": [{"name": "France"}]}, 0, False])
    def test_a_step_that_made_something_says_nothing(self, value):
        assert RH().empty_said(types.SimpleNamespace(name="Parse countries", output_fields=None), value) is None

    def test_a_field_the_plan_says_may_be_empty_is_never_warned(self):
        step = types.SimpleNamespace(name="Find late orders",
                                     output_fields=[{"name": "late", "type": "list", "may_be_empty": True}])
        assert RH().empty_said(step, {"late": []}) is None
        strict = types.SimpleNamespace(name="Find late orders", output_fields=[{"name": "late", "type": "list"}])
        assert RH().empty_said(strict, {"late": []}), "a field the plan expects filled is said when it is empty"


class TestAHeaderOnlyCsv:
    @pytest.mark.parametrize("content", ["name\n", "name,capital", "name,capital\n\n"])
    def test_a_csv_written_with_only_its_header_is_said(self, content):
        said = RH().csv_said("Save CSV", {"path": "countries/sovereign_states.csv", "content": content})
        assert said and "sovereign_states.csv" in said and "no rows" in said, said

    @pytest.mark.parametrize("args", [
        {"path": "sovereign_states.csv", "content": "name\nFrance\n"},
        {"path": "prices.csv", "content": "2026-11-18,70\n", "append": True},
        {"path": "notes.txt", "content": "name\n"},
        {"path": "sovereign_states.csv"},
        {"url": "https://example.com/x.csv"},
    ])
    def test_a_csv_with_rows_an_append_or_another_file_says_nothing(self, args):
        assert RH().csv_said("Save CSV", args) is None

    def test_a_tsv_is_a_csv_here(self):
        assert RH().csv_said("Save", {"path": "a.TSV", "content": "name\tcapital\n"})


# ── through the engine ────────────────────────────────────────────────────────────────────────────────────────────

def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


ANSWERS = {"fetch_page": json.dumps({"success": True, "markdown": PAGE + MARK}, ensure_ascii=False),
           "parse_rows": "[]", "save_file": json.dumps({"path": "sovereign_states.csv", "size": 5, "written": True}),
           "fetch_whole": json.dumps({"success": True, "markdown": PAGE}), "parse_some": '["France"]'}


@pytest.fixture
def tools(monkeypatch):
    from core.orchestration import resolved
    from core.orchestration.steps import ToolStepExecutor

    async def run_custom_tools(engine):
        return [{"name": n, "tool_type": "http", "url": f"http://api.test/{n}", "method": "GET"} for n in ANSWERS]

    async def _execute_tool(self, name, args, engine, resource_events=None, run_id=None):
        return ANSWERS[name]

    monkeypatch.setattr(resolved, "run_custom_tools", run_custom_tools)
    monkeypatch.setattr(ToolStepExecutor, "_execute_tool", _execute_tool)


def _workflow(orch_id: str, fetch: str, parse: str, content: str) -> dict:
    from _fakes import seed as S

    return S.make_orchestration(id=orch_id, entry_step_id="fetch", steps=[
        {"id": "fetch", "name": "Fetch page", "type": "tool", "forced_tool": fetch, "tool_arguments": {},
         "output_key": "page", "next_step_id": "parse"},
        {"id": "parse", "name": "Parse countries", "type": "tool", "forced_tool": parse, "tool_arguments": {},
         "output_key": "rows", "next_step_id": "save"},
        {"id": "save", "name": "Save CSV", "type": "tool", "forced_tool": "save_file",
         "tool_arguments": {"path": "sovereign_states.csv", "content": content}, "output_key": "saved",
         "next_step_id": None},
    ])


async def test_a_run_built_on_a_cut_page_says_each_step_that_warns(tools):
    from core.orchestration.engine import OrchestrationEngine
    from core.models_orchestration import Orchestration

    engine = OrchestrationEngine(Orchestration.model_validate(
        _workflow("orch_f1690_cut", "fetch_page", "parse_rows", "name\n")), _server())
    events = [e async for e in engine.run("go", run_id="run_f1690_cut")]
    warned = {e.get("orch_step_id"): e["message"] for e in events if e.get("type") == "step_warning"}
    assert set(warned) >= {"fetch", "parse", "save"}, warned
    assert "partial" in warned["fetch"] and "empty" in warned["parse"] and "no rows" in warned["save"], warned
    done = [e for e in events if e.get("type") == "orchestration_complete"][-1]
    assert done["status"] == "completed" and done.get("warnings") == 3, done.get("warnings")
    from core.orchestration.state import SharedState

    history = (await SharedState.restore("run_f1690_cut")).run.step_history
    noted = {h["step_id"]: h.get("result_notes") for h in history if isinstance(h, dict)}
    assert all(noted.get(s) for s in ("fetch", "parse", "save")), noted


async def test_a_clean_run_warns_of_nothing(tools):
    from core.orchestration.engine import OrchestrationEngine
    from core.models_orchestration import Orchestration

    engine = OrchestrationEngine(Orchestration.model_validate(
        _workflow("orch_f1690_clean", "fetch_whole", "parse_some", "name\nFrance\n")), _server())
    events = [e async for e in engine.run("go", run_id="run_f1690_clean")]
    assert not [e for e in events if e.get("type") == "step_warning"], [e for e in events if e.get("type") == "step_warning"]
    done = [e for e in events if e.get("type") == "orchestration_complete"][-1]
    assert done["status"] == "completed" and "warnings" not in done, done.get("warnings")
