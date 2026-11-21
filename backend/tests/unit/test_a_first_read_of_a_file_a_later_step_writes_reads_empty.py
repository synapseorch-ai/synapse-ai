"""A Vault file a workflow reads before its own first write reads as empty on the first run (F1692).

A workflow that watches a price read its last price from a Vault file, compared, then wrote the new price to that file.
On its first run the file was not there yet, the read failed, and the run failed on its first step that read it. Now
such a read says it may find no file (`missing_ok`): the plan sets it where a later step writes the file and no step
writes it before the read on every way there, and the card says the first run reads it as empty; the read tool reads a
missing file as empty when asked to; and checking a workflow that reads before its first write says so.

Near misses: a read after the step that writes the file stays strict, and so does a read of a file no step writes
(the person's own file, which a missing one should fail on); a path built from the run's values is not judged.
"""
from __future__ import annotations

import json

import pytest

PRICE = "allbirds_price.txt"


def _tool(sid: str, tool: str, nxt: str | None, **args) -> dict:
    return {"id": sid, "name": sid.replace("_", " ").title(), "type": "tool", "forced_tool": tool,
            "tool_arguments": args, "output_key": sid, "next_step_id": nxt}


def _watch(first: str = "read_last") -> dict:
    """The price watcher: fetch, read the last price, compare, write the new one."""
    steps = [_tool("fetch", "scrape_url", "read_last", url="https://example.com/p"),
             _tool("read_last", "vault_read", "compare", path=PRICE),
             {"id": "compare", "name": "Compare", "type": "llm", "prompt_template": "{{ state.read_last }}",
              "output_key": "compare", "next_step_id": "save"},
             _tool("save", "vault_write", None, path=PRICE, content="{{ state.compare }}")]
    return {"id": "orch_f1692", "name": "Watch the price", "entry_step_id": "fetch", "steps": steps}


def _reads():
    from core.orchestration.vault_reads import first_reads

    return first_reads


class TestWhichReadsComeFirst:
    def test_a_read_before_the_steps_own_first_write_is_found(self):
        assert _reads()(_watch()) == [("read_last", PRICE)]

    def test_a_read_after_the_write_stays_strict(self):
        orch = _watch()
        steps = {s["id"]: s for s in orch["steps"]}
        steps["fetch"]["next_step_id"], steps["save"]["next_step_id"] = "save", "read_last"
        steps["save"]["tool_arguments"]["content"] = "{{ state.fetch }}"
        steps["read_last"]["next_step_id"], steps["compare"]["next_step_id"] = "compare", None
        assert _reads()(orch) == []

    def test_a_read_of_a_file_no_step_writes_stays_strict(self):
        orch = _watch()
        orch["steps"] = [s for s in orch["steps"] if s["id"] != "save"]
        orch["steps"][2]["next_step_id"] = None
        assert _reads()(orch) == []

    def test_a_read_already_marked_is_not_found_again(self):
        orch = _watch()
        orch["steps"][1]["tool_arguments"]["missing_ok"] = True
        assert _reads()(orch) == []

    def test_a_path_from_the_runs_values_is_not_judged(self):
        orch = _watch()
        for s in orch["steps"]:
            if s.get("forced_tool") in ("vault_read", "vault_write"):
                s["tool_arguments"]["path"] = "{{ state.fetch.name }}.txt"
        assert _reads()(orch) == []

    def test_the_same_file_written_on_one_branch_only_is_found(self):
        orch = _watch()
        steps = orch["steps"]
        steps[0]["next_step_id"] = "gate"
        steps.insert(1, {"id": "gate", "name": "Gate", "type": "if_else", "if_condition": "state.fetch",
                         "if_true_step_id": "seed", "if_false_step_id": "read_last"})
        steps.insert(2, _tool("seed", "vault_write", "read_last", path=PRICE, content="0"))
        assert _reads()(orch) == [("read_last", PRICE)]


class TestTheReadTool:
    @pytest.fixture
    def vault(self, tmp_path):
        from core.storage import LocalBlobStore, get_blob_store, set_blob_store

        before = get_blob_store()
        set_blob_store(LocalBlobStore(tmp_path))
        from core.vault_backend import get_vault

        root = get_vault().root
        root.mkdir(parents=True, exist_ok=True)
        yield root
        set_blob_store(before)

    @staticmethod
    def _json(result) -> dict:
        return json.loads(result[0].text)

    def test_a_missing_file_asked_for_with_missing_ok_reads_as_empty(self, vault):
        from tools import sandbox

        out = self._json(sandbox._handle_read({"path": PRICE, "missing_ok": True}))
        assert out.get("content") == "" and out.get("missing") is True and out.get("path") == PRICE, out

    def test_without_it_a_missing_file_still_fails(self, vault):
        from tools import sandbox

        assert "File not found" in self._json(sandbox._handle_read({"path": PRICE})).get("error", "")

    def test_a_file_that_is_there_reads_as_it_is_either_way(self, vault):
        from tools import sandbox

        (vault / PRICE).write_text("$70")
        out = self._json(sandbox._handle_read({"path": PRICE, "missing_ok": True}))
        assert out.get("content") == "$70" and not out.get("missing"), out

    def test_the_tool_says_it_takes_it(self):
        import asyncio

        from tools import sandbox

        tools = asyncio.run(sandbox.list_tools()) if asyncio.iscoroutinefunction(sandbox.list_tools) \
            else sandbox.list_tools()
        read = next(t for t in tools if t.name == "vault_read")
        assert "missing_ok" in read.inputSchema["properties"], read.inputSchema


class TestCheckingAWorkflowSaysIt:
    def test_a_read_before_the_first_write_is_warned_with_its_fix(self):
        from core.builder_tools import _validate_orchestration

        said = [w for w in _validate_orchestration(_watch())["warnings"] if PRICE in w]
        assert said and "Read Last" in said[0] and "missing_ok" in said[0] and "first run" in said[0], said

    def test_a_read_after_the_write_is_not_warned(self):
        from core.builder_tools import _validate_orchestration

        orch = _watch()
        orch["steps"][1]["tool_arguments"]["missing_ok"] = True
        assert not [w for w in _validate_orchestration(orch)["warnings"] if PRICE in w]


class TestThePlanSetsIt:
    def _plan(self):
        from core.native_builder import objects as O

        return O.Plan.model_validate({
            "kind": "create", "title": "Watch the price", "summary": "Watch a price.", "steps": [
                {"id": "fetch", "type": "tool", "name": "Fetch", "does": "fetch the page", "tool": "scrape_url",
                 "next": "read_last", "config": {"tool_arguments": {"url": "https://example.com/p"}}},
                {"id": "read_last", "type": "tool", "name": "Read last price", "does": "read the last price",
                 "tool": "vault_read", "next": "save", "config": {"tool_arguments": {"path": PRICE}}},
                {"id": "save", "type": "tool", "name": "Save price", "does": "save the price", "tool": "vault_write",
                 "next": "end", "config": {"tool_arguments": {"path": PRICE, "content": "{{ state.fetch }}"}}}]})

    def test_the_plan_marks_the_first_read_and_the_card_says_it(self):
        from core.native_builder.build_plan import first_reads_kept_empty

        plan, notes = first_reads_kept_empty(self._plan())
        read = next(s for s in plan.steps if s.id == "read_last")
        assert read.config["tool_arguments"].get("missing_ok") is True, read.config
        assert any("Read last price" in n and "first run" in n and "empty" in n for n in notes), notes
        save = next(s for s in plan.steps if s.id == "save")
        assert "missing_ok" not in save.config["tool_arguments"], save.config

    def test_a_plan_that_writes_before_it_reads_is_left_as_it_is(self):
        from core.native_builder.build_plan import first_reads_kept_empty

        plan = self._plan()
        steps = {s.id: s for s in plan.steps}
        steps["fetch"].next, steps["save"].next, steps["read_last"].next = "save", "read_last", "end"
        out, notes = first_reads_kept_empty(plan)
        assert notes == [] and "missing_ok" not in next(s for s in out.steps if s.id == "read_last").config[
            "tool_arguments"]

    def test_the_plan_check_runs_it(self):
        import inspect

        from core.native_builder import build_plan

        assert "first_reads_kept_empty(" in inspect.getsource(build_plan.examine)
