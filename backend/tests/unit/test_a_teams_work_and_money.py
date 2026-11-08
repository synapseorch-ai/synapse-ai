"""What a team's turn worked and spent, as a host prices and limits it (TEAM1d, F657, F658; 20 Q7, Q8).

F658, the work meter: "parallel saves time, not money" — a host prices a Team agent's turn as its agents' minutes
added together, so the engine reports, for this stretch of the turn only (a resumed turn counts what it ran after the
resume): the lead's idle time inside `wait`, its members' working seconds, and what helpers waited for added beyond
the wait itself (two helpers side by side for a minute are two minutes of work in one minute of waiting). It rides
the answer's `final.team.work` and the turn's snapshots, never the saved turn.

F657, the money check: each next model call is priced before it is made against the Team agent's spend limit. The
rate card prices a model with an install's own key; a host's own models (cloud's `synapse.*`, paid from its wallet)
are unpriced there, so a team on them counted $0 and its limit never stopped it. Now a call's cost is what the
gateway charged when it says (`charged_microusd`), and a host lays its models' prices over the settings
(`team_model_prices`) for the allowance of a call not yet made.
"""
import asyncio
import time

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from test_team_runtime import ANA, BEN, LEAD, _of, _turn, assign, both, llm, server, team  # noqa: F401

from core import team as team_mod
from core.team import TeamConfig


def _team(config=None, state=None):
    return team_mod.Team(lead={"id": LEAD}, members={}, config=config or TeamConfig(3, 6, None),
                         session_id="s_work", source="chat", run_id=None, server_module=None,
                         lead_model="gpt-4.1-mini", should_stop=None, can_park=False, state=state)


class TestTheWorkMeter:
    async def test_the_answer_says_what_the_lead_idled_and_its_members_worked(self, server, llm, team):
        async def slow_a(_kw):
            await asyncio.sleep(0.4)
            return "A is fine"

        async def slow_b(_kw):
            await asyncio.sleep(0.4)
            return "B is fine"

        llm.on("PLAN IT", [both(assign("Ana", "CHECK A"), assign("Ben", "CHECK B")), tool_call("wait", seconds=10),
                           tool_call("wait", seconds=10), "both fine"])
        llm.on("CHECK A", [slow_a]).on("CHECK B", [slow_b])
        events = await _turn(server)

        work = _of(events, "final")[-1]["team"]["work"]
        assert set(work) == {"lead_idle_s", "members_s", "helpers_s"}
        assert work["members_s"] >= 0.7          # two members, 0.4 s each, side by side
        assert 0.3 <= work["lead_idle_s"] <= 1.5  # the lead waited for them
        assert work["helpers_s"] == 0

    async def test_a_lead_that_answers_alone_worked_alone(self, server, llm, team):
        llm.on("PLAN IT", ["hello there"])
        events = await _turn(server)
        assert _of(events, "final")[-1]["team"]["work"] == {"lead_idle_s": 0.0, "members_s": 0.0, "helpers_s": 0.0}

    async def test_the_saved_turn_keeps_no_meter(self, server, llm, team):
        from core.session import _get_conversation_history

        llm.on("PLAN IT", [assign("Ana", "CHECK A"), tool_call("wait", seconds=10), "lead answer"])
        llm.on("CHECK A", ["A is fine"])
        await _turn(server, session_id="s_work_kept")
        saved = (await _get_conversation_history("s_work_kept", agent_id=LEAD))[-1]
        assert "work" not in saved["team"]

    def test_a_resumed_team_counts_only_what_it_works_after_the_resume(self):
        """A member that worked 12 s before the pause is not counted again; its seconds after are."""
        t = _team(state={"members": [{"id": "m1", "agent_id": ANA, "name": "Ana", "task": "x", "card_id": "c1",
                                      "status": "completed", "seconds": 12.0}]})
        assert t.work()["members_s"] == 0.0

    def test_idle_and_helpers_are_added_as_noted(self):
        t = _team()
        t.note_idle(1.25)
        t.note_helpers(3.5)
        t.note_helpers(-2)      # never less than nothing
        assert t.work() == {"lead_idle_s": 1.2, "members_s": 0.0, "helpers_s": 3.5}

    def test_it_rides_the_snapshot_and_a_restore_starts_it_again(self):
        t = _team()
        t.note_idle(2.0)
        state = t.state()
        assert state["work"]["lead_idle_s"] == 2.0
        assert _team(state=state).work()["lead_idle_s"] == 0.0


class TestHelpersSideBySide:
    async def test_helpers_waited_for_add_what_ran_beside_the_wait(self, monkeypatch):
        """Two helpers of 0.3 s side by side: 0.6 s of work in a 0.3 s wait, so 0.3 s more than the wait."""
        from core import helpers

        async def fake_run(helper, **_kw):
            helper.started_at = time.time()
            await asyncio.sleep(0.3)
            helper.ended_at = time.time()
            yield {"type": "helper_finished", "helper_id": helper.id}

        monkeypatch.setattr(helpers, "run", fake_run)
        made = [helpers.Helper(id=f"h{i}", name=f"H{i}", task="t", tools=[], withheld=[], model="m", agent={}) for i in range(2)]
        t = _team()
        token = team_mod.current.set(t)
        try:
            [e async for e in helpers.batch(made, key=("k",))]
        finally:
            team_mod.current.reset(token)
        assert 0.2 <= t.work()["helpers_s"] <= 0.45

    async def test_outside_a_team_nothing_is_noted(self, monkeypatch):
        from core import helpers

        async def fake_run(helper, **_kw):
            helper.started_at, helper.ended_at = time.time() - 1, time.time()
            yield {"type": "helper_finished", "helper_id": helper.id}

        monkeypatch.setattr(helpers, "run", fake_run)
        made = [helpers.Helper(id="h1", name="H1", task="t", tools=[], withheld=[], model="m", agent={})]
        assert [e["type"] async for e in helpers.batch(made, key=("k2",))] == ["helper_finished"]


class TestTheMoneyCheck:
    def test_a_charged_call_counts_what_the_gateway_charged(self, monkeypatch):
        from core import usage_tracker

        monkeypatch.setattr(usage_tracker, "_pricing_snapshot",
                            {"gpt-4.1-mini": {"provider": "openai", "input_per_1m": 0.4, "output_per_1m": 1.6}})
        t = _team()
        token = team_mod.current.set(t)
        try:
            team_mod.note_cost("synapse.nova-pro", 1000, 100, charged_microusd=12_345)   # the card prices it $0
            team_mod.note_cost("gpt-4.1-mini", 1_000_000, 0)                               # an own key: the card
        finally:
            team_mod.current.reset(token)
        assert t.spent_usd == pytest.approx(0.012345 + 0.4)

    def test_a_hosts_prices_size_the_allowance_of_a_model_the_card_leaves_out(self):
        settings = {"team_model_prices": {"synapse.nova-pro": [1.0, 4.0]}}
        config = team_mod.config_of({"type": "delegate", "team": {"mode": "team", "spend_limit_usd": 0.01}},
                                    settings=settings)
        t = _team(config=config)
        # 40,000 characters is 10,000 tokens in ($0.01) and 1,600 out ($0.0064): past a $0.01 limit.
        assert t.allowance("synapse.nova-pro", 40_000, None) == pytest.approx(0.01 + 0.0064)
        assert t.may_call("synapse.nova-pro", 40_000, None, lead=True) is False
        assert t.may_call("synapse.nova-pro", 400, 100, lead=True) is True

    def test_without_a_hosts_prices_an_unpriced_model_costs_nothing_here(self):
        t = _team(config=TeamConfig(3, 6, 0.01))
        assert t.allowance("synapse.nova-pro", 40_000, None) == 0.0

    def test_the_prices_do_not_change_what_a_config_compares_as(self):
        a = team_mod.config_of({"type": "delegate", "team": {"mode": "team"}}, settings={})
        b = team_mod.config_of({"type": "delegate", "team": {"mode": "team"}},
                               settings={"team_model_prices": {"synapse.x": [1.0, 1.0]}})
        assert (a.at_once, a.task_budget, a.spend_limit_usd) == (b.at_once, b.task_budget, b.spend_limit_usd)


class TestAMemberWaitingOnAPerson:
    """Its wait is not work (F623's rule): counted only up to its question, before the pause and after the answer."""

    def test_its_wait_is_left_out_of_the_stretch_it_asked_in(self):
        t = _team()
        m = team_mod.Member(id="m1", agent_id=ANA, name="Ana", task="x", card_id="c1")
        now = time.time()
        m.status, m.started_at, m.asked_at = "waiting_for_person", now - 100.0, now - 90.0
        t.members[m.id] = m
        assert t.work()["members_s"] == 10.0

    def test_a_resumed_member_counts_what_it_works_after_the_answer(self):
        now = time.time()
        t = _team(state={"members": [{"id": "m1", "agent_id": ANA, "name": "Ana", "task": "x", "card_id": "c1",
                                      "status": "waiting_for_person", "started_at": now - 100.0,
                                      "asked_at": now - 90.0, "seconds": 100.0}]})
        assert t.work()["members_s"] == 0.0
        m = t.members["m1"]
        m.worked_before = 10.0                       # as resume_member sets it
        m.status, m.started_at = "running", time.time() - 10.0 - 5.0   # its clock carried on: 5 s more work
        assert t.work()["members_s"] == 5.0
