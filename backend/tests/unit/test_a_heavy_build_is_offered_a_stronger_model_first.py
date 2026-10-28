"""Before a heavy build, one offer of a stronger model (F1153, SAI2-P3, D300; pro drafts/sai2/EFFORT-CHECK.md, Naveen's
answers 10-28).

Code decides, a model only labels: the quick classify says `effort` and its model reasons; code adds its own reasons
from facts (providers, the message's length, what can't connect here, an earlier failure in this conversation) and
offers only when the build is heavy, a stronger model this workspace can call and pay for exists, and this build was
not asked already. The offer is the turn's end; its press sends the ask again with `effort_of`, and the server picks
the model; any other reply goes on with the ask on the usual model, never asked again in that build.
"""
from __future__ import annotations

import pytest

from core import builder_tools  # noqa: F401 — loads the builder's modules first, as at boot
from core import events_public, try_stronger
from core.native_builder import effort
from core.native_builder import models_menu as MM
from core.native_builder.read_turn import decide


def _entry(id, *, reasoning=False, out=1.0, pay=True):
    return MM.ModelEntry(id=id, name=id.split(".")[1], provider=id.split(".")[1].split("-")[0], source="managed",
                         tools="native", vision=False, reasoning=reasoning, price_in=out / 4, price_out=out,
                         can_pay=pay)


MENU = [_entry("synapse.claude-haiku", reasoning=True, out=5), _entry("synapse.claude-sonnet", reasoning=True, out=15)]
SESSION = "builder::panel:c1"
HEAVY_ASK = ("Every morning, pull yesterday's Stripe payouts, match them to the QuickBooks invoices, post the "
             "unmatched ones to Slack, and have a reviewer agent and a writer agent work together on the summary.")


@pytest.fixture(autouse=True)
def menu(monkeypatch):
    now = {"menu": list(MENU)}

    async def fake():
        return list(now["menu"])

    monkeypatch.setattr(MM, "menu", fake)
    return now


# ── heavy, in code ────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("request_, message, failed, heavy", [
    ({"kind": "build", "effort": "heavy", "why": ["many_steps"]}, "x", False, True),     # one model reason
    ({"kind": "build", "effort": "heavy", "why": ["team"]}, "x", False, True),
    ({"kind": "build", "providers": ["Stripe", "QuickBooks", "Slack"]}, "x" * 1300, False, True),  # two code ones
    ({"kind": "build", "providers": ["Stripe", "QuickBooks", "Slack"]}, "x", False, False),  # one code reason
    ({"kind": "build"}, "x", True, True),                                                    # an earlier failure
    ({"kind": "build", "effort": "light"}, "x", False, False),
    # A model's "heavy" with no reason of its own is no reason: code reads the reasons, never the word alone.
    ({"kind": "build", "effort": "heavy"}, "x", False, False),
])
def test_heavy_is_code_s_reading_of_the_reasons(request_, message, failed, heavy):
    whys = effort.reasons(request_, message, connectable={"stripe", "quickbooks", "slack"}, failed_before=failed)
    assert effort.is_heavy(whys) is heavy, whys


def test_a_provider_that_cannot_connect_here_is_a_code_reason():
    whys = effort.reasons({"kind": "build", "providers": ["Acuity Scheduling"]}, "x", connectable={"slack"},
                          failed_before=False)
    assert "api_only" in whys


# ── the check ─────────────────────────────────────────────────────────────────────────────────────────────────────

async def _check(**state):
    from core.models_orchestration import OrchestrationRun
    from core.orchestration import run_images
    from core.orchestration.engine import ACTIVE_TIME_KEY, OrchestrationEngine, live
    from core.orchestration.state import SharedState

    refs = state.pop("_step_progress_refs", None)

    run = OrchestrationRun(run_id="builder_effort", orchestration_id="orch_native_builder", session_id=SESSION,
                           shared_state={"user_message": HEAVY_ASK, "intent": "create", "may_change": True,
                                         OrchestrationEngine.MODEL_OVERRIDE_KEY: "synapse.claude-haiku",
                                         "request": {"kind": "build", "effort": "heavy", "why": ["many_steps", "team"],
                                                     "providers": ["Stripe", "QuickBooks", "Slack"]},
                                         **state})
    if refs:  # the turn's images, kept by the engine at its start (F395)
        run.step_progress[ACTIVE_TIME_KEY] = {run_images.REFS_KEY: refs}
    await SharedState(run).checkpoint()
    import json

    from core.builder_tools import execute_builder_tool_with_events

    with live(run):  # as the engine calls it inside the run, which it names
        raw, events = await execute_builder_tool_with_events("effort_check", {}, None, run_id=run.run_id)
    return (json.loads(raw) if isinstance(raw, str) else raw), events


async def test_a_heavy_build_ends_its_turn_with_one_offer(client):
    out, events = await _check()
    assert out["next"] == "offer" and out["ask"] == HEAVY_ASK
    [offer] = [e for e in events if e.get("type") == effort.KIND]
    assert (offer["model"], offer["default"], offer["effort_of"]) == ("synapse.claude-sonnet", "synapse.claude-haiku",
                                                                      "builder_effort")
    assert offer["use"] == "Use claude-sonnet" and offer["continue"] == "Continue with claude-haiku"
    assert "bigger build" in out["say"] and "about 3× the price" in out["say"]


@pytest.mark.parametrize("state, entry", [
    ({"request": {"kind": "build", "effort": "light"}}, "step_plan_room"),
    ({"request": {"kind": "build", "effort": "light"}, "intent": "update"}, "step_fetch_existing"),
    ({effort.DECLINED_KEY: "builder_before"}, "step_plan_room"),     # no second ask in that build
    ({effort.ACCEPTED_KEY: "builder_before"}, "step_plan_room"),     # already on the stronger model
    ({try_stronger.RETRIED_KEY: "builder_failed"}, "step_plan_room"),  # F1018's retry is its own offer
    ({"may_change": False}, "step_plan_room"),                       # a role that may not build is not offered
])
async def test_no_offer(client, state, entry):
    out, events = await _check(**state)
    assert out["next"] == entry and not [e for e in events if e.get("type") == effort.KIND]


@pytest.mark.parametrize("menu_now", [
    [MENU[0]],                                                        # nothing stronger here
    [MENU[0], _entry("synapse.claude-sonnet", reasoning=True, out=15, pay=False)],  # the wallet can't pay for it
])
async def test_no_offer_without_a_stronger_model_it_can_pay_for(client, menu, menu_now):
    menu["menu"] = menu_now
    out, events = await _check()
    assert out["next"] == "step_plan_room" and not events


def test_the_offer_reaches_the_panel():
    assert effort.KIND in events_public.PUBLIC_EVENT_TYPES


# ── any other reply goes on, on the usual model ───────────────────────────────────────────────────────────────────

OFFERED = {"effort": {"next": "offer", "ask": HEAVY_ASK, "say": "This looks like a bigger build."}}


@pytest.mark.parametrize("said", ["go on", "just do it", "Continue with claude-haiku", "ok"])
def test_a_go_ahead_builds_the_ask_on_the_usual_model(said):
    turn = decide(said, previous=OFFERED, facts={})
    assert (turn.route, turn.then, turn.message) == ("effort_continue", "classify", HEAVY_ASK)


def test_a_typed_change_goes_on_with_the_ask_and_the_change():
    turn = decide("and post it to Teams instead of Slack", previous=OFFERED, facts={})
    assert turn.route == "effort_continue" and turn.message.startswith(HEAVY_ASK)
    assert turn.message.endswith("and post it to Teams instead of Slack")


def test_without_an_offer_a_message_is_read_as_before():
    assert decide("go on", previous={}, facts={}).route != "effort_continue"


# ── the press: the server checks it and picks the model ───────────────────────────────────────────────────────────

async def _turn(run_id, *, state, session=SESSION, status="completed"):
    from core.models_orchestration import OrchestrationRun
    from core.orchestration.engine import OrchestrationEngine
    from core.orchestration.state import SharedState

    run = OrchestrationRun(run_id=run_id, orchestration_id="orch_native_builder", session_id=session, status=status,
                           shared_state={OrchestrationEngine.MODEL_OVERRIDE_KEY: "synapse.claude-haiku", **state})
    await SharedState(run).checkpoint()


async def test_the_press_builds_the_ask_on_the_server_s_pick(client):
    await _turn("builder_offered", state=OFFERED)
    got = await effort.effort_press("builder_offered", SESSION)
    assert (got["model"], got["message"], got["images"]) == ("synapse.claude-sonnet", HEAVY_ASK, [])
    assert got["state"] == {effort.ACCEPTED_KEY: "builder_offered", effort.PRESSED_KEY: True}


@pytest.mark.parametrize("why, setup", [
    ("another chat's", lambda: _turn("builder_offered", state=OFFERED, session="builder::panel:c2")),
    ("a turn with no offer", lambda: _turn("builder_offered", state={})),
])
async def test_a_press_is_refused_for(client, why, setup):
    await setup()
    assert isinstance(await effort.effort_press("builder_offered", SESSION), str), why


async def test_a_stale_or_repeated_press_is_refused(client):
    import asyncio

    await _turn("builder_offered", state=OFFERED)
    await asyncio.sleep(0.01)
    await _turn("builder_after", state={effort.ACCEPTED_KEY: "builder_offered"})  # pressed once already
    assert await effort.effort_press("builder_offered", SESSION) == effort.NOT_OPEN


async def test_a_press_when_nothing_stronger_can_be_paid_for_now_is_refused(client, menu):
    await _turn("builder_offered", state=OFFERED)
    menu["menu"] = [MENU[0]]
    assert await effort.effort_press("builder_offered", SESSION) == try_stronger.NOTHING_STRONGER


# ── after a failure: said plainly, then F1018's one offer; on the stronger model, said only ─────────────────────────

async def test_a_build_on_the_stronger_model_is_never_offered_another(client):
    from core.models_orchestration import OrchestrationRun
    from core.native_builder import seed
    from core.orchestration.state import SharedState

    await seed.seed_native_builder()
    run = OrchestrationRun(run_id="builder_strong_failed", orchestration_id="orch_native_builder", session_id=SESSION,
                           status="failed", current_step_id="step_plan",
                           shared_state={effort.ACCEPTED_KEY: "builder_offered"})
    await SharedState(run).checkpoint()
    assert await try_stronger.panel_offer("builder_strong_failed") is None


def test_a_failure_is_said_plainly():
    assert effort.failed_line("claude-haiku", "The planner gave no plan.") == (
        "This didn't build on claude-haiku: The planner gave no plan.")


async def test_a_failed_build_is_said_plainly_with_its_model_and_why(client):
    from core.models_orchestration import OrchestrationRun
    from core.orchestration.engine import OrchestrationEngine
    from core.orchestration.run_error import set_run_error
    from core.orchestration.state import SharedState

    run = OrchestrationRun(run_id="builder_failed_plain", orchestration_id="orch_native_builder", session_id=SESSION,
                           status="failed", shared_state={"intent": "create",
                                                          OrchestrationEngine.MODEL_OVERRIDE_KEY: "synapse.claude-haiku"})
    set_run_error(run, "The planner gave no plan")
    await SharedState(run).checkpoint()
    said = await effort.failure_notice("builder_failed_plain")
    assert said == {"type": "builder_notice", "reason": "build_failed", "final": False,
                    "message": "This didn't build on claude-haiku: The planner gave no plan."}


async def test_an_answer_that_failed_is_not_a_build_s_line(client):
    from core.models_orchestration import OrchestrationRun
    from core.orchestration.state import SharedState

    run = OrchestrationRun(run_id="builder_answer_failed", orchestration_id="orch_native_builder", session_id=SESSION,
                           status="failed", shared_state={"intent": "answer"})
    await SharedState(run).checkpoint()
    assert await effort.failure_notice("builder_answer_failed") is None


def test_the_definition_checks_the_effort_after_the_role_door_on_the_build_route_only():
    from core.native_builder import bundled_definitions

    steps = {s["id"]: s for s in bundled_definitions()["orchestration"]["steps"]}
    door = steps["step_change_route"]
    assert door["switch_cases"]["create"] == door["switch_cases"]["update"] == "step_effort"
    assert door["switch_cases"]["manage"] == "step_change_context", "the change route stays on the usual model"
    assert steps["step_effort"]["forced_tool"] == "effort_check" and steps["step_effort"]["output_key"] == "effort"
    assert steps["step_effort_route"]["switch_cases"] == {
        "offer": "step_effort_offered", "step_plan_room": "step_plan_room", "step_fetch_existing": "step_fetch_existing"}
    assert steps["step_effort_offered"]["type"] == "print" and steps["step_effort_offered"]["next_step_id"] == "step_end"



async def _failed_build(run_id, **state):
    from core.models_orchestration import OrchestrationRun
    from core.native_builder import seed
    from core.orchestration.engine import OrchestrationEngine
    from core.orchestration.run_error import set_run_error
    from core.orchestration.state import SharedState

    await seed.seed_native_builder()
    run = OrchestrationRun(run_id=run_id, orchestration_id="orch_native_builder", session_id=SESSION, status="failed",
                           current_step_id="step_plan",
                           shared_state={"intent": "create",
                                         OrchestrationEngine.MODEL_OVERRIDE_KEY: "synapse.claude-haiku", **state})
    set_run_error(run, "The planner gave no plan")
    await SharedState(run).checkpoint()


async def test_a_failed_build_ends_with_its_line_then_the_one_offer_kept_on_the_run(client):
    """F1155's engine half: one function both hosts call before a run's stream ends (the self-hosted route, the
    fleet's adapter), so a panel reads the same on either; the offer is kept for a host that checks the press."""
    from core.orchestration.state import SharedState

    await _failed_build("builder_end")
    said, offered = await try_stronger.end_events("builder_end", retried=False)
    assert said["reason"] == "build_failed" and said["message"].startswith("This didn't build on claude-haiku:")
    assert offered["type"] == try_stronger.KIND and offered["model"] == "synapse.claude-sonnet"
    kept = (await SharedState.restore("builder_end")).run.shared_state[try_stronger.OFFER_KEY]
    assert kept == offered


async def test_a_retried_build_that_failed_ends_with_its_line_only(client):
    await _failed_build("builder_end_retried")
    events = await try_stronger.end_events("builder_end_retried", retried=True)
    assert [e["type"] for e in events] == ["builder_notice"]


def test_both_hosts_end_a_builder_run_the_same_way():
    import inspect

    from core.routes import builder
    from core.scale import worker_engine_adapter as adapter

    assert "try_stronger.end_events(run_id, retried=retried)" in inspect.getsource(builder.try_stronger_after)
    source = inspect.getsource(adapter.WorkerEngineAdapter)
    assert source.count("await self._end_events(") == 2, "the run's end and a resume's end"
    assert "try_stronger.end_events(" in inspect.getsource(adapter.WorkerEngineAdapter._end_events)



# ── the review of 9aa04e56 + 3a8d9e84 (the manager's independent reviewer, 10-28) ──────────────────────────────────

CONTEXT = {"current_orchestration_id": "orch_digest", "current_orchestration_source": "named",
           "current_orchestration_name": "Digest", "prior_findings": {"options": [{"provider": "Stripe"}]},
           "export_guide": "", "export_source": ""}


async def test_1_the_offer_keeps_its_turns_context_and_images_for_what_comes_next(client):
    """Press and Continue rebuilt the turn from scratch: no findings (it researched again), the target read from
    "Continue with …", no images, no pasted export."""
    from core.orchestration import run_images

    image = "data:image/png;base64,iVBORw0KGgo="
    refs = await run_images.keep("builder_effort", [image])
    out, _ = await _check(**CONTEXT, **{"_step_progress_refs": refs})
    assert out["next"] == "offer"
    assert {k: out["carry"][k] for k in CONTEXT} == CONTEXT and out["carry"]["request"]["kind"] == "build"
    following = await effort.next_turn("builder_effort", "completed", {"effort": out})
    assert following["state"]["current_orchestration_id"] == "orch_digest"
    assert following["state"]["prior_findings"] == CONTEXT["prior_findings"]
    assert following["images"] == [image] and following["model"] is None


async def test_2_a_retry_builds_the_failed_turns_own_message(client):
    await _failed_build("builder_retry_msg", user_message=HEAVY_ASK)
    got = await try_stronger.panel_retry("builder_retry_msg", SESSION)
    assert got["message"] == HEAVY_ASK, "never the last bubble (\"Continue with …\" after a declined offer)"


@pytest.mark.parametrize("answer", [effort.DECLINED_KEY, effort.ACCEPTED_KEY])
async def test_3_an_answered_build_is_not_asked_again_until_it_saves(client, answer):
    await _turn("builder_answered", state={answer: "builder_offered", "intent": "create"}, status="completed")
    out, events = await _check()
    assert out["next"] == "step_plan_room" and not events


async def test_3_a_build_that_saved_is_done_and_the_next_one_may_be_asked(client):
    await _turn("builder_saved", state={effort.DECLINED_KEY: "x", "intent": "create",
                                        "_run_writes": [{"kind": "orchestration", "action": "created"}]})
    out, _ = await _check()
    assert out["next"] == "offer"


@pytest.mark.parametrize("status", ["paused", "cancelled"])
async def test_4_a_build_on_the_stronger_model_keeps_it_for_its_next_round(client, status):
    from core.orchestration.engine import OrchestrationEngine

    previous = {effort.ACCEPTED_KEY: "builder_offered", "intent": "create",
                OrchestrationEngine.MODEL_OVERRIDE_KEY: "synapse.claude-sonnet"}
    following = await effort.next_turn("builder_strong", status, previous)
    assert following["model"] == "synapse.claude-sonnet" and following["state"][effort.ACCEPTED_KEY]


def test_5_an_answered_build_ends_as_any_turn_does():
    import inspect

    from core.routes import builder

    source = inspect.getsource(builder.builder_resume)
    assert "try_stronger_after(OrchestrationEngine.resume(" in source


async def test_6_two_presses_build_once(client):
    import asyncio

    await _turn("builder_offered", state=OFFERED)
    got = await asyncio.gather(effort.effort_press("builder_offered", SESSION),
                               effort.effort_press("builder_offered", SESSION))
    assert sorted(isinstance(g, str) for g in got) == [False, True]


async def test_6_two_retry_presses_try_again_once(client):
    import asyncio

    await _failed_build("builder_retry_twice")
    got = await asyncio.gather(try_stronger.panel_retry("builder_retry_twice", SESSION),
                               try_stronger.panel_retry("builder_retry_twice", SESSION))
    assert sorted(isinstance(g, str) for g in got) == [False, True]


def test_7_a_press_goes_to_the_build_route_never_classified_again():
    turn = decide("Use claude-sonnet", previous=OFFERED, facts={}, pressed=True)
    assert (turn.route, turn.then) == ("effort_build", "build")
    from core.native_builder import bundled_definitions

    steps = {s["id"]: s for s in bundled_definitions()["orchestration"]["steps"]}
    assert steps["step_turn_route"]["switch_cases"]["build"] == "step_may_change", "through the role door"


async def test_8_a_change_handed_to_the_build_route_is_not_offered(client):
    out, events = await _check(request={"kind": "change", "effort": "heavy", "why": ["many_steps"]}, intent="update")
    assert out["next"] == "step_fetch_existing" and not events


async def test_9_an_answer_that_failed_is_no_failure_of_a_build(client):
    await _turn("builder_answer_failed_before", state={"intent": "answer"}, status="failed")
    out, _ = await _check(request={"kind": "build"})
    assert out["next"] == "step_plan_room", "retry_history counts failed builds only"


def test_9_a_failed_build_is_said_once():
    from pathlib import Path

    events = (Path(__file__).resolve().parents[3] / "frontend" / "src" / "lib" / "builderEvents.ts").read_text()
    assert "event.reason === 'build_failed'" in events and "!m.key.startsWith('error-')" in events
