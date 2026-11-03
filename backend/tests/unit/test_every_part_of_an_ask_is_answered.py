"""Every part of an ask is answered (F1302, TRI1; CAP3's tier 1 cap-02, A1 and A2; Naveen 11-03: build as restated).

"Answer customer questions from our help docs, and look up an order's status in our orders API" got half an answer
twice, two ways, both in the plan:

* A1 made one agent with both tools but answered from "Clinic FAQ", a knowledge base already here that the person never
  named, so "how long do I have to return something?" missed the 41 days in their own docs. The pack lists every base by
  name, so F1253's form counted it as given. Now an existing base the plan uses whose exact name the person never said
  is their call: it goes on the same form, the planner's pick filled in, to keep or change. Whether the name was said,
  and nothing more: no reading of what they meant.
* A2 put the order lookup in a `tool` step behind a classify-and-route, apart from the agent people talk to, which then
  said "I can't help with order lookups". # Structure's rules 1–2 (a fetch, one known call: a `tool` step) are for a
  workflow's fixed steps; an agent people ask things of holds every tool and knowledge base its questions need. That
  is the planner's judgement, so it is a prompt line: code can't tell "the agent that answers" from the graph without
  misreading a fair route-to-a-tool workflow.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

import core.builder_tools  # noqa: F401 — loads the builder's modules in the product's order
from core.native_builder import build_plan as B
from core.native_builder.seed import bundled_definitions

MESSAGE = "Answer customer questions from our help docs, and look up an order's status in our orders API."


def _ws(**names):
    return B.Workspace(kbs={i: SimpleNamespace(id=i, name=n) for i, n in names.items()})


WS = _ws(kb_faq="Clinic FAQ", kb_price="Price list")


class TestAnExistingBaseNobodyNamed:
    def test_a1s_pick_is_the_persons_call(self):
        draft = {"agents": [{"name": "Support", "knowledge": ["Clinic FAQ"], "tools": ["orders_api_getorderstatus"]}]}
        assert B.unnamed_knowledge(draft, WS, [MESSAGE]) == ["Clinic FAQ"]

    @pytest.mark.parametrize("said", ["Answer from the Clinic FAQ please", "use clinic faq for the answers"])
    def test_a_base_they_named_is_theirs(self, said):
        draft = {"agents": [{"name": "Support", "knowledge": ["Clinic FAQ"]}]}
        assert B.unnamed_knowledge(draft, WS, [said]) == []

    def test_a_named_base_in_an_answer_counts(self):
        draft = {"agents": [{"name": "Support", "knowledge": ["Clinic FAQ"]}]}
        assert B.unnamed_knowledge(draft, WS, [MESSAGE, {"answer": "The Clinic FAQ, yes"}]) == []

    def test_steps_name_bases_by_id_or_name(self):
        draft = {"steps": [{"id": "find", "type": "retrieve", "config": {"retrieve": {"kb_ids": ["kb_faq"]}}},
                           {"id": "keep", "type": "ingest", "config": {"ingest": {"kb_id": "Price list"}}}]}
        assert B.unnamed_knowledge(draft, WS, [MESSAGE]) == ["Clinic FAQ", "Price list"]

    def test_a_base_the_build_makes_is_not_a_pick(self):
        draft = {"knowledge": [{"name": "Help Docs", "urls": ["http://localhost:9413"], "kind": "site"}],
                 "agents": [{"name": "Support", "knowledge": ["Help Docs"]}]}
        assert B.unnamed_knowledge(draft, WS, [MESSAGE]) == []

    def test_no_knowledge_here_is_nothing_to_ask(self):
        draft = {"agents": [{"name": "Support", "knowledge": ["Clinic FAQ"]}]}
        assert B.unnamed_knowledge(draft, B.Workspace(kbs=None), [MESSAGE]) == []


class TestTheForm:
    @pytest.fixture
    def asked(self, monkeypatch):
        from core.native_builder import addresses

        run = SimpleNamespace(step_history=[], shared_state={})
        monkeypatch.setattr(B, "_run", lambda: ("builder_f1302", run))
        monkeypatch.setattr(addresses, "unsaid", lambda draft, given: [])

        async def given(state, ws, asked):
            return []

        monkeypatch.setattr(B, "_given", given)
        return run

    async def test_it_goes_on_the_same_form_with_the_pick_filled_in(self, asked):
        state = {"user_message": MESSAGE}
        draft = {"agents": [{"name": "Support", "knowledge": ["Clinic FAQ"]}]}
        out, said = await B._asked_first({}, draft, [], WS, state)
        assert said == [] and out["outcome"] == "ask"
        [field] = out["form"]
        assert field["value"] == "Clinic FAQ" and "Clinic FAQ" in field["label"] and "didn't name" in field["label"]
        assert out["question"].startswith(B.UNSAID_LEAD)

    async def test_once_kept_it_is_not_asked_again(self, asked):
        state = {"user_message": MESSAGE}
        draft = {"agents": [{"name": "Support", "knowledge": ["Clinic FAQ"]}]}
        await B._asked_first({}, draft, [], WS, state)
        out, _ = await B._asked_first({}, draft, [], WS, state)
        assert out is None

    async def test_a_named_base_asks_nothing(self, asked):
        state = {"user_message": MESSAGE + " Use the Clinic FAQ."}
        draft = {"agents": [{"name": "Support", "knowledge": ["Clinic FAQ"]}]}
        out, _ = await B._asked_first({}, draft, [], WS, state)
        assert out is None


class TestTheAgentPeopleAskHoldsItsTools:
    @pytest.fixture(scope="class")
    def structure(self):
        prompt = next(a for a in bundled_definitions()["agents"]
                      if a["id"] == "agent_native_builder_planner")["system_prompt"]
        start = prompt.index("# Structure")
        return prompt[start:prompt.index("\n# ", start + 1)]

    def test_rules_1_and_2_are_for_fixed_steps(self, structure):
        assert "Rules 1 and 2 are for a workflow's fixed steps" in structure
        assert "holds every tool and knowledge base its questions need" in structure

    def test_the_numbering_stands(self, structure):
        """An exception to rules 1–2, not a new rule: the section still counts 1 to 5."""
        assert re.findall(r"^(\d+)\. ", structure, re.M) == ["1", "2", "3", "4", "5"]
