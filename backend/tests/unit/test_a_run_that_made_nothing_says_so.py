"""A run that made no answer says so, and a cancelled one is not "completed".

A Human → End workflow answered "Orchestration completed. 1 steps executed.":
`result_of` skips a Human step, so the run had made nothing, and the fallback
counted history entries. That sentence was an orchestrator agent's chat
answer, v1's run read and what a nested child handed its parent. A cancelled
run fell through to the same "Orchestration completed…" (SP7's note).
"""
from _fakes import seed as S
from test_parallel_agents_run_together import _answer, _run

NOTHING = "The workflow finished without producing an answer."


def _human_then_end():
    return S.make_orchestration(id="orch_nothing", entry_step_id="ask", steps=[
        {"id": "ask", "name": "Ask", "type": "human", "human_prompt": "Anything?", "output_key": "said",
         "next_step_id": "end"},
        {"id": "end", "name": "End", "type": "end", "next_step_id": None},
    ])


class TestARunThatMadeNothing:
    async def test_a_human_then_end_run_says_it_made_no_answer(self):
        orch = _human_then_end()
        await S.seed_orchestrations([orch])
        await _run(orch, run_id="run_nothing")
        events = await _answer("run_nothing", "fine")
        [final] = [e for e in events if e.get("type") == "final" and not e.get("orch_step_id")]
        assert final["response"] == NOTHING
        assert "steps executed" not in final["response"]

    def test_a_cancelled_run_is_not_called_completed(self):
        from core.models_orchestration import OrchestrationRun
        from core.orchestration.engine import OrchestrationEngine
        run = OrchestrationRun(run_id="run_x", orchestration_id="orch_x", status="cancelled",
                               step_history=[{"step_id": "a", "step_type": "llm", "status": "completed"}])
        assert OrchestrationEngine._build_final_response(run) == "Orchestration cancelled."

    def test_a_run_with_an_answer_still_gives_it(self):
        from core.models_orchestration import OrchestrationRun
        from core.orchestration.engine import OrchestrationEngine
        run = OrchestrationRun(run_id="run_y", orchestration_id="orch_y", status="completed",
                               shared_state={"o": "the answer"},
                               step_history=[{"step_id": "a", "step_type": "llm", "output_key": "o",
                                              "status": "completed"}])
        assert OrchestrationEngine._build_final_response(run) == "the answer"
