"""A team's questions are a list, each answered by its own id (TEAM1c, D287; 20-agent-teams.md Q6).

TEAM1a parked a team's turn on one member's question and refused a second member's call meanwhile. Now
each member asks on its own: a paused turn lists every waiting member's question, each with its own id
(two members asking the same call are two questions), an answer resumes only its member, and an answer
given while the turn runs reaches that member at once — or, given as the turn starts or pauses, is held
and taken up exactly once. A row parked before the list reads as a list of one. The bell asks each
question as its own item and settles each by its own id.
"""
import asyncio

import pytest
from fastapi import HTTPException

from core import chat_approval as ca
from core import chat_turns
from core.routes import chat as chat_routes
from core.routes.chat import ApprovalDecision
from core.store import sessions as store

SID, LEAD = "s_list", "lead_list"


def _question(member, name, approval="ap_same", tool="danger", args=None):
    return {"question_id": ca.question_id(member, approval), "approval_id": approval, "member_id": member,
            "member_name": name, "tool_name": tool, "tool_args": args or {}, "category": "destructive"}


def _team_row(*questions):
    """What a team's paused turn parks: the first question's fields, every question, the team's snapshot."""
    first = questions[0]
    members = [{"id": q["member_id"], "agent_id": f"agent_{q['member_id']}", "name": q["member_name"],
                "task": f"TASK {q['member_name']}", "status": "waiting_for_person",
                "approval": {k: q[k] for k in ("approval_id", "tool_name", "tool_args", "category")},
                "progress": {"turn": 1, "context_text": f"{q['member_name']}'s work"}} for q in questions]
    entries = [{"question_id": q["question_id"], "member_id": q["member_id"], "agent_id": f"agent_{q['member_id']}",
                "task": f"TASK {q['member_name']}",
                "approval": {k: q[k] for k in ("approval_id", "tool_name", "tool_args", "category")}}
               for q in questions]
    return {"tool_name": first["tool_name"], "tool_args": first["tool_args"], "approval_id": first["approval_id"],
            "category": first["category"], "message": "PLAN IT", "questions": [dict(q) for q in questions],
            "progress": {"turn": 4, "context_text": "the lead's", "team": {"members": members},
                         "delegate_pending": {**entries[0], "progress": members[0]["progress"]},
                         "team_pending": entries}}


ANA = _question("m_ana", "Ana", args={"item": "a"})
BEN = _question("m_ben", "Ben", args={"item": "b"})


class TestTheShape:
    def test_a_row_parked_before_the_list_reads_as_a_list_of_one(self):
        old = {"tool_name": "danger", "tool_args": {"x": 1}, "approval_id": "ap1", "category": "destructive"}
        assert ca.questions_of(old) == [{"question_id": "ap1", "approval_id": "ap1", "member_id": None,
                                         "member_name": None, "tool_name": "danger", "tool_args": {"x": 1},
                                         "category": "destructive"}]
        assert ca.find_question(old, approval_id="ap1")["question_id"] == "ap1"
        assert ca.find_question(old) is not None

    def test_an_answer_names_its_question_by_id_or_by_a_call_only_one_asks(self):
        row = _team_row(ANA, BEN)
        assert ca.find_question(row, question=BEN["question_id"])["member_name"] == "Ben"
        # Both asked the same call: the call alone does not say which.
        assert ca.find_question(row, approval_id="ap_same") is None
        assert ca.find_question(row) is None
        assert ca.find_question(row, question="m_cara:ap_same") is None
        other = _team_row(ANA, _question("m_ben", "Ben", approval="ap_other"))
        assert ca.find_question(other, approval_id="ap_other")["member_name"] == "Ben"

    def test_a_reopened_page_sees_every_question_with_its_member(self):
        view = ca.public_view(_team_row(ANA, BEN))
        assert [(q["member_name"], q["question_id"]) for q in view["questions"]] == [
            ("Ana", ANA["question_id"]), ("Ben", BEN["question_id"])]
        assert all("Allow it?" in q["prompt"] for q in view["questions"])
        assert "progress" not in view and view["tool_name"] == "danger"

    def test_moved_past_every_question_is_saved_expired_and_none_was_run(self):
        _, answer = ca.moved_past_turn(_team_row(ANA, BEN), why=ca.MOVED_ON)
        assert [(a["member_name"], a["outcome"]) for a in answer["approvals"]] == [("Ana", "expired"),
                                                                                  ("Ben", "expired")]
        assert [c["refused"] for c in answer["calls"]] == [True, True]
        assert "None was run" in answer["content"]

    def test_one_answer_resumes_its_member_and_the_rest_wait(self):
        state = ca.resume_state_for(_team_row(ANA, BEN), approve=True, question=BEN["question_id"])
        assert state["delegate"]["member_id"] == "m_ben"
        assert state["delegate"]["resume_state"]["pending_call"]["arguments"] == {"item": "b"}
        assert state["delegate"]["resume_state"]["context_text"] == "Ben's work"   # Ben's own snapshot
        assert state["team_answers"] == []
        assert "team_pending" not in state and "delegate_pending" not in state

    def test_answers_together_resume_each_as_itself(self):
        state = ca.resume_state_for(_team_row(ANA, BEN), approve=True, verdicts=[
            {"question_id": ANA["question_id"], "approve": True},
            {"question_id": BEN["question_id"], "approve": False, "reason": "no"}])
        assert state["delegate"]["member_id"] == "m_ana"
        (ben,) = state["team_answers"]
        assert ben["member_id"] == "m_ben" and ben["approved"] is False
        assert ben["resume_state"]["pending_call"]["approved"] is False
        assert ben["resume_state"]["denied_reasons"] == {"ap_same": "no"}
        assert ben["resume_state"]["context_text"] == "Ben's work"

    def test_an_answer_to_no_question_it_asks_is_refused(self):
        with pytest.raises(KeyError):
            ca.resume_state_for(_team_row(ANA, BEN), approve=True, question="m_cara:ap_same")


class TestHeldAnswers:
    def test_an_answer_with_no_team_at_work_is_held_and_taken_once(self):
        assert ca.post_answer(SID, LEAD, {"question_id": BEN["question_id"], "approve": True}) == "held"
        assert ca.post_answer(SID, LEAD, {"question_id": BEN["question_id"], "approve": False}) == "held"
        assert ca.take_answers(SID, LEAD, [ANA["question_id"]]) == []
        assert ca.take_answers(SID, LEAD, [BEN["question_id"]]) == [
            {"question_id": BEN["question_id"], "approve": True}]   # the first answer is the one kept
        assert ca.take_answers(SID, LEAD) == []

    def test_a_team_at_work_takes_its_members_answer_now(self, monkeypatch):
        from core import team
        got = []

        class _Working:
            agent_key = LEAD

            def answer(self, question, *, approve, reason=""):
                got.append((question, approve))
                return True

        monkeypatch.setattr(team, "find", lambda session_id: _Working())
        assert ca.post_answer(SID, LEAD, {"question_id": ANA["question_id"], "approve": True}) == "applied"
        assert got == [(ANA["question_id"], True)] and ca.take_answers(SID, LEAD) == []


@pytest.fixture
def captured(monkeypatch):
    seen = {}

    def fake_events(request, _server, resume_state=None, *, expand_mentions):
        seen["resume_state"] = resume_state

        async def _empty():
            if False:
                yield b""
        return _empty()

    monkeypatch.setattr(chat_routes, "_event_stream", fake_events)
    return seen


class TestTheAnswerRoute:
    async def test_a_paused_turn_resumes_with_the_answered_member_and_any_answer_held_for_it(self, captured):
        await store.set_pending_approval(SID, LEAD, _team_row(ANA, BEN))
        ca.post_answer(SID, LEAD, {"question_id": ANA["question_id"], "approve": False, "reason": "later"})
        await chat_routes.approve_chat_tool(SID, ApprovalDecision(approve=True, agent_id=LEAD,
                                                                  question_id=BEN["question_id"]))
        await asyncio.sleep(0)
        state = captured["resume_state"]
        assert state["delegate"]["member_id"] == "m_ben"
        assert [(a["member_id"], a["approved"]) for a in state["team_answers"]] == [("m_ana", False)]
        assert await store.get_pending_approval(SID, LEAD) is None
        assert ca.take_answers(SID, LEAD) == []

    async def test_a_call_two_members_asked_must_be_named_and_the_question_stays(self, captured):
        await store.set_pending_approval(SID, LEAD, _team_row(ANA, BEN))
        with pytest.raises(HTTPException) as refused:
            await chat_routes.approve_chat_tool(SID, ApprovalDecision(approve=True, agent_id=LEAD,
                                                                      approval_id="ap_same"))
        assert refused.value.status_code == 409
        assert len((await store.get_pending_approval(SID, LEAD))["questions"]) == 2

    async def test_a_team_at_work_here_takes_the_answer_live(self, monkeypatch, captured):
        from core import team

        class _Working:
            agent_key = LEAD

            def answer(self, question, *, approve, reason=""):
                return question == ANA["question_id"]

        monkeypatch.setattr(team, "find", lambda session_id: _Working())
        reply = await chat_routes.approve_chat_tool(SID, ApprovalDecision(
            approve=True, agent_id=LEAD, question_id=ANA["question_id"]))
        assert reply.status_code == 200 and b'"live":true' in reply.body and b'"allowed"' in reply.body
        assert "resume_state" not in captured

    async def test_an_answer_while_the_turn_pauses_is_held_then_resumes_it_once(self, captured):
        """The turn is pausing: its row is not written yet and it still holds the conversation."""
        with chat_turns.claim(SID, agent_key=LEAD):
            reply = await chat_routes.approve_chat_tool(SID, ApprovalDecision(
                approve=True, agent_id=LEAD, question_id=BEN["question_id"]))
            assert b'"sent"' in reply.body
            await store.set_pending_approval(SID, LEAD, _team_row(ANA, BEN))
        await asyncio.gather(*list(chat_routes._HELD_ANSWERS))
        assert captured["resume_state"]["delegate"]["member_id"] == "m_ben"
        assert await store.get_pending_approval(SID, LEAD) is None

    async def test_a_held_answer_the_turn_never_asked_does_nothing(self, captured):
        with chat_turns.claim(SID, agent_key=LEAD):
            await chat_routes.approve_chat_tool(SID, ApprovalDecision(
                approve=True, agent_id=LEAD, question_id=BEN["question_id"]))
        await asyncio.gather(*list(chat_routes._HELD_ANSWERS))
        assert "resume_state" not in captured

    async def test_a_held_answer_a_starting_team_took_is_not_used_again(self, captured):
        with chat_turns.claim(SID, agent_key=LEAD):
            await chat_routes.approve_chat_tool(SID, ApprovalDecision(
                approve=True, agent_id=LEAD, question_id=BEN["question_id"]))
            assert ca.take_answers(SID, LEAD) != []   # as `Team.register` takes it
            await store.set_pending_approval(SID, LEAD, _team_row(ANA, BEN))
        await asyncio.gather(*list(chat_routes._HELD_ANSWERS))
        assert "resume_state" not in captured
        assert await store.get_pending_approval(SID, LEAD) is not None


class TestTheFleetMailbox:
    @pytest.fixture
    def redis(self):
        import fakeredis.aioredis
        return fakeredis.aioredis.FakeRedis()

    async def test_an_answer_is_taken_by_one_taker_only(self, redis):
        from core.scale.pubsub import publish_member_answer, take_member_answer
        assert await publish_member_answer(redis, SID, {"question_id": "q1", "approve": True})
        assert not await publish_member_answer(redis, SID, {"question_id": "q1", "approve": False})
        got = await asyncio.gather(*[take_member_answer(redis, SID, "q1") for _ in range(5)])
        assert [g for g in got if g] == [{"question_id": "q1", "approve": True}]

    async def test_the_jobs_watcher_hands_its_team_its_answers_and_leaves_them_while_it_pauses(self, redis,
                                                                                              monkeypatch):
        from core import team
        from core.scale import worker
        from core.scale.pubsub import held_member_answers, publish_member_answer
        given = []

        class _Working:
            parking = False
            ended = False

            def questions(self):
                return [{"question_id": "q1"}]

            def answer(self, question, *, approve, reason=""):
                given.append(question)
                return True

        working = _Working()
        monkeypatch.setattr(team, "find", lambda session_id: working)
        await publish_member_answer(redis, SID, {"question_id": "q1", "approve": True})
        await publish_member_answer(redis, SID, {"question_id": "q_later", "approve": True})
        working.parking = True
        await worker._hand_over_answers(redis, SID)
        assert given == [] and sorted(await held_member_answers(redis, SID)) == ["q1", "q_later"]
        working.parking = False
        await worker._hand_over_answers(redis, SID)
        assert given == ["q1"] and await held_member_answers(redis, SID) == ["q_later"]


class TestTheBell:
    def test_each_question_is_its_own_item_settled_by_its_own_id(self):
        from core.notifications import hub
        for q in (ANA, BEN):
            hub.publish("tool_approval", "needs approval", data={"approval_id": q["approval_id"],
                                                                 "question_id": q["question_id"]})
        hub.resolve_approval("ap_same", "allowed", question_id=BEN["question_id"])
        mine = {i["data"]["question_id"]: i.get("resolved_as") for i in hub.list()
                if i["kind"] == "tool_approval" and i["data"].get("question_id") in (ANA["question_id"],
                                                                                    BEN["question_id"])}
        assert mine == {ANA["question_id"]: None, BEN["question_id"]: "allowed"}

    def test_a_pause_again_asks_only_the_questions_no_earlier_pause_asked(self):
        row = _team_row(ANA, BEN)
        row["questions"][0]["asked_before"] = True
        assert [q["member_name"] for q in ca.to_ring(row)] == ["Ben"]

    def test_after_a_resumed_turn_the_questions_it_no_longer_asks_are_settled(self):
        settled = []
        ca.settle_resumed(_team_row(ANA, BEN), _team_row(BEN), settled.append)
        assert [q["member_name"] for q in settled] == ["Ana"]
        settled.clear()
        ca.settle_resumed(_team_row(ANA, BEN), None, settled.append)
        assert [q["member_name"] for q in settled] == ["Ana", "Ben"]


def _team_event():
    row = _team_row(ANA, BEN)
    return {"type": "human_input_required", "kind": "tool_approval", "tool_name": row["tool_name"],
            "tool_args": row["tool_args"], "approval_id": row["approval_id"], "category": row["category"],
            "questions": [{**q, "asked_before": False} for q in row["questions"]]}


class TestEachDoorAsksEachQuestion:
    async def test_an_api_turns_pause_rings_one_item_per_member(self):
        from core import api_approvals
        from core.notifications import hub
        session = "openai_t1c_bell"
        await api_approvals.park(session_id=session, agent={"id": LEAD, "name": "Lead"}, event=_team_event(),
                                 message="PLAN IT", progress={}, resume={}, hours=24)
        items = [i for i in hub.list() if i["kind"] == "tool_approval" and i["data"].get("session_id") == session]
        assert [(i["data"]["member_name"], i["data"]["question_id"]) for i in items] == [
            ("Ana", ANA["question_id"]), ("Ben", BEN["question_id"])]
        assert len((await store.get_pending_approval(session, LEAD))["questions"]) == 2

    async def test_a_schedules_pause_again_rings_only_the_new_question(self):
        from core.notifications import hub
        from core.scheduler import ScheduleManager
        event = _team_event()
        event["questions"][0]["asked_before"] = True
        s = {"id": "sched_t1c", "name": "Nightly", "target_id": LEAD}
        await ScheduleManager()._park(s, "run_t1c", event, message="PLAN IT", progress={})
        items = [i for i in hub.list() if i["kind"] == "tool_approval"
                 and i["data"].get("schedule_id") == "sched_t1c"]
        assert [i["data"]["member_name"] for i in items] == ["Ben"]
        assert items[0]["title"] == "Nightly: Ben needs approval"
