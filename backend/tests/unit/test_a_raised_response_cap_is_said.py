"""When thinking raises an agent's Max response length, the turn says so (F432).

Measured on the fleet (C2, acme's own Gemini key): `gemini-3-flash-preview`
with `max_output_tokens: 256`, "Write an 800-word essay about tides" — the answer
was complete at 956 words, 1,890 output tokens. `reasoning.plan_for` raises the
cap past the model's thinking by design (a budget that does not fit under the
cap is refused outright, and a larger cap only costs what is written), so on a
thinking model the person's cap never binds — and nothing said so.

Naveen's call (11, C5's decisions, option (a)): the field says so, and the run
log notes it when it happens. One line per turn, at its start; nothing when no
cap was set, the model does not think, or the cap already clears its thinking.
A widget's or a channel's visitor is not told.
"""
import types

from _fakes import seed as S


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _notes(fake_llm, monkeypatch, *, agent_cap=None, workspace_cap=None,
                 model="gemini-3-flash-preview", effort="medium"):
    import core.react_engine as re_mod
    from core.react_engine import run_agent_step

    real = re_mod.load_settings
    settings = {"model": model}
    if workspace_cap:
        settings["max_output_tokens"] = workspace_cap
    monkeypatch.setattr(re_mod, "load_settings", lambda: {**real(), **settings})

    agent = S.make_agent(tools=[], skip_default_tools=True, model=model, reasoning_effort=effort)
    if agent_cap:
        agent["max_output_tokens"] = agent_cap
    fake_llm.script(["Tides rise and fall."])
    events = [ev async for ev in run_agent_step(
        message="Write an 800-word essay about tides.", agent_id=agent["id"], session_id="s_f432",
        server_module=_server(), agent_override=agent, tools_override=[], max_turns=2)]
    return [e["message"] for e in events if e.get("type") == "status" and "Max response length" in e.get("message", "")]


class TestTheNote:
    async def test_an_agents_cap_raised_for_thinking_is_said_once(self, fake_llm, monkeypatch):
        from core.reasoning import plan_for
        raised = plan_for("gemini-3-flash-preview", "medium", 256, None).max_output_tokens
        notes = await _notes(fake_llm, monkeypatch, agent_cap=256)
        assert notes == [
            f"This agent's Max response length (256 tokens) was raised to {raised:,} so "
            "gemini-3-flash-preview has room to think; its answer may run past 256."]

    async def test_gemini_3_at_off_still_thinks_so_it_still_says_so(self, fake_llm, monkeypatch):
        """Gemini 3 cannot stop thinking; at Off its cap is still raised (to 8,192)."""
        notes = await _notes(fake_llm, monkeypatch, agent_cap=256, effort="off")
        assert len(notes) == 1 and "raised to 8,192" in notes[0]

    async def test_the_workspaces_cap_names_the_workspace(self, fake_llm, monkeypatch):
        notes = await _notes(fake_llm, monkeypatch, workspace_cap=512)
        assert len(notes) == 1 and notes[0].startswith("The workspace's Max response length (512 tokens)")


class TestNoNote:
    async def test_with_no_cap_set(self, fake_llm, monkeypatch):
        assert await _notes(fake_llm, monkeypatch) == []

    async def test_when_the_model_does_not_think(self, fake_llm, monkeypatch):
        assert await _notes(fake_llm, monkeypatch, agent_cap=256, model="mistral", effort=None) == []

    async def test_when_thinking_is_off(self, fake_llm, monkeypatch):
        assert await _notes(fake_llm, monkeypatch, agent_cap=256, model="claude-sonnet-4-5", effort="off") == []

    async def test_when_the_cap_already_clears_the_thinking(self, fake_llm, monkeypatch):
        assert await _notes(fake_llm, monkeypatch, agent_cap=60_000, effort="low") == []

    async def test_to_a_visitor(self, fake_llm, monkeypatch):
        from core import visitor as visitor_mod
        with visitor_mod.scope(visitor_mod.VisitorScope(surface="widget")):
            assert await _notes(fake_llm, monkeypatch, agent_cap=256) == []
