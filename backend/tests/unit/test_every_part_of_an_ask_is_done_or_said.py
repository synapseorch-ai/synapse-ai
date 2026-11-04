"""Every part of an ask is done or said, and an agent is asked only what it can answer (F1364, TRI1; CAP3's FF18 gate,
cap-02; Naveen 11-04: "Build as planned"; eb's changes).

cap-02's person asked for three things in one message; the change planner submitted one op for the first, and the other
two were never done or mentioned. Its Ask card then went to an agent holding none of the tools the question needed, so
the answer came from nothing it could read.

(a) `submit_change` takes `asked`: each part of the ask in the person's words, with the numbers of the ops that do it,
or `why_not`. A part with neither, an op no part names, and an op number that is no op are sent back, at most
`ASKED_SEND_BACKS` times in a run, then the change goes on as written. One op may serve several parts. What the parts
say isn't done, and why, is said with the change: under its review, or with what it says. A change sent without
`asked` is taken as it was (code can't see a part nobody wrote down); a one-part, one-op change passes untouched.

(b) talk_to_agent takes `needs`, the tools and knowledge bases its answer needs: the card goes only to an agent that
holds them (a tool it is given, or every tool; a knowledge base it answers from), otherwise it is said what the agent
lacks, how to give it, and which agents have it.
"""
from __future__ import annotations

import json

import pytest

from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import change_ops as C
from core.native_builder import change_tools
from core.native_builder import models_menu as MM
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

    async def owner(user_id):
        return Role.OWNER

    hooks.set_role_source(owner)
    yield
    hooks.set_role_source(None)
    builder_access.forget_runs()


_N = {"n": 0}


async def _run(**state):
    _N["n"] += 1
    run = OrchestrationRun(run_id=f"builder_parts_{_N['n']}", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, **state})
    await SharedState(run).checkpoint()
    return run


async def _call(tool, args, run):
    with live(run):
        raw, events = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return json.loads(raw), events


def _off(name):
    return {"op": "schedule_off", "target": {"kind": "schedule", "name": name}, "says": f"pause {name}"}


# Three things in one message, as cap-02's person asked (neutral words: no brief's).
NIGHTLY, WEEKLY = _off("nightly"), _off("weekly")
THREE = ["pause the nightly schedule", "send me a summary of last week's runs", "add a reminder for Mondays"]


class TestSentBack:
    async def test_parts_no_op_does_and_no_reason_covers_are_sent_back(self, client):
        """cap-02: one op for the first part; the other two named, with nothing done and nothing said."""
        out, _ = await _call("submit_change", {"ops": [NIGHTLY], "asked": [
            {"part": THREE[0], "ops": [1]}, {"part": THREE[1]}, {"part": THREE[2], "ops": []}]}, await _run())
        assert out["error"].startswith("Fix these and call submit_change again: "), out
        assert f'part "{THREE[1]}": no op does it and no why_not says why' in out["error"]
        assert f'part "{THREE[2]}": no op does it and no why_not says why' in out["error"]
        assert THREE[0] not in out["error"]

    async def test_an_op_no_part_names_is_sent_back(self, client):
        out, _ = await _call("submit_change", {"ops": [NIGHTLY, WEEKLY], "asked": [{"part": THREE[0], "ops": [1]}]},
                             await _run())
        assert "op 2: no part of the ask names it; put it under the part it serves" in out["error"], out
        assert "op 1:" not in out["error"]

    async def test_an_op_number_that_is_no_op_is_sent_back(self, client):
        out, _ = await _call("submit_change", {"ops": [NIGHTLY], "asked": [{"part": THREE[0], "ops": [1, 3]}]},
                             await _run())
        assert f'part "{THREE[0]}": op 3 is not one of the ops' in out["error"], out

    async def test_sent_back_twice_then_it_goes_on_as_written(self, client):
        run = await _run()
        gap = {"ops": [NIGHTLY], "asked": [{"part": THREE[0], "ops": [1]}, {"part": THREE[1]}]}
        for _ in range(change_tools.ASKED_SEND_BACKS):
            assert "error" in (await _call("submit_change", gap, run))[0]
        out, _ = await _call("submit_change", gap, run)
        assert out.get("finish") == "submit_change", out
        assert change_tools.ASKED_SEND_BACKS == 2


class TestTaken:
    async def test_a_one_part_one_op_change_passes_untouched(self, client):
        out, _ = await _call("submit_change", {"ops": [NIGHTLY], "asked": [{"part": THREE[0], "ops": [1]}]},
                             await _run())
        assert out == {"finish": "submit_change", "ops": [{**NIGHTLY, "fields": {}}], "questions": [],
                       "asked": [{"part": THREE[0], "ops": [1]}]}

    async def test_a_change_without_asked_is_taken_as_it_was(self, client):
        out, _ = await _call("submit_change", {"ops": [NIGHTLY]}, await _run())
        assert out == {"finish": "submit_change", "ops": [{**NIGHTLY, "fields": {}}], "questions": []}

    async def test_one_op_may_serve_several_parts_and_a_part_several_ops(self, client):
        out, _ = await _call("submit_change", {"ops": [NIGHTLY, WEEKLY], "asked": [
            {"part": "pause both schedules", "ops": [1, 2]}, {"part": "stop the nightly emails", "ops": [1]}]},
            await _run())
        assert out.get("finish") == "submit_change", out

    async def test_parts_not_done_with_a_reason_are_taken(self, client):
        asked = [{"part": THREE[0], "ops": [1]},
                 {"part": THREE[1], "why_not": "a card can't write it: ask for it as a run"},
                 {"part": THREE[2], "why_not": "another route builds a reminder"}]
        out, _ = await _call("submit_change", {"ops": [NIGHTLY], "asked": asked}, await _run())
        # Typed: a part done by no op has `ops: []`.
        assert out.get("finish") == "submit_change" and out["asked"] == [{"ops": [], **p} for p in asked], out


class TestSaid:
    async def test_what_isnt_done_is_said_under_the_review(self, client):
        """The review's own lines are unchanged (its approval digest covers ops and calls); what isn't done is said
        under them, as the review's human_prompt shows `change_checked.say`."""
        from core.store.resources import save_agent
        from tests._fakes.seed import make_agent

        await save_agent(make_agent(name="Front desk"))
        op = {"op": "rename_agent", "target": {"kind": "agent", "name": "Front desk"}, "fields": {"name": "Harbor desk"},
              "says": "rename it"}
        run = await _run(user_message='rename the agent to "Harbor desk" and send me last week\'s runs')
        bare, _ = await _call("check_change", {"draft": {"finish": "submit_change", "ops": [op]}}, run)
        told, _ = await _call("check_change", {"draft": {"finish": "submit_change", "ops": [op], "asked": [
            {"part": "rename the agent", "ops": [1]},
            {"part": "send me last week's runs", "why_not": "a run sends it, not a change"}]}}, await _run(
            user_message='rename the agent to "Harbor desk" and send me last week\'s runs'))
        assert bare["outcome"] == told["outcome"] == "review", (bare, told)
        assert told["review"] == bare["review"] and told["ops"] == bare["ops"]
        assert told["say"].endswith("Not done by this change: send me last week's runs: a run sends it, not a "
                                    "change."), told["say"]
        assert "Not done" not in bare["say"]

    def test_only_parts_with_no_op_are_said(self):
        assert change_tools.not_done([{"part": "a", "ops": [1]}, {"part": "b", "ops": [1], "why_not": "x"}]) == ""
        assert change_tools.not_done([{"part": "b", "why_not": "x"}, {"part": "c", "why_not": "y"}]) == \
            "Not done by this change: b: x; c: y."
        assert change_tools.not_done(None) == ""

    def test_the_review_shows_what_the_check_says(self):
        """Where the person reads it: the change review's prompt carries `change_checked.say` after the review."""
        from pathlib import Path

        flow = json.loads((Path(change_tools.__file__).parent / "orchestration.json").read_text("utf-8"))
        prompts = [s.get("human_prompt") or "" for s in flow["steps"]]
        prompts = [p for p in prompts if "change_checked.review" in p]
        assert prompts and all(p.index("change_checked.review") < p.index("change_checked.say") for p in prompts)


# ── (b) an agent is asked only what it can answer ──────────────────────────────────────────────────────────────

def _entry(id):
    return MM.ModelEntry(id=id, name=id, provider="p", source="key", tools="native", vision=False, reasoning=False,
                         price_in=3.0, price_out=15.0, can_pay=True)


@pytest.fixture
async def agents(monkeypatch):
    from core.store.resources import save_agent
    from tests._fakes.seed import make_agent

    made = {name: make_agent(name=name, model="priced-model", tools=tools)
            for name, tools in (("Notes bot", []), ("Orders bot", ["lookup_order"]), ("Everything bot", ["all"]))}
    for agent in made.values():
        await save_agent(agent)

    async def menu():
        return [_entry("priced-model")]

    monkeypatch.setattr(MM, "menu", menu)
    return made


async def _prepare(args):
    run = await _run()
    out, events = await _call("prepare_action", {"action": "talk_to_agent", "message": "Where is order 1042?",
                                                 **args}, run)
    return out, [e for e in events if e.get("type") == "action_needed"]


class TestTheCard:
    async def test_an_agent_without_the_tool_gets_no_card_and_who_has_it_is_said(self, client, agents):
        out, cards = await _prepare({"agent": "Notes bot", "needs": ["lookup_order"]})
        assert cards == [], cards
        assert out["error"] == ('"Notes bot" has no `lookup_order`: give it the tool first (set_agent_tools), or ask an '
                                'agent that has it. "Orders bot", "Everything bot" have it.'), out

    async def test_an_agent_holding_the_tool_or_every_tool_is_asked(self, client, agents):
        for name in ("Orders bot", "Everything bot"):
            _, [card] = await _prepare({"agent": name, "needs": ["lookup_order"]})
            assert card["title"] == f'Ask "{name}"' and card["buttons"][0]["tool"] == "chat_with_agent"

    async def test_no_needs_is_the_card_as_it_was(self, client, agents):
        _, [card] = await _prepare({"agent": "Notes bot"})
        assert card["title"] == 'Ask "Notes bot"'

    @pytest.mark.usefixtures("knowledge_store")
    async def test_a_knowledge_base_it_does_not_answer_from_is_said(self, client, agents):
        from core.knowledge import bases
        from core.knowledge.api_models import KnowledgeBaseCreate
        from core.store.resources import save_agent

        kb = await bases.create_base(KnowledgeBaseCreate(name="Returns policy", description="x",
                                                         folder="knowledge/returns"))
        out, cards = await _prepare({"agent": "Everything bot", "needs": ["returns policy"]})
        assert cards == [] and out["error"].startswith(
            '"Everything bot" does not answer from the knowledge base "Returns policy": add it to its `knowledge` '
            "first (set_agent_settings), or ask an agent that does."), out
        await save_agent({**agents["Notes bot"], "knowledge": [kb.id]})
        _, [card] = await _prepare({"agent": "Notes bot", "needs": [kb.id]})
        assert card["title"] == 'Ask "Notes bot"'

    async def test_through_the_change_route_it_is_said_not_carded(self, client, agents):
        op = {"op": "talk_to_agent", "target": {"kind": "agent", "name": "Notes bot"},
              "fields": {"message": "Where is order 1042?", "needs": ["lookup_order"]}, "says": "ask it"}
        run = await _run(user_message="ask the notes bot where order 1042 is")
        out, events = await _call("check_change", {"draft": {"finish": "submit_change", "ops": [op]}}, run)
        assert [e for e in events if e.get("type") == "action_needed"] == []
        assert "has no `lookup_order`" in json.dumps(out), out


def test_needs_is_talk_to_agents_list_and_still_set_agent_models_word():
    talk, model = C.OPS["talk_to_agent"], C.OPS["set_agent_model"]
    assert "needs" in talk.fields and talk.required == ("message",)
    assert C._field_issue(talk, "needs", ["lookup_order"]) is None
    assert "list of the tools and knowledge bases" in C._field_issue(talk, "needs", "lookup_order")
    assert C._field_issue(model, "needs", ["lookup_order"]).startswith("`needs` is one of")
    assert C.act_arguments(talk, "agent_1", {"message": "Hi", "needs": ["x"]}) == {
        "action": "talk_to_agent", "agent": "agent_1", "message": "Hi", "needs": ["x"]}
