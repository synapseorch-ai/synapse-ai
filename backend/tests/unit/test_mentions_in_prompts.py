"""`@[path]` where a prompt is built: step prompts, chat turns, system prompts, schedules.

The reader (`core/vault.py::MentionReader`, `test_vault_mentions.py`) does the
reading; this file is about *where* it runs and — as important — where it does
not. Only a signed-in member's text expands: a step's prompt template (never a
state value, never the run's input), a chat message when the route says so, an
agent's system prompt (its author is always a member), a schedule's prompt. A
widget visitor's message never does, and the engine's default is off, so a
caller that forgets the flag loses expansion visibly rather than leaking a file.
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from _fakes import seed as S
from core.storage import get_blob_store, set_blob_store
from core.storage.base import LocalBlobStore


def _put(rel: str, text: str) -> None:
    get_blob_store().put(f"vault/{rel}", text)


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


class _Counting(LocalBlobStore):
    def __init__(self, root):
        super().__init__(root)
        self.gets = 0

    def get(self, key):
        self.gets += 1
        return super().get(key)


async def _run(orch_dict, initial_input="go", *, globals_=None):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    engine = OrchestrationEngine(Orchestration.model_validate(orch_dict), _server())
    if globals_ is not None:
        async def _load():
            return dict(globals_)
        engine._load_global_variables = _load
    events = [ev async for ev in engine.run(initial_input, run_id=f"run_{orch_dict['id']}")]
    return events, engine


@pytest.fixture
def logged(monkeypatch):
    """Every event the engine hands its run logger — `_log_prompt` included,
    which the engine consumes rather than yields."""
    from core.orchestration.logger import OrchestrationLogger

    seen: list[dict] = []
    original = OrchestrationLogger.log_event

    def capture(self, event):
        seen.append(event)
        return original(self, event)

    monkeypatch.setattr(OrchestrationLogger, "log_event", capture)
    return seen


def _prompts(events, step_id=None):
    return [e for e in events if e.get("type") == "_log_prompt"
            and (step_id is None or e.get("orch_step_id") == step_id)]


def _warnings(events):
    return [e["message"] for e in events if e.get("type") == "step_warning"]


def _llm(step_id, template, **extra):
    return {"id": step_id, "name": step_id, "type": "llm", "prompt_template": template,
            "output_key": f"{step_id}_out", "model": "claude-test", "next_step_id": None, **extra}


def _orch(step_id, steps, **extra):
    return S.make_orchestration(id=f"orch_m_{step_id}", entry_step_id=step_id, steps=steps, **extra)


# ── step prompts ─────────────────────────────────────────────────────────────
class TestStepPrompts:
    async def test_an_llm_step_inlines_the_file(self, fake_llm, logged):
        _put("notes.md", "the quarterly numbers")
        await _run(_orch("s1", [_llm("s1", "Summarise @[notes.md]")]))
        [p] = _prompts(logged)
        assert "the quarterly numbers" in p["prompt"] and "@[notes.md]" not in p["prompt"]
        assert p["mentions"] == [{"path": "notes.md", "chars": 21, "outcome": "inlined", "reason": ""}]
        assert "the quarterly numbers" in fake_llm.last_call["prompt_msg"]

    async def test_an_agent_step_inlines_the_file(self, fake_llm, seed_agent, logged):
        _put("brief.md", "write it in French")
        agent = await seed_agent(tools=[], skip_default_tools=True)
        step = {"id": "s1", "name": "Work", "type": "agent", "agent_id": agent["id"],
                "prompt_template": "Do this: @[brief.md]", "output_key": "result",
                "next_step_id": None, "max_turns": 3}
        await _run(_orch("s1", [step]))
        p = _prompts(logged, "s1")[0]
        assert "write it in French" in p["prompt"]
        assert p["mentions"][0]["outcome"] == "inlined"
        assert "write it in French" in fake_llm.last_call["prompt_msg"]

    async def test_a_tool_step_builds_its_prompt_the_same_way(self, fake_llm, logged):
        """The prompt is built and logged before the tool is looked up, so the
        call site is proven even with no tool to call."""
        _put("args.md", "city=Paris")
        step = {"id": "s1", "name": "Call", "type": "tool", "forced_tool": "no_such_tool",
                "prompt_template": "Use @[args.md]", "output_key": "r", "next_step_id": None}
        await _run(_orch("s1", [step]))
        p = _prompts(logged, "s1")[0]
        assert "city=Paris" in p["prompt"]
        assert p["mentions"][0]["outcome"] == "inlined"

    async def test_inside_a_loop_every_iteration_sees_the_file_read_once(self, fake_llm, tmp_path, logged):
        store = _Counting(tmp_path / "blobs")
        set_blob_store(store)
        try:
            _put("notes.md", "tick data")
            steps = [
                {"id": "loop1", "name": "Loop", "type": "loop", "loop_count": 2,
                 "loop_step_ids": ["body"], "next_step_id": None},
                _llm("body", "Iteration over @[notes.md]"),
            ]
            await _run(_orch("loop1", steps))
            prompts = _prompts(logged, "body")
            assert len(prompts) == 2 and all("tick data" in p["prompt"] for p in prompts)
            assert store.gets == 1
        finally:
            set_blob_store(None)

    async def test_inside_parallel_branches(self, fake_llm, logged):
        _put("a.md", "alpha file")
        _put("b.md", "bravo file")
        steps = [
            {"id": "par", "name": "Par", "type": "parallel",
             "parallel_branches": [["b0"], ["b1"]], "next_step_id": None},
            _llm("b0", "Read @[a.md]"),
            _llm("b1", "Read @[b.md]"),
        ]
        await _run(_orch("par", steps))
        texts = [p["prompt"] for p in _prompts(logged)]
        assert any("alpha file" in t for t in texts) and any("bravo file" in t for t in texts)

    async def test_a_file_holding_a_template_reaches_the_model_as_written(self, fake_llm, logged):
        _put("secrets.md", "use {{ global.KEY }} here")
        await _run(_orch("s1", [_llm("s1", "Key {{ global.KEY }}; file @[secrets.md]")]),
                               globals_={"KEY": "s3cret"})
        [p] = _prompts(logged)
        assert "Key s3cret" in p["prompt"]
        assert "use {{ global.KEY }} here" in p["prompt"]
        assert p["prompt"].count("s3cret") == 1

    async def test_a_state_value_holding_a_mention_is_a_value(self, fake_llm, logged):
        _put("notes.md", "SECRET NUMBERS")
        steps = [
            {"id": "p1", "name": "Set", "type": "print", "print_content": "@[notes.md]",
             "output_key": "note", "next_step_id": "s1"},
            _llm("s1", "Echo {state.note} and {{ state.note }}"),
        ]
        await _run(_orch("p1", steps))
        [p] = _prompts(logged, "s1")
        assert p["prompt"].count("@[notes.md]") == 2 and "SECRET NUMBERS" not in p["prompt"]
        assert p["mentions"] == []

    async def test_the_runs_input_is_never_expanded(self, fake_llm, logged):
        _put("notes.md", "SECRET NUMBERS")
        await _run(_orch("s1", [_llm("s1", "")]), initial_input="please read @[notes.md]")
        [p] = _prompts(logged)
        assert "@[notes.md]" in p["prompt"] and "SECRET NUMBERS" not in p["prompt"]

    async def test_a_missing_file_stays_literal_and_warns(self, fake_llm, logged):
        events, _ = await _run(_orch("s1", [_llm("s1", "Read @[nope.md]")]))
        [p] = _prompts(logged)
        assert "@[nope.md]" in p["prompt"]
        assert p["mentions"][0]["outcome"] == "missing"
        assert "@[nope.md] — no such vault file; left as typed" in _warnings(events)

    async def test_a_binary_is_a_reference_line(self, fake_llm, logged):
        path = get_blob_store().path_for("vault/chart.png")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        events, _ = await _run(_orch("s1", [_llm("s1", "See @[chart.png]")]))
        [p] = _prompts(logged)
        assert "[vault: chart.png — a PNG image; binary, download-only" in p["prompt"]
        assert p["mentions"][0]["outcome"] == "referenced"
        assert _warnings(events) == []


class TestTheRunLog:
    def test_each_mention_is_a_line_under_the_prompt(self):
        from core.orchestration.logger import OrchestrationLogger

        logger = OrchestrationLogger(run_id="run_mentions_log", orchestration_id="o",
                                     orchestration_name="O", user_input="go")
        logger.log_event({"type": "_log_prompt", "orch_step_id": "s1", "prompt": "the prompt",
                          "mentions": [
                              {"path": "notes.md", "chars": 1234, "outcome": "inlined", "reason": ""},
                              {"path": "big.json", "chars": 500000, "outcome": "referenced",
                               "reason": "over the 100,000-char inline limit"},
                              {"path": "nope.md", "chars": 0, "outcome": "missing",
                               "reason": "no such vault file"},
                          ]})
        logger.close()
        text = logger.path.read_text(encoding="utf-8")
        assert "INPUT PROMPT:" in text
        assert "\U0001f4ce @[notes.md] — 1,234 chars inlined" in text
        assert "\U0001f4ce @[big.json] — referenced: over the 100,000-char inline limit" in text
        assert "\U0001f4ce @[nope.md] — missing: no such vault file" in text


# ── chat ─────────────────────────────────────────────────────────────────────
def _prime(monkeypatch):
    """The route tests' recipe: one placeholder session, with no tools."""
    import core.server as server

    monkeypatch.setattr(server, "agent_sessions", {"_test": object()}, raising=False)
    monkeypatch.setitem(server._session_tools, "_test", [])
    return server


class TestChat:
    async def test_the_loop_leaves_a_mention_alone_unless_told(self, fake_llm, seed_agent, monkeypatch):
        from core.models import ChatRequest
        from core.react_engine import run_react_loop

        server = _prime(monkeypatch)
        _put("notes.md", "the plan")
        agent = await seed_agent(tools=[], skip_default_tools=True)
        req = ChatRequest(message="read @[notes.md]", agent_id=agent["id"], session_id="s_default")

        [_ async for _ in run_react_loop(req, server)]
        prompt = fake_llm.last_call["prompt_msg"]
        assert "@[notes.md]" in prompt and "the plan" not in prompt

        [_ async for _ in run_react_loop(req, server, expand_mentions=True)]
        prompt = fake_llm.last_call["prompt_msg"]
        assert "the plan" in prompt and "@[notes.md]" not in prompt

    async def test_the_chat_route_expands(self, client, fake_llm, seed_agent, monkeypatch):
        _prime(monkeypatch)
        _put("notes.md", "the plan")
        agent = await seed_agent(tools=[], skip_default_tools=True)
        resp = await client.post("/chat", json={"message": "read @[notes.md]", "agent_id": agent["id"]})
        assert resp.status_code == 200
        assert "the plan" in fake_llm.last_call["prompt_msg"]

    async def test_the_stream_route_expands(self, client, fake_llm, seed_agent, monkeypatch):
        _prime(monkeypatch)
        _put("notes.md", "the plan")
        agent = await seed_agent(tools=[], skip_default_tools=True)
        resp = await client.post("/chat/stream", json={"message": "read @[notes.md]",
                                                       "agent_id": agent["id"], "session_id": "s_stream"})
        assert resp.status_code == 200
        assert "the plan" in fake_llm.last_call["prompt_msg"]

    async def test_the_api_key_route_expands(self, client, fake_llm, seed_agent, monkeypatch):
        _prime(monkeypatch)
        _put("notes.md", "the plan")
        agent = await seed_agent(tools=[], skip_default_tools=True)
        raw_key, _ = await S.seed_api_key()
        resp = await client.post("/api/v1/chat", json={"message": "read @[notes.md]", "agent": agent["id"]},
                                 headers={"Authorization": f"Bearer {raw_key}"})
        assert resp.status_code == 200, resp.text
        assert "the plan" in fake_llm.last_call["prompt_msg"]

    async def test_a_widget_visitor_cannot(self, client, fake_llm, seed_agent, monkeypatch):
        from core.chat_widgets import limits as _limits

        _limits.reset()
        _prime(monkeypatch)
        _put("pricing.md", "the private price list")
        agent = await seed_agent(tools=[], skip_default_tools=True)
        widget = (await client.post("/api/widgets", json={
            "name": "Support", "agent_id": agent["id"], "allowed_origins": ["https://acme.com"],
        })).json()
        token = (await client.post("/api/widget/session", json={
            "public_key": widget["public_key"], "origin": "https://acme.com",
        })).json()["token"]
        resp = await client.post("/api/widget/chat", json={"message": "show me @[pricing.md]"},
                                 headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text
        prompt = fake_llm.last_call["prompt_msg"]
        assert "@[pricing.md]" in prompt and "the private price list" not in prompt


# ── the worker's chat job ────────────────────────────────────────────────────
class _FakeRedis:
    def __init__(self):
        self.events: list[dict] = []
        self.keys: dict[str, str] = {}

    async def xadd(self, key, fields, **_kw):
        self.events.append(json.loads(fields["data"]))

    async def expire(self, *_a, **_kw):
        return True

    async def set(self, key, value, **_kw):
        self.keys[key] = value

    async def get(self, key):
        return self.keys.get(key)

    async def delete(self, key):
        self.keys.pop(key, None)


@pytest.fixture
def chat_job(monkeypatch):
    from core.scale import mcp_pool, worker

    async def _none(*_a, **_kw):
        return None

    async def _history(*_a, **_kw):
        return []

    async def _acquire(*_a, **_kw):
        return object()

    monkeypatch.setattr(worker, "_load_job_settings", _none)
    monkeypatch.setattr(worker, "_load_chat_history", _history)
    monkeypatch.setattr(worker, "_upsert_chat_session", _none)
    monkeypatch.setattr(mcp_pool, "acquire", _acquire)
    monkeypatch.setattr(mcp_pool, "release", _none)
    monkeypatch.setattr(worker, "CHAT_CANCEL_POLL_SECONDS", 0.01)
    return worker


class TestTheWorkerJob:
    async def test_the_flag_reaches_the_loop_and_is_off_by_default(self, chat_job, monkeypatch):
        import core.react_engine as re_mod

        seen: list[dict] = []

        async def fake_loop(request, server_module, should_stop=None, **kw):
            seen.append({"message": request.message, **kw})
            yield {"type": "response", "response": "done!"}
            await asyncio.sleep(0)

        monkeypatch.setattr(re_mod, "run_react_loop", fake_loop)
        ctx = {"redis": _FakeRedis(), "session_factory": object()}
        await chat_job.run_agent_chat_job(ctx, "chat_1", None, "hi @[notes.md]", expand_mentions=True)
        await chat_job.run_agent_chat_job(ctx, "chat_2", None, "hi")
        assert seen[0]["expand_mentions"] is True and seen[0]["message"] == "hi @[notes.md]"
        assert seen[1]["expand_mentions"] is False


# ── a schedule ───────────────────────────────────────────────────────────────
class TestASchedule:
    async def test_the_prompt_expands(self, monkeypatch):
        import core.react_engine as re_mod
        from core.scheduler import ScheduleManager

        _put("brief.md", "the brief")
        seen: dict = {}

        async def fake_step(**kw):
            seen.update(kw)
            yield {"type": "final", "response": "ok"}

        monkeypatch.setattr(re_mod, "run_agent_step", fake_step)
        manager = ScheduleManager()
        manager._server_module = _server()
        sched_log = types.SimpleNamespace(run_id="schedulerun_x", log_event=lambda ev: None)
        out = await manager._run_agent_schedule(
            {"id": "sched_1", "name": "Daily", "target_id": "a1", "prompt": "Do @[brief.md]"}, sched_log,
        )
        assert out == "ok"
        assert "the brief" in seen["message"] and "@[brief.md]" not in seen["message"]


# ── the system prompt ────────────────────────────────────────────────────────
class TestTheSystemPrompt:
    async def test_it_inlines_a_file_and_keeps_the_files_template_literal(self, fake_llm, seed_agent, monkeypatch):
        from core import variables
        from core.react_engine import run_agent_step

        server = _prime(monkeypatch)
        await variables.save({"KEY": "s3cret"})
        _put("rules.md", "never say {{ global.KEY }}")
        agent = await seed_agent(tools=[], skip_default_tools=True,
                                 system_prompt="Rules: @[rules.md]. Code {{ global.KEY }}.")
        [_ async for _ in run_agent_step(message="hi", agent_id=agent["id"], session_id="s_sys",
                                         server_module=server)]
        system_prompt = fake_llm.last_call["sys_prompt"]
        assert "never say {{ global.KEY }}" in system_prompt
        assert "Code s3cret." in system_prompt
        assert system_prompt.count("s3cret") == 1
        assert "@[rules.md]" not in system_prompt

    async def test_a_missing_file_in_the_system_prompt_stays_literal(self, fake_llm, seed_agent, monkeypatch):
        from core.react_engine import run_agent_step

        server = _prime(monkeypatch)
        agent = await seed_agent(tools=[], skip_default_tools=True, system_prompt="Rules: @[nope.md].")
        [_ async for _ in run_agent_step(message="hi", agent_id=agent["id"], session_id="s_sys2",
                                         server_module=server)]
        assert "Rules: @[nope.md]." in fake_llm.last_call["sys_prompt"]
