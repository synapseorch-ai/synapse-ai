"""What a person reads never carries Synapse AI's own agent id, or a hint written for the model (F1674).

Asked to add a file to a knowledge base "then answer from it", Synapse AI put up the knowledge base's card and also
asked an agent, named by its own id, to answer. Every agent picker leaves Synapse AI out (it answers in Chat; a card
cannot ask it, and a channel, widget or schedule cannot be answered by it), so the pick was refused with
"No agent is called 'agent_synapse_ai'. Topic `agents` lists them.", and that line, written for the model, was joined
into the person's reply as it was, beside "No knowledge_base is called "Policies".".

Now an agent picker that is given Synapse AI itself, by its id or its name, or one of its own builder agents, says so
in plain words without the id; and where the change's lines become the person's reply, a hint for the model is left
out, a kind is said in words, and Synapse AI's own id is never printed.

Near misses: an agent that is not there is still named as asked; the model's own tool error keeps its hint; a line
with nothing to clean is unchanged.
"""
from __future__ import annotations

import pytest

from core import builder_actions as A
from core.default_agent import DEFAULT_AGENT_ID
from core.native_builder import change_apply, change_tools

AGENTS = [
    {"id": DEFAULT_AGENT_ID, "name": "Synapse AI", "type": "builder"},
    {"id": "agent_native_builder_planner", "name": "Plan It", "type": "builder", "system": "native_builder"},
    {"id": "agent_frontdesk", "name": "Front desk", "type": "conversational"},
]


@pytest.fixture
def workspace(monkeypatch):
    async def load_agents():
        return [dict(a) for a in AGENTS]

    import core.routes.agents as routes_agents
    import core.store.resources as resources

    monkeypatch.setattr(resources, "load_agents", load_agents)
    monkeypatch.setattr(routes_agents, "load_user_agents", load_agents)


async def _refusal(args: dict) -> str:
    with pytest.raises(A._NotOffered) as why:
        await A._talk(args)
    return str(why.value)


class TestAPickerGivenSynapseAiItself:
    @pytest.mark.parametrize("named", [DEFAULT_AGENT_ID, "Synapse AI", "synapse ai", "agent_native_builder_planner"])
    async def test_says_so_in_plain_words_without_the_id(self, workspace, named):
        said = await _refusal({"agent": named, "message": "How many days do customers have to return an item?"})
        assert "Synapse AI itself" in said, said
        assert DEFAULT_AGENT_ID not in said and "agent_native_builder" not in said, said

    async def test_a_widget_s_agent_is_refused_the_same_way(self, workspace):
        with pytest.raises(A._NotOffered) as why:
            await A._widget_agent(DEFAULT_AGENT_ID)
        assert "Synapse AI itself" in str(why.value) and DEFAULT_AGENT_ID not in str(why.value)

    async def test_an_agent_that_is_not_there_is_still_named_as_asked(self, workspace):
        said = await _refusal({"agent": "Billing helper", "message": "hi"})
        assert said == "No agent is called 'Billing helper'. Topic `agents` lists them."

    async def test_the_workspace_s_own_agent_is_still_picked(self, workspace):
        assert (await A._pick_agent([AGENTS[2]], "Front desk"))["id"] == "agent_frontdesk"


class TestTheLinesThePersonReads:
    P5 = [{"status": "card", "say": 'A card is up: Make a knowledge base "Policies". Press "Make it" to do it.'},
          {"status": "refused", "say": 'No knowledge_base is called "Policies".'},
          {"status": "noop", "say": f"No agent is called '{DEFAULT_AGENT_ID}'. Topic `agents` lists them."}]

    def test_the_p5_reply_has_no_id_no_hint_and_kinds_in_words(self):
        said = change_tools._said(self.P5)
        assert DEFAULT_AGENT_ID not in said, said
        assert "Topic" not in said and "lists them" not in said, said
        assert 'No knowledge base is called "Policies".' in said, said
        assert said.startswith('A card is up: Make a knowledge base "Policies".'), said

    def test_a_hint_in_brackets_goes_too(self):
        said = change_tools.said_to_the_person("Say which run: its run_id (topic `runs` or `waiting` lists them).")
        assert said == "Say which run: its run_id."

    def test_a_line_with_nothing_to_clean_is_unchanged(self):
        line = 'No workflow is called "Weekly report".'
        assert change_tools.said_to_the_person(line) == line

    def test_the_make_new_reply_s_lines_are_cleaned_the_same_way(self):
        lines = change_apply._unmade_lines({}, {"ops": self.P5[1:]})
        assert lines and all(DEFAULT_AGENT_ID not in x and "Topic" not in x for x in lines), lines
