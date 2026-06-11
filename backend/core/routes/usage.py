"""
Usage & Cost API endpoints.
"""
from typing import Optional
from fastapi import APIRouter, Query
from pydantic import BaseModel

from core.usage_tracker import (
    get_usage_logs,
    get_usage_summary,
    get_cache_summary,
    get_pricing_table,
    save_pricing_table,
    clear_usage_logs,
)
from core.cache import store as cache_store

router = APIRouter()


@router.get("/api/usage/summary")
async def usage_summary():
    """Aggregate cost/token totals, grouped by model and session."""
    return get_usage_summary()


@router.get("/api/usage/cache_summary")
async def usage_cache_summary():
    """Cache-focused aggregates for the Cache Analytics dashboard.

    Returns total estimated savings, per-model hit rates, and the top 20
    orchestration runs by savings. Disk-only — no LLM call.
    """
    out = get_cache_summary()
    out["disk_stats"] = cache_store.stats()
    return out


@router.get("/api/usage/logs")
async def usage_logs(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session_id: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    run_id: Optional[str] = Query(None),
):
    """Paginated detailed per-call usage records, newest first."""
    logs = get_usage_logs(limit=limit, offset=offset, session_id=session_id, source=source, run_id=run_id)
    return {"logs": logs, "count": len(logs)}


@router.get("/api/usage/pricing")
async def usage_pricing():
    """Return the current pricing table from model_pricing.json."""
    return get_pricing_table()


@router.put("/api/usage/pricing")
async def update_pricing(body: dict):
    """Save an updated pricing table to model_pricing.json."""
    # Basic validation — each entry must have provider, input_per_1m, output_per_1m
    for model_key, entry in body.items():
        if not isinstance(entry, dict):
            return {"error": f"Invalid entry for model '{model_key}'"}
        for field in ("provider", "input_per_1m", "output_per_1m"):
            if field not in entry:
                return {"error": f"Missing field '{field}' for model '{model_key}'"}
    save_pricing_table(body)
    return {"status": "ok", "saved": len(body)}


@router.delete("/api/usage/logs")
async def clear_logs():
    """Delete all usage logs."""
    count = clear_usage_logs()
    return {"deleted": count, "status": "ok"}

