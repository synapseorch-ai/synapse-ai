"""Every event type the engine yields is either one a client sees or the engine's own — F854 (BF11, D311).

`core.events_public.public_event` drops a type it does not know, so a new event would vanish from every client
until someone listed it; and before F854 an event nobody listed was passed to the chat stream as it was, which is
how `_log_llm_call` reached the browser. This reads every `{"type": "<literal>", …}` in `core/` and makes each say
which it is. A `"type"` that is not an event (a JSON schema's, a content part's, an agent's) is named below once.
"""
from __future__ import annotations

import ast
from pathlib import Path

from core.events_public import (
    DOOR_FRAME_TYPES,
    INTERNAL_EVENT_TYPES,
    PUBLIC_EVENT_TYPES,
    public_event,
)

CORE = Path(__file__).resolve().parents[2] / "core"

#: `"type"` keys in `core/` that are not events.
NOT_EVENTS = frozenset({
    # JSON schema types.
    "array", "boolean", "integer", "number", "object", "string", "function",
    # Model request parts and options.
    "text", "image", "image_url", "base64", "adaptive", "enabled", "ephemeral", "default",
    # OpenAI's structured answer (`response_format`), asked of an llm step that declares its answer fields (F1553).
    "json_schema",
    # Agent, tool and vault kinds; a native-builder step; voice relay frames; ASGI messages; a header name.
    "builder", "conversational", "orchestrator", "mcp_native", "file", "folder", "end",
    "http.response.start", "http.response.body", "SHARED_KEY",
    # A human field's type: the person's turn in the browser, carried on `human_input_required` (BRW1, F1099);
    # a choice on a form the builder's code asks, such as a name already here (F1395).
    "browser_turn", "choice",
    # The engine's input to the browser component's viewer `/input` (mouse, key, text, scroll, nav): an HTTP body to
    # the component, never yielded to a client (BRW1, F1142's "Record a task" opens at its start address).
    "nav",
    # What a product tool's preview will create or delete (`will_create` / `will_delete`: {"type": <its kind>, …}),
    # the shape every preview shares and an MCP client reads: a Python tool (F1198), a knowledge base (F1200, F1218).
    "tool", "knowledge_base",
})


def _literal_types() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in CORE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and key.value == "type"
                        and isinstance(value, ast.Constant) and isinstance(value.value, str)):
                    found.setdefault(value.value, set()).add(str(path.relative_to(CORE)))
    return found


def test_each_type_the_engine_yields_says_whether_a_client_sees_it():
    unsaid = {kind: sorted(where) for kind, where in _literal_types().items()
              if kind not in PUBLIC_EVENT_TYPES | INTERNAL_EVENT_TYPES | DOOR_FRAME_TYPES | NOT_EVENTS}
    assert unsaid == {}, ("Add each to core/events_public.py — PUBLIC_EVENT_TYPES if a client may see it, "
                          "INTERNAL_EVENT_TYPES if only the engine reads it")


def test_the_lists_do_not_overlap_and_internal_ones_never_pass():
    assert not PUBLIC_EVENT_TYPES & INTERNAL_EVENT_TYPES
    for kind in INTERNAL_EVENT_TYPES:
        assert public_event({"type": kind, "system_prompt": "x"}) is None
    assert public_event({"type": "a_type_nobody_listed"}) is None
    assert public_event({"type": "queued"}, also=frozenset({"queued"})) == {"type": "queued"}


def test_a_finished_runs_state_leaves_without_the_engines_own_keys():
    state = {"answer": "42", "_exec_memory_s1": {"inputs": "…"}, "_caller": {"user": "u"}, "_mine": 1}
    done = public_event({"type": "orchestration_complete", "final_state": state})
    final = public_event({"type": "final", "data": {"run_id": "r", "shared_state": state}})
    assert done["final_state"] == {"answer": "42", "_mine": 1}
    assert final["data"]["shared_state"] == {"answer": "42", "_mine": 1}
    assert state["_caller"] == {"user": "u"}, "the run's own state is untouched"
