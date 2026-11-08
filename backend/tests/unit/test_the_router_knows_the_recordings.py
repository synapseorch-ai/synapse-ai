"""The router and the change route know the workspace's recordings (F1231; BRW1, D328; CAP3's stage 1, brw-01, brw-03
and brw-04).

"Every Monday at 9, run my Confirm a delivery recording", "for each order … run Confirm a delivery", "run Confirm a
delivery for the order I give": the quick model labelled each a *change* ("run X"), and no code rule knew a
recording, so nothing corrected it. The change route knew no recordings either and asked "which workflow?".

A recording is replayed by a workflow's Browser step (F1193), so a request to run, schedule or repeat one is a build
(Naveen, 10-29: a one-off run too). Code knows the recordings by name (`_inventory`), and a message that names one is
a build unless it is a question or an edit of the recording itself, which the app does. A name that is both a
recording and a workflow is asked once, through F1226's seam, unless the message says which. The change route reads
the recordings (`read_workspace` topic `recordings`) and hands a recording's request to the build route
(`use_recording`), as `edit_steps` hands a workflow's steps to its update path.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.browser import recordings
from core.models_orchestration import OrchestrationRun
from core.native_builder import change_tools
from core.native_builder.classify import BOTH_KEY, override
from core.native_builder.objects import Request
from core.orchestration.engine import live
from core.orchestration.state import SharedState

INVENTORY = {"schedule": ["Nightly invoices"], "agent": ["Front desk", "Web"],
             "workflow": ["Weekly Delivery Confirmation for HP-2211", "Saltmarsh Delivery Confirmation"],
             "recording": ["Confirm a delivery"]}
BUILDER = {"may_change": True, "may_act": True}


def _route(message, kind, *, inventory=INVENTORY, state=None):
    return override(Request(kind=kind), message=message, state={**BUILDER, **(state or {})}, inventory=inventory,
                    connectable=set())


class TestTheRouter:
    @pytest.mark.parametrize("message", [
        "Every Monday at 9, run my Confirm a delivery recording for order HP-2211.",
        "For each order in the list I paste, run Confirm a delivery one after another.",
        "Run Confirm a delivery for the order I give; if the page has changed, let my Web agent find the way.",
        "run the delivery recording for HP-2213 now",
    ])
    def test_a_request_to_run_a_recording_is_a_build_of_a_new_workflow(self, message):
        routed = _route(message, "change")
        assert (routed.kind, routed.route, routed.overridden_by) == ("build", "step_plan_room", "recording_named")
        assert routed.model_kind == "change"

    def test_the_conversation_s_last_build_does_not_take_it(self):
        routed = _route("Every Monday, run my Confirm a delivery recording", "change",
                        state={"current_orchestration_source": "built", "current_orchestration_id": "orch_last"})
        assert routed.route == "step_plan_room"

    def test_a_recording_added_to_a_named_workflow_edits_that_workflow(self):
        routed = _route("add my Confirm a delivery recording as a step", "build",
                        state={"current_orchestration_source": "viewing", "current_orchestration_id": "orch_v"})
        assert (routed.kind, routed.route) == ("build", "step_fetch_existing")

    @pytest.mark.parametrize("message", ["rename my Confirm a delivery recording to Check a delivery",
                                         "delete the Confirm a delivery recording"])
    def test_an_edit_of_the_recording_itself_is_left_to_the_model(self, message):
        assert _route(message, "change").overridden_by != "recording_named"

    def test_a_question_about_a_recording_stays_an_answer(self):
        routed = _route("What does my Confirm a delivery recording do?", "answer")
        assert (routed.kind, routed.route) == ("answer", "step_answer")

    def test_a_message_that_names_no_recording_is_untouched(self):
        assert _route("run Nightly invoices now", "answer").overridden_by == "act_on_existing"


SHARED = {**INVENTORY, "workflow": [*INVENTORY["workflow"], "Confirm a delivery"]}


class TestASharedName:
    def test_a_name_that_is_both_goes_to_the_change_route_to_be_asked(self):
        routed = _route("run Confirm a delivery for HP-2211", "change", inventory=SHARED)
        assert (routed.kind, routed.route, routed.overridden_by) == ("change", "step_change_context",
                                                                    "recording_or_workflow")

    def test_saying_recording_settles_it(self):
        # The workflow of that name is the target code found by name; the recording is what was meant.
        named = {"current_orchestration_source": "named", "current_orchestration_id": "orch_same",
                 "current_orchestration_name": "Confirm a delivery"}
        routed = _route("run my Confirm a delivery recording for HP-2211", "change", inventory=SHARED, state=named)
        assert (routed.route, routed.overridden_by) == ("step_plan_room", "recording_named")

    def test_saying_workflow_settles_it(self):
        routed = _route("run the Confirm a delivery workflow now", "change", inventory=SHARED)
        assert routed.overridden_by not in ("recording_named", "recording_or_workflow")


async def test_the_inventory_has_the_recordings():
    from core.native_builder.classify import _inventory

    await recordings.create("Confirm a delivery", [{"id": "s1", "action": "open", "url": "https://a.example/"}],
                            by="you")
    assert "Confirm a delivery" in (await _inventory())["recording"]


# ── the change route ──────────────────────────────────────────────────────────────────────────────────────────────

class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture
async def rec():
    builder_access.forget_runs()
    yield (await recordings.create("Confirm a delivery", [{"id": "s1", "action": "open",
                                                           "url": "https://portal.saltmarsh.example/"}], by="you"))
    builder_access.forget_runs()


async def _run(**state):
    run = OrchestrationRun(run_id="builder_rec", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "manage", **state})
    await SharedState(run).checkpoint()
    return run


async def _check(run, ops):
    with live(run):
        raw, _ = await execute_builder_tool_with_events(
            "check_change", {"draft": {"finish": "submit_change", "ops": ops}}, _Server(), run_id=run.run_id)
    return json.loads(raw)


def _use(name: str) -> dict:
    return {"op": "use_recording", "target": {"kind": "recording", "name": name},
            "fields": {"what": f"run {name} every Monday at 9"}}


class TestTheChangeRoute:
    async def test_the_planner_reads_the_recordings(self, rec):
        from core.builder_readers import read_workspace, shape_problems
        from core.native_builder.seed import bundled_definitions

        planner = next(a for a in bundled_definitions()["agents"] if a["id"] == "agent_native_builder_planner")
        assert "read_workspace" in planner["tools"] and "use_recording" in planner["system_prompt"]
        read = await read_workspace({"topic": "recordings"}, None, [])
        assert [i["name"] for i in read["items"]] == ["Confirm a delivery"]
        assert shape_problems("recordings", {k: v for k, v in read.items() if k != "topic"}) == []

    async def test_a_recording_s_request_is_handed_to_the_build_route(self, rec):
        run = await _run()
        out = await _check(run, [_use("confirm a delivery")])
        assert out["outcome"] == "steps", out
        assert run.shared_state["intent"] == "create"
        assert not run.shared_state.get("current_orchestration_id")

    async def test_a_recording_that_is_not_there_is_said_with_how_one_is_made(self, rec):
        out = await _check(await _run(), [_use("Pay the rent")])
        assert out["outcome"] == "said" and "No recording is called" in out["say"]
        assert "Record" in out["say"]


class TestASharedNameIsAskedOnce:
    async def _shared(self):
        from core.store.resources import save_orchestration
        from tests._fakes.seed import make_orchestration

        await save_orchestration(make_orchestration(id="orch_same", name="Confirm a delivery"))

    async def test_it_is_asked_once_with_both_and_counted(self, rec):
        await self._shared()
        run = await _run(**{BOTH_KEY: "Confirm a delivery"})
        out = await _check(run, [{"op": "run_workflow", "target": {"kind": "workflow", "name": "Confirm a delivery"},
                                  "fields": {}}])
        assert out["outcome"] == "ask", out
        assert "recording" in out["question"] and "workflow" in out["question"]
        asked = run.shared_state[change_tools.ASKED_KEY]
        assert len(asked) == 1 and asked[0]["about"] == "which"

    async def test_the_recording_answered_goes_to_the_build_route(self, rec):
        await self._shared()
        run = await _run(**{BOTH_KEY: "Confirm a delivery"})
        await _check(run, [_use("Confirm a delivery")])
        run.shared_state["change_clarify"] = {"answer": 'The recording "Confirm a delivery"'}
        out = await _check(run, [_use("Confirm a delivery")])
        assert out["outcome"] == "steps" and run.shared_state["intent"] == "create", out
        assert len(run.shared_state[change_tools.ASKED_KEY]) == 1, "never asked twice"

    async def test_the_workflow_answered_goes_on_as_the_change(self, rec):
        await self._shared()
        run = await _run(**{BOTH_KEY: "Confirm a delivery"})
        op = {"op": "run_workflow", "target": {"kind": "workflow", "name": "Confirm a delivery"}, "fields": {}}
        await _check(run, [op])
        run.shared_state["change_clarify"] = {"answer": 'The workflow "Confirm a delivery"'}
        out = await _check(run, [op])
        assert out["outcome"] not in ("ask", "steps"), out
        assert len(run.shared_state[change_tools.ASKED_KEY]) == 1


async def test_a_plan_without_the_browser_reads_no_recordings_in_the_same_shape(rec):
    """Cloud's Free has no recordings library (0098): the topic answers its shape with the host's sentence, never an
    error (cloud's TestEveryReaderInOneShape)."""
    from core import builder_tools
    from core.builder_readers import read_workspace, shape_problems

    async def check(kind):
        return "Recordings come with Solo and up." if kind == "recording" else None

    builder_tools.set_browser_check(check)
    try:
        read = await read_workspace({"topic": "recordings"}, None, [])
    finally:
        builder_tools.set_browser_check(None)
    assert read["items"] == [] and read["note"] == "Recordings come with Solo and up." and "error" not in read
    assert shape_problems("recordings", {k: v for k, v in read.items() if k != "topic"}) == []
