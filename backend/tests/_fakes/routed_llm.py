"""A model that answers by whose prompt it is — for turns whose calls interleave (helpers, SUB1a).

`FakeLLM` answers in the order calls arrive, which is no order at all once several
helpers run side by side. This one answers each prompt from its own script, chosen
by how the prompt begins (`otherwise` for the rest): every agent's transcript starts `User Request: <its
message>`, so a parent asked "PLAN IT" and a helper briefed "CHECK A" never share a
script, however their calls interleave.

    llm.on("PLAN IT", [tool_call("spawn_subtask", task="CHECK A", tools=[]), "all done"])
    llm.on("CHECK A", ["A is fine"])

Each script is consumed in order and its last reply repeats. A reply may be a string,
a `Streamed`, or a callable taking the call's kwargs and returning either (for a test
that acts mid-call). `calls` records every call's kwargs with the script it matched.
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any


class RoutedLLM:
    def __init__(self, default: str = "Task complete.") -> None:
        self.default = default
        self._scripts: list[tuple[str, list[Any]]] = []
        self._otherwise: list[Any] | None = None
        self.calls: list[dict[str, Any]] = []

    def on(self, request_starts: str, replies: list[Any]) -> "RoutedLLM":
        self._scripts.append((request_starts, list(replies)))
        return self

    def otherwise(self, replies: list[Any]) -> "RoutedLLM":
        """The script for a prompt no `on` matches (a workflow step's, say)."""
        self._otherwise = list(replies)
        return self

    def calls_for(self, request_starts: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c.get("_matched") == request_starts]

    async def __call__(self, *args: Any, **kwargs: Any):
        from _fakes.fake_llm import Streamed

        prompt = str(kwargs.get("prompt_msg") or (args[0] if args else ""))
        matched, script = None, None
        for starts, replies in self._scripts:
            if prompt.startswith(f"User Request: {starts}"):
                matched, script = starts, replies
                break
        self.calls.append({**kwargs, "_matched": matched})
        if script is None and self._otherwise:
            script = self._otherwise
        if script is None:
            reply: Any = self.default
        else:
            reply = script.pop(0) if len(script) > 1 else script[0]
        if callable(reply) and not isinstance(reply, Streamed):
            reply = reply(kwargs)
            if inspect.isawaitable(reply):
                reply = await reply
        await asyncio.sleep(0)
        if isinstance(reply, Streamed):
            from core import model_wait
            if model_wait.streaming_wanted():
                for chunk in reply.chunks:
                    model_wait.emit_delta(chunk)
                    await asyncio.sleep(reply.delay)
            return reply.text
        return reply


def install(monkeypatch, llm: RoutedLLM) -> RoutedLLM:
    """Put `llm` at the three places `fake_llm` patches (see conftest)."""
    import core.llm_providers as _llm
    import core.react_engine as _re
    import core.routes.agents as _agents

    monkeypatch.setattr(_llm, "generate_response", llm, raising=False)
    monkeypatch.setattr(_re, "llm_generate_response", llm, raising=False)
    monkeypatch.setattr(_agents, "llm_generate_response", llm, raising=False)
    return llm
