"""Every agent call that never ran, or ran and raised, is marked what it was, with why (F1752).

F1751 marked a call whose tool said it failed. Other calls that did not do what was asked went unmarked: a tool that
does not exist, one removed, one whose app is not connected or whose session went, a hand-off to an agent that is not
there, a skill that is not there or cannot be read, a memory save nobody can approve, a SQL call to a database the
agent was not given, an app whose session did not answer before the call was sent; and a custom tool, an MCP call, a
builder tool, a memory tool or a caller's own tool that raised. Screens read them by their first word, and a custom
HTTP tool that raised sent no result at all, so its call read as running until the turn ended.

Now each says what happened. A call that never reached its tool is marked `refused` (not run), with why; one that ran,
or may have, and raised is marked `failed`, with why. Its reason is what its own preview already says (never new
text, never what the agent is told to do), so a public stream gains no words. What the model reads is unchanged at
every site. A builder tool's answer is read for failure only by `built_in_failure`, as an MCP built-in's is.

Near misses: a builder tool's answer that is not its failure shape stays unmarked; an MCP call whose result was said
and whose hook raised afterwards is not marked failed; a SQL call to the one database the agent has goes through.
"""
from __future__ import annotations

import json
import types

import httpx
import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from _fakes.routed_llm import RoutedLLM, install
from core.tools import VirtualTool
from tests.unit import test_a_tool_that_is_not_available_fails_gracefully as AV
from tests.unit.test_an_agents_failed_call_is_said_failed import (  # noqa: F401 — `answered` is a fixture
    NOTE, _mcp, _results, _told, _tools, _turn, answered)
from tests.unit.test_skills import _load_call, seeded_skill  # noqa: F401 — `seeded_skill` is a fixture
from tests.unit.test_telemetry_metrics_carry_no_tenant import _points, metered  # noqa: F401 — a fixture


def _one(events, tool: str) -> dict:
    found = [e for e in _results(events) if e.get("tool_name") == tool]
    assert len(found) == 1, found
    return found[0]


#: What the agent is told to do, at these sites: never part of a reason a person (or an API's caller) reads.
TOLD = ("Do what you can", "Choose one of", "Do not attempt", "Use one of these", "tell the person", "never say",
        "Report it in your answer")


def _why(result: dict) -> str:
    """Its reason: words its own preview already says (so no longer than it), and none the agent is told."""
    why = result.get("reason")
    assert why and why.casefold() in result["preview"].casefold(), result
    assert len(why) <= len(result["preview"]) <= 503, result
    assert not any(t.casefold() in why.casefold() for t in TOLD), why
    return why


def _not_run(result: dict) -> str:
    """Marked not run, with why."""
    assert result.get("refused") is True and "failed" not in result, result
    return _why(result)


def _failed(result: dict) -> str:
    """Marked failed, with why."""
    assert result.get("failed") is True and "refused" not in result, result
    return _why(result)


def _named(tool: str, **args) -> str:
    """A call whose arguments include `name` (which `tool_call`'s own first argument would take)."""
    return json.dumps({"tool": tool, "arguments": args})


def _everything(**kw) -> dict:
    return S.make_agent(**{"name": "Order desk", "tools": ["all"], "skip_default_tools": True, **kw})


class _Pingless:
    """An app's session that does not answer its ping: the call is never sent."""

    def __init__(self):
        self.called = []

    async def send_ping(self):
        raise TimeoutError("no answer in 5s")

    async def call_tool(self, name, args, **_kw):
        self.called.append(name)


class _Raising:
    """An app's session whose call raises once it is sent."""

    async def send_ping(self):
        return None

    async def call_tool(self, name, args, **_kw):
        raise RuntimeError("connection reset by peer")


def _mcp_with(monkeypatch, session) -> types.SimpleNamespace:
    server = _mcp(monkeypatch, "orders_app", "update_order", "", is_error=False)
    server.agent_sessions["orders_app"] = session
    return server


class TestNotRun:
    async def test_a_tool_that_does_not_exist(self, fake_llm):
        events = await _turn(fake_llm, [tool_call("send_fax", to="x"), "No fax."], agent=_everything())
        assert _not_run(_one(events, "send_fax")) == "tool not found"
        assert "System: Tool 'send_fax' does not exist and cannot be called." in _told(fake_llm)

    async def test_a_tool_that_was_removed(self, fake_llm):
        events = await _turn(fake_llm, [tool_call("query_past_conversations", q="x"), "Gone."], agent=_everything())
        assert _not_run(_one(events, "query_past_conversations")) == "tool removed"
        assert "System: The 'query_past_conversations' tool is no longer available." in _told(fake_llm)

    async def test_a_hand_off_to_an_agent_that_is_not_there(self, fake_llm):
        member = S.make_agent(id="agent_f1752_member", name="Member", tools=[])
        lead = S.make_agent(id="agent_f1752_lead", name="Lead", type="delegate", tools=[],
                            delegate_agent_ids=[member["id"]])
        await S.seed_agents([member, lead])
        events = await _turn(fake_llm, [tool_call("delegate_to_agent", agent_id="nobody", task="x"), "No one."],
                             agent=lead)
        _not_run(_one(events, "delegate_to_agent"))
        assert "System: Agent 'nobody' not found or not available for delegation." in _told(fake_llm)

    async def test_a_skill_that_is_not_there(self, fake_llm, seeded_skill):
        events = await _turn(fake_llm, [_load_call("nonexistent"), "No skill."],
                             agent=S.make_agent(tools=[], skills=["market-summary"]))
        _not_run(_one(events, "load_skill"))
        assert "System: Unknown skill 'nonexistent'. Your skills: market-summary." in _told(fake_llm)

    async def test_a_skill_that_cannot_be_read(self, fake_llm, seeded_skill, monkeypatch):
        import core.skills

        async def gone(name):
            return None

        monkeypatch.setattr(core.skills, "get_skill", gone)
        events = await _turn(fake_llm, [_load_call("market-summary"), "Unreadable."],
                             agent=S.make_agent(tools=[], skills=["market-summary"]))
        _not_run(_one(events, "load_skill"))
        assert "System: Skill 'market-summary' could not be read (it may have been deleted)." in _told(fake_llm)

    async def test_a_memory_save_nobody_here_can_approve(self, fake_llm):
        from core.react_engine import run_agent_step

        agent = _everything(memory=True)

        async def read(name, args):
            return "The handover note says: keep the Tuesday slot."

        fake_llm.script([tool_call("read_doc", path="ops/handover.md"),
                         _named("remember", name="tuesday", content="Keep the Tuesday slot."), "Noted."])
        events = [ev async for ev in run_agent_step(
            message="summarise ops/handover.md", agent_id=agent["id"], session_id="s_f1752_mem",
            server_module=types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}),
            agent_override=agent, max_turns=4, tool_executor=read, persist_turn=False)]
        _not_run(_one(events, "remember"))
        assert "System: Memory not saved: this turn read content from a file or a tool" in _told(fake_llm)

    async def test_a_tool_whose_app_is_not_connected(self, monkeypatch, fake_llm, client):
        server = AV._agent_server(monkeypatch, AV._server())
        events, calls = await AV._turn(server, fake_llm, [tool_call(AV.GMAIL, to="ops@harborpine.example"), "No."])
        why = _not_run(_one(events, AV.GMAIL))
        assert "Do what you can" not in why, "the reason is why, never what the agent is told to do"
        assert "isn't available" in calls[1] and "Do what you can without it" in calls[1]

    async def test_a_tool_whose_apps_session_went(self, monkeypatch, fake_llm, client):
        server = AV._agent_server(monkeypatch, AV._server(routed=True), offer_gmail=True)
        events, calls = await AV._turn(server, fake_llm, [tool_call(AV.GMAIL, to="ops@harborpine.example"), "No."])
        assert "Do what you can" not in _not_run(_one(events, AV.GMAIL))
        assert "isn't available" in calls[1]

    async def test_a_sql_call_to_a_database_not_named(self, fake_llm, monkeypatch):
        server = _mcp(monkeypatch, "sql", "update_order", "[]", is_error=False)
        agent = S.make_agent(name="Order desk", tools=["update_order"], skip_default_tools=True,
                             db_configs=["db_orders", "db_stock"])
        events = await _turn(fake_llm, [tool_call("update_order", order="HP-2041"), "Which one?"],
                             agent=agent, server=server)
        assert _not_run(_one(events, "update_order")) == \
            "This agent can query more than one database, so `db_id` is required"
        assert ("System: This agent can query more than one database, so `db_id` is required. "
                "Choose one of: db_orders, db_stock.") in _told(fake_llm)

    async def test_an_app_whose_session_does_not_answer_is_never_called(self, fake_llm, monkeypatch):
        session = _Pingless()
        events = await _turn(fake_llm, [tool_call("update_order", order="HP-2041"), "Not reached."],
                             "update_order", server=_mcp_with(monkeypatch, session))
        assert _not_run(_one(events, "update_order")) == "session unresponsive"
        assert session.called == [], "the ping comes before the call: it was never sent"
        assert "System: Error — MCP session 'orders_app' unresponsive (ping: no answer in 5s)." in _told(fake_llm)

    async def test_its_saved_turn_keeps_it_not_run(self, fake_llm):
        from core.store import sessions as store

        agent = _everything()
        await _turn(fake_llm, [tool_call("send_fax", to="x"), "No fax."], agent=agent)
        saved = await store.get("s_f1751", agent["id"])
        [call] = [c for m in saved["messages"] for c in (m.get("calls") or [])]
        assert call.get("refused") is True and call["reason"] == "tool not found" and not call.get("failed")


@pytest.fixture
def unreachable(monkeypatch):
    """Every request an HTTP tool sends raises `unreachable.error` (a refused connection unless set)."""
    import core.http_tool as http_tool

    record = types.SimpleNamespace(error=httpx.ConnectError("connection refused"))

    async def send(client, tool, args, variables, **kw):
        raise record.error

    monkeypatch.setattr(http_tool, "send", send)
    return record


class TestRaised:
    async def test_a_custom_http_tool_that_raised_has_a_result_marked_failed(self, fake_llm, unreachable):
        await _tools(NOTE)
        events = await _turn(fake_llm, [tool_call("send_note", text="Ships Wednesday."), "Not sent."], "send_note")
        assert _failed(_one(events, "send_note")) == "connection refused"
        assert "System: Error executing custom tool send_note: connection refused\n" in _told(fake_llm)

    async def test_a_custom_http_tool_that_may_have_reached_says_so_to_the_model_as_before(self, fake_llm,
                                                                                          unreachable):
        from core.tool_tags import MAY_HAVE_RUN

        unreachable.error = httpx.ReadTimeout("timed out reading the answer")
        await _tools(NOTE)
        events = await _turn(fake_llm, [tool_call("send_note", text="Ships Wednesday."), "Unsure."], "send_note")
        assert _failed(_one(events, "send_note")) == "timed out reading the answer"
        assert (f"System: Error executing custom tool send_note: timed out reading the answer The request reached "
                f"the server before it failed: {MAY_HAVE_RUN}.") in _told(fake_llm)

    async def test_a_custom_python_tool_that_raised(self, fake_llm):
        await _tools({"name": "count_orders", "description": "Counts the day's orders.", "tool_type": "python",
                      "code": "  ", "inputSchema": {"type": "object", "properties": {}}})
        events = await _turn(fake_llm, [tool_call("count_orders"), "No count."], "count_orders")
        assert _failed(_one(events, "count_orders")) == "Python tool has no code defined."
        assert ("System: Error executing Python tool 'count_orders': Python tool has no code defined."
                in _told(fake_llm))

    async def test_an_mcp_call_that_raised(self, fake_llm, monkeypatch):
        events = await _turn(fake_llm, [tool_call("update_order", order="HP-2041"), "Not updated."],
                             "update_order", server=_mcp_with(monkeypatch, _Raising()))
        assert _failed(_one(events, "update_order")) == "connection reset by peer"
        assert "System: Error executing tool update_order: connection reset by peer" in _told(fake_llm)

    async def test_a_callers_own_tool_that_raised(self, fake_llm):
        from core.react_engine import run_agent_step

        async def transfer(args):
            raise RuntimeError("the line is busy")

        declared = VirtualTool("transfer_call", "Transfers the call.", {"type": "object", "properties": {}}, None)
        agent = _everything()
        fake_llm.script([tool_call("transfer_call"), "Couldn't transfer."])
        events = [ev async for ev in run_agent_step(
            message="Put me through.", agent_id=agent["id"], session_id="s_f1752_call",
            server_module=types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}),
            agent_override=agent, max_turns=3, extra_tools=[(declared, transfer)])]
        assert _failed(_one(events, "transfer_call")) == "the line is busy"
        assert "] transfer_call: Error: the line is busy\n" in _told(fake_llm)

    async def test_a_memory_tool_that_raised(self, fake_llm, monkeypatch):
        import core.agent_memory

        def broken(*_a, **_kw):
            raise OSError("the memory folder is read-only")

        monkeypatch.setattr(core.agent_memory, "read_memory", broken)
        events = await _turn(fake_llm, [_named("read_memory", name="tuesday"), "Can't read it."],
                             agent=_everything(memory=True))
        assert _failed(_one(events, "read_memory")) == "the memory folder is read-only"
        assert "System: Memory error: the memory folder is read-only" in _told(fake_llm)

    @pytest.mark.parametrize("answer,why", [
        ('{"error": "no agent named Ops"}', "no agent named Ops"),
        (RuntimeError("the store is offline"), "the store is offline"),
    ])
    async def test_a_builder_tool_that_answered_its_failure_or_raised(self, fake_llm, monkeypatch, answer, why):
        import core.builder_tools

        async def run(name, args, server_module, **kw):
            if isinstance(answer, Exception):
                raise answer
            return answer, []

        monkeypatch.setattr(core.builder_tools, "execute_builder_tool_with_events", run)
        events = await _turn(fake_llm, [tool_call("get_agent", agent_id="ops"), "No such agent."],
                             agent=_everything())
        assert _failed(_one(events, "get_agent")) == why
        assert f"Tool 'get_agent' returned:\n{json.dumps({'error': why})}\n" in _told(fake_llm)


class TestNearMisses:
    async def test_a_builder_tools_answer_that_is_not_its_failure_shape_is_unmarked(self, fake_llm, monkeypatch):
        import core.builder_tools

        async def run(name, args, server_module, **kw):
            return json.dumps({"agent": {"name": "Ops", "errors_last_week": 0}, "error": None}), []

        monkeypatch.setattr(core.builder_tools, "execute_builder_tool_with_events", run)
        events = await _turn(fake_llm, [tool_call("get_agent", agent_id="ops"), "Found it."], agent=_everything())
        result = _one(events, "get_agent")
        assert "failed" not in result and "refused" not in result

    async def test_an_mcp_call_whose_result_was_said_is_not_failed_by_a_hook_after_it(self, fake_llm, monkeypatch):
        from core.react_engine import run_agent_step

        server = _mcp(monkeypatch, "orders_app", "update_order", '{"updated": true}', is_error=False)

        async def hook(name, raw):
            raise RuntimeError("the hook broke")
            yield  # an async generator

        agent = S.make_agent(name="Order desk", tools=["update_order"], skip_default_tools=True)
        fake_llm.script([tool_call("update_order", order="HP-2041"), "Updated."])
        events = [ev async for ev in run_agent_step(
            message="Update HP-2041.", agent_id=agent["id"], session_id="s_f1752_hook", server_module=server,
            agent_override=agent, max_turns=3, post_tool_hook=hook)]
        said = [e for e in _results(events) if e.get("tool_name") == "update_order"]
        assert said[0]["preview"] == '{"updated": true}'
        assert not any(e.get("failed") for e in said), said

    async def test_a_sql_call_to_its_one_database_goes_through(self, fake_llm, monkeypatch):
        server = _mcp(monkeypatch, "sql", "update_order", "[]", is_error=False)
        agent = S.make_agent(name="Order desk", tools=["update_order"], skip_default_tools=True,
                             db_configs=["db_orders"])
        events = await _turn(fake_llm, [tool_call("update_order", order="HP-2041"), "Done."], agent=agent,
                             server=server)
        result = _one(events, "update_order")
        assert "refused" not in result and "failed" not in result


# ── The readers of a call's events ──────────────────────────────────────────────────────────────────────────────────

LOOKUP = VirtualTool("lookup", "looks a thing up", {"type": "object", "properties": {}}, None)


class _Erring:
    async def send_ping(self):
        return None

    async def call_tool(self, name, args, **_kw):
        return types.SimpleNamespace(content=[types.SimpleNamespace(text="the lookup service is down")],
                                     isError=True)


@pytest.fixture
def erring(monkeypatch):
    """An app whose `lookup` answers `isError` (F1751: marked failed), offered to every agent that names it."""
    import core.react_engine as re_mod

    async def aggregated(server_module, agent, custom_tools, **_):
        held = [LOOKUP] if {"all", "lookup"} & set(agent.get("tools") or []) else []
        ollama = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                    "parameters": t.inputSchema}} for t in held]
        return held, {t.name: t.inputSchema for t in held}, ollama, ""

    monkeypatch.setattr(re_mod, "aggregate_all_tools", aggregated)
    return types.SimpleNamespace(agent_sessions={"fake": _Erring()}, memory_store=None,
                                 tool_router={"lookup": ("fake", "lookup")})


@pytest.fixture
def routed(monkeypatch):
    return install(monkeypatch, RoutedLLM())


class TestTheRecordsKeepIt:
    async def test_a_helpers_record_keeps_its_call_failed(self, erring, routed):
        from core.react_engine import run_agent_step

        spawn = json.dumps({"tool": "spawn_subtask", "arguments": {"task": "CHECK A", "tools": ["lookup"],
                                                                     "name": "Check A"}})
        routed.on("PLAN IT", [spawn, "all done"]).on("CHECK A", [tool_call("lookup", item="A"), "It is down."])
        parent = S.make_agent(tools=["spawn_subtask", "lookup"])
        events = [ev async for ev in run_agent_step(message="PLAN IT", agent_id=parent["id"],
                                                    session_id="s_f1752_helper", server_module=erring,
                                                    agent_override=parent, max_turns=6)]
        record = [e for e in events if e.get("type") == "helper_finished"][0]["record"]
        [call] = record["calls"]
        assert call.get("failed") is True and call["reason"] == "the lookup service is down", call

    async def test_a_team_members_record_keeps_its_call_failed(self, erring, routed):
        from core.react_engine import run_agent_step

        ana = S.make_agent(id="ana_f1752", name="Ana", tools=["lookup"], skip_default_tools=True)
        lead = S.make_agent(id="lead_f1752", name="Lead", type="delegate", tools=[], delegate_agent_ids=[ana["id"]],
                            team={"mode": "team"})
        await S.seed_agents([ana, lead])
        routed.on("PLAN IT", [tool_call("assign", member="Ana", task="CHECK A"), tool_call("wait", seconds=10),
                              "lead answer"])
        routed.on("CHECK A", [tool_call("lookup", item="A"), "It is down."])
        events = [ev async for ev in run_agent_step(message="PLAN IT", agent_id=lead["id"],
                                                    session_id="s_f1752_team", server_module=erring, max_turns=8)]
        record = [e for e in events if e.get("type") == "member_finished"][0]["record"]
        [call] = record["calls"]
        assert call.get("failed") is True and call["reason"] == "the lookup service is down", call


class TestTelemetry:
    async def test_a_failed_call_is_counted_an_error_and_one_not_run_refused(self, metered, fake_llm, answered):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        from core.orchestration.runner import stream_engine_events
        from core.telemetry import attributes as A

        await _tools(NOTE)
        answered.status, answered.body, answered.ctype = 405, "Method not allowed", "text/plain"
        await S.seed_agents([S.make_agent(id="m_f1752", tools=["all"], max_turns=4)])
        orch = S.make_orchestration(id="orch_f1752m", entry_step_id="work", steps=[
            {"id": "work", "name": "Work", "type": "agent", "agent_id": "m_f1752",
             "prompt_template": "{state.user_input}", "output_key": "out", "next_step_id": None}])
        fake_llm.script([tool_call("send_note", text="x"), tool_call("send_fax", to="x"), "done"])
        engine = OrchestrationEngine(Orchestration.model_validate(orch),
                                     types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}))
        [e async for e in stream_engine_events(engine.run("go", run_id="run_f1752m"), "run_f1752m")]
        outcomes = sorted(p["outcome"] for p in _points(metered)["synapse.tool.calls"])
        assert outcomes == ["error", "refused"], outcomes
        assert all(set(p) <= A.METRIC_ATTRIBUTES for p in _points(metered)["synapse.tool.calls"])


class TestAnHttpRaiseInAWorkflow:
    """The call that used to end with no result: its run's history and cost are what they were (kept for comparison
    across the change in `user_properties`), and its result is said once."""

    async def test_its_run_is_unchanged_and_its_call_has_one_result(self, fake_llm, unreachable, request):
        from sqlalchemy import select

        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        from core.store import get_store
        from core.store.models import OrchestrationRunDB

        await _tools(NOTE)
        await S.seed_agents([S.make_agent(id="w_f1752", tools=["send_note"], skip_default_tools=True, max_turns=3)])
        orch = S.make_orchestration(id="orch_f1752w", entry_step_id="work", steps=[
            {"id": "work", "name": "Work", "type": "agent", "agent_id": "w_f1752",
             "prompt_template": "{state.user_input}", "output_key": "out", "next_step_id": None}])
        await S.seed_orchestrations([orch])
        fake_llm.script([tool_call("send_note", text="Ships Wednesday."), "It could not be sent."])
        engine = OrchestrationEngine(Orchestration.model_validate(orch),
                                     types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}))
        events = [e async for e in engine.run("go", run_id="run_f1752w")]
        async with (await get_store())() as session:
            row = (await session.execute(
                select(OrchestrationRunDB).where(OrchestrationRunDB.run_id == "run_f1752w"))).scalar_one()
        history = json.loads(row.step_history) if isinstance(row.step_history, str) else list(row.step_history)
        steady = [{k: v for k, v in h.items() if k not in ("started_at", "ended_at", "duration_ms", "timestamp",
                                                           "duration", "duration_s")} for h in history]
        request.node.user_properties.append(("run", {"status": row.status, "history": steady,
                                                     "cost": row.total_cost_usd, "tokens": row.total_tokens_used}))
        assert row.status == "completed"
        said = [e for e in events if e.get("type") == "tool_result" and e.get("tool_name") == "send_note"]
        assert len(said) == 1 and said[0].get("failed") is True, said
