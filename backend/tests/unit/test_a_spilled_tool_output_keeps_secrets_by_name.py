"""F1474 (CodeQL 9936/9937): a tool's output too long for the context is kept in the vault with the run's secrets
marked, as a chat turn and a compaction archive already keep them (F854, F1307).

`maybe_vault` wrote the output as it came: a secret variable rendered into the call, or a workspace secret a tool
echoed (a connection's token, a tool's header), was kept in clear in the vault file, where a member who opens the
Vault, or Knowledge, which follows the vault, reads it. The same output under the spill line was stored masked.

Both doors: a workflow step (the engine refreshes the workspace's held secrets at each step's start) and a chat
turn, which refreshed nothing, so even a scrub at the write would have missed the workspace's secrets there.
"""
from __future__ import annotations

import dataclasses
import json
import types

import pytest

from _fakes import seed as S
from core.tools import VirtualTool

#: A workspace secret: a tool's header holds it (`held_secrets`' tool_headers source).
HELD = "sk-held-7Qp2xV9mL4rT8wZ1"
#: A secret variable this task holds by name (`hold_secrets`, F854).
VARIABLE = "var-secret-3Kd8Lm2Qx9"
FILLER = "line of tool output that goes on\n" * 200


def _long(secret: str) -> str:
    return f"{FILLER}token={secret}\n{FILLER}"


def _vault_files() -> dict:
    from core.vault_backend import get_vault

    root = get_vault().root
    return {p: p.read_text(encoding="utf-8") for p in root.rglob("*") if p.is_file()} if root.exists() else {}


def _written_since(before: dict) -> dict:
    return {p: t for p, t in _vault_files().items() if p not in before}


@pytest.fixture
async def held_header():
    from core.store.resources import save_tool

    await save_tool({"id": "tool_whoami", "name": "whoami", "type": "http", "url": "https://whoami.test/",
                     "method": "GET", "headers": {"Authorization": f"Bearer {HELD}"}})


@pytest.fixture
def small_spill(monkeypatch):
    """A spill line of 2,000 characters, so a 13,000-character output goes to the vault."""
    import core.context_budget as cb

    real = cb.budget
    monkeypatch.setattr(cb, "budget", lambda *a, **k: dataclasses.replace(real(*a, **k), spill_at=2_000))


# ── maybe_vault itself: each of its writes ───────────────────────────────────────────────────────────────────────
@pytest.fixture
def held_variable():
    from core.secret_scrub import _HELD, hold_secrets

    token = _HELD.set(())
    hold_secrets({"API_KEY": VARIABLE})
    yield
    _HELD.reset(token)


@pytest.mark.parametrize("shape", ["text", "json", "documents"])
def test_each_vault_write_keeps_a_held_variable_by_name(held_variable, shape):
    from core.vault import maybe_vault

    if shape == "text":
        output = _long(VARIABLE)
    elif shape == "json":
        output = json.dumps({"body": _long(VARIABLE), "auth": VARIABLE})
    else:
        output = json.dumps({"results": [{"title": f"Page {i}", "url": f"https://x.test/{i}?k={VARIABLE}",
                                          "content": _long(VARIABLE)} for i in range(3)]})
    before = _vault_files()

    reference = maybe_vault("fetch", output, threshold=2_000)

    written = _written_since(before)
    assert written, reference
    assert not [p for p, t in written.items() if VARIABLE in t], [p.name for p, t in written.items() if VARIABLE in t]
    # Nor in a file's name: a document's file is named from its title or link (`_slug`).
    assert not [p for p in written if VARIABLE.lower() in str(p).lower()], [p.name for p in written]
    assert any("API_KEY" in t for t in written.values())
    assert VARIABLE not in reference


async def test_a_vault_write_keeps_a_workspace_secret_marked(held_header):
    from core.secret_scrub import CREDENTIAL_MARK, refresh_held
    from core.vault import maybe_vault

    await refresh_held()
    before = _vault_files()

    maybe_vault("whoami", _long(HELD), threshold=2_000)

    written = _written_since(before)
    assert written and not [p for p, t in written.items() if HELD in t]
    assert any(CREDENTIAL_MARK in t for t in written.values())


# ── the two doors ────────────────────────────────────────────────────────────────────────────────────────────────
CLOCK = VirtualTool("clock", "reads the time", {}, None)


@pytest.fixture
def server(monkeypatch):
    import core.tools as tools_mod

    async def one(server_module, name, agent, custom_tools):
        return CLOCK if name == "clock" else None

    monkeypatch.setattr(tools_mod, "resolve_one_tool", one)

    class _Session:
        async def send_ping(self):
            return None

        async def call_tool(self, name, args, **_kw):
            return types.SimpleNamespace(content=[types.SimpleNamespace(text=_long(HELD))], isError=False)

    return types.SimpleNamespace(agent_sessions={"fake": _Session()}, memory_store=None,
                                 tool_router={"clock": ("fake", "clock")})


async def test_a_workflow_steps_spill_keeps_a_workspace_secret_marked(fake_llm, held_header, small_spill, server):
    from _fakes.fake_llm import tool_call
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    from core.secret_scrub import CREDENTIAL_MARK

    fake_llm.script([tool_call("clock"), "Read it."])
    await S.seed_agents([S.make_agent(id="agent_f1474", name="Reader", tools=["clock"])])
    orch = S.make_orchestration(id="orch_f1474", entry_step_id="read", steps=[
        {"id": "read", "name": "Read", "type": "agent", "agent_id": "agent_f1474", "prompt_template": "use it",
         "output_key": "result", "max_turns": 3, "next_step_id": None}])
    await S.seed_orchestrations([orch])
    before = _vault_files()

    events = [e async for e in OrchestrationEngine(Orchestration.model_validate(orch), server).run(
        "go", run_id="run_f1474")]

    assert any(e.get("type") == "orchestration_complete" for e in events), [e.get("type") for e in events]
    written = _written_since(before)
    assert written, "the step's output did not reach the vault"
    assert not [p for p, t in written.items() if HELD in t]
    assert any(CREDENTIAL_MARK in t for t in written.values())


async def test_a_chat_turns_spill_keeps_a_workspace_secret_marked(fake_llm, held_header, small_spill):
    from core.react_engine import run_agent_step
    from core.secret_scrub import _MATCHER, CREDENTIAL_MARK

    schema = {"type": "function", "function": {"name": "whoami", "description": "w", "parameters": {
        "type": "object", "properties": {}}}}

    async def executor(name, args):
        return _long(HELD)

    _MATCHER.set(None)  # a chat turn's task: no workflow step loaded anything
    fake_llm.script([json.dumps({"tool": "whoami", "arguments": {}}), "Read it."])
    agent = S.make_agent(tools=["all"], skip_default_tools=True)
    before = _vault_files()

    events = [e async for e in run_agent_step(
        message="Who am I?", agent_id=agent["id"], session_id="s_f1474",
        server_module=types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}),
        agent_override=agent, tools_override=[schema], tool_executor=executor, max_turns=3)]

    assert any(e.get("type") == "tool_result" for e in events)
    written = _written_since(before)
    assert written, "the turn's output did not reach the vault"
    assert not [p for p, t in written.items() if HELD in t]
    assert any(CREDENTIAL_MARK in t for t in written.values())


async def test_a_later_turn_in_the_same_task_marks_a_secret_saved_since(fake_llm, held_header, small_spill):
    """Loaded at every turn, not once per task: a task that runs turn after turn (a worker's job loop) would keep
    the first turn's secrets and miss one saved since."""
    from core.react_engine import run_agent_step
    from core.store.resources import save_tool

    later = "sk-later-9Wc4Hn7Tz2Ry5"
    schema = {"type": "function", "function": {"name": "whoami", "description": "w", "parameters": {
        "type": "object", "properties": {}}}}
    agent = S.make_agent(tools=["all"], skip_default_tools=True)

    async def turn(said: str) -> dict:
        async def executor(name, args):
            return _long(said)

        fake_llm.script([json.dumps({"tool": "whoami", "arguments": {}}), "Read it."])
        before = _vault_files()
        _ = [e async for e in run_agent_step(
            message="Who am I?", agent_id=agent["id"], session_id="s_f1474b",
            server_module=types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}),
            agent_override=agent, tools_override=[schema], tool_executor=executor, max_turns=3)]
        return _written_since(before)

    first = await turn(HELD)
    await save_tool({"id": "tool_later", "name": "later", "type": "http", "url": "https://later.test/",
                     "method": "GET", "headers": {"X-Api-Key": later}})
    second = await turn(later)

    assert first and not [p for p, t in first.items() if HELD in t]
    assert second and not [p for p, t in second.items() if later in t]
