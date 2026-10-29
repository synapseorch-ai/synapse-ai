"""A workflow step's task list is its own (F1249; TRI1's triage of Naveen's prod chat, 10-29, part 2).

Every agent step of a run was seeded with the run's whole list (`run.tasks`) and wrote its own over it. Naveen's
write_posts started with the scraper's plan in its context ("Current task list for this run"), and spent 2 of its 3
turns keeping the scraper's tasks; the run page then showed only the last step's. Now each step starts with its own
list and rewrites only its own entries in the run's, each tagged with its step; the run's list stays the whole run's
view. A Loop's second item is a run of the step of its own and starts afresh too, or its end check would meet the
first item's leftovers; a step that resumes where it stopped gets back what it had.
"""
from __future__ import annotations

import json
import types

from _fakes import seed as S
from _fakes.fake_llm import tool_call

SCRAPE = [{"content": "scrape the product page", "status": "completed"},
          {"content": "pick three features", "status": "completed"}]
WRITE = [{"content": "write the post", "status": "completed"}]


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _run(orch: dict, run_id: str) -> list[dict]:
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
    return [ev async for ev in engine.run("go", run_id=run_id)]


async def _saved_tasks(run_id: str) -> list[dict]:
    import core.orchestration.state as state_mod

    return (await state_mod.get_state_backend().load(run_id)).tasks


def _by_step(tasks: list[dict]) -> list[tuple[str, str, str]]:
    return [(t.get("step_id"), t["content"], t["status"]) for t in tasks]


def _saw(calls: list[dict], tasks: list[dict]) -> list[str]:
    text = json.dumps(calls, default=str)
    return [t["content"] for t in tasks if t["content"] in text]


async def test_naveen_s_writer_starts_without_the_scraper_s_plan(fake_llm, seed_agent):
    agent = await seed_agent(tools=[])
    orch = S.make_orchestration(id="orch_f1249_a", entry_step_id="scrape", steps=[
        {"id": "scrape", "name": "Scrape", "type": "agent", "agent_id": agent["id"], "next_step_id": "write_posts"},
        {"id": "write_posts", "name": "Write posts", "type": "agent", "agent_id": agent["id"], "next_step_id": None}])
    fake_llm.script([tool_call("update_tasks", tasks=SCRAPE), "scraped",
                     tool_call("update_tasks", tasks=WRITE), "posted"])
    events = await _run(orch, "run_f1249_a")
    assert any(e.get("type") == "orchestration_complete" for e in events)
    assert _saw(fake_llm.calls[2:], SCRAPE) == [], "the writer's turns never hold the scraper's tasks"
    saved = await _saved_tasks("run_f1249_a")
    assert _by_step(saved) == [("scrape", t["content"], t["status"]) for t in SCRAPE] + [
        ("write_posts", t["content"], t["status"]) for t in WRITE], "the run's list is every step's, each tagged"


async def test_a_loop_s_second_item_starts_afresh(fake_llm, seed_agent):
    """8a's question (10-30): item 2 starts with none of item 1's tasks, finished or not."""
    agent = await seed_agent(tools=[])
    orch = S.make_orchestration(id="orch_f1249_b", entry_step_id="each", steps=[
        {"id": "each", "name": "Each", "type": "loop", "loop_count": 2, "loop_step_ids": ["post"],
         "next_step_id": None},
        {"id": "post", "name": "Post", "type": "agent", "agent_id": agent["id"], "next_step_id": None}])
    first, second = [{"content": "post item 1", "status": "completed"}], [{"content": "post item 2",
                                                                          "status": "completed"}]
    fake_llm.script([tool_call("update_tasks", tasks=first), "one", tool_call("update_tasks", tasks=second), "two"])
    await _run(orch, "run_f1249_b")
    assert _saw(fake_llm.calls[2:], first) == [], "item 2's turns never hold item 1's tasks"
    assert _by_step(await _saved_tasks("run_f1249_b")) == [("post", "post item 2", "completed")], \
        "the step's latest run is its view"


async def test_the_same_step_in_two_branches_at_once_keeps_two_lists(fake_llm, seed_agent):
    """8a (10-30): one step running twice at once (it sits in both branches of a Parallel) never overwrites the other
    run's list, and each run's turns hold only its own."""
    agent = await seed_agent(tools=[])
    orch = S.make_orchestration(id="orch_f1249_c", entry_step_id="both", steps=[
        {"id": "both", "name": "Both", "type": "parallel", "parallel_branches": [["post"], ["post"]],
         "next_step_id": None},
        {"id": "post", "name": "Post", "type": "agent", "agent_id": agent["id"], "next_step_id": None}])
    one, two = [{"content": "post to X", "status": "completed"}], [{"content": "post to Bluesky", "status": "completed"}]
    fake_llm.script([tool_call("update_tasks", tasks=one), tool_call("update_tasks", tasks=two), "done", "done"])
    events = await _run(orch, "run_f1249_c")
    assert any(e.get("type") == "orchestration_complete" for e in events)
    saved = await _saved_tasks("run_f1249_c")
    assert sorted(_by_step(saved)) == [("post", "post to Bluesky", "completed"), ("post", "post to X", "completed")]
    assert len({t["run_of"] for t in saved}) == 2, "two runs of the step, two lists"
    assert all(not (_saw([c], one) and _saw([c], two)) for c in fake_llm.calls), "no turn holds the other run's"


class TestTheStepsOwnList:
    def test_a_fresh_start_has_none_and_a_resume_has_its_own(self):
        from core.task_list import begin_step_tasks, end_step_tasks

        run = [{"content": "a", "status": "completed", "step_id": "s1", "run_of": "k1"},
               {"content": "b", "status": "in_progress", "step_id": "s2", "run_of": "k2"},
               {"content": "old", "status": "pending"}]
        key, seed = begin_step_tasks("r1", "s2", run, resuming=False)
        assert seed == [] and key != "k2"
        end_step_tasks("r1", "s2", key)
        key, seed = begin_step_tasks("r1", "s2", run, resuming=True)
        assert (key, seed) == ("k2", [{"content": "b", "status": "in_progress"}])
        end_step_tasks("r1", "s2", key)
        key, seed = begin_step_tasks("r1", "s3", run, resuming=True)
        assert seed == [], "a list from before the tag is no step's"
        end_step_tasks("r1", "s3", key)

    def test_a_rewrite_replaces_its_own_and_the_step_s_finished_runs(self):
        from core.task_list import begin_step_tasks, end_step_tasks, with_step_tasks

        run = [{"content": "a", "status": "completed", "step_id": "s1", "run_of": "k1"},
               {"content": "b", "status": "pending", "step_id": "s2", "run_of": "old"},
               {"content": "c", "status": "pending", "step_id": "s3", "run_of": "k3"}, {"content": "x", "status": "pending"}]
        key, _ = begin_step_tasks("r2", "s2", run, resuming=False)
        new = with_step_tasks(run, "r2", "s2", key, [{"content": "b2", "status": "pending"}])
        assert new == [run[0], {"content": "b2", "status": "pending", "step_id": "s2", "run_of": key}, run[2], run[3]]
        assert with_step_tasks(new, "r2", "s2", key, []) == [run[0], run[2], run[3]]
        end_step_tasks("r2", "s2", key)

    def test_another_run_of_the_step_going_on_now_is_kept(self):
        from core.task_list import begin_step_tasks, end_step_tasks, with_step_tasks

        first, _ = begin_step_tasks("r3", "s", [], resuming=False)
        second, _ = begin_step_tasks("r3", "s", [], resuming=False)
        run = with_step_tasks([], "r3", "s", first, [{"content": "x", "status": "pending"}])
        run = with_step_tasks(run, "r3", "s", second, [{"content": "y", "status": "pending"}])
        assert [(t["content"], t["run_of"]) for t in run] == [("x", first), ("y", second)]
        end_step_tasks("r3", "s", first)
        end_step_tasks("r3", "s", second)
