"""A new knowledge base that starts empty is said, and the bases already here are offered instead (F1742).

Asked to answer "from our office plans" in a workspace whose "Help centre" base holds them, the planner, which sees
only bases' names, made a new "Office plans" base with nothing in it, and the card never said so: each run answered
"I don't have the exact details … I'll come back to you". Now a new base the plan makes with no files, no pages and
no ingest step filling it, while other bases are here, goes on F1302's form as a choice: every base here by its name,
or keep the new one and add documents to it. A pick swaps the plan to that base in code (its part, its agents'
knowledge, its retrieve and ingest steps), so nothing new is made. No reading of what they meant: the bases here, by
name, and their pick.

Near misses read as before: a new base given Vault files or pages, one an ingest step of the plan fills, a base named
by its exact name, Knowledge off, and no other base here.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import core.builder_tools  # noqa: F401 — loads the builder's modules in the product's order
from core.native_builder import build_plan as B

MESSAGE = "Answer each sales enquiry from our office plans, and draft the reply for me to send."
LABEL = ('The plan makes a new knowledge base "Office plans", which starts empty, so its agents can\'t answer from it '
         'until documents are added. Answer from one you already have instead?')
KEEP = 'Keep "Office plans" new and add documents to it'


def _ws(*names):
    return B.Workspace(kbs={f"kb_{i}": SimpleNamespace(id=f"kb_{i}", name=n) for i, n in enumerate(names)})


WS = _ws("Zebra notes", "Help centre")
FIND = {"id": "find", "type": "retrieve", "name": "Find", "does": "Finds the plan",
        "config": {"retrieve": {"kb_ids": ["Office plans"], "query": "{{ state.input }}"}}}
KEEP_IT = {"id": "keep", "type": "ingest", "name": "Keep", "does": "Keeps the note",
           "config": {"ingest": {"kb_id": "Office plans"}}}


def _draft(**part):
    return {"knowledge": [{"name": "Office plans", **part}], "steps": [FIND],
            "agents": [{"name": "Sales desk", "knowledge": ["Office plans"]}]}


class TestWhichBaseIsNewAndEmpty:
    def test_a_new_base_with_nothing_in_it_is_one(self):
        assert B.empty_new_knowledge(_draft(), WS) == ["Office plans"]

    @pytest.mark.parametrize("part", [{"files": ["help/offices.md"]}, {"urls": ["https://example.com/offices"]}])
    def test_one_given_files_or_pages_is_not(self, part):
        assert B.empty_new_knowledge(_draft(**part), WS) == []

    def test_one_an_ingest_step_of_the_plan_fills_is_not(self):
        draft = {**_draft(), "steps": [FIND, KEEP_IT]}
        assert B.empty_new_knowledge(draft, WS) == []
        inner = {**_draft(), "inner_workflows": [{"title": "Keep notes", "steps": [KEEP_IT]}]}
        assert B.empty_new_knowledge(inner, WS) == [], "an inner workflow's ingest fills it too"

    def test_a_base_named_by_its_exact_name_is_used_not_made(self):
        draft = {"knowledge": [{"name": "help centre"}], "agents": [{"name": "Sales desk", "knowledge": ["Help centre"]}]}
        assert B.empty_new_knowledge(draft, WS) == []

    def test_knowledge_off_or_no_other_base_here_is_as_before(self):
        assert B.empty_new_knowledge(_draft(), B.Workspace(kbs=None)) == []
        assert B.empty_new_knowledge(_draft(), _ws()) == []


class TestTheForm:
    @pytest.fixture
    def asked(self, monkeypatch):
        from core.native_builder import addresses

        run = SimpleNamespace(step_history=[], shared_state={})
        monkeypatch.setattr(B, "_run", lambda: ("builder_f1742", run))
        monkeypatch.setattr(addresses, "unsaid", lambda draft, given: [])

        async def given(state, ws, asked):
            return []

        monkeypatch.setattr(B, "_given", given)
        return run

    async def test_it_goes_on_f1302s_form_as_a_choice_of_every_base_here_or_keep(self, asked):
        out, _ = await B._asked_first({}, _draft(), [], WS, {"user_message": MESSAGE})
        assert out["outcome"] == "ask" and out["question"].startswith(B.UNSAID_LEAD)
        [field] = out["form"]
        assert field["type"] == "choice" and field["label"] == LABEL
        assert field["options"] == ["Help centre", "Zebra notes", KEEP], "every base by name, then keep"

    async def test_every_base_is_offered_however_many(self, asked):
        names = [f"Base {chr(65 + i)}" for i in range(14)]
        out, _ = await B._asked_first({}, _draft(), [], _ws(*reversed(names)), {"user_message": MESSAGE})
        assert out["form"][0]["options"] == [*names, KEEP]

    async def test_once_asked_it_is_not_asked_again(self, asked):
        state = {"user_message": MESSAGE}
        await B._asked_first({}, _draft(), [], WS, state)
        out, _ = await B._asked_first({}, _draft(), [], WS, state)
        assert out is None

    async def test_a_near_miss_asks_nothing(self, asked):
        out, _ = await B._asked_first({}, _draft(files=["help/offices.md"]), [], WS, {"user_message": MESSAGE})
        assert out is None


class TestAPick:
    def test_a_base_here_takes_the_new_ones_place_everywhere(self):
        draft = {**_draft(), "steps": [FIND, {**KEEP_IT, "id": "save"}],
                 "inner_workflows": [{"title": "More", "steps": [{**FIND, "id": "again"}]}]}
        draft["knowledge"][0]["files"] = []
        picked = B.knowledge_picked(draft, [{"question": LABEL, "answer": "help centre"}], WS)
        assert [k["name"] for k in picked["knowledge"]] == ["Help centre"]
        assert picked["agents"][0]["knowledge"] == ["Help centre"]
        assert picked["steps"][0]["config"]["retrieve"]["kb_ids"] == ["Help centre"]
        assert picked["steps"][1]["config"]["ingest"]["kb_id"] == "Help centre"
        assert picked["inner_workflows"][0]["steps"][0]["config"]["retrieve"]["kb_ids"] == ["Help centre"]
        assert draft["knowledge"][0]["name"] == "Office plans", "the planner's draft is left as it was"

    def test_keep_or_any_other_answer_leaves_the_plan_as_it_is(self):
        for answer in (KEEP, "the one with the office plans", ""):
            assert B.knowledge_picked(_draft(), [{"question": LABEL, "answer": answer}], WS) == _draft()

    def test_a_base_the_plan_already_uses_is_not_named_twice(self):
        draft = {**_draft(), "knowledge": [{"name": "Office plans"}, {"name": "Help centre"}],
                 "agents": [{"name": "Sales desk", "knowledge": ["Office plans", "Help centre"]}]}
        picked = B.knowledge_picked(draft, [{"question": LABEL, "answer": "Help centre"}], WS)
        assert [k["name"] for k in picked["knowledge"]] == ["Help centre"]
        assert picked["agents"][0]["knowledge"] == ["Help centre"]
