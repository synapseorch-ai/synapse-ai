"""Memory can answer "what did you do" — and says so when it cannot (F272).

"Memory Keeper" (memory on) was asked in fresh sessions what it had done: the
md5 it took came back invented (`7d686f03…`, truth `b665e6c2…`), a fact at the
end of a long answer was "not captured", and a workflow's `grep -c` was
replaced by an invented `ls | wc -l`, "confirmed" by further searches — 24–25
turns each. The data existed and was thrown away: a chat trace kept
`user[:200] → answer[:200] (tools: names)`, and a run summary cut every state
value at 300 characters, the tool trace inside the first call.

Now a chat trace keeps each call's arguments and the start of its result and
the answer's head and tail; a run summary keeps the tool calls verbatim, outside
its compression; `read_session` opens a past session whole; and the index says
to say so when memory does not have it. Recalled tool output is content nobody
typed, so reading it back holds a later `remember` for a person (F279).
"""
import json
import types

import pytest

from _fakes import seed as S

from core import agent_memory as am


def _call(tool: str, **args) -> str:
    return json.dumps({"tool": tool, "arguments": args})


@pytest.fixture(autouse=True)
def _fresh_index():
    am.set_memory_index(None)
    yield
    am.set_memory_index(None)


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _chat(fake_llm, agent, script, message, session_id, executor=None):
    from core.react_engine import run_agent_step

    async def _default(name, args):
        return "b665e6c2d1f0a9e8  a.txt"

    fake_llm.script(script)
    return [ev async for ev in run_agent_step(
        message=message, agent_id=agent["id"], session_id=session_id, server_module=_server(),
        agent_override=agent, max_turns=6, source="chat", tool_executor=executor or _default)]


def _trace(agent, session_id):
    return am._store().get(am._session_key(agent["id"], am._session_stem(session_id))) or ""


class TestAChatTrace:
    async def test_it_keeps_the_calls_and_the_end_of_the_answer(self, fake_llm):
        agent = S.make_agent(tools=["all"], memory=True)
        answer = "Done. " + "Details. " * 150 + "Fewest units: sku C3 with 17,840."
        await _chat(fake_llm, agent, [_call("run_bash", command="md5sum a.txt"), answer],
                    "create the files and take an md5", "s_a1")

        trace = _trace(agent, "s_a1")
        assert "md5sum a.txt" in trace, "the call's arguments"
        assert "b665e6c2" in trace, "what it returned"
        assert "17,840" in trace, "the end of a long answer"
        assert "create the files and take an md5" in trace

    async def test_a_session_file_keeps_its_newest_part(self, fake_llm):
        for i in range(120):
            am.append_session_note("agent_big", "s_big", f"- turn {i} " + "w" * 600)
        content = am._store().get(am._session_key("agent_big", am._session_stem("s_big")))
        assert content.startswith("---\nsession: s_big"), "the header stays"
        assert "turn 119 " in content and "turn 0 " not in content
        assert len(content) <= am.MAX_SESSION_FILE_CHARS + 1_000


class TestARunSummary:
    async def test_it_keeps_the_tool_trace(self, fake_llm, seed_agent):
        from core.models_orchestration import Orchestration, OrchestrationRun
        from core.orchestration.engine import OrchestrationEngine

        agent = await seed_agent(tools=[], memory=True)
        orch = Orchestration.model_validate(S.make_orchestration(
            id="orch_f272", entry_step_id="s1",
            steps=[{"id": "s1", "name": "Count", "type": "agent", "agent_id": agent["id"],
                    "next_step_id": None}]))
        trace = [{"execution": 1, "agent": agent["name"], "origin": None,
                  "inputs": {"user_input": "count the Widget rows " + "i" * 400},
                  "trace": {"tool_calls": [{"name": "run_bash",
                                            "args": {"command": "grep -c 'Widget' data/sales.csv"},
                                            "result_preview": "757"}],
                            "tools_used": ["run_bash"], "turn_count": 2, "final_output": "757 rows"}}]
        run = OrchestrationRun(run_id="run_f272", orchestration_id="orch_f272", session_id="s_run",
                               status="completed",
                               shared_state={"user_input": "count", "_exec_memory_s1": trace,
                                             **{f"k{i}": "v" * 900 for i in range(10)}},
                               step_history=[{"step_id": "s1", "step_name": "Count", "step_type": "agent",
                                              "status": "completed"}])
        fake_llm.set_default("A short summary that dropped everything.")

        await OrchestrationEngine(orch, _server())._write_memory_summaries(run)

        summary = _trace(agent, "run_f272")
        assert "grep -c 'Widget' data/sales.csv" in summary
        assert "757" in summary


class TestReadSession:
    async def test_it_opens_a_past_session_and_a_remember_after_it_asks(self, fake_llm):
        agent = S.make_agent(tools=[], memory=True)
        am.append_session_note(agent["id"], "s_old", "- user: take the md5 → b665e6c2 (md5sum a.txt)")
        events = await _chat(fake_llm, agent, [
            _call("read_session", name="s_old"),
            _call("remember", name="the-md5", content="The md5 of a.txt is b665e6c2."),
            "done"], "what md5 did you take?", "s_new")

        opened = fake_llm.calls[1]["prompt_msg"]
        assert "b665e6c2" in opened and "md5sum a.txt" in opened
        pause = events[-1]
        assert pause["type"] == "human_input_required" and pause["tool_name"] == "remember", (
            "recalled output is content nobody typed (F279)")

    async def test_a_session_hit_from_search_taints_too(self, fake_llm):
        agent = S.make_agent(tools=[], memory=True)
        am.append_session_note(agent["id"], "s_old2", "- user: count widgets → 757 (grep -c Widget)")
        events = await _chat(fake_llm, agent, [
            _call("search_memory", query="widgets count"),
            _call("remember", name="widgets", content="757 widgets."),
            "done"], "how many widgets?", "s_new2")
        assert events[-1]["type"] == "human_input_required"

    async def test_an_item_hit_does_not(self, fake_llm):
        agent = S.make_agent(tools=[], memory=True)
        await am.write_memory(agent["id"], "gold", "Track XAU in USD.")
        events = await _chat(fake_llm, agent, [
            _call("search_memory", query="XAU gold"),
            _call("remember", name="silver", content="Track XAG too."),
            "done"], "and silver?", "s_new3")
        assert not any(e.get("type") == "human_input_required" for e in events)
        assert am.read_memory(agent["id"], "silver") is not None

    async def test_a_visitor_never_gets_it(self):
        from core import visitor
        assert "read_session" in am.MEMORY_TOOL_NAMES
        assert visitor.clean_tools(["read_session", "peek"]) == ["peek"]


class TestSayingSo:
    def test_the_index_says_to_say_so(self):
        block = am.render_index_block("- [gold] Track XAU")
        assert "does not have it" in block and "say" in block
