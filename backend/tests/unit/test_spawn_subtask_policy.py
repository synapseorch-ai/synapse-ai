"""
A sub-agent can do no more than its parent may (F266).

`spawn_subtask` ran each sub-agent as an ephemeral agent with no tool policy of
its own, so it got the workspace's defaults and not the parent's. An agent
refused `run_bash` by its own policy could ask a sub-agent to run it, and the
file appeared; under `confirm` it ran with no card. And the batch of spawns ran
before the parent's tool guard and policy, so an agent that may not spawn at all
— or a step that took `spawn_subtask` away — spawned anyway. Measured on a
scratch install with a scripted model: all three touched their flag file.

Now a sub-agent is given only the tools the parent could run without asking; a
tool the parent would be refused or asked about is withheld, and the parent is
told to call it itself (where a person can be asked).
"""
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from core.tools import VirtualTool


def _ann(**kw):
    from mcp.types import ToolAnnotations
    return ToolAnnotations(**kw)


DANGER = VirtualTool("danger", "changes things", {}, _ann(readOnlyHint=False, destructiveHint=True))
PEEK = VirtualTool("peek", "reads things", {}, _ann(readOnlyHint=True, idempotentHint=True))


class _Session:
    """An MCP session that records what it was asked to run."""

    def __init__(self):
        self.calls: list[str] = []

    async def send_ping(self):
        return None

    async def call_tool(self, name, args, **_kw):
        self.calls.append(name)
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=f"ran-{name}")], isError=False)


@pytest.fixture
def server(monkeypatch):
    import core.react_engine as re_mod

    async def aggregated(server_module, agent, custom_tools):
        wanted = agent.get("tools") or []
        held = [t for t in (DANGER, PEEK) if "all" in wanted or t.name in wanted]
        ollama = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                    "parameters": t.inputSchema}} for t in held]
        return held, {t.name: t.inputSchema for t in held}, ollama, ""

    monkeypatch.setattr(re_mod, "aggregate_all_tools", aggregated)
    session = _Session()
    return types.SimpleNamespace(agent_sessions={"fake": session}, memory_store=None,
                                 tool_router={"danger": ("fake", "danger"), "peek": ("fake", "peek")},
                                 session=session)


async def _run(fake_llm, server, script, policy, **kw):
    from core.react_engine import run_agent_step

    agent = S.make_agent(tools=["spawn_subtask", "danger", "peek"], tool_policy=policy)
    fake_llm.script(script)
    return [ev async for ev in run_agent_step(
        message="go", agent_id=agent["id"], session_id="s_spawn", server_module=server,
        agent_override=agent, max_turns=4, **kw,
    )]


# parent: spawn → sub-agent: danger, "sub done" → parent: "done"
SPAWN_DANGER = [tool_call("spawn_subtask", task="touch the flag", tools=["danger"]),
                tool_call("danger"), "sub done", "done"]


def _spawn_result(events) -> str:
    return " ".join(e.get("preview", "") for e in events
                    if e.get("type") == "tool_result" and e.get("tool_name") == "spawn_subtask")


class TestTheSubAgentGetsOnlyWhatTheParentMayRun:
    async def test_a_tool_the_parent_is_refused_is_withheld(self, fake_llm, server):
        events = await _run(fake_llm, server, SPAWN_DANGER, {"destructive": "deny"})
        assert server.session.calls == []
        assert "danger" in _spawn_result(events) and "Not given to the sub-agent" in _spawn_result(events)

    async def test_a_tool_the_parent_would_be_asked_about_is_withheld_not_run(self, fake_llm, server):
        events = await _run(fake_llm, server, SPAWN_DANGER, {"destructive": "confirm"})
        assert server.session.calls == []
        assert not any(e["type"] == "human_input_required" for e in events)
        assert "call it yourself" in _spawn_result(events)

    async def test_a_tool_the_parent_may_run_is_given(self, fake_llm, server):
        """The guard that this is about policy, not about subtasks."""
        await _run(fake_llm, server, SPAWN_DANGER, {})
        assert server.session.calls == ["danger"]

    async def test_asking_for_only_withheld_tools_does_not_fall_back_to_others(self, fake_llm, server):
        """The fallback for "no valid tools" handed over the parent's first five."""
        await _run(fake_llm, server, [tool_call("spawn_subtask", task="t", tools=["nonexistent"]),
                                      tool_call("danger"), "sub done", "done"], {"destructive": "deny"})
        assert server.session.calls == []


class TestTheSpawnItselfIsGated:
    async def test_a_parent_that_may_not_spawn_does_not(self, fake_llm, server):
        """`spawn_subtask` declares nothing, so `undeclared` decides it — and the
        batch used to run before that decision was ever made."""
        events = await _run(fake_llm, server, [SPAWN_DANGER[0], "done"], {"undeclared": "deny"})
        assert server.session.calls == []
        assert fake_llm.call_count == 2, "no sub-agent ran a model call"
        assert any("Blocked by policy" in (e.get("preview") or "") for e in events)

    async def test_a_parent_that_must_ask_shows_the_card_first(self, fake_llm, server):
        events = await _run(fake_llm, server, [SPAWN_DANGER[0], "done"], {"undeclared": "confirm"})
        assert events[-1]["type"] == "human_input_required"
        assert events[-1]["tool_name"] == "spawn_subtask"
        assert server.session.calls == [] and fake_llm.call_count == 1

    async def test_a_step_that_took_spawn_away_is_obeyed(self, fake_llm, server):
        await _run(fake_llm, server, [SPAWN_DANGER[0], "done"], {},
                   allowed_tools_override=["danger", "peek"])
        assert server.session.calls == []
        assert fake_llm.call_count == 2

    async def test_a_steps_narrowing_reaches_the_sub_agent(self, fake_llm, server):
        await _run(fake_llm, server, SPAWN_DANGER, {},
                   allowed_tools_override=["spawn_subtask", "peek"])
        assert server.session.calls == []


class TestTheSubAgentKeepsTheParentsScoping:
    async def test_databases_repos_and_the_default_tools_setting_carry_over(self, monkeypatch):
        """A parent limited to the `sales` database holds `run_sql_query`; an empty
        `db_configs` is no restriction, so a sub-agent without it could query `hr`."""
        import core.react_engine as re_mod

        seen = []

        async def fake_step(**kwargs):
            seen.append(kwargs["agent_override"])
            yield {"type": "final", "response": "ok"}

        monkeypatch.setattr(re_mod, "run_agent_step", fake_step)
        parent = {"id": "p", "db_configs": ["sales"], "repos": ["api"], "skip_default_tools": True}
        await re_mod._run_spawn_subtask_batch(
            subtask_calls=[{"task": "count orders", "tools": ["run_sql_query"]}], parent_agent_id="p",
            available_tools=["run_sql_query"], session_id="s", server_module=None, source="chat",
            run_id=None, policy={"destructive": "allow", "open_world": "allow", "undeclared": "allow"},
            declarations={}, parent_agent=parent)
        sub = seen[0]
        assert sub["tools"] == ["run_sql_query"]
        assert sub["db_configs"] == ["sales"] and sub["repos"] == ["api"] and sub["skip_default_tools"] is True
