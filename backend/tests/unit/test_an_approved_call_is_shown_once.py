"""An approved call is one call when its turn resumes — in the prompt and in the record (F638; TEAM1c, D287).

Found by TEAM1c's live check (Claude Haiku 5.5): a lead wrote "The refund call appears twice in the log because
the task was interrupted and resumed" and asked a person to confirm that only one refund existed — it had run once.
The gate's snapshot (F312, F319) already ends with the turn's `[Turn N — Action]`, and the replay appended the same
action again; a team member's record also kept the gated call (no result) beside the run one. Every approval's resume
did the first, a plain agent's included. A one-at-a-time delegate's is in
`test_approving_a_delegates_call_runs_it.py`.
"""
from test_team_runtime import (ANA, BEN, CARA, LEAD, _lead, _of, _parked, _turn, assign, both,  # noqa: F401
                               llm, server, team3, tool_call)



def _actions(prompt: str, tool: str) -> int:
    """How many times `prompt` holds an action calling `tool`."""
    blocks = prompt.split("\n[Turn ")
    return sum(1 for b in blocks if "— Action]" in b.split("\n", 1)[0] and f'"tool": "{tool}"' in b)


def _prompts(llm, starts: str) -> list[str]:
    return [str(c.get("prompt_msg") or "") for c in llm.calls_for(starts)]


class TestOneAgent:
    async def test_its_resumed_prompt_holds_the_action_once(self, server, llm, team3):
        from core.chat_approval import parked_payload, resume_state_for

        llm.on("SOLO", [tool_call("danger", item="b"), "unused"])
        first = await _turn(server, agent_id=BEN, message="SOLO", session_id="s_solo", progress_events=True)
        card = _of(first, "human_input_required")[-1]
        snap = {k: v for k, v in _of(first, "_step_progress")[-1].items() if k != "type"}
        llm._scripts.clear()
        llm.on("SOLO", ["done solo"])
        events = await _turn(server, agent_id=BEN, message="SOLO", session_id="s_solo", progress_events=True,
                             resume_state=resume_state_for(parked_payload(card, message="SOLO", progress=snap),
                                                           approve=True))
        assert _actions(_prompts(llm, "SOLO")[-1], "danger") == 1
        assert server.session.calls == ["danger"]
        assert _of(events, "final")[-1]["response"] == "done solo"

    async def test_a_refused_one_too(self, server, llm, team3):
        from core.chat_approval import parked_payload, resume_state_for

        llm.on("SOLO", [tool_call("danger", item="b"), "unused"])
        first = await _turn(server, agent_id=BEN, message="SOLO", session_id="s_solo2", progress_events=True)
        card = _of(first, "human_input_required")[-1]
        snap = {k: v for k, v in _of(first, "_step_progress")[-1].items() if k != "type"}
        llm._scripts.clear()
        llm.on("SOLO", ["not done"])
        await _turn(server, agent_id=BEN, message="SOLO", session_id="s_solo2", progress_events=True,
                    resume_state=resume_state_for(parked_payload(card, message="SOLO", progress=snap),
                                                  approve=False, reason="no"))
        assert _actions(_prompts(llm, "SOLO")[-1], "danger") == 1
        assert server.session.calls == []


class TestATeamMember:
    async def test_its_record_and_its_prompt_hold_the_call_once(self, server, llm, team3):
        from core.chat_approval import resume_state_for

        llm.on("PLAN IT", [assign("Ben", "CHANGE B"), tool_call("wait", seconds=10), "unused"])
        llm.on("CHANGE B", [tool_call("danger", item="b"), "changed B"])
        first = await _turn(server, progress_events=True)
        pending = _parked(first)
        llm._scripts.clear()
        llm.on("PLAN IT", [tool_call("wait", seconds=10), "all changed"]).on("CHANGE B", ["changed B"])
        events = await _turn(server, progress_events=True, resume_state=resume_state_for(
            pending, approve=True, question=pending["questions"][0]["question_id"]))
        ben = next(f for f in _of(events, "member_finished") if f["member_name"] == "Ben")
        assert [(c["tool"], bool(c.get("result"))) for c in ben["record"]["calls"]] == [("danger", True)]
        assert _actions(_prompts(llm, "CHANGE B")[-1], "danger") == 1
        assert server.session.calls == ["danger"]
