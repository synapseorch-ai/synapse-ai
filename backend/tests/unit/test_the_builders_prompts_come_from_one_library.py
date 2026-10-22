"""Synapse AI's prompts come from one library, and three prompt-form rules hold over all of them (SAI2-P1, F773).

The same paragraph was pasted into three agents and two steps (the savers' parallel section, "a tool's description
must match", the findings steps' opening), so a fix to one copy left the others as they were (F764). A passage two
places share now lives once, in `core/native_builder/prompts/<name>.md`, and each place names it as
`{{PROMPT:name}}`, expanded when the bundle is read exactly as `{{STEP_TYPE_CHEATSHEET}}` is.

Moving them changed no word: the expanded bundle is byte-for-byte what it was (its hash is pinned below; a phase that
changes a prompt on purpose updates the hash in the same commit, and says so). Three rules hold over the whole bundle:

* F757: no prompt asks for a marker block (`[REASONING]`, `[WHY]` …), which the Claude 5 family refuses;
* F764: no sentence of twelve words or more is written in two places unless both name one fragment;
* F767: a for-each loop's body step leads its prompt with the item it is working on.
"""
import hashlib
import json

import pytest

from core.native_builder import prompt_lint, seed

#: The expanded bundle. Change it only with a prompt change made on purpose, in the same commit. At v2.0 635a3967,
#: before any passage moved into the library: 0edc6f5b…; SAI2-P2 (F799): the change route on a typed Change,
#: 074d96f7…; F800: the analyst no longer told never to call a tool it does not hold, 2b9c8941…; a change's hand-over
#: goes back through the door, 7eb7dec5…; a code step's error is said, 29c31610…; too many rounds end in words,
#: 5fc3d1f8…; one new thing is made by the maker after its review.
BUNDLE_SHA256 = "29341fcf1c93489af0b0cd7ee636722f1362a50cdbb0af704f2a8845cf040660"


def _sha(definitions) -> str:
    return hashlib.sha256(json.dumps(definitions, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


class TestTheBundleIsWhatItWas:
    def test_byte_for_byte(self):
        assert _sha(seed.bundled_definitions()) == BUNDLE_SHA256
        assert seed.bundle_version() == BUNDLE_SHA256[:16]

    def test_no_token_is_left_unexpanded(self):
        text = json.dumps(seed.bundled_definitions(), ensure_ascii=False)
        assert "{{PROMPT:" not in text and "{{STEP_TYPE_CHEATSHEET}}" not in text

    def test_the_shared_passages_are_named_not_pasted(self):
        """The sources name their fragments; the fragments are the library's."""
        raw = prompt_lint.raw_places()
        named = {name for text in raw.values() for name in seed.FRAGMENT.findall(text)}
        assert named == set(seed.fragment_names()), "every fragment is used, and every one used exists"
        assert all(len(seed.prompt_fragment(n)) > 40 for n in named)


class TestAFragment:
    def test_an_unknown_name_is_an_error_not_a_blank(self):
        with pytest.raises(KeyError, match="no_such_passage"):
            seed.expand_prompts("Before. {{PROMPT:no_such_passage}} After.")

    def test_it_expands_where_it_is_named(self):
        name = seed.fragment_names()[0]
        assert seed.expand_prompts(f"A {{{{PROMPT:{name}}}}} B") == f"A {seed.prompt_fragment(name)} B"


class TestThePromptFormRules:
    def test_f757_no_marker_block(self):
        assert prompt_lint.marker_blocks(prompt_lint.expanded_places()) == []

    def test_f757_finds_one(self):
        assert prompt_lint.marker_blocks({"agent:x": "Write a [REASONING] block before every call."}) == [
            ("agent:x", "[REASONING]")]

    def test_f764_no_sentence_in_two_places(self):
        assert prompt_lint.duplicated_sentences(prompt_lint.raw_places(with_fragments=True)) == {}

    def test_f764_finds_one(self):
        twice = "Call the tool once for every provider the report below looked at, then stop and wait."
        found = prompt_lint.duplicated_sentences({"step:a": f"Hello. {twice}", "step:b": twice})
        assert list(found.values()) == [["step:a", "step:b"]]

    def test_f767_every_loop_leads_with_its_item(self):
        assert prompt_lint.loops_not_item_first(seed.bundled_definitions()["orchestration"]) == []

    def test_f767_finds_one(self):
        orch = {"steps": [
            {"id": "each", "type": "loop", "loop_over": "state.orders", "loop_item_key": "order",
             "loop_step_ids": ["ask", "say"]},
            {"id": "ask", "type": "agent", "prompt_template": "Status of the order?\n{state.order}"},
            {"id": "say", "type": "llm", "prompt_template": "Order {state.order}: write one line on its status."},
        ]}
        assert prompt_lint.loops_not_item_first(orch) == ["ask"]


class TestAPromptTeachesOnlyTheToolsItsAgentHolds:
    """F800 (SAI2-P2): the class behind F803. A model taught a tool it cannot call calls it: gpt-oss-120b called the
    `update_tasks` its prompt taught but its turn did not offer, 3 times in 12. A prompt an agent reads — its own and
    its steps' — names in backticks only tools that agent holds. The one today's bundle had (the analyst's "NEVER call
    `list_all_tools`", a tool it does not hold) is gone rather than listed."""

    def test_none_does(self):
        assert prompt_lint.tools_not_held() == []

    def test_one_that_does_is_named(self):
        agents = [{"id": "a", "tools": ["read_workspace"], "system_prompt": "Read with `read_workspace`, then call "
                                                                             "`delete_agent` and `update_tasks`."}]
        orch = {"steps": [{"id": "s", "agent_id": "a", "prompt_template": "Finish with `submit_change`."}]}
        assert prompt_lint.tools_not_held(agents, orch) == [("agent:a", "delete_agent"),
                                                            ("step:s.prompt_template", "submit_change")]

    def test_a_name_that_is_no_tool_is_not_counted(self):
        agents = [{"id": "a", "tools": [], "system_prompt": "Use the op `edit_steps` and its `what` field."}]
        assert prompt_lint.tools_not_held(agents, {"steps": []}) == []
