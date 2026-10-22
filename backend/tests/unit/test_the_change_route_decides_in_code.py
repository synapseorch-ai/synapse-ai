"""
Synapse AI's change route decides every fact in code: the model only proposes ops (F793 / F794, SAI2-P2, D308).

Before, `step_manage_plan` (a model with no tools) wrote a markdown plan, F755's gate picked cards or a review by
searching that markdown for tool names, and the operator agent re-read it to act. Now the change planner finishes
on `submit_change`, `ask_person` or `cannot`, and `check_change` decides, for each op: which target (two that match
are a question with both ids, F760), whether the person's role may do it (the registry tool's own `min_role`, read
now), whether it would change anything (live state), what uses a delete (F690 / F467), and whether it is a card or
goes to the review (by the op's kind, F755). The review's lines are code's words.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks
from core.product_tools.roles import Role


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
def fresh():
    confirm.set_claim_store(None)
    builder_access.forget_runs()
    yield
    hooks.set_role_source(None)
    builder_access.forget_runs()


@pytest.fixture
def role():
    held = {"role": Role.OWNER}

    async def source(user_id):
        return held["role"]

    hooks.set_role_source(source)
    return held


async def _run(user_id="u1", run_id="builder_chg", **state):
    run = OrchestrationRun(run_id=run_id, orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": user_id}, **state})
    await SharedState(run).checkpoint()
    return run


async def _call(tool, args, run):
    with live(run):
        raw, events = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return json.loads(raw), events


async def _check(ops, run=None, **draft):
    run = run or await _run()
    out, events = await _call("check_change", {"draft": {"finish": "submit_change", "ops": ops, **draft}}, run)
    return out, [e for e in events if e.get("type") == "action_needed"], run


async def _agent(**kw):
    from core.store.resources import save_agent
    from tests._fakes.seed import make_agent

    agent = make_agent(**kw)
    await save_agent(agent)
    return agent


async def _schedule(**kw):
    from core.store import schedules

    item = {"id": "sched_n", "name": "Every night at 2", "enabled": True, "target_type": "orchestration",
            "target_id": "orch_x", "prompt": "Go", "schedule_type": "cron", "cron_expression": "0 2 * * *", **kw}
    await schedules.save(item)
    return item


async def _workflow(**kw):
    from core.store.resources import save_orchestration
    from tests._fakes.seed import make_orchestration

    orch = make_orchestration(**kw)
    await save_orchestration(orch)
    return orch


# ── the planner's finish tools ──────────────────────────────────────────────────────────────────────────────────

async def test_submit_change_sends_back_what_to_fix(client):
    run = await _run()
    out, _ = await _call("submit_change", {"ops": [
        {"op": "rename_agent", "target": {"kind": "agent", "name": "Front desk"}, "fields": {"title": "x"}}]}, run)
    assert "cannot set title" in out["error"] and "needs name" in out["error"]


async def test_submit_change_finishes_with_the_typed_change(client):
    run = await _run()
    out, _ = await _call("submit_change", {"ops": [
        {"op": "schedule_off", "target": {"kind": "schedule", "name": "nightly"}, "says": "pause it",
         "extra": "dropped"}]}, run)
    assert out == {"finish": "submit_change", "ops": [
        {"op": "schedule_off", "target": {"kind": "schedule", "name": "nightly"}, "fields": {}, "says": "pause it"}],
        "questions": []}


async def test_ask_person_and_cannot_need_their_words(client):
    run = await _run()
    assert "error" in (await _call("ask_person", {"about": "when"}, run))[0]
    assert "error" in (await _call("cannot", {"why": "No printers."}, run))[0]
    asked, _ = await _call("ask_person", {"question": "Which time?", "about": "when", "options": ["2am", "3am"]}, run)
    assert asked == {"finish": "ask_person", "question": "Which time?", "about": "when", "options": ["2am", "3am"]}


async def test_the_finish_tools_and_the_check_change_nothing(client):
    for tool in ("submit_change", "ask_person", "cannot", "check_change"):
        assert builder_access.KINDS[tool] == "card"
    assert builder_access.KINDS["read_change_context"] == "read"
    assert builder_access.KINDS["apply_change"] == builder_access.KINDS["approve_change"] == "write"


# ── the check ───────────────────────────────────────────────────────────────────────────────────────────────────

async def test_a_rename_by_name_goes_to_the_review_with_code_s_words(client, role):
    agent = await _agent(name="Front desk")
    out, cards, _ = await _check([{"op": "rename_agent", "target": {"kind": "agent", "name": "front desk"},
                                   "fields": {"name": "Harbor desk"}, "says": "rename it"}])
    assert out["outcome"] == "review" and cards == []
    assert out["calls"] == [["update_agent", {"agent_id": agent["id"], "fields": {"name": "Harbor desk"}}]]
    assert out["review"] == '- Rename the agent "Front desk" — to "Harbor desk"; rename it'
    assert out["digest"] and out["object"] == "change"


async def test_two_that_match_are_a_question_with_both_ids_and_nothing_else_happens(client, role):
    first = await _workflow(name="Patient reminders", description="front desk")
    second = await _workflow(name="Patient reminders", description="billing")
    out, cards, _ = await _check([{"op": "run_workflow", "target": {"kind": "workflow", "name": "Patient reminders"}}])
    assert out["outcome"] == "ask" and cards == []
    assert {o["id"] for o in out["picks"][0]["options"]} == {first["id"], second["id"]}
    assert first["id"] in out["question"] and "billing" in out["question"]


async def test_an_act_is_a_card_not_a_review(client, role):
    await _schedule()
    out, [card], _ = await _check([{"op": "schedule_off", "target": {"kind": "schedule", "name": "every night"}}])
    assert out["outcome"] == "said" and out["calls"] == []
    assert card["title"] == 'Turn off "Every night at 2"'
    assert "A card is up" in out["say"]


async def test_an_act_that_would_do_nothing_says_so_with_no_card(client, role):
    await _schedule(enabled=False)
    out, cards, _ = await _check([{"op": "schedule_off", "target": {"kind": "schedule", "id": "sched_n"}}])
    assert out["outcome"] == "said" and cards == []
    assert out["say"] == '"Every night at 2" is already off.'


async def test_a_write_that_would_change_nothing_is_said(client, role):
    await _schedule()
    out, _, _ = await _check([{"op": "set_schedule", "target": {"kind": "schedule", "id": "sched_n"},
                               "fields": {"cron_expression": "0 2 * * *"}}])
    assert out["outcome"] == "said" and "already that way" in out["say"]


async def test_a_write_the_role_cannot_do_is_refused_before_any_promise(client, role):
    role["role"] = Role.APPROVER
    await _agent(name="Front desk")
    out, _, _ = await _check([{"op": "rename_agent", "target": {"kind": "agent", "name": "Front desk"},
                               "fields": {"name": "Desk"}}])
    assert out["outcome"] == "said" and out["calls"] == []
    assert "needs the builder role; you are a approver" in out["say"]


async def test_a_delete_something_uses_is_refused_naming_it(client, role):
    agent = await _agent(name="Support bot")
    await _schedule(target_type="agent", target_id=agent["id"], name="Morning check")
    out, _, _ = await _check([{"op": "delete_agent", "target": {"kind": "agent", "id": agent["id"]}}])
    assert out["outcome"] == "said" and "Morning check" in out["say"]


async def test_a_delete_nothing_uses_is_reviewed_with_its_danger_said(client, role):
    agent = await _agent(name="Support bot")
    out, _, _ = await _check([{"op": "delete_agent", "target": {"kind": "agent", "name": "Support bot"}}])
    assert out["outcome"] == "review"
    assert out["review"].startswith('- ⚠️ DELETES: Delete the agent "Support bot" — nothing uses it.')
    assert "new copy" in out["review"]
    assert out["calls"] == [["delete_agent", {"agent_id": agent["id"]}]]


async def test_a_name_that_matches_nothing_is_said(client, role):
    out, _, _ = await _check([{"op": "rename_agent", "target": {"kind": "agent", "name": "Nobody"},
                               "fields": {"name": "X"}}])
    assert out["outcome"] == "said" and out["say"] == 'No agent is called "Nobody".'


async def test_a_new_schedule_needs_a_free_name_and_what_it_starts(client, role):
    flow = await _workflow(name="Nightly digest")
    await _schedule()
    taken, _, _ = await _check([{"op": "create_schedule", "target": {"kind": "schedule", "name": "Every night at 2"},
                                 "fields": {"runs": {"kind": "workflow", "name": "nightly digest"}, "prompt": "Go",
                                            "cron_expression": "0 9 * * 1"}}])
    assert taken["outcome"] == "said" and "already called" in taken["say"]
    out, _, _ = await _check([{"op": "create_schedule", "target": {"kind": "schedule", "name": "Mondays"},
                               "fields": {"runs": {"kind": "workflow", "name": "nightly digest"}, "prompt": "Go",
                                          "cron_expression": "0 9 * * 1"}}])
    assert out["outcome"] == "review"
    assert out["calls"][0] == ["create_schedule", {"name": "Mondays", "target_type": "orchestration",
                                                   "target_id": flow["id"], "prompt": "Go",
                                                   "cron_expression": "0 9 * * 1", "schedule_type": "cron"}]


async def test_a_mixed_change_puts_up_cards_and_reviews_the_writes(client, role):
    await _schedule()
    agent = await _agent(name="Front desk")
    out, [card], _ = await _check([
        {"op": "run_schedule_now", "target": {"kind": "schedule", "id": "sched_n"}},
        {"op": "set_agent_model", "target": {"kind": "agent", "id": agent["id"]}, "fields": {"model": "m2"}}])
    assert out["outcome"] == "review" and card["title"].startswith("Run ")
    assert [c[0] for c in out["calls"]] == ["update_agent"]
    assert "A card is up" in out["say"]


async def test_the_review_is_kept_on_the_person_s_card(client, role):
    from core.boards import assistant

    await _agent(name="Front desk")
    out, _, run = await _check([{"op": "rename_agent", "target": {"kind": "agent", "name": "Front desk"},
                                 "fields": {"name": "Desk"}}])
    card = await assistant.card(run.run_id)
    assert card.stage == "waiting" and card.owner_id == "u1"
    assert card.data["change"]["calls"] == out["calls"]


# ── the other finishes and the hand-overs ───────────────────────────────────────────────────────────────────────

async def test_a_question_and_a_cannot_are_passed_on(client, role):
    run = await _run()
    asked, _ = await _call("check_change", {"draft": {"finish": "ask_person", "question": "Which time?",
                                                      "about": "when", "options": ["2am"]}}, run)
    assert asked["outcome"] == "ask" and asked["question"] == "Which time?\n1. 2am"
    said, _ = await _call("check_change", {"draft": {"finish": "cannot", "why": "This product does not print.",
                                                     "nearest": "A workflow can email the form each morning."}}, run)
    assert said == {**said, "outcome": "said",
                    "say": "This product does not print. A workflow can email the form each morning."}


async def test_a_step_edit_goes_to_today_s_update_path_with_its_workflow(client, role):
    flow = await _workflow(name="Invoice sync")
    run = await _run()
    out, _, _ = await _check([{"op": "edit_steps", "target": {"kind": "workflow", "name": "invoice sync"},
                               "fields": {"what": "add an approval before the fetch"}}], run=run)
    assert out["outcome"] == "steps" and out["workflow"]["id"] == flow["id"]
    assert run.shared_state["current_orchestration_id"] == flow["id"]
    # The rest of the run reads it as the update it is: the saver's route is `state.intent` (review finding 1).
    assert run.shared_state["intent"] == "update"


@pytest.mark.parametrize("kind, said", [("agent", "agent"), ("skill", "skill"), ("tool", "HTTP tool"),
                                         ("openapi", "OpenAPI import")])
async def test_making_one_new_thing_is_reviewed_then_made_by_the_maker(client, role, kind, said):
    """Naveen, 10-23 (the hand-over probe: today's create path made none of a standalone tool, skill or agent, 0/6):
    one new agent, skill, HTTP tool or OpenAPI import is shown on the review in code's words and, after the yes,
    made by the maker step with those four tools only (P3's build route replaces it)."""
    run = await _run()
    out, cards, _ = await _check([{"op": "create", "target": {"kind": kind, "name": "Mailchimp"},
                                   "fields": {"what": "lists their audiences"}}], run=run)
    assert out["outcome"] == "create" and cards == [] and out["calls"] == []
    assert out["review"] == f'- Make the {said} "Mailchimp" — lists their audiences'
    assert "intent" not in run.shared_state or run.shared_state["intent"] != "create"


async def test_a_new_thing_needs_a_free_name_and_the_builder_role(client, role):
    await _agent(name="Billing helper")
    taken, _, _ = await _check([{"op": "create", "target": {"kind": "agent", "name": "billing helper"},
                                 "fields": {"what": "answers billing questions"}}])
    assert taken["outcome"] == "said" and "already called" in taken["say"]
    role["role"] = Role.APPROVER
    refused, _, _ = await _check([{"op": "create", "target": {"kind": "skill", "name": "Polite replies"},
                                   "fields": {"what": "be polite"}}])
    assert refused["outcome"] == "said" and "needs the builder role" in refused["say"]


async def test_a_skill_is_changed_in_the_library_only(client, role, monkeypatch):
    """A skill in the library and on disk is one skill to change, the library's (it shadows the disk copy); a skill
    read only from disk is not changed here, and that is said before the review, not by `update_skill` after the yes
    (the branch review's finding 6)."""
    from core import skills

    await skills.save_library([{"name": "triage", "description": "t", "body": "Old."}])
    monkeypatch.setattr(skills, "scan_disk_skills", lambda: [
        {"name": "triage", "description": "disk", "source": "disk"},
        {"name": "local-notes", "description": "disk only", "source": "disk"}])
    one, _, _ = await _check([{"op": "update_skill", "target": {"kind": "skill", "name": "triage"},
                               "fields": {"body": "New."}}])
    assert one["outcome"] == "review" and one["calls"] == [["update_skill", {"name": "triage", "body": "New."}]]
    disk, _, _ = await _check([{"op": "update_skill", "target": {"kind": "skill", "name": "local-notes"},
                                "fields": {"body": "New."}}])
    assert disk["outcome"] == "said"
    assert disk["say"] == 'The skill "local-notes" is read from a folder on disk, so it is changed there, not here.'


@pytest.mark.parametrize("prompt", ["Use key sk-live-abcdefghijklmnopqrstuvwx1234 for calls.",
                                    "Use key ‹credential removed› for calls."])
async def test_a_change_that_would_write_a_key_is_refused_before_anything_keeps_it(client, role, prompt):
    """A key in a change's new value would sit on Synapse AI's board card, in the review's event and in the run's
    state. The door already masks one the person types; one a model copies or invents is refused here, before the
    review, and nothing keeps it (the branch review's finding 7)."""
    from core.boards import assistant

    agent = await _agent(name="Front desk")
    out, _, run = await _check([{"op": "set_agent_instructions", "target": {"kind": "agent", "id": agent["id"]},
                                 "fields": {"system_prompt": prompt}}])
    assert out["outcome"] == "said" and out["calls"] == []
    assert "Keys never go in a change" in out["say"]
    assert "sk-live" not in json.dumps(out) and await assistant.card(run.run_id) is None


async def test_a_which_one_question_is_kept_on_the_person_s_card(client, role):
    """The pick list is what a later turn reads "the second one" from (P4's read_turn): it waits on the person's card,
    where `read_change_context` finds it (the branch review's finding 11)."""
    first = await _workflow(name="Patient reminders", description="front desk")
    await _workflow(name="Patient reminders", description="billing")
    out, _, _ = await _check([{"op": "run_workflow", "target": {"kind": "workflow", "name": "Patient reminders"}}])
    assert out["outcome"] == "ask"
    context, _ = await _call("read_change_context", {}, await _run(run_id="builder_next"))
    waiting = context["waiting_pick"]
    assert waiting["question"] == out["question"]
    assert first["id"] in {o["id"] for o in waiting["picks"][0]["options"]}


async def test_an_undo_and_other_ops_in_one_submit_are_sent_back_not_half_done(client):
    """`undo_of` builds its own ops, so ops beside it were dropped without a word (the branch review's finding 14)."""
    run = await _run()
    out, _ = await _call("submit_change", {"undo_of": "builder_x", "ops": [
        {"op": "schedule_off", "target": {"kind": "schedule", "name": "nightly"}}]}, run)
    assert "undo_of alone" in out["error"]


async def test_a_rename_to_a_name_something_has_is_said_before_the_review(client, role):
    """Two of one name are what makes a person pick by id (F760): a rename does not make a second one, as a create
    does not (the branch review's finding 14)."""
    await _agent(name="Front desk")
    other = await _agent(name="Reception")
    out, _, _ = await _check([{"op": "rename_agent", "target": {"kind": "agent", "id": other["id"]},
                               "fields": {"name": "front desk"}}])
    assert out["outcome"] == "said" and out["say"] == 'An agent is already called "front desk": say another name.'


async def test_two_hand_overs_are_said_as_two_requests_not_one_followed(client, role):
    """A hand-over goes on as its own route for the rest of the run, so only one can be taken: the first was, and the
    others were dropped without a word (the branch review's finding 14)."""
    await _workflow(name="Invoice sync")
    out, _, _ = await _check([
        {"op": "edit_steps", "target": {"kind": "workflow", "name": "Invoice sync"}, "fields": {"what": "add a step"}},
        {"op": "create", "target": {"kind": "skill", "name": "Polite replies"}, "fields": {"what": "a skill"}}])
    assert out["outcome"] == "said"
    assert "Invoice sync" in out["say"] and "Polite replies" in out["say"] and "one at a time" in out["say"]


async def test_the_change_check_takes_back_what_its_pick_list_printed(client, role):
    """The change route's question lists `1. "name" (id) — detail`; a target written back in that form, quoted or
    not, picks by its id (QA1c's F760 data point, 10-23)."""
    await _workflow(name="Patient reminders", description="front desk")
    second = await _workflow(name="Patient reminders", description="billing")
    for name in (f'Patient reminders ({second["id"]})', f'"Patient reminders" ({second["id"]}) — billing'):
        out, [card], _ = await _check([{"op": "run_workflow", "target": {"kind": "workflow", "name": name}}])
        assert card["buttons"][0]["arguments"]["orchestration_id"] == second["id"], name
