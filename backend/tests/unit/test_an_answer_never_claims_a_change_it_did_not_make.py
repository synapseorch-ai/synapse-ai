"""An answer never claims a change this turn did not make (F1323, TRI1; BLD1's read of CAP3's prod-13; Naveen 11-04:
"Build as restated").

prod-13 P1 on 63dfb505 (run builder_efe5e6d352f1): the review's yes was not read as one, so no op was applied, and the
planner's plain reply said "Done. The schedule is now enabled and all tools have been added…" — the schedule was on
only because a card had been pressed. FF14 prod-13 pass2-p2 turn 22: the player typed its own "[pressed Connect …]"
and the reply said "Both APIs are now connected", while the real presses had failed. The answer route's check passed
both: it held only F1243's "say approve".

No answer step can change anything (the talker and the reporter hold reads and card tools; the planner's plain reply
is its no-change finish), so the run's own record of its writes (`run_writes`, F490) is what a claim is held to. When
this turn wrote nothing, each whole sentence that claims a change was made — a "Done" or "All set", "I've turned on…",
"X is now enabled", "successfully connected" — is cut, and code's line is said once in its place. A status, history
with its time, a how-to and "I haven't changed anything" stay; a turn that did write keeps its words.
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from core.native_builder import answer_pack

DEF = json.loads((Path(__file__).resolve().parents[2] / "core" / "native_builder" / "orchestration.json").read_text())
STEPS = {s["id"]: s for s in DEF["steps"]}

PROD_13 = ("Done. The schedule is now enabled and all tools have been added, so the workflow will check new orders "
           "every hour.")
TURN_22 = ("Both APIs are now connected. The secure screens have registered your credentials for Tidewell Goods API "
           "and Ledgerline API, and the workflow can use them.")
LINE = ("Nothing was changed in this reply: to have something changed, ask for it, and a change comes up for you to "
        "check and approve.")
WROTE = [{"kind": "schedule", "action": "updated", "id": "sch_1", "name": "Hourly orders", "enabled": True}]


def _checked(text: str, writes=None) -> str:
    """The claim check, read by name so a red fails by behaviour."""
    check = getattr(answer_pack, "no_change_claimed", None)
    assert check is not None, "the answer route holds no claim to what this turn wrote"
    return check(text, writes)


class TestAClaimWithNothingBehindIt:
    def test_prod_13s_reply_is_cut_and_the_line_said(self):
        out = _checked(PROD_13)
        assert "Done" not in out and "now enabled" not in out and "have been added" not in out, out
        assert out == LINE

    def test_turn_22s_claim_is_cut_and_the_line_follows_the_rest(self):
        # A perfect tense with no "now" ("have registered") is how a history answer speaks ("has been paused since
        # Monday"), so it stays; the line after it says what this reply changed.
        out = _checked(TURN_22)
        assert out == ("The secure screens have registered your credentials for Tidewell Goods API and Ledgerline API, "
                       "and the workflow can use them.\n\n" + LINE)

    @pytest.mark.parametrize("claim", [
        "All set!", "I've turned on the schedule.", "I have added the Slack tool to the agent.",
        "I just renamed it to Harbor desk.", "The tool was successfully added.", "Your agent is now live.",
        "The schedule has now been paused.", "We've connected your Gmail account.",
    ])
    def test_each_way_of_claiming(self, claim):
        assert _checked(f"Here is where things are. {claim}") == f"Here is where things are.\n\n{LINE}"

    def test_the_line_is_said_once_and_the_same_answer_gets_the_same_words(self):
        out = _checked(f"{PROD_13}\n\n{TURN_22}")
        assert out.count(LINE) == 1 and out == _checked(f"{PROD_13}\n\n{TURN_22}")


class TestWhatClaimsNothingStays:
    @pytest.mark.parametrize("text", [
        'The schedule "Hourly orders" is enabled and runs every hour.',
        "It was turned off on Monday at 9:00.",
        "Once it's enabled, the schedule runs every night at 2.",
        "I haven't changed anything: press Turn on on the card to do it.",
        "I can turn it on for you: ask me to, and a change comes up to approve.",
        "Press Connect on the card, and Tidewell Goods API is connected.",
        "",
    ])
    def test_it_is_untouched(self, text):
        assert _checked(text) == text

    def test_a_turn_that_wrote_keeps_its_words(self):
        assert _checked(PROD_13, WROTE) == PROD_13


class TestOnTheRouteAsShipped:
    """Both replies through the definition's own steps: the planner's plain reply (step_say) and the talker's answer
    (step_answer_talk), each into the answer check and out as what the chat draws."""

    async def _run(self, entry: dict, monkeypatch, *, reply: str | None = None, state: dict | None = None) -> str:
        import core.react_engine as re_mod
        from _fakes import seed as S
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine

        if reply is not None:
            async def talker(**_kwargs):
                yield {"type": "final", "response": reply}

            monkeypatch.setattr(re_mod, "run_agent_step", talker)
            await S.seed_agents([S.make_agent(id="agent_f1323", name="Answers")])
            entry = {**entry, "agent_id": "agent_f1323"}
        # The slice is Synapse AI's own steps, which run only in its own run (F1315): the host's builder ids name it.
        from core.native_builder import identity

        orch_id = f"orch_f1323_{entry['id']}"

        async def builder_ids():
            return [orch_id]

        monkeypatch.setattr(identity, "_host_ids", builder_ids)
        orch = Orchestration.model_validate({
            "id": orch_id, "name": "Answer slice", "entry_step_id": entry["id"],
            "state_schema": {k: DEF["state_schema"][k] for k in ("summary", "answer_checked", "plan_draft")},
            "steps": [entry, STEPS["step_answer_checked"], STEPS["step_answer_said"], STEPS["step_end"]]})
        engine = OrchestrationEngine(orch, types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}))
        events = [e async for e in engine.run("yes", f"run_f1323_{entry['id']}", initial_state=state or {})]
        [done] = [e for e in events if e.get("type") == "orchestration_complete"]
        return str((done.get("final_state") or {}).get("summary") or "")

    async def test_the_planners_plain_reply_after_a_yes_that_applied_nothing(self, monkeypatch):
        said = await self._run(STEPS["step_say"], monkeypatch, state={"plan_draft": {"text": PROD_13}})
        assert said == LINE

    async def test_the_talkers_answer_to_a_typed_press(self, monkeypatch):
        said = await self._run(STEPS["step_answer_talk"], monkeypatch, reply=TURN_22)
        assert "now connected" not in said and said.endswith(LINE)

    async def test_a_turn_that_wrote_keeps_the_planners_words(self, monkeypatch):
        said = await self._run(STEPS["step_say"], monkeypatch,
                               state={"plan_draft": {"text": PROD_13}, "_run_writes": WROTE})
        assert said == PROD_13
