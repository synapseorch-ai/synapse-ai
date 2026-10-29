"""A plan uses a board only where one is needed, and its agents are told how to work it (F1254; Naveen's picks, 10-30).

None of Naveen's nine runs used a board, and the post generator kept nothing between runs, so it repeated itself. The
planner's pack now says when a board is the right thing (work other steps claim mid-run, work that outlives the run,
items tracked over time, such as a "Posted" board a scheduled workflow reads first) and when it is not (a straight
hand-off is the step's output, a known list a Loop or Parallel). The build check sends back a board nobody reads and a
hand-off forced through a board, and each new agent with board tools gets a section code writes from those tools,
on the card the person approves.
"""
from __future__ import annotations

import pytest

from core.native_builder import objects as O
from tests.unit.test_a_build_is_saved_in_code import _reviewed, workspace  # noqa: F401 — the build's fixtures
from tests.unit.test_the_board_asked_for_is_the_one_used import boards_on  # noqa: F401

FIND = {"id": "find", "type": "agent", "name": "Find features", "does": "Finds three product features",
        "agent": "Finder", "prompt": "Find three features of the product.", "next": "write"}
WRITE = {"id": "write", "type": "agent", "name": "Write the post", "does": "Writes the post",
         "agent": "Writer", "prompt": "Write a post about the features."}


def _rule():
    """The module, read when a test runs: on a tree without it each test fails on its own (red-first)."""
    from core.native_builder import boards_rule

    return boards_rule


def _agent(name: str, *tools: str) -> dict:
    return {"name": name, "purpose": f"{name} does its part", "tools": list(tools)}


def _draft(*steps, agents=(), **more) -> O.PlanDraft:
    return O.PlanDraft.model_validate({"kind": "create", "title": "Product posts", "summary": "Posts about it.",
                                       "steps": list(steps), "agents": list(agents), **more})


class TestTheCheck:
    def test_a_board_nothing_reads_is_sent_back(self):
        found = _rule().issues(_draft(FIND, {**WRITE, "agent": "Finder"}, agents=[_agent("Finder", "board_add")]))
        assert len(found) == 1 and "nothing reads" in found[0] and "{{ state.find }}" in found[0]

    def test_a_hand_off_through_a_board_is_sent_back(self):
        found = _rule().issues(_draft(FIND, WRITE, agents=[_agent("Finder", "board_add"),
                                                               _agent("Writer", "board_list", "board_read")]))
        assert len(found) == 1 and "hands its results to step write through the board" in found[0]
        assert "{{ state.find }}" in found[0] and "{state.find}" not in found[0].replace("{{ state.find }}", "")

    @pytest.mark.parametrize("more", [
        {"boards": [{"name": "Posted"}]},
        {"schedule": {"cron": "0 9 * * 1", "says": "every Monday at 9"}},
    ], ids=["a board the person keeps", "a scheduled workflow's later runs read it"])
    def test_a_board_kept_past_the_run_is_its_own_reason(self, more):
        agents = [_agent("Finder", "board_add"), _agent("Writer", "board_list", "board_read")]
        assert _rule().issues(_draft(FIND, WRITE, agents=agents, **more)) == []

    def test_work_other_steps_claim_is_a_board_s(self):
        """Work found mid-run that other steps claim: the reader claims, so it is not a hand-off."""
        agents = [_agent("Finder", "board_add"), _agent("Writer", "board_claim", "board_update")]
        assert _rule().issues(_draft(FIND, WRITE, agents=agents)) == []

    def test_branches_that_work_one_board_are_not_a_hand_off(self):
        fan = {"id": "fan", "type": "parallel", "name": "Write each", "does": "Writes each post", "body": ["write"]}
        agents = [_agent("Finder", "board_add"), _agent("Writer", "board_list", "board_read")]
        assert _rule().issues(_draft({**FIND, "next": "fan"}, fan, WRITE, agents=agents)) == []

    def test_a_plan_without_board_tools_is_not_judged(self):
        assert _rule().issues(_draft(FIND, WRITE, agents=[_agent("Finder"), _agent("Writer")])) == []


class TestTheAgentsAreTold:
    def test_each_tool_has_its_line(self):
        text = _rule().board_lines({"board_list", "board_read", "board_add", "board_update", "board_claim"},
                                       'the board "Posted"')
        assert text.startswith('## Your board\nYou work the board "Posted".')
        for words in ("Read it first", "Claim a card", "Add a card for each thing", "Mark a card done"):
            assert words in text

    def test_only_the_board_agents_get_it_after_their_own_instructions(self):
        plan = _draft(FIND, WRITE, agents=[_agent("Finder", "board_add"), _agent("Writer")])
        out = _rule().with_board_lines(plan, {"Finder": "Find features.", "Writer": "Write the post."})
        assert out["Writer"] == "Write the post."
        assert out["Finder"].startswith("Find features.\n\n## Your board\nYou work this workflow's own board.")


def test_the_planner_is_told_when_a_board_is_the_right_thing():
    assert "never through a board" in _rule().GUIDANCE and '"Posted"' in _rule().GUIDANCE
    assert "{{ state.<step id> }}" in _rule().GUIDANCE and "{state." not in _rule().GUIDANCE.replace(
        "{{ state.", "")


async def test_the_pack_carries_it(boards_on):
    from core import builder_access
    from core.models_orchestration import OrchestrationRun
    from core.orchestration.state import SharedState
    from tests.unit.test_the_build_route_decides_in_code import _call

    run = OrchestrationRun(run_id="builder_f1254_pack", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create"})
    await SharedState(run).checkpoint()
    pack, _ = await _call("read_plan_context", {}, run)
    assert _rule().GUIDANCE in pack["boards"]


async def test_the_post_generator_s_card_tells_its_agent(boards_on):
    """Naveen's post generator with a "Posted" board: the card the person approves shows the agent's board section,
    and the instructions the save reads (`checked["prompts"]`, build_save._prompts) carry it."""
    poster = {"id": "post", "type": "agent", "name": "Write and post", "does": "Writes a post not posted before",
              "agent": "Poster", "prompt": "Write a post about a feature not on the Posted board."}
    draft = {"finish": "submit_plan", "kind": "create", "title": "Product posts", "summary": "Posts about it.",
             "steps": [poster], "agents": [_agent("Poster", "board_list", "board_read", "board_add")],
             "boards": [{"name": "Posted", "purpose": "What was posted already"}]}
    run, checked = await _reviewed(draft, prompts={"Poster": "Write one short post."})
    assert checked["outcome"] == "review", checked
    assert '## Your board\nYou work the board "Posted".' in checked["review"]
    assert checked["prompts"]["Poster"].startswith("Write one short post.\n\n## Your board")


async def test_the_planner_s_hand_off_through_a_board_is_sent_back(boards_on):
    """Through the planner's own finish: the plan goes back with the words, before any card."""
    from tests.unit.test_the_build_route_decides_in_code import _call, _run

    agents = [_agent("Finder", "board_add"), _agent("Writer", "board_list", "board_read")]
    sent, _ = await _call("submit_plan", {"kind": "create", "title": "Product posts", "summary": "Posts about it.",
                                          "steps": [FIND, WRITE], "agents": agents}, await _run())
    assert "hands its results to step write through the board" in sent.get("error", ""), sent
