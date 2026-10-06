"""F313: a workflow agent's run gets the images chat sent, to the steps that take them.

Chat handed a workflow agent's images to nothing: `run_orchestrator_agent` never
set `engine.images`, so an agent step marked "Sees attachments" (`pass_images`)
saw none. A workflow run by a step of another workflow got none either, and nor
did one resumed beneath its parent after a person's answer.

A step that takes attachments now gets what chat sent (or what came with the
answer); a step that does not gets none. A nested workflow gets them only
through a step that passes them, and its own steps must take them too.
"""
import base64
import types

from _fakes import seed as S
from tests.unit.test_a_failed_child_fails_its_parent import _human

PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64).decode()
PNG2 = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x01" * 64).decode()


def _server():
    return types.SimpleNamespace(agent_sessions={"x": object()}, memory_store=None, tool_router={})


def _look(sid="look", *, prompt="Describe the picture.", takes=True, agent_id="agent_eyes", nxt=None):
    step = {"id": sid, "name": sid.title(), "type": "agent", "agent_id": agent_id, "prompt_template": prompt,
            "output_key": f"{sid}_out", "next_step_id": nxt}
    if takes:
        step["pass_images"] = True
    return step


def _eyes():
    return S.make_agent(id="agent_eyes", name="Eyes")


async def _workflow_agent(orch_id, steps, entry, *, more_orchs=(), more_agents=()):
    """Seeds replace what was there, so everything goes in one call each."""
    orch = S.make_orchestration(id=orch_id, entry_step_id=entry, steps=steps)
    await S.seed_orchestrations([orch, *more_orchs])
    agent = S.make_orchestrator_agent(orch_id, id=f"agent_{orch_id}", name="Photo desk")
    await S.seed_agents([_eyes(), agent, *more_agents])
    return agent


async def _chat(agent, message, images):
    from core.models import ChatRequest
    from core.react_engine import run_react_loop
    return [e async for e in run_react_loop(
        ChatRequest(message=message, session_id="sess_f313", agent_id=agent["id"], images=images), _server())]


def _images_for(fake_llm, prompt: str) -> list:
    """The images each model call made for the step whose prompt is `prompt` carried."""
    return [c.get("images") for c in fake_llm.calls if prompt in str(c.get("prompt_msg") or "")]


class TestChatHandsThemToTheRun:
    async def test_a_step_that_passes_images_gets_the_ones_chat_sent(self, fake_llm):
        agent = await _workflow_agent("orch_f313a", [_look()], "look")
        await _chat(agent, "what is this?", [PNG])
        seen = _images_for(fake_llm, "Describe the picture.")
        assert seen and all(s == [PNG] for s in seen), seen

    async def test_a_step_that_does_not_gets_none(self, fake_llm):
        agent = await _workflow_agent("orch_f313b", [
            _look(nxt="write"), _look("write", prompt="Write the caption.", takes=False)], "look")
        await _chat(agent, "what is this?", [PNG])
        assert _images_for(fake_llm, "Describe the picture.")
        seen = _images_for(fake_llm, "Write the caption.")
        assert seen and not any(seen), seen

    async def test_the_images_are_not_in_the_runs_state(self, fake_llm):
        agent = await _workflow_agent("orch_f313c", [_look()], "look")
        await _chat(agent, "what is this?", [PNG])
        from core.orchestration.state import SharedState
        runs = [r for r in await SharedState.list_runs(limit=10) if r["orchestration_id"] == "orch_f313c"]
        run = (await SharedState.restore(runs[0]["run_id"])).run
        assert "base64" not in str(run.shared_state)


def _nest(sid, child_agent_id, *, takes, nxt=None):
    step = {"id": sid, "name": sid.title(), "type": "agent", "agent_id": child_agent_id, "prompt_template": "go",
            "output_key": f"{sid}_out", "next_step_id": nxt}
    if takes:
        step["pass_images"] = True
    return step


async def _nested(prefix, *, outer_takes, inner_steps, inner_entry):
    """A workflow agent whose one step runs another workflow."""
    inner = S.make_orchestration(id=f"{prefix}_in", name="Inner", entry_step_id=inner_entry, steps=inner_steps)
    return await _workflow_agent(
        f"{prefix}_out", [_nest("call", f"agent_{prefix}_in", takes=outer_takes)], "call",
        more_orchs=[inner], more_agents=[S.make_orchestrator_agent(inner["id"], id=f"agent_{prefix}_in")])


class TestANestedWorkflow:
    async def test_a_nested_workflows_step_gets_them_through_a_step_that_passes_them(self, fake_llm):
        agent = await _nested("orch_f313n", outer_takes=True, inner_steps=[_look()], inner_entry="look")
        await _chat(agent, "what is this?", [PNG])
        seen = _images_for(fake_llm, "Describe the picture.")
        assert seen and all(s == [PNG] for s in seen), seen

    async def test_a_nested_workflow_gets_none_through_a_step_that_does_not(self, fake_llm):
        agent = await _nested("orch_f313m", outer_takes=False, inner_steps=[_look()], inner_entry="look")
        await _chat(agent, "what is this?", [PNG])
        seen = _images_for(fake_llm, "Describe the picture.")
        assert seen and not any(seen), seen

    async def test_a_nested_step_after_a_question_gets_what_was_sent_with_the_answer(self, fake_llm):
        """Parked runs keep no images (by design): the answer's own reach the child's step."""
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine

        inner = S.make_orchestration(id="orch_f313q_in", name="Inner", entry_step_id="ask",
                                     steps=[_human("ask", nxt="look"), _look()])
        outer = S.make_orchestration(id="orch_f313q_out", name="Outer", entry_step_id="call",
                                     steps=[_nest("call", "agent_orch_f313q_in", takes=True)])
        await S.seed_orchestrations([inner, outer])
        await S.seed_agents([_eyes(), S.make_orchestrator_agent(inner["id"], id="agent_orch_f313q_in")])

        engine = OrchestrationEngine(Orchestration.model_validate(outer), _server())
        events = [e async for e in engine.run("go", run_id="run_f313q")]
        assert any(e.get("type") == "human_input_required" for e in events)

        [e async for e in OrchestrationEngine.resume(
            "run_f313q", {"approval": "yes"}, _server(), images=[PNG2])]
        seen = _images_for(fake_llm, "Describe the picture.")
        assert seen and all(s == [PNG2] for s in seen), seen
