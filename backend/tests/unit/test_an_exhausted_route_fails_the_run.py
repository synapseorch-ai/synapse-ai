"""A step routed to past its Max iterations fails the run, or goes where it says (F248).

The loop guard used to send a route to an exhausted step on to that step's own
`next_step_id`, unguarded. In the stress pass, coder → run tests → "Tests green?"
({pass: end, fix: coder}), with tests that never passed, ran `c t j c t j c t j t`:
the fourth "fix" skipped the coder and re-ran the tests, and the gate, out of
iterations with nowhere left to go, ended the run `completed` with `error: null`.
A schedule, a v1 caller, the bell and Runs & Logs all saw success.

Now the run fails at the step that routes: "'Coder' gave up after 3 attempts".
A step may name `on_exhausted_step_id`, a step to go to instead (an escalation,
a best-effort finish). The native AI builder's own cycles are set so that a
person's clarifications and revisions are not cut off early.
"""
import copy
import json
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _print(sid, nxt=None, **extra):
    return {"id": sid, "name": extra.pop("name", sid.capitalize()), "type": "print",
            "print_content": sid, "output_key": f"o_{sid}", "next_step_id": nxt, **extra}


def _gate(**extra):
    """An evaluator that never passes: the fake model's reply is not a route, so
    its declared fallback, "fix", is taken every time."""
    return {"id": "gate", "name": "Tests green?", "type": "evaluator",
            "evaluator_prompt": "Did the tests pass?", "route_map": {"pass": None, "fix": "coder"},
            "fallback_route": "fix", "output_key": "verdict", "next_step_id": None, **extra}


def _never_green(orch_id, **coder):
    return S.make_orchestration(id=orch_id, entry_step_id="coder", steps=[
        _print("coder", "tests", name="Coder", **coder), _print("tests", "gate", name="Run tests"), _gate(),
    ])


# ── drivers: in-process and the fleet's jobs ─────────────────────────────────
@pytest.fixture
def worker(monkeypatch):
    from core.scale import mcp_pool
    from core.scale import worker as worker_mod

    async def acquire():
        return _server()

    async def release():
        return None

    async def no_settings():
        return None

    monkeypatch.setattr(mcp_pool, "acquire", acquire)
    monkeypatch.setattr(mcp_pool, "release", release)
    monkeypatch.setattr(worker_mod, "_load_job_settings", no_settings)
    return worker_mod


def _status_of(events):
    if events and events[-1].get("type") == "human_input_required":
        return "paused"
    for e in reversed(events):
        if e.get("type") == "orchestration_complete":
            return e.get("status", "completed")
    return "unknown"


class _Driver:
    def __init__(self, kind, worker_mod, fake_redis):
        self.kind, self._worker, self._redis = kind, worker_mod, fake_redis
        self.events: list[dict] = []

    async def _ctx(self):
        from core.store import get_store
        return {"redis": self._redis, "session_factory": await get_store(), "job_try": 1, "job_id": "job-1"}

    async def start(self, orch, run_id, initial_state=None):
        await S.seed_orchestrations([orch])
        if self.kind == "engine":
            from core.models_orchestration import Orchestration
            from core.orchestration.engine import OrchestrationEngine
            engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
            self.events = [e async for e in engine.run("go", run_id=run_id, initial_state=initial_state)]
            return _status_of(self.events)
        result = await self._worker.run_orchestration_job(
            await self._ctx(), run_id=run_id, orch_id=orch["id"], initial_input="go", initial_state=initial_state)
        return result["status"]

    async def answer(self, run_id, response):
        if self.kind == "engine":
            from core.orchestration.engine import OrchestrationEngine
            self.events = [e async for e in OrchestrationEngine.resume(run_id, response, _server())]
            return _status_of(self.events)
        result = await self._worker.resume_orchestration_job(await self._ctx(), run_id=run_id, human_response=response)
        return result["status"]


@pytest.fixture(params=["engine", "fleet"])
def driver(request, worker, fake_redis):
    return _Driver(request.param, worker, fake_redis)


async def _run(run_id):
    from core.orchestration.state import SharedState
    return (await SharedState.restore(run_id)).run


def _sequence(run):
    return [h["step_id"] for h in run.step_history]


class TestAGateThatNeverPasses:
    async def test_fails_the_run_naming_the_step_that_gave_up(self, driver):
        assert await driver.start(_never_green("orch_f248"), "run_f248") == "failed"

        run = await _run("run_f248")
        # Three honest attempts, and the run stops at the gate that would have
        # sent it round a fourth time — not `… gate tests` and completed.
        assert _sequence(run) == ["coder", "tests", "gate"] * 3
        last = run.step_history[-1]
        assert last["step_id"] == "gate" and last["status"] == "failed"
        assert "'Coder' gave up after 3 attempts" in last["error"]
        if driver.kind == "engine":
            kinds = [e["type"] for e in driver.events]
            assert "loop_limit_reached" in kinds
            error = next(e for e in driver.events if e["type"] == "step_error")
            assert error["orch_step_id"] == "gate" and "gave up" in error["reason"]
            # The limit is said before the failure it causes.
            assert kinds.index("loop_limit_reached") < kinds.index("step_error")

    async def test_the_step_it_names_for_the_limit_is_taken(self, driver):
        orch = _never_green("orch_f248e", on_exhausted_step_id="escalate")
        orch["steps"].append(_print("escalate", name="Escalate to a person"))

        assert await driver.start(orch, "run_f248e") == "completed"

        run = await _run("run_f248e")
        assert _sequence(run) == ["coder", "tests", "gate"] * 3 + ["escalate"]
        assert run.shared_state["o_escalate"] == "escalate"
        if driver.kind == "engine":
            limit = next(e for e in driver.events if e["type"] == "loop_limit_reached")
            assert limit["on_exhausted_step_id"] == "escalate"

    async def test_a_fallback_out_of_iterations_too_gives_up(self, driver):
        orch = _never_green("orch_f248ee", on_exhausted_step_id="escalate")
        orch["steps"].append(_print("escalate", name="Escalate", max_iterations=0))

        assert await driver.start(orch, "run_f248ee") == "failed"
        error = (await _run("run_f248ee")).step_history[-1]["error"]
        assert "'Coder'" in error and "'Escalate'" in error


class TestAtTheAnswerThatRoutesThere:
    """The cap reached on the route out of a person's answer: the resume path."""

    def _orch(self, orch_id, **work):
        return S.make_orchestration(id=orch_id, entry_step_id="ask", steps=[
            {"id": "ask", "name": "Ask", "type": "human", "human_prompt": "ok?",
             "output_key": "answer", "next_step_id": "work"},
            _print("work", name="Work", max_iterations=0, **work),
            _print("instead", name="Instead"),
        ])

    async def test_fails_the_run(self, driver):
        assert await driver.start(self._orch("orch_f248r"), "run_f248r") == "paused"

        assert await driver.answer("run_f248r", "yes") == "failed"

        run = await _run("run_f248r")
        assert run.status == "failed" and not run.waiting_for_human
        assert run.step_history[-1]["step_id"] == "ask"
        assert "'Work' gave up" in run.step_history[-1]["error"]
        assert "o_work" not in run.shared_state

    async def test_or_takes_the_named_step(self, driver):
        orch = self._orch("orch_f248ro", on_exhausted_step_id="instead")
        assert await driver.start(orch, "run_f248ro") == "paused"

        assert await driver.answer("run_f248ro", "yes") == "completed"
        assert (await _run("run_f248ro")).shared_state["o_instead"] == "instead"


# ── the native AI builder's own cycles ───────────────────────────────────────
def _builder_graph() -> dict:
    """The bundled builder orchestration with its routing intact — every id,
    route, cap and fallback — and each agent, LLM and tool step replaced by a
    print step, so only routing decides where the run goes."""
    from core.native_builder.seed import _load_bundled_orchestration

    orch = copy.deepcopy(_load_bundled_orchestration())
    orch["id"] = "orch_builder_graph"
    steps = []
    for s in orch["steps"]:
        if s["type"] in ("agent", "llm", "tool"):
            s = {"id": s["id"], "name": s.get("name", s["id"]), "type": "print",
                 "print_content": s["id"], "output_key": s.get("output_key") or f"o_{s['id']}",
                 "next_step_id": s.get("next_step_id"), "max_iterations": s.get("max_iterations", 3),
                 **({"on_exhausted_step_id": s["on_exhausted_step_id"]} if s.get("on_exhausted_step_id") else {})}
        steps.append(s)
    orch["steps"] = steps
    return orch


def _route(label):
    return tool_call(f"route_{label}", reasoning="scripted")


class TestTheBuildersOwnCycles:
    async def _start(self, driver, fake_llm, script, run_id):
        fake_llm.script([_route(x) for x in script])
        return await driver.start(_builder_graph(), run_id,
                                  initial_state={"plan_room": {"orchestrations_left": 1}})

    async def test_five_clarifications_then_it_plans_with_what_it_has(self, driver, fake_llm):
        assert await self._start(driver, fake_llm, ["create"] + ["clarify"] * 5, "run_bclar") == "paused"
        for n in range(1, 5):
            assert await driver.answer("run_bclar", {"answer": f"detail {n}"}) == "paused"
            assert (await _run("run_bclar")).current_step_id == "step_human_clarify"

        # The fifth answer: the builder reads it, stops asking and drafts the plan.
        assert await driver.answer("run_bclar", {"answer": "detail 5"}) == "paused"
        run = await _run("run_bclar")
        assert run.current_step_id == "step_human_review"
        ran = [h["step_id"] for h in run.step_history]
        assert ran.count("step_understand") == 6        # every answer was read
        assert ran.count("step_clarify_route") == 5
        assert "step_draft_plan" in ran

    async def test_four_revisions_then_approval_saves(self, driver, fake_llm):
        assert await self._start(driver, fake_llm, ["create", "draft"], "run_brev") == "paused"
        for _ in range(4):
            fake_llm.script([_route("revise")])
            assert await driver.answer("run_brev", {"feedback": "change it"}) == "paused"
            assert (await _run("run_brev")).current_step_id == "step_human_review"

        fake_llm.script([_route("approved"), _route("no"), _route("create")])
        assert await driver.answer("run_brev", {"feedback": "approve"}) == "completed"
        run = await _run("run_brev")
        assert "step_save_create" in [h["step_id"] for h in run.step_history]

    async def test_a_fifth_revision_gives_up_and_says_so(self, driver, fake_llm):
        assert await self._start(driver, fake_llm, ["create", "draft"], "run_brev5") == "paused"
        for _ in range(4):
            fake_llm.script([_route("revise")])
            assert await driver.answer("run_brev5", {"feedback": "change it"}) == "paused"

        fake_llm.script([_route("revise")])
        assert await driver.answer("run_brev5", {"feedback": "change it again"}) == "failed"
        assert "gave up after 5 attempts" in (await _run("run_brev5")).step_history[-1]["error"]


class TestTheBundledFlows:
    """The builder's JSON and the example packs, with the new field in place.
    (Each carries validator issues of its own that predate this — dead-end
    notify steps, unwritten input keys — which are not this row's.)"""

    def _orchestrations(self):
        import glob
        import os

        from core.native_builder.seed import _load_bundled_orchestration
        found = [("native builder", _load_bundled_orchestration())]
        here = os.path.dirname(__file__)
        for path in sorted(glob.glob(os.path.join(here, "..", "..", "examples", "*.bundle.json"))):
            with open(path, encoding="utf-8") as f:
                for orch in json.load(f).get("orchestrations") or []:
                    found.append((os.path.basename(path), orch))
        return found

    def test_every_limit_route_names_a_real_step_and_validates(self):
        from core.builder_tools import _validate_orchestration

        routed = 0
        for where, orch in self._orchestrations():
            ids = {s["id"] for s in orch["steps"]}
            for s in orch["steps"]:
                if s.get("on_exhausted_step_id"):
                    routed += 1
                    assert s["on_exhausted_step_id"] in ids, (where, s["id"])
            issues = _validate_orchestration(orch)["issues"]
            assert not [i for i in issues if "reaches its limit" in i], (where, issues)
        assert routed, "the builder's clarify cycle names where it goes at its limit"

    def test_every_cycle_through_a_person_allows_five(self):
        """A person asking for a fourth revision is not a runaway loop."""
        for where, orch in self._orchestrations():
            steps = {s["id"]: s for s in orch["steps"]}
            for cycle in _cycles(orch):
                if any(steps[x]["type"] == "human" for x in cycle):
                    short = [steps[x].get("name") for x in cycle if steps[x].get("max_iterations", 3) < 5]
                    assert not short, (where, orch.get("name"), short)


def _cycles(orch):
    steps = {s["id"]: s for s in orch["steps"]}

    def succ(s):
        out = [s.get(k) for k in ("next_step_id", "if_true_step_id", "if_false_step_id",
                                  "switch_default_step_id", "on_exhausted_step_id")]
        out += list((s.get("route_map") or {}).values()) + list((s.get("switch_cases") or {}).values())
        return [x for x in out if x in steps]

    found = set()

    def dfs(node, path):
        if node in path:
            cyc = path[path.index(node):]
            i = cyc.index(min(cyc))
            found.add(tuple(cyc[i:] + cyc[:i]))
            return
        for nxt in succ(steps[node]):
            dfs(nxt, path + [node])

    dfs(orch["entry_step_id"], [])
    return found
