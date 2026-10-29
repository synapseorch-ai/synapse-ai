"""A reply off the build route never asks for a yes: only the build route's plan card takes one (F1243).

TRI1's turn 3 on prod (10-29): "that's it ?" went to the answer route, whose reporter wrote its own "revised plan" and
"Just say **approve**", with nothing behind it to take the yes; the person's "yes proceed" then started a new build
from chat text. The root is CAP3's F1241/F1242 (the typed revision reached the answer route); this is the net under
it. Code cuts each whole sentence that asks for a yes from every answer (talk, help, workspace) and says where a yes
goes instead; the reporter's rule says never to write a plan or ask for its approval. Kept narrow: a sentence about
approvals that asks for nothing is the person's answer and stays.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.native_builder.answer_pack import NO_YES_HERE, no_yes_offered

DEF = json.loads((Path(__file__).resolve().parents[2] / "core" / "native_builder" / "orchestration.json").read_text())
STEPS = {s["id"]: s for s in DEF["steps"]}
AGENTS = Path(__file__).resolve().parents[2] / "core" / "native_builder" / "agents"

TURN_3 = ("You're right, that only made the agent. Here is the revised workflow:\n\n"
          "1. **Pick a feature**: Product Feature Picker\n2. **Write the post**: the post writer\n\n"
          "Just say **approve** and I'll build it. I can prompt you to connect Twitter as part of the build.")


class TestASentenceThatAsksForAYesIsCut:
    def test_turn_3s_ask_goes_and_the_pointer_comes(self):
        out = no_yes_offered(TURN_3)
        assert "approve" not in out.lower().replace(NO_YES_HERE.lower(), "")
        assert out.endswith("\n\n" + NO_YES_HERE)
        assert out.startswith("You're right, that only made the agent. Here is the revised workflow:")
        assert "I can prompt you to connect Twitter as part of the build." in out, "only the asking sentence goes"

    @pytest.mark.parametrize("asks", [
        "Reply yes to build it.", "Type “approve” and it is done.", "Say go ahead when you're ready.",
        "Just say build it.", "Respond with confirm to go on.", "Tell me ok and I will make it.",
    ])
    def test_each_way_of_asking(self, asks):
        out = no_yes_offered(f"The workflow is ready. {asks}")
        assert out == f"The workflow is ready.\n\n{NO_YES_HERE}"

    def test_the_same_answer_gets_the_same_words(self):
        assert no_yes_offered(TURN_3) == no_yes_offered(TURN_3)


class TestAnAnswerAboutApprovalsStays:
    @pytest.mark.parametrize("text", [
        "A Human step stops the run for your approval before it sends anything.",
        "Tool approvals are set per agent under its Tool Policy.",
        "If you approve it in the editor, the next run uses it.",
        "On the card, press “Approve” to make the change.",
        "4 runs are waiting for an approval: open each under Needs you.",
        "",
    ])
    def test_it_is_untouched(self, text):
        assert no_yes_offered(text) == text


class TestEveryAnswerGoesThroughTheCheck:
    def test_talk_help_and_workspace_lead_to_it(self):
        for sid in ("step_answer_talk", "step_answer_help", "step_answer_workspace"):
            assert STEPS[sid]["next_step_id"] == "step_answer_checked", sid
        check = STEPS["step_answer_checked"]
        assert (check["type"], check["forced_tool"], check["tool_arguments"]) == ("tool", "check_answer", {})
        said = STEPS[check["next_step_id"]]
        assert said["type"] == "print" and said["output_key"] == "summary"
        assert said["print_content"] == "{state.answer_checked.text}"
        assert said["next_step_id"] == "step_end"

    async def test_the_tool_reads_the_answer_from_the_run(self):
        from core.builder_tools import execute_builder_tool_with_events
        from core.models_orchestration import OrchestrationRun
        from core.orchestration.engine import live

        run = OrchestrationRun(run_id="run_f1243", orchestration_id="orch_native_builder",
                               shared_state={"summary": TURN_3})
        with live(run):
            raw, _ = await execute_builder_tool_with_events("check_answer", {}, None, run_id=run.run_id)
        assert json.loads(raw) == {"object": "answer_checked", "text": no_yes_offered(TURN_3)}

    def test_it_is_a_read(self):
        from core import builder_access

        assert "check_answer" in builder_access.READS


class TestTheReportersRule:
    def test_it_never_writes_a_plan_or_asks_for_its_yes(self):
        prompt = " ".join(json.loads((AGENTS / "reporter.json").read_text())["system_prompt"].split())
        assert ("Never write a plan, its steps or a revised version of one, and never ask the person to approve, "
                "say yes or confirm") in prompt
        assert "a plan will be drafted for them to check and approve" in prompt


class TestThePersonSeesOneReplyTheCheckedOne:
    """The chat draws a builder turn's reply from `orchestration_complete`'s `final_state.summary` only, and a step's
    line, a tool's call and its result's preview as it goes (`frontend/src/lib/builderEvents.ts`); an agent's own
    words never stream to it. Run on the definition's own answer steps, the turn shows one reply, the checked one, and
    the sentence that asked for a yes is in no event the chat draws."""

    #: What the chat draws text from, by event type (builderEvents.applyBuilderEvent).
    DRAWN = {"orchestration_complete": lambda e: str((e.get("final_state") or {}).get("summary") or ""),
             "tool_result": lambda e: str(e.get("preview") or ""), "response": lambda e: str(e.get("content") or ""),
             "builder_notice": lambda e: str(e.get("message") or ""), "status": lambda e: str(e.get("message") or ""),
             "thinking": lambda e: str(e.get("message") or ""),
             "human_input_required": lambda e: str(e.get("prompt") or "")}

    async def test_on_the_answer_steps_as_shipped(self, monkeypatch):
        import types

        import core.react_engine as re_mod
        from _fakes import seed as S
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine

        async def reporter(**_kwargs):
            yield {"type": "final", "response": TURN_3}

        monkeypatch.setattr(re_mod, "run_agent_step", reporter)
        await S.seed_agents([S.make_agent(id="agent_answers", name="Answers")])
        answer = {**STEPS["step_answer_workspace"], "agent_id": "agent_answers"}
        orch = Orchestration.model_validate({
            "id": "orch_f1243", "name": "Answer slice", "entry_step_id": answer["id"],
            "state_schema": {k: DEF["state_schema"][k] for k in ("summary", "answer_checked")},
            "steps": [answer, STEPS["step_answer_checked"], STEPS["step_answer_said"], STEPS["step_end"]]})
        engine = OrchestrationEngine(orch, types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}))
        events = [e async for e in engine.run("that's it ?", "run_f1243_events")]

        replies = [self.DRAWN["orchestration_complete"](e) for e in events if e.get("type") == "orchestration_complete"]
        assert replies == [no_yes_offered(TURN_3)]
        drawn = [self.DRAWN[e["type"]](e) for e in events if e.get("type") in self.DRAWN]
        assert not [d for d in drawn if "Just say **approve**" in d], "the asking sentence is drawn nowhere"
