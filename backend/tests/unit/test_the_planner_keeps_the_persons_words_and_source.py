"""The planner keeps the person's words and reaches the source they name (F1365, F1366, F1368, TRI1; CAP3's FF18 gate,
cap-01, cap-05, fix-02; Naveen 11-04: "Build as planned"; eb's F1368 add). F1364's two echoes ride with them.

- F1365 (cap-01): a step's prompt rewrote what the person asked to count in other words, and a word with several
  values in the source was guessed. A step's prompt, its `does` and an agent's `purpose` keep the person's words for
  what to count, filter or pick; a word that could mean more than one value the source shows takes the exact value a
  read showed, or the person is asked which. The prompt writer keeps the plan's words the same way.
- F1366 (cap-05): told the source the person uses, the plan reached another one through a tool that returned the same
  kind of data. The tool bullet says to reach the source they name, and to reuse a tool only when it reaches that
  source and is connected.
- F1368 (fix-02): asked to run a tool once, with some inputs, after it was fixed, the builder built a new workflow.
  Running or trying one tool once is its Test, never a workflow unless they ask for one.
- F1364's echoes: `submit_change` lists every part of the ask in `asked`, and talk_to_agent names what its answer
  `needs`.

Read as the planner and the prompt writer read them. Neutral words, none from a gate brief (checked against CAP3's).
"""
from __future__ import annotations

import re


def _place(key: str) -> str:
    from core.native_builder import prompt_lint

    return " ".join(prompt_lint.expanded_places()[key].split())


def _planner() -> str:
    return _place("agent:agent_native_builder_planner")


def _sentence(text: str, *words: str) -> str:
    """The first sentence (or bullet) of `text` that holds every one of `words`."""
    for sentence in re.split(r"(?<=[.;])\s+|\s(?=- )", text):
        if all(w in sentence for w in words):
            return sentence
    raise AssertionError(f"no sentence holds {words}")


class TestThePersonsWords:
    def test_what_to_count_filter_or_pick_stays_in_their_words(self):
        said = _sentence(_planner(), "person's own words", "count, filter or pick")
        assert "`does`" in said and "`purpose`" in said and "never" in said, said

    def test_a_word_with_several_values_takes_the_exact_one_or_is_asked(self):
        text = _planner()
        start = text.index("more than one value", text.index("person's own words"))
        said = text[text.rindex(". ", 0, start) + 2:text.index(". ", start) + 1]
        assert "exact value" in said and "ask the person which" in said, said

    def test_the_prompt_writer_keeps_the_plans_words(self):
        said = _sentence(_place("agent:agent_native_builder_prompt_writer"), "plan's words")
        assert "counts, filters or picks" in said and "never another word" in said, said


class TestTheSource:
    def test_the_tool_bullet_reaches_the_source_named_and_reuses_only_a_connected_tool_that_reaches_it(self):
        text = _planner()
        bullet = text[text.index("- `tool`: calls one tool"):text.index("- `human`:")]
        said = _sentence(bullet, "source the person names")
        assert "Reach" in said, said
        reuse = _sentence(bullet, "existing tool")
        assert "same source" in reuse and "connected" in reuse and "never another source's tool" in reuse, reuse


class TestRunningAToolOnce:
    def test_running_or_trying_a_tool_once_is_its_test_never_a_workflow_unasked(self):
        text = _planner()
        choose = text[text.index("# Choose what the ask needs"):text.index("# Research")]
        said = _sentence(choose, "tool once")
        assert "Test" in said and "never a workflow unless they ask" in said, said


class TestF1364sEchoes:
    def test_a_change_lists_every_part_of_the_ask(self):
        text = _planner()
        changing = text[text.index("# Changing"):text.index("# Finishing")]
        said = _sentence(changing, "`asked`")
        assert "every part" in said and "`why_not`" in said and "never dropped" in said, said

    def test_asking_an_agent_names_what_its_answer_needs(self):
        said = _sentence(_planner(), "talk_to_agent on the agent")
        assert "`needs`" in said and "only an agent holding them" in said, said
