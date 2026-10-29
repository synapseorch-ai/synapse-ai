"""A big step output is passed on as a file in the vault, not as text every reader retypes or pastes (D339 P4).

Prod, 10-29 (TRI1's triage part 2, S4): a crawling agent retyped 39 pages into its answer, and every later step read
that answer in full — in its prompt, in the context of what earlier steps did, and in the run's answer. Now a tool or
code step's output over the spill line is saved to the vault, and state holds a small reference to the file:

* code reads it as the value it was — a condition, a set argument, a tool's template, a transform;
* a model's prompt has it inline when it fits that step's read share, and otherwise the file to read;
* a person's text (a Print, a Human prompt, the run's answer) shows an excerpt and the file's key.

The native builder's own run is left as it is: its code reads its tool outputs straight from state.
"""
from __future__ import annotations

import json
import types

import pytest

from core import step_refs, templating
from core.storage import set_blob_store
from core.storage.base import tenant_key

PAGE = "word " * 30_000  # 150,000 characters
ROWS = [{"id": i, "title": f"Order {i}", "note": "x" * 200} for i in range(600)]


class _Pathless:
    """A blob store with no filesystem, like S3: what is written is in the store, or nowhere."""

    def __init__(self):
        self.blobs: dict[str, str] = {}

    def put(self, key, content):
        self.blobs[tenant_key(key)] = content

    def get(self, key):
        return self.blobs.get(tenant_key(key))

    def exists(self, key):
        return tenant_key(key) in self.blobs

    def delete(self, key):
        self.blobs.pop(tenant_key(key), None)

    def list(self, prefix=""):
        scoped, cut = tenant_key(prefix), tenant_key("")
        return [k[len(cut):] for k in self.blobs if k.startswith(scoped)]

    def path_for(self, key):
        return None


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNAPSE_SCRATCH_DIR", str(tmp_path / "scratch"))
    step_refs.forget_cache()
    blobs = _Pathless()
    set_blob_store(blobs)
    yield blobs
    set_blob_store(None)
    step_refs.forget_cache()


class TestTheReference:
    def test_a_saved_output_is_a_small_reference_that_reads_back_as_it_was(self, store):
        ref = step_refs.save("run_1", "step_fetch", PAGE)
        assert step_refs.is_ref(ref) and ref["chars"] == len(PAGE) and ref["kind"] == "text"
        assert ref["$vault"].startswith("step_outputs/run_1/step_fetch/")
        assert len(json.dumps(ref)) < 2_000
        step_refs.forget_cache()
        assert step_refs.load(ref) == PAGE, "read from the store, not only from this process's cache"

    def test_an_object_reads_back_as_the_object_with_an_outline_beside_it(self, store):
        ref = step_refs.save("run_1", "step_rows", ROWS)
        assert ref["kind"] == "json" and step_refs.load(ref) == ROWS
        assert ref.get("index"), "a model deciding what to read is told what the file holds"

    def test_only_an_output_over_the_line_is_saved(self, store):
        assert step_refs.spill("r", "s", "short", at=100) == "short"
        assert step_refs.spill("r", "s", None, at=10) is None
        assert step_refs.spill("r", "s", {"a": 1}, at=100) == {"a": 1}
        assert step_refs.is_ref(step_refs.spill("r", "s", PAGE, at=100_000))
        assert step_refs.is_ref(step_refs.spill("r", "s", ROWS, at=100_000))
        assert not store.blobs or len(store.blobs) == 2

    def test_a_reference_is_never_saved_again(self, store):
        ref = step_refs.save("r", "s", PAGE)
        assert step_refs.spill("r", "s2", ref, at=10) == ref

    def test_a_secret_the_run_holds_never_reaches_the_vault(self, store):
        from core.secret_scrub import hold_secrets

        key = "sk-" + "A1b2C3d4" * 4
        hold_secrets({"SHOP_KEY": key})
        try:
            step_refs.save("r", "s", f"token {key} end " + PAGE)
        finally:
            hold_secrets({})
        saved = "".join(store.blobs.values())
        assert key not in saved and "SHOP_KEY" in saved

    def test_a_key_never_climbs_out_of_its_folder(self, store):
        ref = step_refs.save("../../etc", "../passwd", PAGE)
        assert ".." not in ref["$vault"] and ref["$vault"].startswith("step_outputs/")

    def test_a_missing_file_says_so_rather_than_reading_empty(self, store):
        ref = step_refs.save("r", "s", PAGE)
        store.blobs.clear()
        step_refs.forget_cache()
        with pytest.raises(step_refs.MissingOutput, match=ref["$vault"]):
            step_refs.load(ref)


class TestCodeReadsTheValueItWas:
    def test_a_condition_reads_a_field_of_a_saved_object(self, store):
        from core.expressions import evaluate_expression

        state = {"orders": step_refs.save("r", "s", {"count": 600, "rows": ROWS})}
        result = evaluate_expression("state.orders.count > 500", state, None)
        assert result.error is None and result.value is True

    def test_a_set_argument_gets_the_content(self, store):
        from core.orchestration.steps import _state_value

        state = {"page": step_refs.save("r", "s", PAGE), "orders": step_refs.save("r", "s2", {"rows": ROWS})}
        assert _state_value(state, "page") == PAGE
        assert _state_value(state, "orders.rows") == ROWS

    def test_a_tools_template_gets_the_content(self, store):
        context = {"state": {"page": step_refs.save("r", "s", PAGE),
                             "o": step_refs.save("r", "s2", {"a": {"b": "deep"}, "pad": PAGE})}}
        assert templating.render("{{ state.page }}", context) == PAGE
        assert templating.render("{{ state.o.a.b }}", context) == "deep"

    def test_a_transform_is_given_the_values(self, store):
        ref = step_refs.save("r", "s", PAGE)
        assert step_refs.resolved_state({"page": ref, "n": 1}) == {"page": PAGE, "n": 1}


class TestAModelIsGivenAFileItCanRead:
    def test_a_prompt_has_an_output_inline_when_it_fits_its_read_share(self, store):
        from core.orchestration.context import _fill_task_template

        ref = step_refs.save("r", "s", "x" * 5_000)
        assert _fill_task_template("{{ state.page }}", {"page": ref}, {}, read_cap=20_000) == "x" * 5_000

    def test_a_prompt_names_the_file_when_it_does_not_fit(self, store):
        from core.orchestration.context import _fill_task_template

        ref = step_refs.save("r", "s", PAGE)
        out = _fill_task_template("Summarise this: {{ state.page }}", {"page": ref}, {}, read_cap=20_000)
        assert out.startswith("Summarise this: ") and ref["$vault"] in out and "read_file" in out
        assert len(out) < 2_000 and "word word word word" not in out

    def test_a_field_of_a_saved_object_is_its_value(self, store):
        from core.orchestration.context import _fill_task_template

        ref = step_refs.save("r", "s", {"title": "Q3 report", "rows": ROWS})
        assert _fill_task_template("{{ state.doc.title }}", {"doc": ref}, {}, read_cap=20_000) == "Q3 report"

    def test_an_earlier_steps_output_in_the_context_is_the_file(self, store):
        from core.orchestration.context import _format_context_value

        ref = step_refs.save("r", "s", PAGE)
        out = _format_context_value("page", ref, "Fetch the pages", max_chars=5_000)
        assert ref["$vault"] in out and len(out) < 2_000

    def test_a_loops_saved_results_are_files_too(self, store):
        from core.orchestration.context import _format_context_value

        refs = [step_refs.save("r", f"s{i}", PAGE) for i in range(3)]
        out = _format_context_value("pages", [{"iteration": i + 1, "result": r} for i, r in enumerate(refs)],
                                    "Fetch", max_chars=5_000)
        assert all(r["$vault"] in out for r in refs) and len(out) < 6_000

    def test_a_note_an_agent_finished_with_is_kept_beside_its_file(self, store):
        ref = step_refs.save("r", "s", PAGE, note="The 39 pages, one per section.")
        out = step_refs.for_prompt(ref, 20_000)
        assert "The 39 pages, one per section." in out and ref["$vault"] in out


class TestAPersonSeesAnExcerptAndTheFile:
    def test_a_print_shows_an_excerpt_and_the_files_key(self, store):
        from core.orchestration.steps import render_state_refs

        ref = step_refs.save("r", "s", PAGE)
        out = render_state_refs("Fetched: {{ state.page }}", {"page": ref})
        assert out.startswith("Fetched: word word") and ref["$vault"] in out and len(out) < 2_000

    def test_a_field_of_a_saved_object_is_its_value(self, store):
        from core.orchestration.steps import render_state_refs

        ref = step_refs.save("r", "s", {"title": "Q3 report", "rows": ROWS})
        assert render_state_refs("{{ state.doc.title }}", {"doc": ref}) == "Q3 report"

    def test_the_runs_answer_is_an_excerpt_and_the_file(self, store):
        from core.orchestration.engine import OrchestrationEngine

        ref = step_refs.save("r", "s", PAGE)
        run = types.SimpleNamespace(status="completed", step_history=[{"step_id": "fetch", "output_key": "page"}],
                                    shared_state={"page": ref}, error=None)
        out = OrchestrationEngine._build_final_response(run, "page")
        assert ref["$vault"] in out and len(out) < 2_000


# ── where an output is saved: a run, in-process and through the fleet's jobs ─────────────────────────────────────────

def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


@pytest.fixture
def big_tool(monkeypatch):
    """A workspace HTTP tool, `fetch_page`, that answers with PAGE."""
    from core.orchestration import resolved
    from core.orchestration.steps import ToolStepExecutor

    async def run_custom_tools(engine):
        return [{"name": "fetch_page", "tool_type": "http", "url": "http://api.test/page", "method": "GET"}]

    async def _execute_tool(self, name, args, engine, resource_events=None, run_id=None):
        return PAGE

    monkeypatch.setattr(resolved, "run_custom_tools", run_custom_tools)
    monkeypatch.setattr(ToolStepExecutor, "_execute_tool", _execute_tool)


async def _final_state(orch: dict, run_id: str) -> tuple[list[dict], dict]:
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
    events = [e async for e in engine.run("go", run_id=run_id)]
    done = [e for e in events if e.get("type") == "orchestration_complete"][-1]
    assert done.get("status") == "completed", [e for e in events if "error" in str(e.get("type"))]
    return events, done["final_state"]


def _workflow(orch_id: str) -> dict:
    from _fakes import seed as S

    return S.make_orchestration(id=orch_id, entry_step_id="fetch", steps=[
        {"id": "fetch", "name": "Fetch", "type": "tool", "forced_tool": "fetch_page", "tool_arguments": {},
         "output_key": "page", "next_step_id": "say"},
        {"id": "say", "name": "Say", "type": "print", "print_content": "Got: {{ state.page }}",
         "output_key": "said", "next_step_id": None},
    ])


class TestWhereAnOutputIsSaved:
    async def test_a_tool_steps_big_output_is_a_file_in_state(self, store, big_tool):
        events, state = await _final_state(_workflow("orch_p4_tool"), "run_p4_tool")
        assert step_refs.is_ref(state["page"]) and step_refs.load(state["page"]) == PAGE
        assert state["said"].startswith("Got: word word") and len(state["said"]) < 2_000
        previews = [e["preview"] for e in events if e.get("type") == "tool_result"]
        assert previews and len(previews[0]) <= 2_000

    async def test_the_native_builders_run_keeps_its_values(self, store, big_tool):
        from core.native_builder.seed import NATIVE_BUILDER_ORCH_ID

        _events, state = await _final_state(_workflow(NATIVE_BUILDER_ORCH_ID), "run_p4_native")
        assert state["page"] == PAGE
