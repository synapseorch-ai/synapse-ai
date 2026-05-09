"""
LLM Usage & Cost Tracker
------------------------
Persists every LLM call's token counts, context size, and estimated cost
to data/usage_logs.json using the pricing table in data/model_pricing.json.

Actual token counts are sourced from API response objects where available
(OpenAI, Anthropic, Gemini all surface usage metadata). Bedrock and Ollama
fall back to a character-count heuristic (len / 4).
"""
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.config import DATA_DIR

# -------------------------------------------------------------
# Paths
# -------------------------------------------------------------
USAGE_LOGS_FILE = os.path.join(DATA_DIR, "usage_logs.json")
PRICING_FILE = os.path.join(DATA_DIR, "model_pricing.json")

_lock = threading.Lock()

# -------------------------------------------------------------
# Pricing lookup
# -------------------------------------------------------------

def _load_pricing() -> dict:
    """Load the flat model_pricing.json. Returns {} on any error."""
    try:
        if os.path.exists(PRICING_FILE):
            with open(PRICING_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"DEBUG usage_tracker: could not load pricing: {e}")
    return {}


def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return estimated USD cost for a call. Returns 0.0 for unknown models."""
    pricing = _load_pricing()

    # Try the exact model string, then progressively shorter prefixes
    # so e.g. "claude-sonnet-4-20250514" matches its exact key.
    entry = pricing.get(model)
    if entry is None:
        # Fuzzy prefix match -- pick the longest prefix that fits
        for key in sorted(pricing.keys(), key=len, reverse=True):
            if model.startswith(key) or key.startswith(model.split("-")[0]):
                entry = pricing[key]
                break

    if not entry:
        return 0.0

    input_cost = (input_tokens / 1_000_000) * entry.get("input_per_1m", 0.0)
    output_cost = (output_tokens / 1_000_000) * entry.get("output_per_1m", 0.0)
    return round(input_cost + output_cost, 8)


def get_pricing_table() -> dict:
    """Return the raw pricing table for the API."""
    return _load_pricing()


def save_pricing_table(table: dict) -> None:
    """Overwrite model_pricing.json with an updated table."""
    with _lock:
        with open(PRICING_FILE, "w", encoding="utf-8") as f:
            json.dump(table, f, indent=4)
    print(f"DEBUG usage_tracker: pricing table updated ({len(table)} entries)", flush=True)


# -------------------------------------------------------------
# Token estimation fallback
# -------------------------------------------------------------

def estimate_tokens_from_text(text: str) -> int:
    """Rough heuristic: 1 token ? 4 characters. Used when the API doesn't return usage."""
    if not text:
        return 0
    return max(1, len(text) // 4)


# -------------------------------------------------------------
# Usage log persistence
# -------------------------------------------------------------

def _load_logs() -> list:
    if not os.path.exists(USAGE_LOGS_FILE):
        return []
    try:
        with open(USAGE_LOGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_logs(logs: list):
    with open(USAGE_LOGS_FILE, "w", encoding="utf-8") as f:
        json.dump(logs, f, indent=2, ensure_ascii=False)


def log_usage(
    *,
    model: str,
    provider: str,
    input_tokens: int,
    output_tokens: int,
    context_chars: int,
    session_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    source: str = "chat",           # "chat" | "orchestration"
    run_id: Optional[str] = None,   # orchestration run id
    tool_name: Optional[str] = None,  # tool called on this turn (if any)
    latency_seconds: float = 0.0,
):
    """Append a usage record to usage_logs.json (thread-safe)."""
    estimated_cost = calculate_cost(model, input_tokens, output_tokens)
    record = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "model": model,
        "provider": provider,
        "session_id": session_id or "unknown",
        "agent_id": agent_id or "unknown",
        "source": source,
        "run_id": run_id,
        "tool_name": tool_name,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "context_chars": context_chars,
        "estimated_cost": estimated_cost,
        "latency_seconds": round(latency_seconds, 2),
    }
    with _lock:
        logs = _load_logs()
        logs.append(record)
        _save_logs(logs)
    _sid_display = (session_id[:8] + '…') if session_id and len(session_id) > 8 else (session_id or '-')
    print(
        f"DEBUG usage: {model} in={input_tokens} out={output_tokens} "
        f"cost=${estimated_cost:.6f} session={_sid_display}",
        flush=True,
    )


def log_compaction_event(
    *,
    stage: str,                        # "trim" | "llm_summary"
    chars_before: int,
    chars_after: int,
    session_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    run_id: Optional[str] = None,
    archive_path: Optional[str] = None,
    model: str = "",
):
    """Append a compaction event to usage_logs.json.

    Tokens and cost are zero — the Stage-2 LLM call is already captured by the
    regular log_usage() call inside llm_providers. This record exists solely for
    observability: when did compaction fire, how much was saved, where is the archive.
    """
    chars_saved = chars_before - chars_after
    reduction_pct = round(chars_saved / chars_before * 100) if chars_before > 0 else 0
    record = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "event_type": "compaction",
        "source": "compaction",
        "stage": stage,
        "session_id": session_id or "unknown",
        "agent_id": agent_id or "unknown",
        "run_id": run_id,
        "chars_before": chars_before,
        "chars_after": chars_after,
        "chars_saved": chars_saved,
        "reduction_pct": reduction_pct,
        "archive_path": archive_path,
        "model": model,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "estimated_cost": 0.0,
        "latency_seconds": 0.0,
    }
    with _lock:
        logs = _load_logs()
        logs.append(record)
        _save_logs(logs)
    archive_note = f" → archived: {archive_path}" if archive_path else ""
    print(
        f"DEBUG usage: [compaction/{stage}] "
        f"{chars_before:,} → {chars_after:,} chars (-{reduction_pct}%){archive_note}",
        flush=True,
    )


# -------------------------------------------------------------
# Query helpers
# -------------------------------------------------------------

def get_usage_logs(
    limit: int = 100,
    offset: int = 0,
    session_id: Optional[str] = None,
    source: Optional[str] = None,
    run_id: Optional[str] = None,
) -> list:
    """Return paginated usage records.
    - When filtering by session_id or run_id: oldest-first (for per-turn context delta display).
    - Otherwise: newest-first.
    """
    with _lock:
        logs = _load_logs()

    # Filter
    if session_id:
        logs = [r for r in logs if r.get("session_id") == session_id]
    if run_id:
        logs = [r for r in logs if r.get("run_id") == run_id]
    if source:
        logs = [r for r in logs if r.get("source") == source]

    # Ordering: per-session/run ? chronological (oldest first); global ? newest first
    if not session_id and not run_id:
        logs = list(reversed(logs))

    return logs[offset: offset + limit]


def get_usage_summary() -> dict:
    """Return aggregated cost/token totals grouped by model and session.

    Orchestration log entries (those with a non-null run_id) are grouped
    separately by run_id so that each orchestration run appears as a single
    session entry regardless of how many sub-agents ran under it.
    Chat sessions are grouped by session_id as before.
    """
    with _lock:
        logs = _load_logs()

    total_cost = 0.0
    total_input = 0
    total_output = 0
    # Compaction events are observational (0 tokens/cost) — exclude from request count
    total_requests = sum(1 for r in logs if r.get("event_type") != "compaction")
    by_model: dict[str, dict] = {}
    by_session: dict[str, dict] = {}   # keyed by session_id (chat)
    by_run: dict[str, dict] = {}       # keyed by run_id (orchestration runs)
    by_schedule: dict[str, dict] = {}  # keyed by run_id (schedule runs)

    for r in logs:
        if r.get("event_type") == "compaction":
            continue  # purely observational — skip all aggregates
        model = r.get("model", "unknown")
        provider = r.get("provider", "unknown")
        session = r.get("session_id", "unknown")
        run_id = r.get("run_id")  # None for chat, e.g. "run_orch_X_ts" for orch
        source = r.get("source", "chat")
        cost = r.get("estimated_cost", 0.0)
        inp = r.get("input_tokens", 0)
        out = r.get("output_tokens", 0)
        ctx = r.get("context_chars", 0)
        agent_id = r.get("agent_id", "unknown")

        total_cost += cost
        total_input += inp
        total_output += out

        # By model
        if model not in by_model:
            by_model[model] = {
                "model": model,
                "provider": provider,
                "requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "estimated_cost": 0.0,
            }
        bm = by_model[model]
        bm["requests"] += 1
        bm["input_tokens"] += inp
        bm["output_tokens"] += out
        bm["total_tokens"] += inp + out
        bm["estimated_cost"] = round(bm["estimated_cost"] + cost, 8)

        # Schedule entries are grouped by run_id
        if run_id and source == "schedule":
            if run_id not in by_schedule:
                # Extract schedule_id from run_id format: schedulerun_{schedule_id}_{ts}
                parts = run_id.split("_")
                schedule_id = parts[1] if len(parts) > 1 else "unknown"
                by_schedule[run_id] = {
                    "run_id": run_id,
                    "schedule_id": schedule_id,
                    "agent_id": agent_id,
                    "agents_used": set(),
                    "requests": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "context_chars": 0,
                    "estimated_cost": 0.0,
                    "models_used": set(),
                    "first_ts": r.get("timestamp"),
                    "last_ts": r.get("timestamp"),
                    "source": "schedule",
                }
            bsch = by_schedule[run_id]
            bsch["requests"] += 1
            bsch["input_tokens"] += inp
            bsch["output_tokens"] += out
            bsch["total_tokens"] += inp + out
            bsch["context_chars"] += ctx
            bsch["estimated_cost"] = round(bsch["estimated_cost"] + cost, 8)
            bsch["models_used"].add(model)
            bsch["agents_used"].add(agent_id)
            bsch["last_ts"] = r.get("timestamp")

        # Orchestration entries are grouped by run_id
        elif run_id and (source == "orchestration" or source.startswith("orchestration:")):
            if run_id not in by_run:
                by_run[run_id] = {
                    "session_id": session,   # the chat session that spawned this run
                    "run_id": run_id,
                    "agent_id": agent_id,    # first agent seen (show in header)
                    "agents_used": set(),
                    "requests": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "context_chars": 0,
                    "estimated_cost": 0.0,
                    "models_used": set(),
                    "first_ts": r.get("timestamp"),
                    "last_ts": r.get("timestamp"),
                    "source": "orchestration",
                }
            br = by_run[run_id]
            br["requests"] += 1
            br["input_tokens"] += inp
            br["output_tokens"] += out
            br["total_tokens"] += inp + out
            br["context_chars"] += ctx
            br["estimated_cost"] = round(br["estimated_cost"] + cost, 8)
            br["models_used"].add(model)
            br["agents_used"].add(agent_id)
            br["last_ts"] = r.get("timestamp")
        else:
            # Chat / system-prompt-generation: group by session_id
            if session not in by_session:
                by_session[session] = {
                    "session_id": session,
                    "run_id": None,
                    "agent_id": agent_id,
                    "agents_used": set(),
                    "requests": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "context_chars": 0,
                    "estimated_cost": 0.0,
                    "models_used": set(),
                    "first_ts": r.get("timestamp"),
                    "last_ts": r.get("timestamp"),
                    "source": source,
                }
            bs = by_session[session]
            bs["requests"] += 1
            bs["input_tokens"] += inp
            bs["output_tokens"] += out
            bs["total_tokens"] += inp + out
            bs["context_chars"] += ctx
            bs["estimated_cost"] = round(bs["estimated_cost"] + cost, 8)
            bs["models_used"].add(model)
            bs["agents_used"].add(agent_id)
            bs["last_ts"] = r.get("timestamp")

    # Sort by cost descending
    by_model_list = sorted(by_model.values(), key=lambda x: x["estimated_cost"], reverse=True)

    # Merge chat sessions + orchestration runs, convert sets to lists
    all_sessions = list(by_session.values()) + list(by_run.values())
    by_session_list = []
    for bs in sorted(all_sessions, key=lambda x: x.get("last_ts") or "", reverse=True):
        bs["models_used"] = list(bs["models_used"])
        bs["agents_used"] = list(bs["agents_used"])
        by_session_list.append(bs)

    # Schedule runs -- convert sets to lists, sort by last_ts descending
    by_schedule_list = []
    for bsch in sorted(by_schedule.values(), key=lambda x: x.get("last_ts") or "", reverse=True):
        bsch["models_used"] = list(bsch["models_used"])
        bsch["agents_used"] = list(bsch["agents_used"])
        by_schedule_list.append(bsch)

    return {
        "total_cost": round(total_cost, 8),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "total_requests": total_requests,
        "by_model": by_model_list,
        "by_session": by_session_list,
        "by_schedule": by_schedule_list,
    }

def clear_usage_logs() -> int:
    """Delete all usage logs. Returns count deleted."""
    with _lock:
        count = len(_load_logs())
        _save_logs([])
    return count
