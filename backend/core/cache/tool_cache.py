"""
Deterministic tool-result memoization.

A tool is eligible when it says it is: read-only, idempotent, and not reaching
an open world whose answer could have changed underneath. Those are the MCP
annotations a server declares, so a tool that knows its own behaviour no
longer has to be added to a list here by hand.

DETERMINISTIC_TOOLS remains for tools that declare nothing — third-party MCP
servers, mostly — and is the only way such a tool becomes cacheable. It is
also where a *scope* comes from: an annotation cannot say "this answer is per
user", which `personal_details` needs.

Scope rules:
- "session": key includes the session_id (e.g. personal_details, user-bound configs)
- "global":  key includes only tool_name + args (e.g. code_search, pdf_parser)
"""
from typing import Any, Optional

from core.cache import store

NAMESPACE = "tool_results"

# Maps tool name → scope. Listed conservatively: only tools whose output is a
# pure function of their args (and optionally the per-user session).
DETERMINISTIC_TOOLS: dict[str, str] = {
    "code_search":      "global",
    "pdf_parser":       "global",
    "xlsx_parser":      "global",
    "code_indexer":     "global",
    "collect_data":     "global",
    "personal_details": "session",
}


def is_cacheable(tool_name: str, tool=None) -> bool:
    """Whether a previous result may be served for this tool.

    `tool` is the declaration (an MCP `Tool` or a `VirtualTool`); when it
    carries annotations they decide, and the override list is not consulted.
    Without it — or without annotations — the answer is the hand-kept list,
    which is what every third-party tool falls back to.
    """
    if tool is not None:
        from core.tools import tool_hints
        hints = tool_hints(tool)
        if hints.declared:
            return hints.cacheable
    return tool_name in DETERMINISTIC_TOOLS


def _key(tool_name: str, tool_args: dict, session_id: Optional[str]) -> str:
    scope = DETERMINISTIC_TOOLS.get(tool_name, "global")
    sid = session_id or "_global_" if scope == "session" else "_global_"
    return store.make_key("tool", tool_name, sid, tool_args or {})


def get(tool_name: str, tool_args: dict, session_id: Optional[str] = None) -> Optional[Any]:
    """Return the cached tool result, or None if there's no live entry."""
    if not is_cacheable(tool_name):
        return None
    entry = store.get(NAMESPACE, _key(tool_name, tool_args, session_id))
    if entry is None:
        return None
    return entry.get("value")


def set(
    tool_name: str,
    tool_args: dict,
    result: Any,
    ttl_seconds: int = 3600,
    session_id: Optional[str] = None,
) -> None:
    if not is_cacheable(tool_name):
        return
    store.set(
        NAMESPACE,
        _key(tool_name, tool_args, session_id),
        result,
        ttl_seconds=ttl_seconds,
        meta={"tool_name": tool_name, "scope": DETERMINISTIC_TOOLS.get(tool_name)},
    )


def clear_tool(tool_name: str) -> int:
    """Helper for manual invalidation (e.g. after the user re-indexes their codebase)."""
    return store.clear_namespace(f"{NAMESPACE}/{tool_name}")
