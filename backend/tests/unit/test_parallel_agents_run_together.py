"""
A Parallel's agent branches run at the same time (F255).

Only LLM-family branches overlapped. A branch with an agent or a tool step ran
alone, afterwards, whatever it did, because MCP sessions were thought unsafe to
share: the stress pass measured four five-second agent branches at 20.3 s beside
four LLM branches at 5.1 s. The sessions are safe (spawn_subtask has shared them
for a long time); bash was the one server that could not serve two calls at once,
and it can now (F246).

Now every branch runs at the same time, at most four at once, unless it could
stop to ask a person — a run waits on one question at a time — or holds a Loop
or a nested Parallel, drives the browser, or reads a key another branch writes.
Those run afterwards, in order. A failed branch still fails the node once the
running ones finish, and one waiting for a slot does not start. A branch that
asks anyway is held until the others finish, and the node pauses on it.

The fake agent sleeps a fixed 0.3 s, so "at the same time" is a wall-clock fact.
"""
import asyncio
import re
import time
import types

import pytest

from _fakes import seed as S

DELAY = 0.3


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _agent_step(sid, agent_id="a_plain", prompt=None, key=None):
    return {"id": sid, "name": sid.upper(), "type": "agent", "agent_id": agent_id,
            "prompt_template": prompt or f"B:{sid} go", "output_key": key or f"o_{sid}",
            "next_step_id": None}


def _llm(sid):
    return {"id": sid, "name": sid.upper(), "type": "llm", "prompt_template": f"angle {sid}",
            "output_key": f"o_{sid}", "model": "claude-test", "next_step_id": None}


def _fanout(branches, orch_id="orch_par_agents"):
    steps = [{"id": "fan", "name": "Fan", "type": "parallel",
              "parallel_branches": [[b["id"]] for b in branches], "next_step_id": None}, *branches]
    return S.make_orchestration(id=orch_id, entry_step_id="fan", steps=steps)


async def _run(orch, run_id="run_par_agents"):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
    return [ev async for ev in engine.run("go", run_id=run_id)]


async def _answer(run_id, response):
    from core.orchestration.engine import OrchestrationEngine
    return [ev async for ev in OrchestrationEngine.resume(run_id, response, _server())]


def _status(events):
    if events and events[-1].get("type") == "human_input_required":
        return "paused"
    return next(e for e in reversed(events) if e.get("type") == "orchestration_complete")["status"]


async def _saved(run_id):
    from core.orchestration.state import SharedState
    return (await SharedState.restore(run_id)).run


class _Agents:
    """`run_agent_step`, faked: each call sleeps DELAY and answers. Knows which
    step called it from the `B:<id>` in its prompt, and when it ran."""

    def __init__(self):
        self.t0 = time.perf_counter()
        self.active = 0
        self.most = 0
        self.spans: dict[str, list[tuple[float, float]]] = {}
        self.fail: set[str] = set()
        self.ask: set[str] = set()
        self.tasks: dict[str, list[dict]] = {}

    def start(self, key):
        return min(s for s, _ in self.spans[key])

    def end(self, key):
        return max(e for _, e in self.spans[key])

    async def run_agent_step(self, **kw):
        key = re.search(r"B:(\w+)", kw.get("message") or "").group(1)
        began = time.perf_counter() - self.t0
        self.active += 1
        self.most = max(self.most, self.active)
        try:
            resume = kw.get("resume_state") or {}
            if key in self.ask and not resume.get("approved_calls"):
                yield {"type": "human_input_required", "kind": "tool_approval",
                       "approval_id": f"ap_{key}", "tool_name": "danger", "tool_args": {},
                       "category": "destructive", "prompt": f"Allow {key}?"}
                return
            await asyncio.sleep(DELAY)
            if key in self.fail:
                raise RuntimeError(f"{key} exploded")
            if key in self.tasks:
                yield {"type": "tasks_updated", "tasks": self.tasks[key]}
            yield {"type": "final", "response": f"{key} done"}
        finally:
            self.active -= 1
            self.spans.setdefault(key, []).append((began, time.perf_counter() - self.t0))


def _base_agents():
    return [
        S.make_agent(id="a_plain"),
        S.make_agent(id="a_memory", memory=True),
        S.make_agent(id="a_confirm", tool_policy={"destructive": "confirm"}),
        S.make_agent(id="a_delegate", type="delegate"),
    ]


@pytest.fixture
async def agents(monkeypatch):
    import core.react_engine as re_mod
    fake = _Agents()
    monkeypatch.setattr(re_mod, "run_agent_step", fake.run_agent_step)
    await S.seed_agents(_base_agents())  # seeding replaces every agent
    return fake


class TestAgentBranchesOverlap:
    async def test_four_agent_branches_take_the_max_not_the_sum(self, agents):
        events = await _run(_fanout([_agent_step(f"a{i}") for i in range(4)]))

        assert _status(events) == "completed"
        assert agents.most == 4, f"at most {agents.most} ran at once"
        last_end = max(agents.end(f"a{i}") for i in range(4))
        assert last_end < 2 * DELAY, f"the last branch ended at {last_end:.2f}s — they took turns"
        state = (await _saved("run_par_agents")).shared_state
        assert all(state[f"o_a{i}"] == f"a{i} done" for i in range(4))

    async def test_no_more_than_four_run_at_once(self, agents):
        events = await _run(_fanout([_agent_step(f"a{i}") for i in range(6)]))

        assert _status(events) == "completed"
        assert agents.most == 4
        assert sorted(agents.spans) == [f"a{i}" for i in range(6)]

    async def test_an_agent_branch_overlaps_the_llm_ones(self, agents, fake_llm, monkeypatch):
        for k, v in (("MIN", "0.3"), ("MAX", "0.3"), ("PROB", "1")):
            monkeypatch.setenv(f"SYNAPSE_FAKE_LLM_DELAY_{k}", v)
        fake_llm.set_default("branch output")
        events = await _run(_fanout([_llm("l1"), _agent_step("ag"), _llm("l2")]))

        assert _status(events) == "completed"
        llm_done = [e for e in events if e.get("type") == "step_complete" and e.get("orch_step_id") in {"l1", "l2"}]
        assert len(llm_done) == 2
        assert agents.start("ag") < DELAY / 2, "the agent branch waited for the llm branches"


class TestABranchThatCanAskRunsAfterTheOthers:
    @pytest.mark.parametrize("branch, why", [
        (_agent_step("mem", agent_id="a_memory"), "keeps memory"),
        (_agent_step("conf", agent_id="a_confirm"), "tool policy asks"),
        (_agent_step("dlg", agent_id="a_delegate"), "delegates"),
    ])
    async def test_it_starts_once_the_others_are_done(self, agents, branch, why):
        events = await _run(_fanout([_agent_step("a0"), branch, _agent_step("a1")]))

        assert _status(events) == "completed"
        key = branch["id"]
        others_done = max(agents.end("a0"), agents.end("a1"))
        assert agents.start(key) >= others_done - 0.01, f"'{key}' ran beside the others"
        assert abs(agents.start("a0") - agents.start("a1")) < DELAY / 2, "the others did not overlap"
        start = next(e for e in events if e.get("type") == "parallel_start")
        assert [(r["branch"], why in r["reason"]) for r in start["in_order"]] == [(1, True)]

    async def test_a_human_step_keeps_its_branch_in_order(self, agents):
        human = {"id": "ok", "name": "OK", "type": "human", "human_prompt": "ok?", "output_key": "h",
                 "next_step_id": None}
        events = await _run(_fanout([_agent_step("a0"), human, _agent_step("a1")]))

        assert _status(events) == "paused"
        assert abs(agents.start("a0") - agents.start("a1")) < DELAY / 2
        start = next(e for e in events if e.get("type") == "parallel_start")
        assert start["in_order"] == [{"branch": 1, "reason": "a person answers a step in it"}]

    async def test_a_branch_reading_a_siblings_key_waits_for_it(self, agents):
        reader = _agent_step("rd", prompt="B:rd use {state.o_a0}")
        events = await _run(_fanout([_agent_step("a0"), reader, _agent_step("a1")]))

        assert _status(events) == "completed"
        assert agents.start("rd") >= agents.end("a0") - 0.01
        start = next(e for e in events if e.get("type") == "parallel_start")
        assert start["in_order"] == [{"branch": 1, "reason": "it reads 'o_a0', which another branch writes"}]

    async def test_an_agent_with_the_browser_runs_alone(self, agents, monkeypatch):
        import core.tools as tools_mod
        import mcp.types as mt
        await S.seed_agents([*_base_agents(), S.make_agent(id="a_browser", tools=["all"])])
        real = tools_mod.aggregate_all_tools

        async def aggregate(server_module, agent, custom):
            if agent.get("id") == "a_browser":
                return [mt.Tool(name="browser_click", inputSchema={"type": "object"})], {}, [], ""
            return await real(server_module, agent, custom)

        monkeypatch.setattr(tools_mod, "aggregate_all_tools", aggregate)
        events = await _run(_fanout([_agent_step("a0"), _agent_step("web", agent_id="a_browser")]))

        assert _status(events) == "completed"
        assert agents.start("web") >= agents.end("a0") - 0.01
        start = next(e for e in events if e.get("type") == "parallel_start")
        assert "browser" in start["in_order"][0]["reason"]


class TestAFailedBranch:
    async def test_the_running_ones_finish_and_the_waiting_ones_never_start(self, agents):
        from core.orchestration.engine import OrchestrationEngine
        agents.fail = {"a0"}
        orch = _fanout([_agent_step(f"a{i}") for i in range(6)])
        await S.seed_orchestrations([orch])
        events = await _run(orch, run_id="run_par_fail")

        assert _status(events) == "failed"
        assert sorted(agents.spans) == ["a0", "a1", "a2", "a3"], "a branch started after one had failed"
        state = (await _saved("run_par_fail")).shared_state
        assert {k for k in state if k.startswith("o_")} == {"o_a1", "o_a2", "o_a3"}

        agents.fail = set()
        agents.spans.clear()
        replay = [e async for e in OrchestrationEngine.resume_failed("run_par_fail", _server())]
        assert _status(replay) == "completed"
        assert sorted(agents.spans) == ["a0", "a4", "a5"], "the replay reran branches that had finished"


class TestABranchThatAsksAnyway:
    """A branch nobody expected to ask (a plain agent) stops for an approval."""

    async def test_the_others_finish_and_the_node_pauses_on_it(self, agents):
        agents.ask = {"a0"}
        orch = _fanout([_agent_step("a0"), _agent_step("a1"), _agent_step("a2")])
        await S.seed_orchestrations([orch])
        events = await _run(orch, run_id="run_par_ask")

        assert _status(events) == "paused"
        run = await _saved("run_par_ask")
        assert run.shared_state["o_a1"] == "a1 done" and run.shared_state["o_a2"] == "a2 done"
        paused = [h for h in run.step_history if h.get("status") == "paused"]
        assert [h["step_id"] for h in paused] == ["a0"]
        record = run.shared_state["_parallel_progress_fan"]
        assert record["paused"] == {"branch": 0, "step_id": "a0", "answered": False}
        assert record["completed_branches"] == [1, 2]

        agents.spans.clear()
        after = await _answer("run_par_ask", {"approve": True})
        assert _status(after) == "completed"
        assert sorted(agents.spans) == ["a0"], "a branch that had finished ran again"
        assert (await _saved("run_par_ask")).shared_state["o_a0"] == "a0 done"

    async def test_a_second_branch_that_asked_asks_again(self, agents):
        agents.ask = {"a0", "a1"}
        orch = _fanout([_agent_step("a0"), _agent_step("a1"), _agent_step("a2")])
        await S.seed_orchestrations([orch])

        assert _status(await _run(orch, run_id="run_par_ask2")) == "paused"
        first = [h["step_id"] for h in (await _saved("run_par_ask2")).step_history if h.get("status") == "paused"]
        assert first == ["a0"]

        assert _status(await _answer("run_par_ask2", {"approve": True})) == "paused"
        run = await _saved("run_par_ask2")
        assert run.shared_state["o_a0"] == "a0 done"
        assert [h["step_id"] for h in run.step_history if h.get("status") == "paused"] == ["a1"]

        assert _status(await _answer("run_par_ask2", {"approve": True})) == "completed"
        state = (await _saved("run_par_ask2")).shared_state
        assert [state[f"o_a{i}"] for i in range(3)] == ["a0 done", "a1 done", "a2 done"]


class TestTaskLists:
    async def test_two_agents_side_by_side_both_keep_their_plans(self, agents):
        agents.tasks = {
            "a0": [{"content": "fetch prices", "status": "completed"}],
            "a1": [{"content": "fetch reviews", "status": "in_progress"}],
        }
        await _run(_fanout([_agent_step("a0"), _agent_step("a1")]), run_id="run_par_tasks")

        tasks = (await _saved("run_par_tasks")).tasks
        assert sorted(t["content"] for t in tasks) == ["fetch prices", "fetch reviews"]


class TestWorkflowBranches:
    async def _children(self, *specs):
        """Each (orch_id, with_human) a child workflow and its workflow agent."""
        children, agents = [], []
        for orch_id, with_human in specs:
            steps = [_llm("work")]
            if with_human:
                steps[0]["next_step_id"] = "ok"
                steps.append({"id": "ok", "name": "OK", "type": "human", "human_prompt": "ok?",
                              "output_key": "h", "next_step_id": None})
            children.append(S.make_orchestration(id=orch_id, name=orch_id, entry_step_id="work", steps=steps))
            agents.append(S.make_orchestrator_agent(orch_id, id=f"agent_{orch_id}"))
        await S.seed_orchestrations(children)
        await S.seed_agents(agents)

    async def test_workflows_that_cannot_ask_run_together(self, fake_llm, monkeypatch):
        for k, v in (("MIN", "0.3"), ("MAX", "0.3"), ("PROB", "1")):
            monkeypatch.setenv(f"SYNAPSE_FAKE_LLM_DELAY_{k}", v)
        fake_llm.set_default("child output")
        await self._children(*[(f"child{i}", False) for i in range(3)])
        t0 = time.perf_counter()
        events = await _run(_fanout([_agent_step(f"w{i}", agent_id=f"agent_child{i}") for i in range(3)]))
        elapsed = time.perf_counter() - t0

        assert _status(events) == "completed"
        assert elapsed < 0.7, f"{elapsed:.2f}s — the workflows took turns"
        start = next(e for e in events if e.get("type") == "parallel_start")
        assert "in_order" not in start

    async def test_a_workflow_that_can_ask_keeps_its_branch_in_order(self, fake_llm):
        fake_llm.set_default("child output")
        await self._children(("plain", False), ("asks", True))
        events = await _run(_fanout([_agent_step("w0", agent_id="agent_plain"),
                                     _agent_step("w1", agent_id="agent_asks")]))

        start = next(e for e in events if e.get("type") == "parallel_start")
        assert start["in_order"] == [{"branch": 1, "reason": (
            "its workflow 'asks' can ask: a person answers a step in it")}]
