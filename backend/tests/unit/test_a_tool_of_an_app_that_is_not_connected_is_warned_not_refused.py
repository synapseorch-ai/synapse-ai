"""A tool of an app this workspace has set up that isn't connected right now is warned and kept, never refused, and
whether it asks first is decided when it runs (F1679).

The builder's checks ask whether a run could call a tool with the run's own lookup, against the apps connected at that
moment. An app set up here but not connected then (asleep, down, or waiting to be signed in again) has no tools in that
lookup, so each of its tools read as "nothing here provides": validating failed a Tool step on it, `create_agents`
refused it, `get_tools_detail` listed it as not found, and a plan sent it to research. Runs used the same tools fine.

Now such a tool is kept and warned with its app's own reason ("can't be checked right now"), on both hosts (a
self-hosted install's record of its apps, a cloud worker's lists). What it does can't be read either, so whether it
asks first is never decided at build time, which would stick: a save marks it `check` (a Tool step's `tool_check`, an
agent's `tool:<name>: check`), and each run decides it from the tool's real declaration, as a build that could read it
would have: one that only reads runs without asking, anything else asks. A later save while the app is connected
replaces the mark with that decision.

Near misses: a connected app without that tool and an app nobody set up are still refused; a turned-off app's tool is
its own issue; an app still down when the run comes fails gracefully, as an unavailable tool does (F1349).
"""
from __future__ import annotations

import json
import types

import pytest
from _fakes import seed as S

from core import builder_guard
from core.builder_tools import execute_builder_tool

APP = "google-workspace"
READ = f"{APP}__sheets_read"
WRITE = f"{APP}__docs_create"
SESSION = f"ext_mcp_{APP}"


class _Manager:
    def __init__(self, configs: list[dict]):
        self.servers_config = configs
        self.sessions: dict = {}

    def get_server_config(self, name: str):
        return next((c for c in self.servers_config if c.get("name") == name), None)


class _SelfHosted:
    """A self-hosted install whose `APP` is set up and not connected (its record: `mcp_manager`)."""

    agent_sessions: dict = {}

    def __init__(self, status: str = "disconnected", *, enabled: bool = True, last_error: str | None = None):
        config = {"name": APP, "status": status, "enabled": enabled}
        if last_error:
            config["last_error"] = last_error
        self.mcp_manager = _Manager([config])


class _CloudWorker:
    """A cloud worker that could not open `APP` (its lists, as `WorkerServerModule` keeps them)."""

    agent_sessions: dict = {}

    def __init__(self, *, reauth: bool = False):
        self.mcp_disabled = [APP]
        self.mcp_reauth_needed = [APP] if reauth else []
        self.mcp_errors = {APP: "it did not answer within 0 s"}


def _declared(name: str, *, reads: bool):
    hints = types.SimpleNamespace(readOnlyHint=reads, destructiveHint=False, openWorldHint=not reads,
                                  idempotentHint=reads)
    return types.SimpleNamespace(name=name, description=name, inputSchema={"type": "object", "properties": {}},
                                 annotations=hints)


class _Google:
    """`APP` connected: one tool that only reads, one that writes."""

    def __init__(self):
        self.called: list[str] = []

    async def send_ping(self):
        return None

    async def list_tools(self):
        return types.SimpleNamespace(tools=[_declared("sheets_read", reads=True), _declared("docs_create", reads=False)])

    async def call_tool(self, name, args, read_timeout_seconds=None):
        self.called.append(name)
        return types.SimpleNamespace(content=[types.SimpleNamespace(text="done")], isError=False)


def _connected():
    google = _Google()
    server = types.SimpleNamespace(agent_sessions={SESSION: google}, memory_store=None,
                                   tool_router={READ: (SESSION, "sheets_read"), WRITE: (SESSION, "docs_create")},
                                   mcp_manager=_Manager([{"name": APP, "status": "connected", "enabled": True}]))
    return server, google


def _tool_step(tool: str = WRITE, **more) -> dict:
    return {"id": "make", "name": "Make the doc", "type": "tool", "forced_tool": tool, **more}


async def _call(tool: str, args: dict, server) -> dict:
    return json.loads(await execute_builder_tool(tool, args, server))


# ── set up, not connected: warned and kept ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("server", [_SelfHosted(last_error="Connection refused"), _CloudWorker()],
                         ids=["self-hosted", "cloud"])
async def test_validating_a_tool_step_on_it_warns_and_names_the_app(server):
    issues, warnings = await builder_guard.missing_in([{"steps": [_tool_step()]}], {}, server)
    assert issues == [], issues
    assert len(warnings) == 1 and "can't be checked right now" in warnings[0], warnings
    assert WRITE in warnings[0] and f"its app {APP} isn't connected" in warnings[0], warnings


async def test_an_app_waiting_to_be_signed_in_again_says_so():
    issues, warnings = await builder_guard.missing_in([{"steps": [_tool_step()]}], {}, _CloudWorker(reauth=True))
    assert issues == [] and "needs signing in again" in warnings[0], (issues, warnings)


async def test_an_agent_naming_it_is_warned_it_cant_be_checked():
    agents = {"a1": {"id": "a1", "name": "Writer", "tools": [WRITE]}}
    workflow = {"steps": [{"id": "write", "name": "Write", "type": "agent", "agent_id": "a1"}]}
    issues, warnings = await builder_guard.missing_in([workflow], agents, _SelfHosted())
    assert issues == [] and len(warnings) == 1, (issues, warnings)
    assert "can't be checked right now" in warnings[0] and "nothing here provides" not in warnings[0], warnings


async def test_get_tools_detail_says_why_it_cant_be_read():
    got = await _call("get_tools_detail", {"tool_names": [WRITE]}, _SelfHosted())
    assert WRITE not in got.get("_not_found", []), got
    assert f"its app {APP} isn't connected" in got["_not_reachable"][WRITE], got


async def test_a_plan_keeps_it_and_never_sends_it_to_research():
    from core.native_builder.objects import Plan, fill

    plan = Plan.model_validate({"kind": "create", "title": "Doc maker", "summary": "Makes a doc.",
                                "steps": [{"id": "make", "type": "tool", "name": "Make the doc",
                                           "does": "Makes the doc", "tool": WRITE}]})
    out = await fill(plan, _CloudWorker())
    assert out.missing == [], out.missing
    status = next(t for t in out.tools if t.name == WRITE)
    assert status.status == "present" and "can't be checked right now" in (status.detail or ""), status
    assert WRITE not in out.approvals.asks_first, "never decided at build time"


# ── a save marks it `check`; a connected save decides it ─────────────────────────────────────────────────────────

async def test_create_agents_keeps_it_marked_check():
    got = await _call("create_agents", {"agents": [
        {"role": "writer", "name": "Doc writer", "description": "Writes the doc.", "type": "conversational",
         "tools": [WRITE], "system_prompt": "Write the doc."}]}, _CloudWorker())
    assert not got.get("errors"), got
    from core.routes.agents import load_user_agents

    saved = next(a for a in await load_user_agents() if a.get("name") == "Doc writer")
    assert saved["tools"] == [WRITE] and saved["tool_policy"].get(f"tool:{WRITE}") == "check", saved


async def test_a_saved_tool_step_is_marked_and_a_connected_save_decides_it():
    from core.routes.orchestrations import load_orchestrations

    body = {"name": "Doc daily", "description": "d", "entry_step_id": "make",
            "steps": [_tool_step(next_step_id="end"), {"id": "end", "name": "End", "type": "end"}]}
    made = await _call("create_orchestration", body, _CloudWorker())
    assert "error" not in made, made
    orch = next(o for o in await load_orchestrations() if o.get("name") == "Doc daily")
    assert next(s for s in orch["steps"] if s["id"] == "make").get("tool_check") is True, orch["steps"]
    server, _ = _connected()
    again = await _call("update_orchestration", {"orch_id": orch["id"], "fields": {"description": "d2"}}, server)
    assert "error" not in again, again
    orch = next(o for o in await load_orchestrations() if o.get("name") == "Doc daily")
    assert not next(s for s in orch["steps"] if s["id"] == "make").get("tool_check"), "decided once it can be read"


async def test_a_connected_agent_save_replaces_check_with_the_decision():
    made = await _call("create_agent", {"name": "Sheet helper", "description": "Helps.", "type": "conversational",
                                        "tools": [READ, WRITE], "system_prompt": "Help."}, _CloudWorker())
    agent_id = made.get("id") or (made.get("agent") or {}).get("id")
    assert agent_id, made
    server, _ = _connected()
    await _call("update_agent", {"agent_id": agent_id, "fields": {"description": "Helps more."}}, server)
    from core.routes.agents import load_user_agents

    policy = next(a for a in await load_user_agents() if a.get("id") == agent_id).get("tool_policy") or {}
    assert f"tool:{READ}" not in policy, "it only reads: nothing to ask"
    assert policy.get(f"tool:{WRITE}") == "confirm", policy


# ── each run decides it from the real declaration ─────────────────────────────────────────────────────────────────

def test_check_lets_a_tool_that_only_reads_run_and_asks_for_anything_else():
    from core.tool_policy import as_visitor, decide, resolve_policy

    policy = resolve_policy({"tool_policy": {f"tool:{READ}": "check", f"tool:{WRITE}": "check"}}, {})
    assert decide(_declared(READ, reads=True), policy)[0] == "allow"
    assert decide(_declared(WRITE, reads=False), policy)[0] == "confirm"
    visitor = as_visitor(policy)
    assert decide(_declared(READ, reads=True), visitor)[0] == "allow"
    assert decide(_declared(WRITE, reads=False), visitor)[0] == "deny", "a visitor's ask is a refusal"


async def _run(tool: str, server, *, tag: str) -> list[dict]:
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    orch = S.make_orchestration(id=f"orch_f1679_{tag}", entry_step_id="make",
                                steps=[_tool_step(tool, tool_arguments={}, tool_check=True, output_key="made",
                                                  next_step_id=None)])
    await S.seed_orchestrations([orch])
    engine = OrchestrationEngine(Orchestration.model_validate(orch), server)
    return [e async for e in engine.run("go", run_id=f"run_f1679_{tag}")]


async def test_built_asleep_run_connected_a_tool_that_only_reads_runs_without_asking():
    server, google = _connected()
    events = await _run(READ, server, tag="reads")
    assert not [e for e in events if e.get("type") == "human_input_required"], "a scheduled run never stalls here"
    assert google.called == ["sheets_read"] and any(e.get("type") == "orchestration_complete" for e in events)


async def test_built_asleep_run_connected_a_tool_that_writes_asks_first():
    server, google = _connected()
    events = await _run(WRITE, server, tag="writes")
    asked = [e for e in events if e.get("type") == "human_input_required" and e.get("kind") == "tool_approval"]
    assert asked and google.called == [], events


async def test_still_down_when_it_runs_fails_gracefully():
    events = await _run(WRITE, _CloudWorker(), tag="down")
    said = " ".join(str(e.get("error") or e.get("reason") or e.get("response") or "") for e in events)
    assert "Not run: the tool" in said and f"its app {APP} isn't connected" in said, said


# ── near misses: still refused, as before ─────────────────────────────────────────────────────────────────────────

async def test_a_connected_app_without_that_tool_is_still_refused():
    server = _SelfHosted("connected")
    issues, warnings = await builder_guard.missing_in([{"steps": [_tool_step()]}], {}, server)
    assert len(issues) == 1 and "nothing here provides" in issues[0] and warnings == [], (issues, warnings)
    got = await _call("create_agents", {"agents": [
        {"role": "writer", "name": "Doc writer", "description": "d", "type": "conversational", "tools": [WRITE],
         "system_prompt": "s"}]}, server)
    assert not got.get("agents") and WRITE in got["errors"][0]["error"], got


async def test_an_app_nobody_set_up_is_still_refused():
    issues, warnings = await builder_guard.missing_in([{"steps": [_tool_step("nowhere__docs_create")]}], {},
                                                      _SelfHosted())
    assert len(issues) == 1 and "nothing here provides" in issues[0] and warnings == [], (issues, warnings)
    got = await _call("get_tools_detail", {"tool_names": ["nowhere__docs_create"]}, _CloudWorker())
    assert got.get("_not_found") == ["nowhere__docs_create"] and not got.get("_not_reachable"), got


async def test_a_turned_off_app_is_its_own_issue():
    issues, warnings = await builder_guard.missing_in([{"steps": [_tool_step()]}], {}, _SelfHosted(enabled=False))
    assert len(issues) == 1 and warnings == [], (issues, warnings)
    assert f"its app {APP} is turned off" in issues[0] and f"Turn {APP} on under Apps" in issues[0], issues
