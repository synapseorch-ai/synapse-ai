"""
Scriptable fake LLM — the drop-in replacement for the real LLM call.

The single production chokepoint is ``core.llm_providers.generate_response``
(backend/core/llm_providers.py). It is an async function that returns a **plain
string** (the assistant text, which the ReAct engine then parses for tool-call
JSON). This fake mirrors that contract exactly, so the ReAct loop and the
orchestration engine behave the same as in production — no network, no API keys.

Scripting
---------
``fake.script(["...", "..."])`` queues canned responses returned in FIFO order.
This lets a single test drive a multi-turn ReAct loop or a multi-step
orchestration, e.g. ``["<tool-call-json>", "Final answer."]``. When the script
is exhausted (or never set) the ``default`` text is returned.

Delay profiles (the "5–90s" requirement)
-----------------------------------------
The delay is read from the environment on every call so a whole suite can be
switched between profiles without touching test code:

  SYNAPSE_FAKE_LLM_DELAY_MIN / _MAX  uniform sleep bounds in seconds (default 0/0)
  SYNAPSE_FAKE_LLM_DELAY_PROB        probability a given call is "slow" (default 0)

The deploy-gating suite leaves the defaults (instant, so the gate stays fast).
The stress suite sets MIN=5 MAX=90 PROB=0.3 to simulate realistic LLM latency
where *some* calls are slow. Set PROB=1 to make every call slow.
"""
from __future__ import annotations

import asyncio
import os
import random
import threading
from typing import Any


class FakeLLM:
    """An async callable that stands in for ``generate_response``."""

    def __init__(self, default: str = "Task complete.") -> None:
        self._lock = threading.Lock()
        self._script: list[str] = []
        self.default = default
        #: Every invocation is recorded here (kwargs, or {"_args": ...}) so
        #: tests can assert on model/session/tool routing.
        self.calls: list[dict[str, Any]] = []

    # ── scripting ────────────────────────────────────────────────────────────
    def script(self, responses: list[str]) -> "FakeLLM":
        """Queue responses to be returned in order. Returns self for chaining."""
        with self._lock:
            self._script = list(responses)
        return self

    def set_default(self, text: str) -> "FakeLLM":
        self.default = text
        return self

    def reset(self) -> None:
        with self._lock:
            self._script = []
            self.calls = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def last_call(self) -> dict[str, Any] | None:
        return self.calls[-1] if self.calls else None

    # ── delay profile ────────────────────────────────────────────────────────
    @staticmethod
    def _delay_params() -> tuple[float, float, float]:
        def _f(name: str) -> float:
            try:
                return float(os.getenv(name, "0") or 0)
            except ValueError:
                return 0.0
        return (
            _f("SYNAPSE_FAKE_LLM_DELAY_MIN"),
            _f("SYNAPSE_FAKE_LLM_DELAY_MAX"),
            _f("SYNAPSE_FAKE_LLM_DELAY_PROB"),
        )

    async def _maybe_delay(self) -> None:
        dmin, dmax, prob = self._delay_params()
        if dmax > 0 and prob > 0 and random.random() < prob:
            lo = min(dmin, dmax)
            await asyncio.sleep(random.uniform(lo, dmax))

    # ── the call ─────────────────────────────────────────────────────────────
    async def __call__(self, *args: Any, **kwargs: Any) -> str:
        # generate_response is normally called with keyword args
        # (prompt_msg=, sys_prompt=, mode=, current_model=, ...).
        self.calls.append(dict(kwargs) if kwargs else {"_args": args})
        await self._maybe_delay()
        with self._lock:
            if self._script:
                return self._script.pop(0)
        return self.default


def tool_call(name: str, **args: Any) -> str:
    """Build a tool-call JSON string in the shape the ReAct engine parses.

    Mirrors the format real models/adapters produce: a bare JSON object with a
    ``tool`` name and an ``arguments`` object (the engine reads ``arguments``).
    """
    import json
    return json.dumps({"tool": name, "arguments": args})
