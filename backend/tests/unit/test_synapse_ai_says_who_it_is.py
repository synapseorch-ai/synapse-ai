"""Synapse AI says who it is and what the product delivers, on every route and every host (F1382, TRI1; eb's audit,
Naveen 11-04: "Build as planned"; eb's and CAP3's constraints).

Asked what it can do, Synapse AI listed screens: what it read first was a census of screens, and nothing said what the
product delivers or that this chat builds the thing that does the work. `feature_census.WHO_YOU_ARE` says who it is,
ahead of every host's capabilities text (`builder_help.capabilities()`), so cloud's own words get it too; the
answering steps also read `WHAT_IT_DELIVERS`, one plain example of each outcome. The planner never reads the
examples (eb, CAP3: most of them send, and a brief that sends nothing must not gain a send step). Both are descriptive
only: the planner holds public research, so nothing says this chat does not look things up. The two rules (a live fact is offered, never told; "what can you do" is answered
with two or three things fitted to the workspace) are the talk step's alone.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

NATIVE = Path(__file__).resolve().parents[2] / "core" / "native_builder"


def _steps() -> dict:
    return {s["id"]: s for s in json.loads((NATIVE / "orchestration.json").read_text("utf-8"))["steps"]}


def _talk() -> str:
    return " ".join(_steps()["step_answer_talk"]["prompt_template"].split())


def _examples() -> list[str]:
    from core.feature_census import WHAT_IT_DELIVERS

    return [line for line in WHAT_IT_DELIVERS.splitlines() if line.startswith("- ")]


def _sentence(text: str, *words: str) -> str:
    for sentence in re.split(r"(?<=[.?])\s+", text):
        if all(w in sentence for w in words):
            return sentence
    raise AssertionError(f"no sentence holds {words}")


class TestEveryHostSaysIt:
    def test_self_hosted_opens_with_who_you_are(self):
        from core import builder_help, feature_census

        for examples in (False, True):
            text = builder_help.capabilities(examples=examples)
            assert text.startswith(feature_census.WHO_YOU_ARE.strip())
            assert text.endswith(builder_help.SELF_HOSTED_CAPABILITIES.strip())
            assert (feature_census.WHAT_IT_DELIVERS.strip() in text) is examples

    def test_a_hosts_own_words_get_it_too(self):
        from core import builder_help, feature_census

        builder_help.set_capabilities("What this product does:\n- A host's own words.")
        try:
            text = builder_help.capabilities()
            assert text.startswith(feature_census.WHO_YOU_ARE.strip()) and text.endswith("- A host's own words.")
            assert feature_census.WHAT_IT_DELIVERS.strip() in builder_help.capabilities(examples=True)
        finally:
            builder_help.set_capabilities(None)

    async def test_the_planner_and_the_reporter_read_who_it_is_and_no_example(self):
        from core import builder_help, feature_census

        said = (await builder_help.product_capabilities({}, None, []))["capabilities"]
        assert said == builder_help.capabilities() and feature_census.WHO_YOU_ARE.strip() in said
        assert not any(line in said for line in _examples()), "the planner reads no example"

    def test_the_build_pack_reads_it_through_that_tool(self):
        import inspect

        from core.native_builder import build_plan

        source = inspect.getsource(build_plan)
        assert '"capabilities": (await product_capabilities({}, server_module, []))["capabilities"]' in source
        assert "WHAT_IT_DELIVERS" not in source and "examples=True" not in source

    def test_the_answer_pack_reads_the_examples_too(self):
        import inspect

        from core.native_builder import answer_pack

        assert "capabilities=builder_help.capabilities(examples=True)" in inspect.getsource(answer_pack.answer_pack)

    def test_the_census_text_is_still_the_census_alone(self):
        from core import builder_help, feature_census

        assert builder_help.SELF_HOSTED_CAPABILITIES == feature_census.capabilities_text()
        assert feature_census.WHO_YOU_ARE.strip() not in builder_help.SELF_HOSTED_CAPABILITIES
        assert feature_census.WHAT_IT_DELIVERS.strip() not in builder_help.SELF_HOSTED_CAPABILITIES


class TestWhatItSays:
    def test_who_you_are_and_what_the_chat_does(self):
        from core.feature_census import WHO_YOU_ARE

        first = " ".join(WHO_YOU_ARE.split())
        assert first.startswith("Who you are: Synapse AI") and "build" in first and "schedule" in first, first
        assert "plain steps" in first and "an agent where the work needs judgement" in first, first

    def test_each_outcome_with_one_example(self):
        outcomes = _examples()
        assert len(outcomes) >= 5, outcomes
        assert all(": " in line for line in outcomes), outcomes

    def test_it_is_descriptive_with_no_rule_and_nothing_against_looking_up(self):
        """CAP3's constraint: the planner reads it and holds public research, so a line against looking things up
        could make it skip research. No rule words either: the rules are the talk step's."""
        from core.feature_census import WHAT_IT_DELIVERS, WHO_YOU_ARE

        plain = f"{WHO_YOU_ARE}\n{WHAT_IT_DELIVERS}".lower()
        for word in ("look up", "look it up", "live", "from memory", "this chat", "never", "always", "must", "do not",
                     "don't", "cannot", "can't"):
            assert not re.search(rf"\b{re.escape(word)}\b", plain), word

    def test_the_planner_still_researches(self):
        planner = json.loads((NATIVE / "agents" / "planner.json").read_text("utf-8"))
        assert "research_questions" in planner["tools"]
        assert "\n# Research" in planner["system_prompt"]


class TestTheTalkStepsRules:
    def test_a_live_fact_is_offered_never_told(self):
        said = _sentence(_talk(), "changes over time")
        assert "do not look it up in this chat" in said and "never give it from memory" in said, said
        assert "offer to set up a workflow that gets it" in said and "on a schedule" in said, said
        assert "agent" not in said, said
        assert "say what that needs" in said and "end by asking whether they want it" in said, said

    def test_what_you_can_do_is_fitted_to_the_workspace(self):
        said = _sentence(_talk(), "Asked what you can do for them")
        assert "two or three" in said and "fitted to its description above" in said, said
        assert "never a list of screens" in said, said

    def test_they_are_the_talk_steps_alone(self):
        from core.feature_census import WHAT_IT_DELIVERS, WHO_YOU_ARE

        steps = _steps()
        others = [steps["step_answer_help"]["prompt_template"], steps["step_answer_workspace"]["prompt_template"],
                  (NATIVE / "agents" / "planner.json").read_text("utf-8"),
                  (NATIVE / "agents" / "reporter.json").read_text("utf-8"), WHO_YOU_ARE, WHAT_IT_DELIVERS]
        for text in others:
            plain = " ".join(text.split())
            assert "never give it from memory" not in plain and "never a list of screens" not in plain


class TestTheClassifierTypesIt:
    def test_the_request_has_live_fact(self):
        from core.native_builder.objects import Request

        field = Request.model_fields["live_fact"]
        assert field.default is None and "changes over time" in (field.description or "")

    def test_classify_kinds_says_it(self):
        text = (NATIVE / "prompts" / "classify_kinds.md").read_text("utf-8")
        [said] = [line for line in text.splitlines() if line.startswith("- `live_fact`")]
        assert "changes over time" in said and "Empty when" in said, said

    def test_the_offer_is_declared_state(self):
        from core.native_builder.classify import OFFER_TAKEN_KEY, OFFERED_KEY

        schema = json.loads((NATIVE / "orchestration.json").read_text("utf-8"))["state_schema"]
        assert schema[OFFERED_KEY]["type"] == "dict" and schema[OFFER_TAKEN_KEY]["type"] == "bool"
