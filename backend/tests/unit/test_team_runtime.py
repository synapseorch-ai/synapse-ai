"""A Team agent's members work in parallel, talk with the lead while they work, and keep a task list
(TEAM1a, D283; 20-agent-teams.md Q1, Q3, Q5, Q6, Q9).

Today's delegate hands work to one member at a time and waits for each (`delegate_to_agent`).
A delegate whose `team` block says `mode: "team"` instead gives its lead `assign`, `message`,
`wait` and `forward`: `assign` returns at once, the member works in a task of its own, and
its result, questions and messages reach the lead's team inbox between the lead's model
calls. Each assignment is a card on the team's run board, claimed by its member, which only
that member may update. A member's events go out wrapped and its record is kept. A delegate
without the block is unchanged (D204).
"""
import asyncio
import json
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import Streamed, tool_call
from _fakes.routed_llm import RoutedLLM, install
from core.tools import VirtualTool

LEAD, ANA, BEN = "lead_t1a", "ana_t1a", "ben_t1a"


def _ann(**kw):
    from mcp.types import ToolAnnotations
    return ToolAnnotations(**kw)


LOOKUP = VirtualTool("lookup", "looks a thing up", {}, _ann(readOnlyHint=True, idempotentHint=True))
DANGER = VirtualTool("danger", "changes things", {}, _ann(readOnlyHint=False, destructiveHint=True))


class _Session:
    def __init__(self):
        self.calls: list[str] = []

    async def send_ping(self):
        return None

    async def call_tool(self, name, args, **_kw):
        self.calls.append(name)
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=f"{name} says ok")], isError=False)


@pytest.fixture
def server(monkeypatch):
    import core.react_engine as re_mod

    async def aggregated(server_module, agent, custom_tools, **_):
        wanted = agent.get("tools") or []
        held = [t for t in (LOOKUP, DANGER) if "all" in wanted or t.name in wanted]
        ollama = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                    "parameters": t.inputSchema}} for t in held]
        return held, {t.name: t.inputSchema for t in held}, ollama, ""

    monkeypatch.setattr(re_mod, "aggregate_all_tools", aggregated)
    session = _Session()
    return types.SimpleNamespace(agent_sessions={"fake": session}, memory_store=None,
                                 tool_router={"lookup": ("fake", "lookup"), "danger": ("fake", "danger")},
                                 session=session)


@pytest.fixture
def llm(monkeypatch):
    return install(monkeypatch, RoutedLLM())


def _lead(**kw):
    return S.make_agent(**{"id": LEAD, "name": "Lead", "type": "delegate", "tools": [],
                           "delegate_agent_ids": [ANA, BEN], "team": {"mode": "team"}, **kw})


@pytest.fixture
async def team():
    agents = [S.make_agent(id=ANA, name="Ana", tools=["lookup"], skip_default_tools=True),
              S.make_agent(id=BEN, name="Ben", tools=["lookup", "danger"], skip_default_tools=True,
                           tool_policy={"destructive": "confirm"}),
              _lead()]
    await S.seed_agents(agents)
    return agents


async def _turn(server, *, agent_id=LEAD, message="PLAN IT", session_id="s_team", **kw):
    from core.react_engine import run_agent_step

    return [ev async for ev in run_agent_step(
        message=message, agent_id=agent_id, session_id=session_id, server_module=server, max_turns=8, **kw)]


def _of(events, kind):
    return [e for e in events if e.get("type") == kind]


def _wrapped(events, kind=None, member=None):
    inner = [e for e in _of(events, "member_event") if member is None or e["member_name"] == member]
    return [e["event"] for e in inner if kind is None or e["event"].get("type") == kind]


def both(*calls):
    """One reply holding several calls, as a model writes parallel calls."""
    return "\n".join(calls)


def assign(member, task):
    return tool_call("assign", member=member, task=task)


def _lead_prompts(llm):
    return [str(c.get("prompt_msg") or "") for c in llm.calls_for("PLAN IT")]


class TestMembersWorkAtOnce:
    async def test_two_members_run_side_by_side(self, server, llm, team):
        """Each member's first model call waits for the other's: run one after another, they would never meet."""
        met = asyncio.Barrier(2)

        async def meet(_kw):
            try:
                await asyncio.wait_for(met.wait(), 3)
                return "met"
            except asyncio.TimeoutError:
                return "alone"

        llm.on("PLAN IT", [both(assign("Ana", "CHECK A"), assign("Ben", "CHECK B")), tool_call("wait", seconds=10),
                           tool_call("wait", seconds=10), "all done"])
        llm.on("CHECK A", [meet]).on("CHECK B", [meet])
        events = await _turn(server)

        records = {f["member_name"]: f["record"] for f in _of(events, "member_finished")}
        assert records["Ana"]["answer"] == "met" and records["Ben"]["answer"] == "met"
        assert _of(events, "final")[-1]["response"] == "all done"

    async def test_assign_returns_at_once_and_results_reach_the_inbox(self, server, llm, team):
        llm.on("PLAN IT", [assign("Ana", "CHECK A"), tool_call("wait", seconds=10), "done: A is fine"])
        llm.on("CHECK A", [tool_call("lookup", item="A"), "A is fine"])
        events = await _turn(server)

        assigned = next(e["preview"] for e in _of(events, "tool_result") if e["tool_name"] == "assign")
        assert assigned.startswith("Assigned to Ana")
        prompts = _lead_prompts(llm)
        assert "Team inbox" in prompts[-1] and "From Ana (it finished):\nA is fine" in prompts[-1]
        assert "automated, not from the person" in prompts[-1]

    async def test_at_once_holds_the_rest_until_a_slot_frees(self, server, llm, team):
        from core.store import resources
        lead = await resources.get_agent(LEAD)
        await S.seed_agents([*(a for a in team if a["id"] != LEAD), {**lead, "team": {"mode": "team", "at_once": 1}}])
        order: list[str] = []

        both_assigned = asyncio.Event()

        def note(label, answer):
            async def reply(_kw):
                order.append(label)
                # Ana is still working when Ben is assigned: one slot, so Ben waits for it.
                await asyncio.wait_for(both_assigned.wait(), 3)
                return answer
            return reply

        def assigned_both(_kw):
            both_assigned.set()
            return tool_call("wait", seconds=10)

        llm.on("PLAN IT", [both(assign("Ana", "CHECK A"), assign("Ben", "CHECK B")), assigned_both,
                           tool_call("wait", seconds=10), "done"])
        llm.on("CHECK A", [note("A", "a")]).on("CHECK B", [note("B", "b")])
        events = await _turn(server)
        previews = [e["preview"] for e in _of(events, "tool_result") if e["tool_name"] == "assign"]
        assert "It has started." in previews[0] and "starts when a slot frees" in previews[1]
        assert order == ["A", "B"]
        assert {f["member_name"] for f in _of(events, "member_finished")} == {"Ana", "Ben"}


class TestAMemberIsSeenAndKept:
    async def test_its_work_streams_wrapped_and_its_record_is_kept(self, server, llm, team):
        llm.on("PLAN IT", [assign("Ana", "CHECK A"), tool_call("wait", seconds=10), "lead answer"])
        llm.on("CHECK A", [tool_call("lookup", item="A"), Streamed(["member ", "words"])])
        events = await _turn(server, stream_answer=True)

        started = _of(events, "member_started")[0]
        assert started["member_name"] == "Ana" and started["task"] == "CHECK A"
        assert [c["tool_name"] for c in _wrapped(events, "tool_execution", "Ana")] == ["lookup"]
        # A member's call and words are its own: never the lead's.
        assert not [e for e in _of(events, "tool_execution") if e["tool_name"] == "lookup"]
        assert not [e for e in _of(events, "answer_delta") if "member" in e.get("text", "")]
        record = _of(events, "member_finished")[0]["record"]
        assert record["status"] == "completed" and record["answer"] == "member words"
        assert [c["tool"] for c in record["calls"]] == ["lookup"]
        finals = _of(events, "final")
        assert len(finals) == 1 and finals[0]["response"] == "lead answer"
        assert finals[0]["members"][0]["answer"] == "member words"

    async def test_the_saved_turn_keeps_its_members_and_task_list(self, server, llm, team):
        from core.session import _get_conversation_history
        llm.on("PLAN IT", [assign("Ana", "CHECK A"), tool_call("wait", seconds=10), "lead answer"])
        llm.on("CHECK A", ["A is fine"])
        await _turn(server, session_id="s_kept")

        turns = await _get_conversation_history("s_kept", agent_id=LEAD)
        saved = turns[-1]
        assert saved["members"][0]["name"] == "Ana" and saved["members"][0]["answer"] == "A is fine"
        assert saved["team"]["tasks"][0]["status"] == "done" and saved["team"]["answered_alone"] is False

    async def test_a_lead_that_answers_alone_says_so(self, server, llm, team):
        llm.on("PLAN IT", ["hello there"])
        events = await _turn(server)
        assert _of(events, "final")[-1]["team"]["answered_alone"] is True
        assert not _of(events, "member_started")


class TestTheTaskList:
    async def test_a_card_per_task_claimed_by_its_member_and_the_board_gone_at_the_end(self, server, llm, team):
        from core.store import boards as store
        seen: dict = {}

        async def look(_kw):
            board = (await store.list_boards("run"))[0]
            cards = await store.list_cards([board.id])
            seen.update(board=board.id, holder=cards[0].holder_label, status=cards[0].status)
            return "A is fine"

        llm.on("PLAN IT", [assign("Ana", "CHECK A"), tool_call("wait", seconds=10), "done"])
        llm.on("CHECK A", [look])
        events = await _turn(server)

        assert seen["holder"] == "Ana" and seen["status"] == "claimed"
        assert await store.get_board(seen["board"]) is None      # deleted with the turn (Naveen, TEAM1a)
        assert _of(events, "final")[-1]["team"]["tasks"][0]["status"] == "done"
        assert "The team's task list" in _lead_prompts(llm)[-1]

    async def test_a_member_updates_only_its_own_card(self, server, llm, team):
        llm.on("PLAN IT", [both(assign("Ana", "CHECK A"), assign("Ben", "CHECK B")), tool_call("wait", seconds=10),
                           tool_call("wait", seconds=10), "done"])

        async def anas(kw):
            for _ in range(100):          # Ben's card is made by the same reply's second call
                if len(_team_tasks(kw)) == 2:
                    break
                await asyncio.sleep(0.01)
            others = [t for t in _team_tasks(kw) if t != "own"]
            return tool_call("board_update", status="done", result="I did B", card_id=others[0])

        llm.on("CHECK A", [anas, "A done"]).on("CHECK B", ["B done"])
        events = await _turn(server)
        refused = [e for e in _wrapped(events, "tool_result", "Ana") if e["tool_name"] == "board_update"]
        assert refused and refused[0]["preview"].startswith("Refused: You can update only your own task")
        tasks = {t["member"]: t for t in _of(events, "final")[-1]["team"]["tasks"]}
        assert tasks["Ben"]["result"] == "B done"

    async def test_a_board_works_with_boards_off(self, server, llm, team):
        from core.boards.config import boards_enabled
        assert not boards_enabled()
        llm.on("PLAN IT", [assign("Ana", "CHECK A"), tool_call("wait", seconds=10), "done"])
        llm.on("CHECK A", ["fine"])
        events = await _turn(server)
        assert _of(events, "final")[-1]["team"]["tasks"][0]["status"] == "done"


def _team_tasks(kw):
    """The card ids a member's prompt can see: its own (from its system prompt) and the team's (from the store)."""
    from core import team as team_mod
    t = next(iter(team_mod._LIVE.values()))
    own = next(m.card_id for m in t.members.values() if m.name == "Ana")
    return ["own" if c == own else c for c in t.tasks]


class TestTalkingWhileTheyWork:
    async def test_a_members_question_reaches_the_lead_and_the_answer_reaches_the_member(self, server, llm, team):
        llm.on("PLAN IT", [assign("Ana", "CHECK A"), tool_call("wait", seconds=10),
                           tool_call("message", to="Ana", text="Use the EU list"), tool_call("wait", seconds=10),
                           "done"])
        llm.on("CHECK A", [tool_call("message", to="lead", text="Which list?"), tool_call("wait", seconds=10),
                           "used the EU list"])
        events = await _turn(server)

        lead_prompts = _lead_prompts(llm)
        assert any("Message from Ana: Which list?" in p for p in lead_prompts)
        anas = [str(c.get("prompt_msg") or "") for c in llm.calls_for("CHECK A")]
        assert any("Message from the lead: Use the EU list" in p for p in anas)
        assert _of(events, "member_finished")[0]["record"]["answer"] == "used the EU list"

    async def test_the_persons_message_to_a_member_reads_as_the_persons(self, server, llm, team):
        from core import team as team_mod

        def person_writes(_kw):
            t = next(iter(team_mod._LIVE.values()))
            assert t.message_from_person(next(iter(t.members)), "Keep it short")
            return tool_call("wait", seconds=5)

        llm.on("PLAN IT", [assign("Ana", "CHECK A"), tool_call("wait", seconds=10), "done"])
        llm.on("CHECK A", [person_writes, "short"])
        await _turn(server)
        anas = [str(c.get("prompt_msg") or "") for c in llm.calls_for("CHECK A")]
        assert any("From the person (their own words): Keep it short" in p for p in anas)

    async def test_forward_ends_the_turn_with_the_members_answer_word_for_word(self, server, llm, team):
        llm.on("PLAN IT", [assign("Ana", "CHECK A"), tool_call("wait", seconds=10),
                           tool_call("forward", member="Ana"), "never read"])
        llm.on("CHECK A", ["Price: $19 per seat, $45 for teams."])
        events = await _turn(server)
        assert _of(events, "final")[-1]["response"] == "Price: $19 per seat, $45 for teams."

    async def test_an_answer_while_members_work_is_asked_about_once(self, server, llm, team):
        gate = asyncio.Event()

        async def slow(_kw):
            await asyncio.wait_for(gate.wait(), 3)
            return "late"

        def early(_kw):
            return "answering now"

        def again(_kw):
            gate.set()
            return "answering now, really"

        llm.on("PLAN IT", [assign("Ana", "CHECK A"), early, again])
        llm.on("CHECK A", [slow])
        events = await _turn(server)
        prompts = _lead_prompts(llm)
        assert "Ana is still working" in prompts[2]
        assert _of(events, "final")[-1]["response"] == "answering now, really"

    async def test_a_member_can_be_stopped_alone(self, server, llm, team):
        from core import team as team_mod
        hold = asyncio.Event()

        async def stuck(_kw):
            t = next(iter(team_mod._LIVE.values()))
            ana = next(m for m in t.members.values() if m.name == "Ana")
            assert t.stop_member(ana.id)
            await asyncio.sleep(0)
            return tool_call("lookup", item="A")

        llm.on("PLAN IT", [both(assign("Ana", "CHECK A"), assign("Ben", "CHECK B")), tool_call("wait", seconds=10),
                           tool_call("wait", seconds=10), "done"])
        llm.on("CHECK A", [stuck, "never"]).on("CHECK B", ["B fine"])
        events = await _turn(server)
        records = {f["member_name"]: f["record"] for f in _of(events, "member_finished")}
        assert records["Ana"]["status"] == "cancelled" and records["Ben"]["status"] == "completed"
        assert any("From Ana (it was stopped before it finished)" in p for p in _lead_prompts(llm))
        del hold


class TestWhereATeamWorks:
    async def test_a_delegate_without_the_block_is_one_at_a_time_as_before(self, server, llm, team):
        from core.store import resources
        lead = await resources.get_agent(LEAD)
        await S.seed_agents([*(a for a in team if a["id"] != LEAD), {**lead, "team": None}])
        llm.on("PLAN IT", [tool_call("delegate_to_agent", agent_id=ANA, task="CHECK A"), "done"])
        llm.on("CHECK A", ["A fine"])
        events = await _turn(server)
        tools = {t["function"]["name"] for t in llm.calls_for("PLAN IT")[0]["tools"]}
        assert "delegate_to_agent" in tools and "assign" not in tools
        assert not _of(events, "member_started")

    async def test_a_visitors_turn_runs_the_lead_alone(self, server, llm, team):
        from core import visitor
        llm.on("PLAN IT", ["the lead alone"])
        with visitor.scope(visitor.VisitorScope("widget", tools=frozenset())):
            events = await _turn(server)
        tools = {t["function"]["name"] for t in (llm.calls_for("PLAN IT")[0].get("tools") or [])}
        assert not tools & {"assign", "wait", "message", "forward", "delegate_to_agent"}
        assert _of(events, "final")[-1]["response"] == "the lead alone"

    async def test_a_workflow_step_runs_it_one_at_a_time_until_team1e(self, server, llm, team):
        llm.on("PLAN IT", ["step answer"])
        await _turn(server, source="orchestration")
        tools = {t["function"]["name"] for t in (llm.calls_for("PLAN IT")[0].get("tools") or [])}
        assert "assign" not in tools and "delegate_to_agent" in tools

    async def test_a_member_never_leads(self, server, llm, team):
        llm.on("PLAN IT", [assign("Ana", "CHECK A"), tool_call("wait", seconds=10), "done"])
        llm.on("CHECK A", ["fine"])
        await _turn(server)
        tools = {t["function"]["name"] for t in llm.calls_for("CHECK A")[0]["tools"]}
        assert {"message", "wait", "board_update"} <= tools
        assert not tools & {"assign", "forward", "delegate_to_agent"}


class TestTaintTravelsPerItem:
    async def test_a_result_from_a_member_that_read_nothing_leaves_the_lead_unmarked(self, server, llm, team):
        # A step after the one that read the result, so a snapshot is taken after the read.
        llm.on("PLAN IT", [assign("Ana", "CHECK A"), tool_call("wait", seconds=10), tool_call("wait", seconds=1),
                           "done"])
        llm.on("CHECK A", ["from memory"])
        events = await _turn(server, progress_events=True)
        assert _of(events, "_step_progress")[-1]["memory_tainted"] is False

    async def test_a_result_from_a_member_that_read_a_tool_marks_the_lead(self, server, llm, team):
        llm.on("PLAN IT", [assign("Ana", "CHECK A"), tool_call("wait", seconds=10), tool_call("wait", seconds=1),
                           "done"])
        llm.on("CHECK A", [tool_call("lookup", item="A"), "from a tool"])
        events = await _turn(server, progress_events=True)
        assert _of(events, "_step_progress")[-1]["memory_tainted"] is True


class TestApprovalsOneAtATime:
    """Until TEAM1c: a member waits alone; the turn parks on its question once nobody else works."""

    async def test_the_turn_parks_on_the_members_question_after_the_others_finish(self, server, llm, team):
        llm.on("PLAN IT", [both(assign("Ben", "CHANGE B"), assign("Ana", "CHECK A")), tool_call("wait", seconds=10),
                           tool_call("wait", seconds=10), "should not answer yet"])
        llm.on("CHANGE B", [tool_call("danger"), "changed"]).on("CHECK A", ["A fine"])
        events = await _turn(server, progress_events=True)

        card = _of(events, "human_input_required")[-1]
        assert card["tool_name"] == "danger" and card["delegate_from"] == LEAD
        assert card["delegate_agent_name"] == "Ben"
        pending = _of(events, "_step_progress")[-1]["delegate_pending"]
        assert pending["agent_id"] == BEN and pending["task"] == "CHANGE B"
        assert {f["member_name"]: f["status"] for f in _of(events, "member_finished")} == {"Ana": "completed"}
        assert server.session.calls == []
        assert not _of(events, "final")

    async def test_approving_it_runs_the_call_as_that_member_and_the_team_finishes(self, server, llm, team):
        from core.chat_approval import parked_payload, resume_state_for
        llm.on("PLAN IT", [assign("Ben", "CHANGE B"), tool_call("wait", seconds=10), "unused"])
        llm.on("CHANGE B", [tool_call("danger"), "changed it"])
        first = await _turn(server, progress_events=True)
        card = _of(first, "human_input_required")[-1]
        snap = {k: v for k, v in _of(first, "_step_progress")[-1].items() if k != "type"}
        parked = parked_payload(card, message="PLAN IT", progress=snap)

        llm._scripts.clear()
        llm.on("PLAN IT", [tool_call("wait", seconds=10), "all changed"]).on("CHANGE B", ["changed it"])
        events = await _turn(server, progress_events=True, resume_state=resume_state_for(parked, approve=True))
        assert server.session.calls == ["danger"]
        assert _of(events, "member_finished")[-1]["record"]["answer"] == "changed it"
        assert _of(events, "final")[-1]["response"] == "all changed"

    async def test_a_second_member_asking_meanwhile_is_refused_and_says_so(self, server, llm, team):
        from core.store import resources
        ana = await resources.get_agent(ANA)
        await S.seed_agents([{**ana, "tools": ["lookup", "danger"], "tool_policy": {"destructive": "confirm"}},
                             *(a for a in team if a["id"] != ANA)])
        gate = asyncio.Event()

        def first(_kw):
            return tool_call("danger")

        async def second(_kw):
            await asyncio.sleep(0.2)
            return tool_call("danger", item="second")

        llm.on("PLAN IT", [both(assign("Ben", "CHANGE B"), assign("Ana", "CHANGE A")), tool_call("wait", seconds=10),
                           tool_call("wait", seconds=10), tool_call("wait", seconds=10), "unused"])
        llm.on("CHANGE B", [first, "b"]).on("CHANGE A", [second, "I need danger; told the lead"])
        events = await _turn(server, progress_events=True)
        refused = [e for e in _wrapped(events, "tool_result", "Ana") if e.get("refused")]
        assert refused and "already waiting for a person's approval" in refused[0]["reason"]
        assert _of(events, "human_input_required")[-1]["delegate_agent_name"] == "Ben"
        del gate


class TestNothingGoesBackAsAHelpersResult:
    async def test_a_members_message_unread_at_the_end_sends_no_follow_up(self, server, llm, team, monkeypatch):
        """SUB1b's trap: what the conversation's inbox holds at a turn's end goes back as a helper's result."""
        from core import helpers
        from core.models import ChatRequest
        from core.react_engine import run_react_loop
        sent: list = []
        monkeypatch.setattr(helpers, "redeliver", lambda conversation, items: sent.extend(items))

        llm.on("PLAN IT", [assign("Ana", "CHECK A"), tool_call("wait", seconds=10), "done"])
        llm.on("CHECK A", [tool_call("message", to="lead", text="one more thing"), "fine"])
        request = ChatRequest(message="PLAN IT", agent_id=LEAD, session_id="s_follow")
        request._app_chat = True
        events = [e async for e in run_react_loop(request, server)]
        assert _of(events, "final")
        assert not [i for i in sent if i.get("kind") != "helper_result"]
