"""A nested child is handed its task, not its parent's whole prompt (F252).

The child's `user_input` was the parent step's entire built prompt — "## YOUR
ROLE IN THIS WORKFLOW", the parent's MAIN GOAL, its context, then the task —
and at each level that prompt was nested again. It leaked into everything that
reads the input: a child's question read "L3 deep dive on ## YOUR ROLE IN THIS
WORKFLOW…", a condition checking the input for a tag matched the root brief on
every iteration, and the child's logs were titled with the meta-prompt.

Now the child's input is the parent step's task — its template, rendered, or
the parent's own input when it has none. Who called it is not lost: each child
model step is told in its system text which workflow and step called it and
what that workflow is for, and it is told again after the child pauses.
"""
from _fakes import seed as S
from tests.unit.test_a_failed_child_fails_its_parent import (  # noqa: F401 — fixtures
    _human, _print, _run, _server, driver, worker,
)


def _delegate(agent_id, template, nxt=None, sid="delegate"):
    step = {"id": sid, "name": "Deep dive (L3)", "type": "agent", "agent_id": agent_id,
            "output_key": "result", "next_step_id": nxt}
    if template is not None:
        step["prompt_template"] = template
    return step


async def _nest(parent_id, parent_steps, parent_entry, child_steps, child_entry):
    parent = S.make_orchestration(id=parent_id, name="L2 Market Analysis", entry_step_id=parent_entry,
                                  steps=parent_steps)
    child = S.make_orchestration(id=f"{parent_id}_child", name="L3 Deep Dive", entry_step_id=child_entry,
                                 steps=child_steps)
    await S.seed_agents([S.make_orchestrator_agent(child["id"], id=f"agent_{parent_id}")])
    await S.seed_orchestrations([parent, child])
    return parent


class TestTheChildsInputIsItsTask:
    async def test_the_rendered_template(self, driver):
        parent = await _nest("orch_f252t", [_delegate("agent_orch_f252t", "Deep dive on {state.company}")],
                             "delegate", [_print("echo", "{state.user_input}", key="seen")], "echo")

        assert await driver.start(parent, "run_f252t", initial_state={"company": "Linear"}) == "completed"

        assert (await _run("run_f252t")).shared_state["result"] == "Deep dive on Linear"

    async def test_no_template_passes_the_parents_input(self, driver):
        parent = await _nest("orch_f252n", [_delegate("agent_orch_f252n", None)],
                             "delegate", [_print("echo", "{state.user_input}", key="seen")], "echo")

        assert await driver.start(parent, "run_f252n") == "completed"

        assert (await _run("run_f252n")).shared_state["result"] == "go"

    async def test_a_childs_question_names_its_task(self, driver):
        parent = await _nest("orch_f252q", [_delegate("agent_orch_f252q", "{state.company}")], "delegate", [
            {"id": "ask", "name": "Confirm", "type": "human", "human_prompt": "Deep dive on {state.user_input}?",
             "output_key": "approval", "next_step_id": None},
        ], "ask")

        assert await driver.start(parent, "run_f252q", initial_state={"company": "Linear"}) == "paused"

        child = await _run((await _run("run_f252q")).nested_run_id)
        assert child.human_prompt == "Deep dive on Linear?"
        assert child.shared_state["user_input"] == "Linear"

    async def test_the_roots_tag_does_not_reach_every_iteration(self, driver):
        """L3 asked only when its own input carried `[confirm]`; the root brief
        carried it, so every iteration asked (3 pauses where 1 was meant)."""
        parent = await _nest("orch_f252l", [
            {"id": "lp", "name": "Each competitor", "type": "loop", "loop_over": "state.items",
             "loop_step_ids": ["delegate"], "output_key": "dives", "next_step_id": None},
            _delegate("agent_orch_f252l", "{state.loop_item}"),
        ], "lp", [
            {"id": "gate", "name": "Ask?", "type": "if_else", "if_condition": "'[confirm]' in state.user_input",
             "if_true_step_id": "ask", "if_false_step_id": "done"},
            _human("ask", nxt="done"),
            _print("done", "researched {state.user_input}", key="facts"),
        ], "gate")

        status = await driver.start(parent, "run_f252l", initial_state={
            "items": ["Linear [confirm]", "Jira", "Asana"], "user_input": "launch plan [confirm]"})

        # Only the first competitor's own input carries the tag.
        assert status == "paused"
        assert await driver.answer("run_f252l", {"approval": "ok"}) == "completed"


class TestTheChildKnowsWhoCalledIt:
    async def test_a_child_model_step_is_told_in_its_system_text(self, fake_llm):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        fake_llm.script(["researched"])
        parent = await _nest("orch_f252s", [_delegate("agent_orch_f252s", "Deep dive on {state.company}")],
                             "delegate", [
            {"id": "llm", "name": "Research", "type": "llm", "prompt_template": "research {state.user_input}",
             "model": "gpt-4o", "output_key": "facts", "next_step_id": None},
        ], "llm")
        engine = OrchestrationEngine(Orchestration.model_validate(parent), _server())

        [e async for e in engine.run("Plan the launch of our tool", run_id="run_f252s",
                                     initial_state={"company": "Linear"})]

        call = fake_llm.calls[-1]
        assert "## YOUR ROLE IN THIS WORKFLOW\nWorkflow: \"L2" not in call["prompt_msg"]
        assert "Deep dive (L3)" in call["sys_prompt"] and "L2 Market Analysis" in call["sys_prompt"]
        assert "Plan the launch of our tool" in call["sys_prompt"]

    async def test_and_again_after_the_child_pauses(self, fake_llm):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        fake_llm.script(["researched"])
        parent = await _nest("orch_f252r", [_delegate("agent_orch_f252r", "Deep dive on {state.company}")],
                             "delegate", [
            _human("ask", nxt="llm"),
            {"id": "llm", "name": "Research", "type": "llm", "prompt_template": "research {state.user_input}",
             "model": "gpt-4o", "output_key": "facts", "next_step_id": None},
        ], "ask")
        engine = OrchestrationEngine(Orchestration.model_validate(parent), _server())
        [e async for e in engine.run("Plan the launch of our tool", run_id="run_f252r",
                                     initial_state={"company": "Linear"})]

        [e async for e in OrchestrationEngine.resume("run_f252r", {"approval": "ok"}, _server())]

        call = fake_llm.calls[-1]
        assert "L2 Market Analysis" in call["sys_prompt"] and "Plan the launch of our tool" in call["sys_prompt"]
