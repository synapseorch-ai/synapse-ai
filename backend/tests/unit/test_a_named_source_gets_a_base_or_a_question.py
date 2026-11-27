"""A source the person names gets a knowledge base or a question, and "What I assumed" never says a thing here is
missing (F1763).

Asked to draft replies "from our office plans" in a workspace whose "Help centre" base holds them, the planner once
made a new empty base, which F1742's question caught, and once gave its agent no base at all, writing that the plans
were "not yet in a knowledge base" and that the "Send email" tool here was "not yet connected"; the draft that ran had
no facts. Now a source of their own the person names ("from our office plans", "using my price list"), while the plan
gives its agents no base at all and bases are here, goes on F1302's form as a choice: every base here by its name, or
none, saying what the agent then works from. A pick puts that base on the agents that write or answer, in code. And a
line of `assumed` saying a thing here is missing is sent back once with what is true; a second plan's line is said
by code as it is.

Near misses ask nothing: a run's own input ("from the form", "from each card", "from what the customer wrote"), a
customer's own thing ("based on their order"), a person ("from our team"), a board here ("from our Sales board"), a
manner ("using our brand voice"), no source at all, no base here, Knowledge off, a plan that already uses a base (one
here, a new one, a retrieve step), and no new agent that writes. And a tool whose app is set up but not connected, or
one the plan makes, is never said to be here.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import core.builder_tools  # noqa: F401 — loads the builder's modules in the product's order
from core.native_builder import build_plan as B
from core.native_builder import objects as O

MESSAGE = ("Make a sales desk for office enquiries on the Sales board. Its agent claims the oldest open card, drafts "
           "a short, warm reply from our office plans, and closes the card with the draft. Then it shows me the email "
           "to approve, and sends it with Send email if I approve.")
AGENT = "Sales Desk Agent"
LABEL = ('You said "from our office plans", but the plan gives "Sales Desk Agent" no knowledge base to work from. '
         "Use one you already have?")
DRAFT_STEP = {"id": "claim", "type": "agent", "name": "Claim & Draft", "agent": AGENT,
              "does": "Claims the oldest open card and drafts a short warm reply from the office plans",
              "prompt": "Claim the oldest card and draft a reply."}
FIND = {"id": "find", "type": "retrieve", "name": "Find", "does": "Finds the plan",
        "config": {"retrieve": {"kb_ids": ["Help centre"], "query": "{{ state.input }}"}}}


def _ws(*bases, kbs_on=True):
    return B.Workspace(
        kbs={f"kb_{i}": SimpleNamespace(id=f"kb_{i}", name=n) for i, n in enumerate(bases)} if kbs_on else None,
        tools=[{"name": "send_email", "does": "Sends an email."}, {"name": "board_list", "does": "Lists cards."}],
        shared_boards=["Sales"], agents={"agent_old": {"name": "Front door"}})


WS = _ws("Help centre")


def _draft(**more):
    return {"agents": [{"name": AGENT, "purpose": "Drafts replies to enquiries", "tools": ["board_list"]}],
            "steps": [DRAFT_STEP], "knowledge": [], **more}


class TestWhenItAsks:
    def test_a_source_they_named_with_no_base_in_the_plan_is_one(self):
        assert B.unbased_sources(_draft(), WS, [MESSAGE]) == [("from our office plans", [AGENT])]

    @pytest.mark.parametrize("said, phrase", [
        ("Answer each enquiry using our price list.", "using our price list"),
        ("Write the reply based on my notes on returns, then send it.", "based on my notes"),
        ("Reply to each lead according to our pricing and close the card.", "according to our pricing"),
    ])
    def test_other_ways_of_naming_one(self, said, phrase):
        assert B.unbased_sources(_draft(), WS, [said]) == [(phrase, [AGENT])]

    @pytest.mark.parametrize("said", [
        "Its agent drafts a short, warm reply from the form.",
        "Its agent drafts a reply from each card.",
        "Its agent drafts a reply from what the customer wrote.",
        "Its agent drafts a reply based on their order.",
        "Its agent drafts a reply from our team.",
        "Its agent drafts a reply from our Sales board.",
        "Its agent drafts a reply using our brand voice.",
        "Its agent drafts a reply and sends it from our account.",
        "Its agent drafts a short, warm reply.",
        "Sort each card from our backlog by date.",
        "Its agent drafts a reply using our Send email.",
    ])
    def test_a_near_miss_is_none(self, said):
        assert B.unbased_sources(_draft(), WS, [said]) == []

    def test_no_base_here_or_knowledge_off_is_none(self):
        assert B.unbased_sources(_draft(), _ws(), [MESSAGE]) == []
        assert B.unbased_sources(_draft(), _ws("Help centre", kbs_on=False), [MESSAGE]) == []

    @pytest.mark.parametrize("more", [
        {"agents": [{"name": AGENT, "purpose": "Drafts replies", "knowledge": ["Help centre"]}]},
        {"knowledge": [{"name": "Office plans"}]},
        {"steps": [FIND, DRAFT_STEP]},
    ])
    def test_a_plan_that_uses_a_base_is_none(self, more):
        assert B.unbased_sources(_draft(**more), WS, [MESSAGE]) == []

    def test_a_source_named_as_the_base_the_plan_attaches_is_none(self):
        draft = _draft(agents=[{"name": AGENT, "purpose": "Drafts replies", "knowledge": ["Help centre"]}])
        assert B.unbased_sources(draft, WS, ["Its agent drafts a reply from our Help centre."]) == []

    def test_no_new_agent_that_works_is_none(self):
        draft = {"agents": [], "steps": [{**DRAFT_STEP, "agent": "Front door"}], "knowledge": []}
        assert B.unbased_sources(draft, WS, [MESSAGE]) == []

    def test_the_agents_are_the_ones_that_write(self):
        sort = {"id": "sort", "type": "agent", "name": "Sort", "agent": "Sorter", "does": "Sorts the cards by age"}
        draft = _draft(agents=[{"name": AGENT, "purpose": "Drafts"}, {"name": "Sorter", "purpose": "Sorts"}],
                       steps=[sort, DRAFT_STEP])
        assert B.unbased_sources(draft, WS, [MESSAGE]) == [("from our office plans", [AGENT])]


class TestTheForm:
    @pytest.fixture
    def asked(self, monkeypatch):
        from core.native_builder import addresses

        run = SimpleNamespace(step_history=[], shared_state={})
        monkeypatch.setattr(B, "_run", lambda: ("builder_f1763", run))
        monkeypatch.setattr(addresses, "unsaid", lambda draft, given: [])

        async def given(state, ws, asked):
            return []

        monkeypatch.setattr(B, "_given", given)
        return run

    async def test_it_is_a_choice_of_every_base_here_or_none_in_their_words(self, asked):
        out, _ = await B._asked_first({}, _draft(), [], _ws("Zebra notes", "Help centre"), {"user_message": MESSAGE})
        assert out["outcome"] == "ask" and out["question"].startswith(B.UNSAID_LEAD)
        [field] = out["form"]
        assert field["type"] == "choice" and field["label"] == LABEL
        assert field["options"] == ["Help centre", "Zebra notes", B.NO_BASE]
        assert B.NO_BASE == "None: it works only from what each run gives it"
        assert "required" not in field["label"].lower() and "must" not in field["label"].lower()

    async def test_once_asked_it_is_not_asked_again(self, asked):
        state = {"user_message": MESSAGE}
        await B._asked_first({}, _draft(), [], WS, state)
        out, _ = await B._asked_first({}, _draft(), [], WS, state)
        assert out is None

    async def test_past_the_cap_the_card_says_it(self, asked, monkeypatch):
        from core.native_builder import change_tools

        monkeypatch.setattr(change_tools, "questions_asked", lambda state, run: 99)
        out, said = await B._asked_first({}, _draft(), [], WS, {"user_message": MESSAGE})
        assert out is None
        assert said == [B.source_unbased_said("from our office plans", [AGENT])]
        assert said[0].startswith('You said "from our office plans", but "Sales Desk Agent" has no knowledge base')

    async def test_a_near_miss_asks_nothing(self, asked):
        out, _ = await B._asked_first({}, _draft(), [], WS, {"user_message": "Its agent drafts a reply from each card."})
        assert out is None


class TestAPick:
    def test_the_base_goes_on_the_agents_that_write_and_the_line_saying_none_goes(self):
        draft = _draft(assumed=["The office plans the agent drafts from are not yet in a knowledge base.",
                                "Each card holds the sender's email."])
        picked = B.source_picked(draft, [{"question": LABEL, "answer": "help centre"}], WS)
        assert picked["knowledge"] == [{"name": "Help centre"}]
        assert picked["agents"][0]["knowledge"] == ["Help centre"]
        assert picked["assumed"] == ["Each card holds the sender's email."]
        assert draft["knowledge"] == [] and "knowledge" not in draft["agents"][0], "the planner's draft is as it was"

    def test_none_or_an_answer_naming_no_base_here_leaves_the_plan(self):
        for answer in (B.NO_BASE, "the one with the plans", ""):
            assert B.source_picked(_draft(), [{"question": LABEL, "answer": answer}], WS) == _draft()

    def test_once_picked_it_is_not_asked_about(self):
        picked = B.source_picked(_draft(), [{"question": LABEL, "answer": "Help centre"}], WS)
        assert B.unbased_sources(picked, WS, [MESSAGE]) == []


def _plan(assumed, tools=()):
    return O.Plan(kind="create", title="Sales desk", summary="Drafts replies.", assumed=list(assumed),
                  tools=list(tools))


class TestWhatIAssumed:
    @pytest.fixture(autouse=True)
    def own_names(self, monkeypatch):
        from core.routes import tools

        async def custom():
            return [{"name": "send_email", "generalName": "Send email"}]

        monkeypatch.setattr(tools, "load_custom_tools", custom)

    async def test_a_tool_base_or_board_here_said_missing_is_said_as_it_is(self):
        plan = _plan(["Send email is not yet connected — a Connect card will appear after saving.",
                      "The Help centre is not available to this agent yet.",
                      "The board Sales isn't set up.",
                      "Each card holds the sender's email."])
        assert await B.assumed_as_here(plan, WS) == [
            "Send email is a tool here already.", "“Help centre” is a knowledge base here already.",
            'The board "Sales" is here already.', "Each card holds the sender's email."]

    async def test_there_is_no_says_only_of_the_thing_it_names(self):
        plan = _plan(["There is no card on the Sales board when it starts, so it ends.",
                      "There is no Send email tool here yet."])
        assert await B.assumed_as_here(plan, WS) == [
            "There is no card on the Sales board when it starts, so it ends.", "Send email is a tool here already."]

    async def test_an_app_set_up_but_not_connected_and_a_tool_the_plan_makes_are_never_here(self):
        ws = B.Workspace(kbs=None, tools=[{"name": "gmail_send"}, {"name": "send_email"}])
        plan = _plan(["Gmail send is not connected right now.", "Orders lookup is not yet set up."],
                     tools=[O.ToolStatus(name="gmail_send", status="present", detail="its app isn't connected now"),
                            O.ToolStatus(name="orders_lookup", status="found", via="http_tool")])
        assert await B.assumed_as_here(plan, ws) == plan.assumed

    async def test_it_is_sent_back_once_with_what_is_true(self):
        state: dict = {}
        plan = _plan(["Send email is not yet connected."])
        [finding] = await B._said_missing(plan, WS, state)
        assert finding.startswith('assumed: "Send email is not yet connected." says something is missing that is here: '
                                  "Send email is a tool here already.")
        assert await B._said_missing(plan, WS, state) == [], "once a run"
