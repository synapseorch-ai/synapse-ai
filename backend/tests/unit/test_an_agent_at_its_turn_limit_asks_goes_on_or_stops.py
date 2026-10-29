"""At its turn limit an agent asks, goes on or stops, as the workspace or the agent says (F1219; Naveen 10-29).

Its last turn is an answer turn (F1219 part A). When that did not finish, `turn_limit_mode` decides: **ask** parks the
turn as a tool approval does ("I've used N turns on this; keep going?": [Keep going] gives N more, [Answer with what
you have] one more answer turn); **continue** goes on by itself, held by the run's cost cap or, with none, by
`CEILING_USD` and `MAX_EXTENSIONS`; **stop** ends as out of turns. With nothing set, a chat a person is in asks and a
run nobody watches stops; Synapse AI's panel marks its runs as watched. The agent's own setting beats the workspace's,
and the repeat guard (F243) still ends a turn that goes on without getting anywhere.
"""
from __future__ import annotations

import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from _fakes.knowledge_env import use_saved_settings
from test_human_in_containers import _run, driver, worker  # noqa: F401 — fixtures

from core import turn_limit
from core.chat_approval import resume_state_for

from tests.unit.test_a_paused_turn_parks_where_it_stands import _parked
from tests.unit.test_an_agent_step_with_no_answer_fails import _entry, _print, tools  # noqa: F401

LOOK = {"type": "function", "function": {"name": "look", "description": "Look somewhere.",
                                         "parameters": {"type": "object", "properties": {"where": {"type": "string"}}}}}
AGENT = "agent_f413"  # the no-answer tests' agent, whose `tools` fixture gives it `look`


def _looks(n: int, start: int = 0) -> list[str]:
    """`n` calls, each somewhere new, so the repeat guard never sees the same one twice."""
    return [tool_call("look", where=f"shelf {i}") for i in range(start, start + n)]


def _server():
    return types.SimpleNamespace(agent_sessions={"x": object()}, memory_store=None, tool_router={})


@pytest.fixture
async def workspace(monkeypatch):
    use_saved_settings(monkeypatch)
    from core.routes.settings import save_settings

    async def set_mode(mode: str):
        await save_settings({turn_limit.SETTING: mode})
    return set_mode


async def _chat(fake_llm, script, *, max_turns=2, agent=None, resume_state=None, same=False):
    from core.react_engine import run_agent_step

    agent = agent or S.make_agent(tools=["all"], skip_default_tools=True)

    async def executor(name, args):
        return "nothing there" if same else f"found nothing on {args.get('where')}"

    fake_llm.script(script)
    return [e async for e in run_agent_step(
        message="find the invoice", agent_id=agent["id"], session_id="s_f1219", server_module=_server(),
        agent_override=agent, tools_override=[LOOK], tool_executor=executor, max_turns=max_turns,
        progress_events=True, resume_state=resume_state)]


def _final(events):
    return next((e for e in reversed(events) if e.get("type") == "final"), None)


def _asked(events):
    return next((e for e in events if e.get("type") == "human_input_required"), None)


# ── the mode ─────────────────────────────────────────────────────────────────────────────────────────────────────
class TestWhichMode:
    def test_nothing_set_asks_a_person_and_stops_a_run_nobody_watches(self):
        assert turn_limit.mode({}, {}, person=True) == "ask"
        assert turn_limit.mode({}, {}, person=False) == "stop"

    def test_the_workspace_s_setting_holds_for_both(self):
        assert turn_limit.mode({}, {"turn_limit_mode": "continue"}, person=False) == "continue"
        assert turn_limit.mode({}, {"turn_limit_mode": "stop"}, person=True) == "stop"

    def test_the_agent_s_own_beats_the_workspace_s(self):
        assert turn_limit.mode({"turn_limit_mode": "stop"}, {"turn_limit_mode": "continue"}, person=True) == "stop"
        assert turn_limit.mode({"turn_limit_mode": None}, {"turn_limit_mode": "ask"}, person=False) == "ask"
        assert turn_limit.mode({"turn_limit_mode": "sometimes"}, {}, person=True) == "ask", "not a mode: not set"


# ── in a chat ────────────────────────────────────────────────────────────────────────────────────────────────────
class TestInAChat:
    async def test_ask_is_the_default_and_parks_as_an_approval(self, fake_llm, workspace):
        events = await _chat(fake_llm, _looks(2))
        asked = _asked(events)
        assert asked and asked["kind"] == "tool_approval" and asked["tool_name"] == turn_limit.KIND
        assert asked["prompt"] == "I've used 2 turns on this; keep going?"
        assert _final(events) is None, "it waits on the person"

    async def test_keep_going_gives_it_its_turns_again_from_where_it_stands(self, fake_llm, workspace):
        events = await _chat(fake_llm, _looks(2))
        _, parked = _parked(events)
        resumed = await _chat(fake_llm, [*_looks(1, 2), "It was on shelf 2."],
                              resume_state=resume_state_for(parked, approve=True))
        assert _final(resumed)["response"] == "It was on shelf 2."
        assert fake_llm.call_count == 4, "two more turns: one more look, then the answer"

    async def test_answer_with_what_you_have_is_one_answer_turn(self, fake_llm, workspace):
        events = await _chat(fake_llm, _looks(2))
        _, parked = _parked(events)
        resumed = await _chat(fake_llm, ["Not found on shelves 0 or 1."],
                              resume_state=resume_state_for(parked, approve=False))
        assert _final(resumed)["response"] == "Not found on shelves 0 or 1."
        assert not fake_llm.calls[-1].get("tools"), "its one turn is offered no tool"

    async def test_stop_ends_as_out_of_turns(self, fake_llm, workspace):
        await workspace("stop")
        final = _final(await _chat(fake_llm, _looks(3)))
        assert final["no_answer"]["why"] == "turns"

    async def test_continue_goes_on_by_itself(self, fake_llm, workspace):
        await workspace("continue")
        events = await _chat(fake_llm, [*_looks(3), "Shelf 2."])
        assert _final(events)["response"] == "Shelf 2."
        assert [e["extensions"] for e in events if e.get("type") == "turn_limit_extended"] == [1]
        assert _asked(events) is None

    async def test_the_agent_s_own_beats_the_workspace(self, fake_llm, workspace):
        await workspace("continue")
        agent = S.make_agent(tools=["all"], skip_default_tools=True, turn_limit_mode="stop")
        final = _final(await _chat(fake_llm, _looks(3), agent=agent))
        assert final["no_answer"]["why"] == "turns"

    async def test_continue_goes_on_at_most_so_often(self, fake_llm, workspace):
        await workspace("continue")
        events = await _chat(fake_llm, _looks(40))
        assert len([e for e in events if e.get("type") == "turn_limit_extended"]) == turn_limit.MAX_EXTENSIONS
        assert _final(events)["no_answer"]["why"] == "turns"
        assert any("already gone on" in (e.get("message") or "") for e in events if e.get("type") == "status")

    async def test_continue_stops_at_its_ceiling_with_no_cap_set(self, fake_llm, workspace, monkeypatch):
        import core.react_engine as re_mod

        await workspace("continue")
        monkeypatch.setattr(re_mod, "_call_usd", lambda model, result: 0.30)
        events = await _chat(fake_llm, _looks(40))
        assert _final(events)["no_answer"]["why"] == "turns"
        assert any("most it goes on to spend" in (e.get("message") or "") for e in events if e.get("type") == "status")

    async def test_the_repeat_guard_still_ends_a_turn_going_nowhere(self, fake_llm, workspace):
        """Naveen's loop guard (10-29): the same call with the same arguments and the same result three times."""
        await workspace("continue")
        events = await _chat(fake_llm, [tool_call("look", where="vault")] * 12, max_turns=2, same=True)
        assert _final(events)["no_answer"]["why"] == "repeating"
        assert fake_llm.call_count < 12

    async def test_a_turn_nobody_can_answer_stops(self, fake_llm, workspace):
        from core.chat_approval import nobody_to_ask

        with nobody_to_ask():
            events = await _chat(fake_llm, _looks(3))
        assert _asked(events) is None and _final(events)["no_answer"]["why"] == "turns"


# ── in a workflow run ────────────────────────────────────────────────────────────────────────────────────────────
def _orch(oid: str) -> dict:
    return S.make_orchestration(id=oid, entry_step_id="write", steps=[
        {"id": "write", "name": "Write", "type": "agent", "agent_id": AGENT, "prompt_template": "Find it",
         "output_key": "draft", "max_turns": 2, "next_step_id": "after"}, _print("after")])


async def _seed_run(oid: str, **agent) -> dict:
    orch = _orch(oid)
    await S.seed_agents([S.make_agent(id=AGENT, tools=["all"], skip_default_tools=True, **agent)])
    await S.seed_orchestrations([orch])
    return orch


class TestInARun:
    async def test_a_run_nobody_watches_stops_and_says_where_the_limit_is(self, driver, fake_llm, tools, workspace):
        orch = await _seed_run("orch_f1219_r_stop")
        fake_llm.script(_looks(3))
        assert await driver.start(orch, "run_f1219_r_stop") == "failed"
        assert "Max turns" in _entry(await _run("run_f1219_r_stop"))["error"]

    async def test_a_watched_run_asks_and_keep_going_finishes_it(self, driver, fake_llm, tools, workspace):
        orch = await _seed_run("orch_f1219_r_ask")
        fake_llm.script([*_looks(2), "Shelf 1."])
        assert await driver.start(orch, "run_f1219_r_ask", initial_state={turn_limit.ATTENDED_KEY: True}) == "paused"
        assert await driver.answer("run_f1219_r_ask", {"approve": True}) == "completed"
        assert (await _run("run_f1219_r_ask")).shared_state["draft"] == "Shelf 1."

    async def test_answer_with_what_you_have_in_a_run(self, driver, fake_llm, tools, workspace):
        orch = await _seed_run("orch_f1219_r_ans")
        fake_llm.script([*_looks(2), "Not on shelves 0 or 1."])
        assert await driver.start(orch, "run_f1219_r_ans", initial_state={turn_limit.ATTENDED_KEY: True}) == "paused"
        assert await driver.answer("run_f1219_r_ans", {"approve": False}) == "completed"
        assert (await _run("run_f1219_r_ans")).shared_state["draft"] == "Not on shelves 0 or 1."

    async def test_a_workspace_that_chose_ask_parks_an_unwatched_run(self, driver, fake_llm, tools, workspace):
        await workspace("ask")
        orch = await _seed_run("orch_f1219_r_opt")
        fake_llm.script(_looks(2))
        assert await driver.start(orch, "run_f1219_r_opt") == "paused"

    async def test_continue_in_a_run_goes_on(self, driver, fake_llm, tools, workspace):
        orch = await _seed_run("orch_f1219_r_go", turn_limit_mode="continue")
        fake_llm.script([*_looks(3), "Shelf 2."])
        assert await driver.start(orch, "run_f1219_r_go") == "completed"

    async def test_the_run_s_cost_cap_stops_continue(self, driver, fake_llm, tools, workspace):
        from core.orchestration import run_cap

        async def spent(run_id, step_type, cap):
            """Nothing before the step; past its $0.01 cap once the step has made its calls."""
            return run_cap.Spend(usd=1.0 if fake_llm.call_count >= 2 else 0.0)
        run_cap.set_spend_reader(spent)
        try:
            orch = {**await _seed_run("orch_f1219_r_cap", turn_limit_mode="continue"), "max_run_usd": 0.01}
            await S.seed_orchestrations([orch])
            fake_llm.script(_looks(6))
            assert await driver.start(orch, "run_f1219_r_cap") == "failed"
            assert fake_llm.call_count == 2, "it never went on past its two turns"
        finally:
            run_cap.set_spend_reader(None)


# ── who says a person is watching: only the run's own starter ───────────────────────────────────────────────────
class TestAttendanceIsNeverARunsInput:
    """8a's rule: `_attended` steers the engine, so no caller's input may set it — the doors' rule for every engine
    key (D126): v1's and v2's `initial_state`, and the MCP door's `inputs`."""

    @pytest.mark.parametrize("door", ["v1", "v2"])
    def test_the_api_refuses_it(self, door):
        from pydantic import ValidationError

        from core.routes.api_v1 import V1OrchestrationRunRequest
        from core.routes.api_v2 import V2OrchestrationRunRequest

        model = V1OrchestrationRunRequest if door == "v1" else V2OrchestrationRunRequest
        with pytest.raises(ValidationError, match="_attended"):
            model(message="go", initial_state={turn_limit.ATTENDED_KEY: True})

    async def test_the_mcp_door_refuses_it(self):
        from core.mcp_server.runs import start_run
        from core.product_tools.errors import ToolRefusal

        orch = await _seed_run("orch_f1219_mcp")
        with pytest.raises(ToolRefusal, match="_attended"):
            await start_run(orch["id"], "go", {turn_limit.ATTENDED_KEY: True}, 0)
