"""Every `read_workspace` answer that has nothing says what its topic covers and where to look instead (F1237).

In Naveen's prod runs (10-29) the planner, holding read_workspace alone, needed a workflow's steps and asked topic
`run` about it 11–13 times: "'X' has not run yet." each time, and nothing said where else to look, so it asked again.
Two things, in code: one table (`builder_readers.ELSEWHERE`) gives every topic's sentence — on an error answer it
ends the error, on an empty one it rides as `elsewhere`, the same words every time, so CAP3's repeat guard (F1236)
still sees a repeat; and a `workflow` topic answers one workflow's steps in the builder's own words (F1228's
`parts_words`), never its configs, so the place the sentence points to is there for every caller.
"""
from __future__ import annotations

import pytest

from core import builder_access
from core.builder_readers import ELSEWHERE, SHAPES, TOPICS, shape_problems

from test_the_assistant_reads_what_happened import _read, _workflow, viewer  # noqa: F401

RUN_POINTER = "To see a workflow's steps, read topic `workflow` with its name"


@pytest.fixture(autouse=True)
def settings_from_the_store():
    from core import settings_runtime
    from core.config import get_settings_provider, set_settings_provider

    previous = get_settings_provider()
    settings_runtime.reset_state()
    settings_runtime.install_provider()
    yield
    settings_runtime.reset_state()
    set_settings_provider(previous)


class TestTheTable:
    def test_every_topic_has_its_sentence_and_shape(self):
        assert set(ELSEWHERE) == set(TOPICS) == set(SHAPES)
        assert "workflow" in TOPICS

    def test_each_says_what_it_shows_and_names_another_topic(self):
        for topic, said in ELSEWHERE.items():
            assert said.startswith(("This shows", "This lists")), topic
            assert any(f"`{other}`" in said for other in TOPICS if other != topic), topic

    def test_the_run_topic_points_to_workflow(self):
        assert RUN_POINTER in ELSEWHERE["run"]


class TestAnEmptyOrMissingAnswerSaysWhereInstead:
    async def test_a_workflow_that_has_not_run_points_to_its_steps(self, viewer):
        await _workflow(id="orch_post", name="Weekly post")
        builder_access.forget_runs()
        got = await _read(topic="run", name="Weekly post")
        assert got["error"].startswith("'Weekly post' has not run yet.")
        assert got["error"].endswith(ELSEWHERE["run"])
        assert RUN_POINTER in got["error"]

    async def test_the_same_question_gets_the_same_words(self, viewer):
        await _workflow(id="orch_post", name="Weekly post")
        builder_access.forget_runs()
        assert await _read(topic="run", name="Weekly post") == await _read(topic="run", name="Weekly post")

    @pytest.mark.parametrize("topic", sorted(t for t, s in SHAPES.items() if "items" in s.get("keys", [])))
    async def test_an_empty_list_carries_its_sentence_in_its_shape(self, topic, viewer):
        builder_access.forget_runs()
        got = await _read(topic=topic, name="nothing is called this")
        if "error" in got:
            assert got["error"].endswith(ELSEWHERE[topic]), got
            return
        assert got.get("items") == [], got
        assert got["elsewhere"] == ELSEWHERE[topic]
        assert shape_problems(topic, got) == [], got

    async def test_a_list_with_something_in_it_has_no_sentence(self, viewer):
        await _workflow(id="orch_post", name="Weekly post")
        builder_access.forget_runs()
        got = await _read(topic="workflows")
        assert got["items"] and "elsewhere" not in got


class TestTheWorkflowTopic:
    async def test_it_answers_a_workflows_steps_in_the_builders_words(self, viewer):
        await _workflow(id="orch_post", name="Weekly post", entry_step_id="ask", steps=[
            {"id": "ask", "name": "Ask for the topic", "type": "human", "human_prompt": "What is it about?",
             "output_key": "topic", "next_step_id": "write"},
            {"id": "write", "name": "Write it", "type": "llm", "prompt_template": "PRIVATE PROMPT {state.topic}",
             "output_key": "post", "next_step_id": None}])
        builder_access.forget_runs()
        got = await _read(topic="workflow", name="weekly post")
        assert shape_problems("workflow", got) == [], got
        assert (got["workflow"], got["id"]) == ("Weekly post", "orch_post")
        assert got["steps"] == ['1. Ask for the topic: stops and asks you: "What is it about?"',
                                "2. Write it: a model writes it, with no tools"]
        assert "PRIVATE PROMPT" not in str(got), "a step's prompt never leaves"

    async def test_it_reads_by_id_too(self, viewer):
        await _workflow(id="orch_post", name="Weekly post")
        builder_access.forget_runs()
        assert (await _read(topic="workflow", name="orch_post"))["workflow"] == "Weekly post"

    async def test_a_name_that_is_not_here_says_where_the_names_are(self, viewer):
        builder_access.forget_runs()
        got = await _read(topic="workflow", name="Nope")
        assert got["error"] == f"There is no workflow called 'Nope' here. {ELSEWHERE['workflow']}"

    async def test_no_name_asks_for_one(self, viewer):
        builder_access.forget_runs()
        got = await _read(topic="workflow")
        assert got["error"].startswith("Say which workflow: its name or id.")

    async def test_a_workflow_the_person_cannot_list_is_not_here(self, viewer):
        """Its steps are read only for a workflow topic `workflows` lists to the caller, on this host: a host's own
        `workflows` reader (cloud's roles and sharing) decides, and one it leaves out answers not-found."""
        from core.builder_readers import set_workspace_reader

        await _workflow(id="orch_open", name="Open one")
        await _workflow(id="orch_hidden", name="Hidden one")

        async def listing(_args):
            return {"items": [{"name": "Open one", "steps": 1, "scheduled": False}], "count": 1}

        set_workspace_reader("workflows", listing)
        try:
            builder_access.forget_runs()
            for asked in ("Hidden one", "orch_hidden", "hidden"):
                got = await _read(topic="workflow", name=asked)
                assert got["error"] == f"There is no workflow called {asked!r} here. {ELSEWHERE['workflow']}"
                assert "steps" not in got
            assert (await _read(topic="workflow", name="Open one"))["workflow"] == "Open one"
        finally:
            set_workspace_reader("workflows", None)

    async def test_a_deleted_or_system_workflow_is_not_here(self, viewer):
        await _workflow(id="orch_gone", name="Gone one", is_active=False)
        await _workflow(id="orch_sys", name="System one", system=True)
        builder_access.forget_runs()
        for asked in ("Gone one", "System one"):
            assert (await _read(topic="workflow", name=asked))["error"].startswith(
                f"There is no workflow called {asked!r} here.")

    async def test_two_that_match_are_both_named(self, viewer):
        await _workflow(id="orch_a", name="Weekly post")
        await _workflow(id="orch_b", name="Weekly post digest")
        builder_access.forget_runs()
        got = await _read(topic="workflow", name="weekly")
        assert "'Weekly post'" in got["error"] and "'Weekly post digest'" in got["error"]
