"""A tool that isn't available fails gracefully: its step says which tool, why and what to do; an agent runs with
the rest and says so; a run resumed once its app is back goes on (F1349; Naveen 11-04: "if a workflow fails because
a tool isn't available, it should fail gracefully").

Remote MCP apps drop out when a sign-in or a token expires, and a tool can be deleted. A Tool step whose tool was
gone failed "Tool 'Gmail__send_email' not found in available tools" (or "… in tool router"), naming no app, no reason
and no fix, and triage could not tell it from a tool's own failure. An agent naming a tool no connected app provided
had it left out silently: nothing told the model or the step. Now one reading (`core.tool_availability`) says it, from
the host's own record of the app — OSS's manager (its status and F1306's last_error, already scrubbed), a cloud
worker's lists and reasons — the same words on both, scrubbed again:

- a Tool step fails in those words, said once (no class name), never retried; Resume is safe since the tool never
  ran, and triage reads its class (`tool_unavailable`), offering reconnect, another tool or Resume;
- an agent runs with the tools it has: the model is told which is unavailable and why, the step's line says it, and
  calling it anyway answers the words, never a crash;
- a run that failed so and is resumed once the app is back goes on from that step.
"""
from __future__ import annotations

import json
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from core.tools import VirtualTool

GMAIL = "Gmail__send_email"
REASON = "the token expired: the server answered 401 Unauthorized"
LOOKUP = VirtualTool("lookup_order", "looks an order up", {"type": "object", "properties": {}}, None)


class _Manager:
    """OSS's record of each app (`MCPClientManager`): its config, status and F1306's last_error."""

    def __init__(self, **servers):
        self.servers = servers

    def get_server_config(self, name):
        return self.servers.get(name)

    async def _set_status(self, name, status):
        self.servers.setdefault(name, {"name": name})["status"] = status


class _App:
    """A connected Gmail: it advertises send_email and answers it, or fails as it is told to."""

    def __init__(self, fails: BaseException | None = None):
        self.fails, self.calls = fails, []

    async def send_ping(self):
        return None

    async def list_tools(self):
        tool = types.SimpleNamespace(name="send_email", description="Sends an email", annotations=None,
                                     inputSchema={"type": "object", "properties": {"to": {"type": "string"}}})
        return types.SimpleNamespace(tools=[tool])

    async def call_tool(self, name, args, **_kw):
        self.calls.append((name, dict(args)))
        if self.fails is not None:
            raise self.fails
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text='{"sent": true}')],
                                     isError=False)


def _server(*, status: str = "reauth_needed", last_error: str = REASON, worker: bool = False,
            app: _App | None = None, routed: bool = False):
    """A host whose Gmail is `status`; with `app`, connected (routed and its session open); with `routed` and no
    app, routed but its session gone."""
    s = types.SimpleNamespace(agent_sessions={}, tool_router={}, memory_store=None)
    if worker:  # a cloud worker's own record (core/scale/worker_server_module.py)
        s.mcp_disabled = ["Gmail"] if status != "connected" else []
        s.mcp_reauth_needed = ["Gmail"] if status == "reauth_needed" else []
        s.mcp_errors = {"Gmail": last_error}
    else:
        s.mcp_manager = _Manager(Gmail={"name": "Gmail", "status": status, "last_error": last_error,
                                        "server_type": "remote"})
    if app is not None or routed:
        s.tool_router[GMAIL] = ("ext_mcp_Gmail", "send_email")
    if app is not None:
        s.agent_sessions["ext_mcp_Gmail"] = app
    return s


def _orch(tool: str = GMAIL) -> dict:
    return S.make_orchestration(id="orch_f1349", entry_step_id="send", steps=[
        {"id": "send", "name": "Send the summary", "type": "tool", "forced_tool": tool,
         "tool_arguments": {"to": "ops@harborpine.example"}, "output_key": "sent", "next_step_id": "after"},
        {"id": "after", "name": "After", "type": "print", "print_content": "sent", "output_key": "after",
         "next_step_id": None}])


async def _run(server, tool: str = GMAIL, run_id: str = "run_f1349"):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    orch = _orch(tool)
    await S.seed_orchestrations([orch])
    return [e async for e in OrchestrationEngine(Orchestration.model_validate(orch), server).run("go", run_id=run_id)]


def _said(events) -> str:
    return " ".join(str(e.get("response") or e.get("error") or e.get("message") or "") for e in events)


def _done(events) -> bool:
    return any(e.get("type") == "orchestration_complete" for e in events)


async def _step_error(run_id: str) -> str:
    """What the failed step's history entry says, as the run page and triage read it."""
    from sqlalchemy import select

    from core.store import get_store
    from core.store.models import OrchestrationRunDB

    async with (await get_store())() as session:
        found = await session.execute(select(OrchestrationRunDB).where(OrchestrationRunDB.run_id == run_id))
        row = found.scalar_one()
    history = json.loads(row.step_history) if isinstance(row.step_history, str) else list(row.step_history or [])
    entry = next(e for e in reversed(history) if isinstance(e, dict) and e.get("step_id") == "send")
    return str(entry.get("error") or "")


class TestAToolStep:
    async def test_its_app_needing_sign_in_fails_the_step_in_clear_words(self, client):
        events = await _run(_server())
        said = _said(events)
        assert not _done(events)
        for words in (GMAIL, "isn't available", "Gmail", "needs signing in again", REASON, "Reconnect Gmail",
                      "Resume", "never ran"):
            assert words in said, (words, said)
        assert "not found in available tools" not in said and "not found in tool router" not in said
        assert "Traceback" not in said

    async def test_the_steps_error_is_its_words_and_triage_reads_its_class(self, client):
        from core.orchestration import triage

        await _run(_server(), run_id="run_f1349_triage")
        error = await _step_error("run_f1349_triage")
        assert error.startswith("Not run: the tool"), error  # the words, said once, no class name before them
        kind, _ = triage.classify(error, {"step_type": "tool"}, status="failed")
        assert kind == "tool_unavailable"
        assert {"app_reconnect", "resume_run"} <= set(triage.FIXES[kind]) and triage.MEANS[kind]

    async def test_a_cloud_workers_record_says_the_same(self, client):
        said = _said(await _run(_server(worker=True), run_id="run_f1349_worker"))
        for words in (GMAIL, "Gmail", "needs signing in again", REASON, "Reconnect Gmail"):
            assert words in said, (words, said)

    async def test_an_app_that_is_disconnected_says_so(self, client):
        said = _said(await _run(_server(status="disconnected", last_error="Connection refused"),
                                run_id="run_f1349_off"))
        assert "Gmail isn't connected" in said and "Connection refused" in said and "Reconnect Gmail" in said, said

    async def test_routed_with_its_session_gone_is_the_same_never_a_key_error(self, client):
        said = _said(await _run(_server(routed=True), run_id="run_f1349_gone"))
        assert "needs signing in again" in said and "KeyError" not in said, said

    async def test_a_deleted_tool_says_to_pick_another(self, client):
        said = _said(await _run(_server(), tool="fetch_orders", run_id="run_f1349_deleted"))
        assert "fetch_orders" in said and "no app or tool here provides it" in said and "Pick another tool" in said

    async def test_a_sign_in_that_expires_during_the_call_says_the_same(self, client):
        app = _App(fails=RuntimeError("HTTP 401 Unauthorized: invalid_token"))
        said = _said(await _run(_server(status="connected", last_error="", app=app), run_id="run_f1349_401"))
        assert "needs signing in again" in said and "Reconnect Gmail" in said and "invalid_token" in said, said

    async def test_a_key_in_the_apps_reason_is_never_said(self, client):
        key = "sk-" + "Q7" * 14
        said = _said(await _run(_server(last_error=f"401 Unauthorized for Bearer {key}"), run_id="run_f1349_key"))
        assert "needs signing in again" in said and key not in said, said

    async def test_resumed_once_its_app_is_back_it_goes_on(self, client):
        from core.orchestration.engine import OrchestrationEngine

        server = _server()
        events = await _run(server, run_id="run_f1349_resume")
        assert not _done(events)
        app = _App()
        server.tool_router[GMAIL] = ("ext_mcp_Gmail", "send_email")
        server.agent_sessions["ext_mcp_Gmail"] = app
        server.mcp_manager.servers["Gmail"].update(status="connected", last_error=None)
        resumed = [e async for e in OrchestrationEngine.resume_failed("run_f1349_resume", server)]
        assert _done(resumed), _said(resumed)
        assert app.calls == [("send_email", {"to": "ops@harborpine.example"})]


def _agent_server(monkeypatch, server, *, offer_gmail: bool = False):
    import core.react_engine as re_mod

    async def aggregated(server_module, agent, custom_tools, **_):
        offered = [LOOKUP, *([VirtualTool(GMAIL, "Sends an email", {"type": "object", "properties": {}}, None)]
                             if offer_gmail else [])]
        ollama = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                    "parameters": t.inputSchema}} for t in offered]
        return offered, {t.name: t.inputSchema for t in offered}, ollama, ""

    monkeypatch.setattr(re_mod, "aggregate_all_tools", aggregated)
    return server


async def _turn(server, fake_llm, script):
    from core.react_engine import run_agent_step

    a = S.make_agent(tools=["lookup_order", GMAIL], skip_default_tools=True)
    fake_llm.script(script)
    events = [ev async for ev in run_agent_step(message="Send ops the order summary.", agent_id=a["id"],
                                                session_id="s_f1349", server_module=server, agent_override=a,
                                                max_turns=4)]
    return events, [json.dumps(c, default=str, ensure_ascii=False) for c in fake_llm.calls]


class TestAnAgent:
    async def test_it_runs_with_the_rest_and_is_told_which_and_why(self, monkeypatch, fake_llm, client):
        events, calls = await _turn(_agent_server(monkeypatch, _server()), fake_llm, ["I could not send it."])
        assert [e for e in events if e.get("type") == "final"][0]["response"] == "I could not send it."
        assert GMAIL in calls[0] and "needs signing in again" in calls[0], calls[0][:2000]
        lines = " ".join(str(e.get("message") or "") for e in events if e.get("type") == "status")
        assert GMAIL in lines and "needs signing in again" in lines, lines

    async def test_calling_it_anyway_answers_the_words_never_does_not_exist(self, monkeypatch, fake_llm, client):
        events, calls = await _turn(_agent_server(monkeypatch, _server()), fake_llm,
                                    [tool_call(GMAIL, to="ops@harborpine.example"), "I could not send it."])
        after = " ".join(calls[1:])
        assert "isn't available" in after and "needs signing in again" in after, after[:2000]
        assert "does not exist and cannot be called" not in after

    async def test_offered_but_its_session_gone_is_the_words_never_a_crash(self, monkeypatch, fake_llm, client):
        server = _agent_server(monkeypatch, _server(routed=True), offer_gmail=True)
        events, calls = await _turn(server, fake_llm, [tool_call(GMAIL, to="ops@harborpine.example"), "Not sent."])
        assert [e for e in events if e.get("type") == "final"][0]["response"] == "Not sent."
        assert "isn't available" in " ".join(calls[1:])

    async def test_a_tool_that_is_there_is_not_said(self, monkeypatch, fake_llm, client):
        server = _agent_server(monkeypatch, _server(status="connected", last_error="", app=_App()),
                               offer_gmail=True)
        events, calls = await _turn(server, fake_llm, ["Sent."])
        assert "isn't available" not in calls[0] and "needs signing in again" not in calls[0]


@pytest.mark.parametrize("name,app", [(GMAIL, "Gmail"), ("lookup_order", None), ("all", None), ("group:ops", None)])
def test_an_apps_tool_is_known_by_its_name(name, app):
    """An app's tools are named `{app}__{tool}`, as an agent's list holds them (`tools.aggregate_all_tools`)."""
    from core.tool_availability import app_of

    assert app_of(name) == app
