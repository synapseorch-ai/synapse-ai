"""A plan runs once on made-up answers before the person sees it, and what a step reads and doesn't get goes back to
the planner (F1619).

The plan check judges a read against what the steps before it declare, so it can't see what code makes, or a read
deeper in an answer than its first field: a Transform's `r['temperature']` over items that have `temp_c`, a Loop over
a Transform's object, a tool argument naming a field a Transform never writes. The practice run makes each declared
answer up from its shape, runs the plan's code with no network, and reads what each step reads. What it finds goes
back to the planner up to the plan check's own sendbacks, then the plan goes on and its card says each one: never a
block, as made-up answers can be wrong where real ones are right. A miss about an MCP server's declared answer is a
card note only (F1620's rule). A failure that depends on the made-up value itself (a 0 divided by) says nothing.

Code here runs on this computer for the test (`_Here`); the product runs it only where no network is promised.
"""
from __future__ import annotations

import dataclasses

import pytest

from core.native_builder import build_plan, practice_run

CITIES = {"type": "object", "properties": {"data": {"type": "array", "items": {
    "type": "object", "properties": {"name": {"type": "string"}, "temp_c": {"type": "number"}},
    "required": ["name", "temp_c"], "additionalProperties": False}}},
    "required": ["data"], "additionalProperties": False}


def _ws(owner: str | None = "custom") -> build_plan.Workspace:
    tool = {"name": "list_cities", "does": "Lists cities with today's temperature", "outputSchema": CITIES,
            **({"answer_owner": owner} if owner else {})}
    return build_plan.Workspace(tools=[tool, {"name": "send_note", "does": "Sends a note"},
                                       {"name": "undeclared", "does": "Answers something"}])


FETCH = {"id": "fetch", "type": "tool", "name": "List the cities", "does": "Lists cities", "tool": "list_cities",
         "next": "shape"}


def _code(code: str, nxt: str = "end", sid: str = "shape") -> dict:
    return {"id": sid, "type": "transform", "name": "Shape the rows", "does": "Shapes the rows",
            "config": {"transform_code": code}, "next": nxt}


def _plan(*steps: dict) -> dict:
    return {"kind": "create", "title": "City temperatures", "summary": "Today's temperature for each city.",
            "steps": list(steps)}


class _Here:
    """Runs a practice's code on this computer, as a test may: the product never runs it where no network is
    promised."""

    def __init__(self):
        self.calls = 0

    async def run(self, request):
        from core.execution import HostRunner, Network

        self.calls += 1
        return await HostRunner().run(dataclasses.replace(request, network=Network.INTERNET))


@pytest.fixture
def here():
    runner = _Here()
    practice_run.set_runner(runner)
    yield runner
    practice_run.set_runner(practice_run.OFF)


async def _found(plan: dict, ws: build_plan.Workspace | None = None, state: dict | None = None):
    return await build_plan.examine(plan, None, ws=ws or _ws(), state={"user_message": "x"} if state is None
                                    else state)


WRONG_FIELD = "rows = state.fetch['data']\nresult = [{'city': r['name'], 't': r['temperature']} for r in rows]"


class TestWhatGoesBack:
    async def test_code_reading_a_field_the_items_lack_goes_back_with_what_it_read(self, here):
        found = await _found(_plan(FETCH, _code(WRONG_FIELD)))
        assert len(found.practice) == 1, found.practice
        line = found.practice[0]
        assert line.startswith("step shape: its code stopped on made-up answers"), line
        assert "temperature" in line and "practice run" in line
        # Never a block: the plan's own errors are not it.
        assert not found.errors

    async def test_a_loop_over_an_object_code_makes_goes_back(self, here):
        loop = {"id": "each", "type": "loop", "name": "Each city", "does": "Goes through each city",
                "loop_over": "state.shape", "body": ["note"], "next": "end"}
        note = {"id": "note", "type": "tool", "name": "Send a note", "does": "Sends one note", "tool": "send_note",
                "config": {"tool_arguments": {"text": "{{state.loop_item}}"}}, "next": "end"}
        code = "result = {'rows': [{'city': r['name']} for r in state.fetch['data']]}"
        found = await _found(_plan(FETCH, _code(code, "each"), loop, note))
        assert any(p.startswith("step each:") and "an object (fields: rows)" in p and "state.shape.rows" in p
                   for p in found.practice), found.practice

    async def test_a_tool_argument_naming_a_field_code_never_writes_goes_back(self, here):
        note = {"id": "note", "type": "tool", "name": "Send a note", "does": "Sends the warmest city",
                "tool": "send_note", "config": {"tool_arguments": {"text": "{{state.shape.warmest}}"}}, "next": "end"}
        code = "result = {'hottest': max(state.fetch['data'], key=lambda r: r['temp_c'])['name']}"
        found = await _found(_plan(FETCH, _code(code, "note"), note))
        assert any(p.startswith("step note: its arguments read `{{state.shape.warmest}}`") and "`hottest`" in p
                   for p in found.practice), found.practice

    async def test_a_condition_on_a_field_code_never_writes_goes_back(self, here):
        decide = {"id": "decide", "type": "if_else", "name": "Warm?", "does": "Checks the warmth",
                  "condition": "state.shape.is_warm == True", "branches": {"true": "end", "false": "end"}}
        found = await _found(_plan(FETCH, _code("result = {'warm': True}", "decide"), decide))
        assert any(p.startswith("step decide: its condition reads `shape.is_warm`") and "`warm`" in p
                   for p in found.practice), found.practice

    async def test_each_arm_runs_once_so_a_miss_on_the_arm_not_taken_is_found(self, here):
        decide = {"id": "decide", "type": "if_else", "name": "Any cities?", "does": "Checks for cities",
                  "condition": "len(state.fetch['data']) > 0", "branches": {"true": "end", "false": "shape"}}
        found = await _found(_plan({**FETCH, "next": "decide"}, decide, _code(WRONG_FIELD)))
        assert any(p.startswith("step shape:") for p in found.practice), found.practice

    async def test_a_vault_file_is_read_for_its_shape_and_its_values_stay_here(self, here, monkeypatch):
        real = "city,budget_usd\nPorto,1200\nLisbon,980\n"

        async def file_text(path):
            return real if path == "budgets.csv" else None

        monkeypatch.setattr(build_plan, "_vault_shape_text", file_text)
        sent = []
        runner_run = here.run

        async def watched(request):
            sent.append(request.code)
            return await runner_run(request)

        monkeypatch.setattr(here, "run", watched)
        code = ("import csv, io\nrows = list(csv.DictReader(io.StringIO(read_vault('budgets.csv'))))\n"
                "result = sum(float(r['budget']) for r in rows)")
        found = await _found(_plan(_code(code)))
        # Its columns are read here, never said: what goes back names the read, the file's own words stay here.
        assert any("KeyError: 'budget'" in p for p in found.practice), found.practice
        assert not any("budget_usd" in p for p in found.practice)
        assert sent and not any(v in c for c in sent for v in ("Porto", "1200", "Lisbon", "980"))


class TestWhatStaysQuiet:
    @pytest.mark.parametrize("code", [
        # The made-up value itself: a 0 divided by, made-up text read as a number.
        "rows = state.fetch['data']\nresult = 1 / (len(rows) - 2)",
        "result = float(state.fetch['data'][0]['name'])",
        # A read that says what it does when a field is absent.
        "result = [r.get('temperature', 0) for r in state.fetch['data']]",
        # A filter that keeps nothing of made-up rows.
        "result = [r for r in state.fetch['data'] if r['temp_c'] > 100]",
    ])
    async def test_a_value_failure_a_guarded_read_and_an_empty_filter_say_nothing(self, here, code):
        found = await _found(_plan(FETCH, _code(code)))
        assert found.practice == [] and not [n for n in found.notes if "made-up" in n], found.practice

    async def test_code_over_an_answer_nothing_declares_is_not_run(self, here):
        fetch = {**FETCH, "tool": "undeclared"}
        found = await _found(_plan(fetch, _code("result = state.fetch['anything']['deep']")))
        assert found.practice == [] and here.calls == 0

    async def test_a_miss_in_an_mcp_servers_declared_answer_is_a_card_note_never_sent_back(self, here):
        found = await _found(_plan(FETCH, _code(WRONG_FIELD)), ws=_ws(owner=None))
        assert found.practice == []
        assert any(n.startswith("Shape the rows: its code stopped on made-up answers") and "may be wrong" in n
                   for n in found.notes), found.notes

    async def test_no_runner_here_leaves_code_out_and_says_nothing(self):
        # The suite's own runner (conftest): none.
        found = await _found(_plan(FETCH, _code(WRONG_FIELD)))
        assert found.practice == [] and not [n for n in found.notes if "practi" in n]

    async def test_a_runner_that_cant_promise_no_network_is_said_as_not_practised(self):
        from core.execution import ExecUnsupported

        class _Open:
            async def run(self, request):
                raise ExecUnsupported("no network can't be promised here")

        practice_run.set_runner(_Open())
        try:
            found = await _found(_plan(FETCH, _code(WRONG_FIELD)))
        finally:
            practice_run.set_runner(practice_run.OFF)
        assert found.practice == []
        assert "Shape the rows: its code was not practised, as this install can't run code with no network" \
            in found.notes

    async def test_one_plan_checked_again_in_a_run_runs_its_code_once_and_never_for_another_run(self, here,
                                                                                               monkeypatch):
        from core.models_orchestration import OrchestrationRun

        plan = _plan(FETCH, _code(WRONG_FIELD))
        for run_id in ("builder_one", "builder_two"):
            run = OrchestrationRun(run_id=run_id, orchestration_id="orch_native_builder",
                                   shared_state={"user_message": "x"})
            monkeypatch.setattr(build_plan, "_run", lambda run=run: (run.run_id, run))
            before = dict(run.shared_state)
            first = await _found(plan, state=run.shared_state)
            second = await _found(plan, state=run.shared_state)
            assert first.practice == second.practice and first.practice
            # Kept beside the run, never in it: the planner's own check writes nothing to the run.
            assert run.shared_state == before
        # Once in each run: a practice can name what one workspace's Vault file holds, so no run reads another's.
        assert here.calls == 2


class TestTheSendbacks:
    async def test_sent_back_twice_then_the_plan_goes_on_with_its_card_saying_it(self, here, monkeypatch):
        from core.models_orchestration import OrchestrationRun
        from core.orchestration.engine import live

        async def ws(server_module):
            return _ws()

        monkeypatch.setattr(build_plan, "workspace", ws)
        run = OrchestrationRun(run_id="builder_practice", orchestration_id="orch_native_builder",
                               shared_state={"intent": "create"})
        monkeypatch.setattr(build_plan, "_run", lambda: (run.run_id, run))
        plan = _plan(FETCH, _code(WRONG_FIELD))
        with live(run):
            for _ in range(build_plan.SENDBACKS):
                out = await build_plan.submit_plan(plan, None, [])
                assert "step shape: its code stopped on made-up answers" in out.get("error", ""), out
            out = await build_plan.submit_plan(plan, None, [])
            assert out.get("finish") == "submit_plan", out
            found = await build_plan.examine(plan, None, ws=_ws(), state=run.shared_state)
        assert any(n.startswith("Shape the rows: its code stopped on made-up answers") for n in found.notes)
        assert not found.errors
