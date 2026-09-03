"""
LLM Usage & Cost Tracker
------------------------
Persists every LLM call's token counts, context size, and estimated cost to
the `usage_logs` table, priced from the `model_pricing` table.

Actual token counts are sourced from API response objects where available
(OpenAI, Anthropic, Gemini all surface usage metadata). Bedrock and Ollama
fall back to a character-count heuristic (len / 4).

The pricing table is read through a process snapshot, refreshed by the async
paths below. `calculate_cost` and `calculate_savings` stay synchronous and
pure: they are called on the per-turn path, priced from a table that changes
about as often as a vendor changes a price list, and making them `async`
would push an await into every cost calculation for a lookup in a dict.
"""
from datetime import datetime, timezone
from typing import Optional

from core.store import usage as usage_store

# -------------------------------------------------------------
# Pricing lookup
# -------------------------------------------------------------

#: The rate card as last read from the store. `None` means "not loaded yet",
#: which is distinct from "loaded and empty" — the former is worth a read.
_pricing_snapshot: dict | None = None


async def _refresh_pricing() -> dict:
    """Load the rate card into the process snapshot."""
    global _pricing_snapshot
    try:
        _pricing_snapshot = await usage_store.load_pricing()
    except Exception as e:
        print(f"DEBUG usage_tracker: could not load pricing: {e}")
        _pricing_snapshot = _pricing_snapshot or {}
    return _pricing_snapshot


async def _ensure_pricing() -> dict:
    if _pricing_snapshot is None:
        return await _refresh_pricing()
    return _pricing_snapshot


def _load_pricing() -> dict:
    """The rate card, synchronously. Empty until an async path has loaded it."""
    return _pricing_snapshot or {}


def _resolve_pricing_entry(model: str, pricing: dict | None = None) -> dict | None:
    """Look up a model's pricing entry with fuzzy-prefix fallback."""
    pricing = pricing if pricing is not None else _load_pricing()
    entry = pricing.get(model)
    if entry is None:
        # Fuzzy prefix match -- pick the longest prefix that fits
        for key in sorted(pricing.keys(), key=len, reverse=True):
            if model.startswith(key) or key.startswith(model.split("-")[0]):
                entry = pricing[key]
                break
    return entry


# Provider-specific cache pricing as a fraction of base input rate.
# Used when a pricing entry doesn't explicitly set cache_read_per_1m / cache_write_per_1m.
# Sourced from each provider's docs at the time of writing.
_CACHE_RATE_DEFAULTS = {
    "anthropic":  {"read": 0.10, "write": 1.25},
    "openai":     {"read": 0.50, "write": 1.00},  # OpenAI has no write surcharge
    "deepseek":   {"read": 0.10, "write": 1.00},
    "gemini":     {"read": 0.25, "write": 1.00},
    "bedrock":    {"read": 0.10, "write": 1.25},  # Bedrock Anthropic mirrors Anthropic
    "grok":       {"read": 0.25, "write": 1.00},
}


def _cache_rates(entry: dict, input_per_1m: float) -> tuple[float, float]:
    """Resolve cache_read / cache_write per-1M rates, applying provider defaults."""
    read = entry.get("cache_read_per_1m")
    write = entry.get("cache_write_per_1m")
    if read is not None and write is not None:
        return float(read), float(write)
    provider = (entry.get("provider") or "").lower()
    defaults = _CACHE_RATE_DEFAULTS.get(provider, {"read": 0.10, "write": 1.25})
    if read is None:
        read = input_per_1m * defaults["read"]
    if write is None:
        write = input_per_1m * defaults["write"]
    return float(read), float(write)


def calculate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Return estimated USD cost for a call. Returns 0.0 for unknown models.

    `input_tokens` should be the TOTAL prompt tokens billed at full rate (i.e.
    cache-miss tokens). `cache_read_tokens` and `cache_write_tokens` are billed
    separately using provider-specific discounted/premium tiers.
    """
    entry = _resolve_pricing_entry(model)
    if not entry:
        return 0.0

    input_per_1m = entry.get("input_per_1m", 0.0)
    output_per_1m = entry.get("output_per_1m", 0.0)
    cache_read_per_1m, cache_write_per_1m = _cache_rates(entry, input_per_1m)

    input_cost = (input_tokens / 1_000_000) * input_per_1m
    output_cost = (output_tokens / 1_000_000) * output_per_1m
    cache_read_cost = (cache_read_tokens / 1_000_000) * cache_read_per_1m
    cache_write_cost = (cache_write_tokens / 1_000_000) * cache_write_per_1m
    return round(input_cost + output_cost + cache_read_cost + cache_write_cost, 8)


def calculate_savings(
    model: str,
    cache_read_tokens: int,
) -> float:
    """USD saved on this call vs. paying full input rate for the cache_read tokens."""
    if cache_read_tokens <= 0:
        return 0.0
    entry = _resolve_pricing_entry(model)
    if not entry:
        return 0.0
    input_per_1m = entry.get("input_per_1m", 0.0)
    cache_read_per_1m, _ = _cache_rates(entry, input_per_1m)
    delta_per_1m = max(0.0, input_per_1m - cache_read_per_1m)
    return round((cache_read_tokens / 1_000_000) * delta_per_1m, 8)


async def get_pricing_table() -> dict:
    """Return the raw pricing table for the API."""
    return await _ensure_pricing()


async def save_pricing_table(table: dict) -> None:
    """Replace the rate card."""
    await usage_store.save_pricing(table)
    await _refresh_pricing()
    print(f"DEBUG usage_tracker: pricing table updated ({len(table)} entries)", flush=True)


async def seed_pricing_table() -> int:
    """Insert any shipped models the table is missing. Called at startup."""
    from core.model_pricing import DEFAULT_MODEL_PRICING

    added = await usage_store.seed_pricing(DEFAULT_MODEL_PRICING)
    await _refresh_pricing()
    return added


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

async def log_usage(
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
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    response_cache_hit: bool = False,  # True when the LLM call was skipped entirely
):
    """Append one usage record.

    `input_tokens` should be the cache-miss prompt tokens (i.e. tokens billed
    at the full input rate). Provider helpers split this out before calling us.
    """
    await _ensure_pricing()
    estimated_cost = calculate_cost(
        model, input_tokens, output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )
    estimated_savings = calculate_savings(model, cache_read_tokens)
    await usage_store.append({
        "timestamp": datetime.now(timezone.utc),
        "model": model,
        "provider": provider,
        "session_id": session_id or "unknown",
        "agent_id": agent_id or "unknown",
        "source": source,
        "run_id": run_id,
        "tool_name": tool_name,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens + cache_read_tokens + cache_write_tokens,
        "context_chars": context_chars,
        "estimated_cost": estimated_cost,
        "latency_seconds": round(latency_seconds, 2),
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "estimated_savings": estimated_savings,
        "response_cache_hit": response_cache_hit,
    })
    _sid_display = (session_id[:8] + '…') if session_id and len(session_id) > 8 else (session_id or '-')
    _cache_tag = ""
    if response_cache_hit:
        _cache_tag = " [response_cache_hit]"
    elif cache_read_tokens or cache_write_tokens:
        _cache_tag = f" cache_r={cache_read_tokens} cache_w={cache_write_tokens}"
    print(
        f"DEBUG usage: {model} in={input_tokens} out={output_tokens} "
        f"cost=${estimated_cost:.6f} session={_sid_display}{_cache_tag}",
        flush=True,
    )


async def log_compaction_event(
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
    """Append a compaction event.

    Tokens and cost are zero — the Stage-2 LLM call is already captured by the
    regular log_usage() call inside llm_providers. This record exists solely for
    observability: when did compaction fire, how much was saved, where is the archive.
    """
    chars_saved = chars_before - chars_after
    reduction_pct = round(chars_saved / chars_before * 100) if chars_before > 0 else 0
    await usage_store.append({
        "timestamp": datetime.now(timezone.utc),
        "event_type": "compaction",
        "source": "compaction",
        "session_id": session_id or "unknown",
        "agent_id": agent_id or "unknown",
        "run_id": run_id,
        "model": model,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "estimated_cost": 0.0,
        "latency_seconds": 0.0,
        # The fields only a compaction event has. Columns for these would be
        # NULL on every LLM-call row, which is the whole table.
        "details": {
            "stage": stage,
            "chars_before": chars_before,
            "chars_after": chars_after,
            "chars_saved": chars_saved,
            "reduction_pct": reduction_pct,
            "archive_path": archive_path,
        },
    })
    archive_note = f" → archived: {archive_path}" if archive_path else ""
    print(
        f"DEBUG usage: [compaction/{stage}] "
        f"{chars_before:,} → {chars_after:,} chars (-{reduction_pct}%){archive_note}",
        flush=True,
    )


# -------------------------------------------------------------
# Query helpers
# -------------------------------------------------------------

async def get_usage_logs(
    limit: int = 100,
    offset: int = 0,
    session_id: Optional[str] = None,
    source: Optional[str] = None,
    run_id: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> list:
    """Return paginated usage records.
    - When filtering by session_id or run_id: oldest-first (for per-turn context delta display).
    - Otherwise: newest-first.

    Filtering, ordering and paging all happen in the database now. This used to
    read every record ever written and slice the result, which is what
    `limit=100_000` at the end of an orchestration run was paying for.
    """
    return await usage_store.query(
        limit=limit, offset=offset, session_id=session_id, source=source,
        run_id=run_id, agent_id=agent_id,
    )


async def get_agent_summary(agent_id: str) -> dict:
    """Totals for one agent — what the agent editor's Metrics tab shows."""
    return await usage_store.agent_summary(agent_id)


async def get_usage_summary() -> dict:
    """Return aggregated cost/token totals grouped by model and session.

    Orchestration log entries (those with a non-null run_id) are grouped
    separately by run_id so that each orchestration run appears as a single
    session entry regardless of how many sub-agents ran under it.
    Chat sessions are grouped by session_id as before.

    Grouped in Python rather than by SQL because `models_used` and
    `agents_used` are sets, and set aggregation is the one thing SQLite and
    Postgres do not spell the same way — see `core/store/usage.py`.
    """
    logs = await usage_store.all_records()

    total_cost = 0.0
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_write = 0
    total_savings = 0.0
    total_cache_hits = 0
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

        cache_r = r.get("cache_read_tokens", 0) or 0
        cache_w = r.get("cache_write_tokens", 0) or 0
        savings = r.get("estimated_savings", 0.0) or 0.0
        is_response_hit = bool(r.get("response_cache_hit"))

        total_cost += cost
        total_input += inp
        total_output += out
        total_cache_read += cache_r
        total_cache_write += cache_w
        total_savings += savings
        if is_response_hit:
            total_cache_hits += 1

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
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "estimated_savings": 0.0,
                "response_cache_hits": 0,
            }
        bm = by_model[model]
        bm["requests"] += 1
        bm["input_tokens"] += inp
        bm["output_tokens"] += out
        bm["total_tokens"] += inp + out
        bm["estimated_cost"] = round(bm["estimated_cost"] + cost, 8)
        bm["cache_read_tokens"] += cache_r
        bm["cache_write_tokens"] += cache_w
        bm["estimated_savings"] = round(bm["estimated_savings"] + savings, 8)
        if is_response_hit:
            bm["response_cache_hits"] += 1

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
        "total_cache_read_tokens": total_cache_read,
        "total_cache_write_tokens": total_cache_write,
        "total_estimated_savings": round(total_savings, 8),
        "total_response_cache_hits": total_cache_hits,
        "by_model": by_model_list,
        "by_session": by_session_list,
        "by_schedule": by_schedule_list,
    }

async def get_cache_summary() -> dict:
    """Return cache-focused aggregates: per-model + per-run hit rates and savings.

    Powers the cache analytics dashboard. Cheaper than walking get_usage_summary()
    on the frontend because it strips out the chat/session detail.
    """
    logs = await usage_store.all_records()

    by_model: dict[str, dict] = {}
    by_run: dict[str, dict] = {}
    total_savings = 0.0
    total_requests = 0
    total_cache_hits = 0
    total_cache_read = 0
    total_cache_write = 0

    for r in logs:
        if r.get("event_type") == "compaction":
            continue
        total_requests += 1
        model = r.get("model", "unknown")
        run_id = r.get("run_id")
        cache_r = r.get("cache_read_tokens", 0) or 0
        cache_w = r.get("cache_write_tokens", 0) or 0
        savings = r.get("estimated_savings", 0.0) or 0.0
        is_hit = bool(r.get("response_cache_hit"))
        cost = r.get("estimated_cost", 0.0) or 0.0

        total_savings += savings
        total_cache_read += cache_r
        total_cache_write += cache_w
        if is_hit:
            total_cache_hits += 1

        m = by_model.setdefault(model, {
            "model": model,
            "requests": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "estimated_savings": 0.0,
            "estimated_cost": 0.0,
            "response_cache_hits": 0,
        })
        m["requests"] += 1
        m["cache_read_tokens"] += cache_r
        m["cache_write_tokens"] += cache_w
        m["estimated_savings"] = round(m["estimated_savings"] + savings, 8)
        m["estimated_cost"] = round(m["estimated_cost"] + cost, 8)
        if is_hit:
            m["response_cache_hits"] += 1

        if run_id:
            br = by_run.setdefault(run_id, {
                "run_id": run_id,
                "requests": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "estimated_savings": 0.0,
                "estimated_cost": 0.0,
                "response_cache_hits": 0,
                "last_ts": r.get("timestamp"),
            })
            br["requests"] += 1
            br["cache_read_tokens"] += cache_r
            br["cache_write_tokens"] += cache_w
            br["estimated_savings"] = round(br["estimated_savings"] + savings, 8)
            br["estimated_cost"] = round(br["estimated_cost"] + cost, 8)
            if is_hit:
                br["response_cache_hits"] += 1
            br["last_ts"] = r.get("timestamp")

    by_model_list = sorted(by_model.values(), key=lambda x: x["estimated_savings"], reverse=True)
    by_run_list = sorted(by_run.values(), key=lambda x: x["estimated_savings"], reverse=True)[:20]

    overall_hit_rate = (total_cache_hits / total_requests) if total_requests else 0.0

    return {
        "total_estimated_savings": round(total_savings, 8),
        "total_requests": total_requests,
        "total_response_cache_hits": total_cache_hits,
        "response_cache_hit_rate": round(overall_hit_rate, 4),
        "total_cache_read_tokens": total_cache_read,
        "total_cache_write_tokens": total_cache_write,
        "by_model": by_model_list,
        "by_run": by_run_list,
    }


async def clear_usage_logs() -> int:
    """Delete this tenant's usage logs. Returns count deleted."""
    return await usage_store.clear()
