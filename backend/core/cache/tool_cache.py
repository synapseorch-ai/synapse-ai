"""
Deterministic tool-result memoization.

Only tools in DETERMINISTIC_TOOLS are eligible — anything that reads live state
(bash, sql_agent, web_scraper, sandbox) is bypassed because cached results
would silently mask reality.

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
    "time":             "global",
    "code_indexer":     "global",
    "collect_data":     "global",
    "personal_details": "session",
}


def is_cacheable(tool_name: str) -> bool:
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
