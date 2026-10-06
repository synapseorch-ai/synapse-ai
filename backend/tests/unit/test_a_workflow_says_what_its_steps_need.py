"""F313: what a workflow agent's steps run on, read from its definition.

Chat judged a workflow agent's readiness and Attach by the workspace's default
model, which no step may use. `core.orchestration.step_models` reads each model
step the way the engine will run it — its own model, then its agent's, then the
workspace's — follows a step that runs another workflow, and says which steps an
attached image reaches. A broken definition is a sentence, never an exception.
"""
import pytest

from core.orchestration.step_models import attach_verdict, readiness_problem, walk_agent

WS = "ollama.mistral"


def _agent(aid, model=None, **kw):
    return {"id": aid, "name": aid, "type": "conversational", "model": model, **kw}


def _workflow_agent(aid, orch_id):
    return {"id": aid, "name": aid, "type": "orchestrator", "orchestration_id": orch_id}


def _orch(oid, steps):
    return {"id": oid, "name": oid, "entry_step_id": steps[0]["id"], "steps": steps}


def _step(sid, kind="agent", nxt=None, **kw):
    step = {"id": sid, "name": kw.pop("name", sid.title()), "type": kind, "next_step_id": nxt}
    if kind == "agent":
        step.setdefault("agent_id", kw.pop("agent_id", "a_plain"))
        step["prompt_template"] = "go"
    if kind in ("llm", "evaluator"):
        step["prompt_template"] = "go"
    if kind == "tool":
        step["forced_tool"] = "get_datetime"
    if kind == "human":
        step["human_prompt"] = "ok?"
    if kind == "transform":
        step["transform_code"] = "result = 1"
    step.update(kw)
    return step


async def _walk(top, agents=(), orchs=()):
    agents = {a["id"]: a for a in agents}
    orchs = {o["id"]: o for o in orchs}

    async def load_agent(aid):
        return agents.get(aid)

    async def load_orchestration(oid):
        return orchs.get(oid)

    return await walk_agent(top, load_agent=load_agent, load_orchestration=load_orchestration, workspace_model=WS)


class TestWhatEachStepRunsOn:
    async def test_each_model_step_runs_on_its_own_then_its_agents_then_the_workspaces(self):
        orch = _orch("o", [
            _step("own", model="gpt-5", agent_id="a_claude"),
            _step("agents", agent_id="a_claude"),
            _step("defaulted", model="Default", agent_id="a_plain"),
            _step("write", "llm"),
            _step("fetch", "tool", model="gemini-2.5-flash", agent_id="a_claude"),
            _step("judge", "evaluator", model="default"),
        ])
        walked = await _walk(_workflow_agent("w", "o"), [_agent("a_claude", "claude-sonnet-4"), _agent("a_plain")],
                             [orch])
        assert [(s.path, s.model) for s in walked.steps] == [
            ("Own", "gpt-5"), ("Agents", "claude-sonnet-4"), ("Defaulted", WS), ("Write", WS),
            ("Fetch", "gemini-2.5-flash"), ("Judge", WS)]
        assert walked.problems == []

    async def test_steps_that_run_no_model_are_not_listed(self):
        orch = _orch("o", [_step("ask", "human"), _step("calc", "transform"), _step("say", "print", message="hi"),
                           _step("pick", "extract_json"), _step("write", "llm", model="gpt-5")])
        walked = await _walk(_workflow_agent("w", "o"), [_agent("a_plain")], [orch])
        assert [s.path for s in walked.steps] == ["Write"]


class TestANestedWorkflow:
    async def test_a_nested_workflows_steps_are_named_by_their_path(self):
        inner = _orch("in", [_step("research", "llm", model="gpt-5")])
        outer = _orch("out", [_step("triage", agent_id="w_in")])
        walked = await _walk(_workflow_agent("w", "out"), [_workflow_agent("w_in", "in")], [outer, inner])
        assert [(s.path, s.model) for s in walked.steps] == [("Triage › Research", "gpt-5")]

    async def test_a_workflow_that_runs_itself_is_reported_once(self):
        outer = _orch("out", [_step("again", agent_id="w"), _step("write", "llm")])
        walked = await _walk(_workflow_agent("w", "out"), [_workflow_agent("w", "out")], [outer])
        assert walked.problems == ["Step 'Again' runs a workflow that is already running it, so it never ends."]
        assert [s.path for s in walked.steps] == ["Write"]

    async def test_a_nest_deeper_than_the_engine_allows_is_reported(self):
        levels = [_orch(f"o{i}", [_step(f"s{i}", agent_id=f"w{i + 1}"), _step(f"m{i}", "llm")]) for i in range(5)]
        agents = [_workflow_agent(f"w{i}", f"o{i}") for i in range(6)]
        walked = await _walk(agents[0], agents, levels)
        assert len(walked.problems) == 1 and "deeper than 3 levels" in walked.problems[0]
        assert [s.path.count("›") for s in walked.steps] == [3, 2, 1, 0]

    async def test_a_missing_agent_or_workflow_is_reported_not_raised(self):
        outer = _orch("out", [_step("gone", agent_id="a_gone"), _step("lost", agent_id="w_lost"),
                              _step("write", "llm")])
        walked = await _walk(_workflow_agent("w", "out"), [_workflow_agent("w_lost", "o_gone")], [outer])
        assert walked.problems == [
            "Step 'Gone' names an agent that no longer exists (a_gone).",
            "Step 'Lost' runs a workflow that no longer exists (o_gone).",
        ]
        assert [s.path for s in walked.steps] == ["Write"]
        missing = await _walk(_workflow_agent("w", "nowhere"))
        assert missing.problems == ["Its workflow no longer exists (nowhere)."]

    async def test_a_stored_workflow_that_does_not_load_is_reported(self):
        broken = _orch("out", [_step("write", "llm", pass_images=True)])
        walked = await _walk(_workflow_agent("w", "out"), [], [broken])
        assert len(walked.problems) == 1 and walked.problems[0].startswith("The workflow 'out' does not load")
        assert walked.steps == []


class TestWhereAttachmentsGo:
    async def test_attachments_reach_a_nested_step_only_through_steps_that_pass_them(self):
        inner = _orch("in", [_step("look", pass_images=True), _step("note", nxt=None)])
        outer = _orch("out", [_step("through", agent_id="w_in", pass_images=True, nxt="past"),
                              _step("past", agent_id="w_in"), _step("direct", pass_images=True)])
        walked = await _walk(_workflow_agent("w", "out"), [_workflow_agent("w_in", "in"), _agent("a_plain")],
                             [outer, inner])
        assert [s.path for s in walked.attaching()] == ["Through › Look", "Direct"]

    async def test_the_verdict_says_no_step_takes_them(self):
        walked = await _walk(_workflow_agent("w", "o"), [_agent("a_plain", "gpt-5")], [_orch("o", [_step("look")])])
        assert attach_verdict(walked, {"gpt-5": True}).startswith("No step of this workflow takes attachments")

    async def test_the_verdict_names_the_step_whose_model_cannot_see(self):
        walked = await _walk(_workflow_agent("w", "o"), [_agent("a_plain", "gpt-5")], [_orch("o", [
            _step("look", pass_images=True, nxt="read"), _step("read", pass_images=True, model="deepseek-chat")])])
        assert attach_verdict(walked, {"gpt-5": True, "deepseek-chat": False}) == (
            "Step 'Read' takes attachments, but deepseek-chat cannot read images.")

    async def test_the_verdict_is_none_when_every_taking_step_sees(self):
        walked = await _walk(_workflow_agent("w", "o"), [_agent("a_plain", "gpt-5")], [_orch("o", [
            _step("look", pass_images=True), _step("other", model="deepseek-chat")])])
        assert attach_verdict(walked, {"gpt-5": True, "deepseek-chat": False}) is None


class TestReadiness:
    async def test_unready_models_are_named_with_their_steps_and_structural_problems_first(self):
        orch = _orch("o", [_step("a", "llm", model="gpt-5"), _step("b", "llm", model="gpt-5"),
                           _step("c", "llm", model="gemini-2.5-flash"), _step("gone", agent_id="a_gone")])
        walked = await _walk(_workflow_agent("w", "o"), [], [orch])
        said = readiness_problem(walked, {"gpt-5": "No OpenAI key is saved.", "gemini-2.5-flash": None})
        assert said == ("Step 'Gone' names an agent that no longer exists (a_gone). "
                        "Steps 'A' and 'B' (gpt-5): No OpenAI key is saved.")
        assert readiness_problem(walked.__class__(steps=walked.steps), {"gpt-5": None}) is None


class TestTheWalkAndTheEngineAgree:
    async def test_the_walk_and_the_engine_agree_on_which_steps_got_images(self, fake_llm):
        """The walk's `takes_attachments` is what the engine does, through a nest and around it."""
        from _fakes import seed as S
        from core.scale.context import resolve_agent, resolve_orchestration
        from tests.unit.test_a_workflow_agent_sees_what_chat_sends import PNG, _chat

        def look(sid, prompt, takes, nxt=None, agent="agent_eyes"):
            return {"id": sid, "name": sid.title(), "type": "agent", "agent_id": agent, "prompt_template": prompt,
                    "output_key": f"{sid}_out", "next_step_id": nxt, **({"pass_images": True} if takes else {})}

        inner = S.make_orchestration(id="orch_agree_in", entry_step_id="i1", steps=[
            look("i1", "Inner one.", True, "i2"), look("i2", "Inner two.", False)])
        outer = S.make_orchestration(id="orch_agree_out", entry_step_id="o1", steps=[
            look("o1", "go", True, "o2", agent="agent_in"), look("o2", "Outer two.", True, "o3"),
            look("o3", "Outer three.", False)])
        await S.seed_orchestrations([inner, outer])
        top = S.make_orchestrator_agent("orch_agree_out", id="agent_agree")
        await S.seed_agents([S.make_agent(id="agent_eyes"), S.make_orchestrator_agent("orch_agree_in", id="agent_in"),
                             top])

        walked = await walk_agent(top, load_agent=resolve_agent, load_orchestration=resolve_orchestration,
                                  workspace_model=WS)
        await _chat(top, "what is this?", [PNG])
        by_prompt = {"Inner one.": "O1 › I1", "Inner two.": "O1 › I2", "Outer two.": "O2", "Outer three.": "O3"}
        got = {path for prompt, path in by_prompt.items()
               if any(prompt in str(c.get("prompt_msg") or "") and c.get("images") for c in fake_llm.calls)}
        assert {s.path for s in walked.attaching()} == got == {"O1 › I1", "O2"}
