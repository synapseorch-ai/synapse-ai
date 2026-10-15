"""The shapes a tool call written as text comes in — one reader for the engine and the stream.

The engine's own shape is `{"tool": name, "arguments": {...}}`: what its prompt asks a model to write, and what it
turns a provider's native calls into. A model sometimes writes another one (F537): gpt-4.1-mini wrote OpenAI's
parallel-call pseudo-format, which OpenAI's own tool runner uses internally, as plain text —

    {"tool_uses": [{"recipient_name": "functions.lookup_product", "parameters": {"name": "hubspot"}}, …]}

— twice, in 1 of 12 TEAM0 runs. Nothing read it as a call, so it became the agent's answer, and the stream showed it.
`functions.` is OpenAI's namespace, not part of the name; `multi_tool_use.parallel` is the same block wrapped.

`calls_in` is what `react_engine._scan_tool_calls` and `answer_filter` both ask, so a shape one of them reads is a
shape the other holds back. A reply that is only JSON objects names no call (`bare_objects`); the engine decides,
from the tools it offered, whether those are a call's arguments (F551).
"""
from __future__ import annotations

from typing import Any

#: OpenAI's namespace for a function in its pseudo-format.
_NAMESPACE = "functions."
#: The pseudo-format's wrapper for parallel calls.
_PARALLEL = "multi_tool_use.parallel"


def is_engine_call(obj: Any) -> bool:
    """`{"tool": name, …}` with a name: the engine's own shape."""
    return isinstance(obj, dict) and isinstance(obj.get("tool"), str) and bool(obj["tool"].strip())


def _recipient(use: Any) -> list[dict] | None:
    """One `{"recipient_name", "parameters"}` entry as engine calls; None when it is not one."""
    if not isinstance(use, dict):
        return None
    name = use.get("recipient_name")
    if not isinstance(name, str) or not name.strip():
        return None
    params = use.get("parameters", {})
    if not isinstance(params, dict):
        return None
    name = name.strip()
    if name == _PARALLEL:
        return _uses(params.get("tool_uses"))
    if name.startswith(_NAMESPACE):
        name = name[len(_NAMESPACE):]
    return [{"tool": name, "arguments": params}] if name else None


def _uses(uses: Any) -> list[dict] | None:
    if not isinstance(uses, list) or not uses:
        return None
    calls: list[dict] = []
    for use in uses:
        got = _recipient(use)
        if got is None:
            return None
        calls.extend(got)
    return calls


def calls_in(obj: Any) -> list[dict]:
    """The tool calls `obj` is, in the engine's shape; [] when it is not a call."""
    if is_engine_call(obj):
        return [obj]
    if not isinstance(obj, dict):
        return []
    if "tool_uses" in obj:
        return _uses(obj["tool_uses"]) or []
    if "recipient_name" in obj:
        return _recipient(obj) or []
    return []


def leading_objects(text: str) -> tuple[list[dict], str]:
    """The JSON objects `text` opens with, none of them a call, and what follows them; ([], text) otherwise.
    For F558: gpt-4.1-mini opened a reply with one call's arguments and went on to say the call was done."""
    import json

    decoder = json.JSONDecoder()
    rest, found = text.strip(), []
    while rest.startswith("{"):
        try:
            obj, end = decoder.raw_decode(rest)
        except ValueError:
            break
        if not isinstance(obj, dict) or not obj or calls_in(obj):
            return [], text
        found.append(obj)
        rest = rest[end:].lstrip()
    return (found, rest) if found else ([], text)


def bare_objects(text: str) -> list[dict]:
    """The JSON objects `text` is made of, when it is nothing but JSON objects and none of them a call; [] otherwise.
    For F551: gpt-4.1-mini answered with its calls' arguments alone — `{"order":"HP-2041"}` twice. Whether such a
    reply is arguments (not an answer) or a structured answer is the engine's to say, from the tools it offered."""
    found, rest = leading_objects(text)
    return found if not rest else []


def parallel_calls(call: Any) -> list[dict]:
    """The calls an engine-shaped call of OpenAI's `multi_tool_use.parallel` holds — `{"tool":
    "multi_tool_use.parallel", "arguments": {"tool_uses": [...]}}`, as gpt-4.1-mini wrote it (F558); [] for any other
    call. Whether to run them in its place is the engine's to say: an agent may offer a tool by that name."""
    if not is_engine_call(call) or call.get("tool") != _PARALLEL:
        return []
    return calls_in(call.get("arguments"))


def is_pseudo_format(obj: Any) -> bool:
    """Whether `obj` is a call in OpenAI's pseudo-format rather than the engine's own."""
    return not is_engine_call(obj) and bool(calls_in(obj))
