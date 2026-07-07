"""
Helpers to fake the ReAct/orchestration engine at the route boundary.

Route-contract tests patch ``core.routes.<module>.run_react_loop`` (and the
orchestration engine) with a canned async generator so the HTTP handler and SSE
serialization can be verified deterministically, independent of engine internals.
Separate engine-integration tests drive the *real* loop with the fake LLM.
"""
from __future__ import annotations

from typing import Any


def gen_from(events: list[dict]):
    """Return an async-generator *function* yielding the given events.

    Usage:
        monkeypatch.setattr(chat, "run_react_loop", gen_from([...]))
    The returned callable accepts and ignores any args (matching run_react_loop's
    ``(request, server_module)`` signature and the engine's ``run(...)``).
    """
    async def _gen(*args: Any, **kwargs: Any):
        for ev in events:
            yield ev
    return _gen


# ── common event builders (shapes match react_engine / orchestration engine) ──
def status(msg: str = "Processing...") -> dict:
    return {"type": "status", "message": msg}


def thinking(msg: str = "thinking...") -> dict:
    return {"type": "thinking", "message": msg}


def tool_execution(name: str, args: dict | None = None) -> dict:
    return {"type": "tool_execution", "tool_name": name, "args": args or {}}


def tool_result(name: str, preview: str = "ok") -> dict:
    return {"type": "tool_result", "tool_name": name, "preview": preview}


def final(response: str = "Done.", intent: str = "chat",
          data: Any = None, tool_name: str | None = None, **extra: Any) -> dict:
    ev = {"type": "final", "response": response, "intent": intent,
          "data": data, "tool_name": tool_name}
    ev.update(extra)
    return ev


def error(msg: str = "boom") -> dict:
    return {"type": "error", "message": msg}


# orchestration lifecycle
def orch_start(run_id: str = "run_1", name: str = "Test", orch_id: str = "orch_1") -> dict:
    return {"type": "orchestration_start", "run_id": run_id,
            "orchestration_name": name, "orchestration_id": orch_id}


def step_start(step_id: str = "s1", name: str = "Step 1", step_type: str = "llm") -> dict:
    return {"type": "step_start", "orch_step_id": step_id, "step_name": name,
            "step_type": step_type}


def step_complete(step_id: str = "s1", name: str = "Step 1", duration: float = 0.01) -> dict:
    return {"type": "step_complete", "orch_step_id": step_id, "step_name": name,
            "duration_seconds": duration}


def orch_complete(run_id: str = "run_1", status_str: str = "completed") -> dict:
    return {"type": "orchestration_complete", "run_id": run_id, "status": status_str}


def human_input_required(step_id: str = "s1", prompt: str = "Approve?",
                         fields: list | None = None) -> dict:
    return {"type": "human_input_required", "orch_step_id": step_id, "prompt": prompt,
            "fields": fields or [], "agent_context": ""}


def fake_engine(events: list[dict]):
    """Return a drop-in ``OrchestrationEngine`` class whose run/resume methods
    yield ``events``. Patch it in via
    ``monkeypatch.setattr(engine_mod, "OrchestrationEngine", fake_engine([...]))``.
    """
    class _FakeEngine:
        def __init__(self, orch=None, server_module=None):
            self.orch = orch

        async def run(self, message, run_id, **kwargs):
            for ev in events:
                yield ev

        @classmethod
        async def resume(cls, run_id, human_response, server_module):
            for ev in events:
                yield ev

        @classmethod
        async def resume_failed(cls, run_id, server_module):
            for ev in events:
                yield ev

    return _FakeEngine
