"""What the workspace spent on models, as a product tool `get_usage` (CLI1, D294).

Naveen's call (2026-10-17): usage reaches a key through the registry, not a route of its own, so MCP clients and
the CLI both read it. The Usage screen's figures: each model call's estimated dollars and tokens, summed over a
window by day, agent or model. A compaction event costs nothing and is left out, as the screen leaves it; an
embedding is spend, and is in, as the screen has it (F1297).

A reader's act for any member (viewer, `read` scope). Where the rows come from is a host's: this process's usage log
by default; cloud's are its own (`set_usage_source`).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from core.product_tools.errors import ToolRefusal
from core.product_tools.registry import ProductTool
from core.product_tools.roles import Role

GROUPS = ("day", "agent", "model")
MAX_DAYS = 90

#: `source(since, by) -> rows`, each `{key, cost_usd, input_tokens, output_tokens, calls}` and, where the host
#: charges for models of its own, `charged_usd`: largest spend first (by day: oldest first). `cost_usd` is the
#: estimate of what each provider bills the workspace; `charged_usd` is what the host took. They are never added.
Source = Callable[[datetime, str], Awaitable[list[dict]]]

_source: Source | None = None


def set_usage_source(source: Source | None) -> None:
    """A host's own usage rows; None restores this process's usage log."""
    global _source
    _source = source


async def _from_the_log(since: datetime, by: str) -> list[dict]:
    from sqlalchemy import func, select

    from core.store import session
    from core.store.models import UsageLogDB
    from core.tenancy import get_tenant

    key = {"day": func.date(UsageLogDB.timestamp), "agent": UsageLogDB.agent_id, "model": UsageLogDB.model}[by]
    stmt = (
        select(key.label("key"),
               func.coalesce(func.sum(UsageLogDB.estimated_cost), 0.0),
               func.coalesce(func.sum(UsageLogDB.input_tokens), 0),
               func.coalesce(func.sum(UsageLogDB.output_tokens), 0),
               func.count())
        # What the Usage screen counts (`usage_tracker.get_usage_summary`): every call but a compaction event, which
        # costs nothing; an embedding is spend too (F1297: left out here, the CLI's total was not the screen's).
        .where(UsageLogDB.tenant_id == get_tenant(), UsageLogDB.timestamp >= since,
               (UsageLogDB.event_type.is_(None)) | (UsageLogDB.event_type != "compaction"))
        .group_by(key)
    )
    async with session() as s:
        rows = (await s.execute(stmt)).all()
    out = [{"key": str(k) if k is not None else "unknown", "cost_usd": round(float(cost or 0), 6),
            "input_tokens": int(tin or 0), "output_tokens": int(tout or 0), "calls": int(n or 0)}
           for k, cost, tin, tout, n in rows]
    if by == "day":
        return sorted(out, key=lambda r: r["key"])
    return sorted(out, key=lambda r: (-r["cost_usd"], r["key"]))


async def _agent_names() -> dict[str, str]:
    from core.routes.agents import load_user_agents

    return {a["id"]: a.get("name") or a["id"] for a in await load_user_agents() if a.get("id")}


async def _usage(caller: Any, args: dict[str, Any]) -> dict:
    by = str(args.get("by") or "day")
    if by not in GROUPS:
        raise ToolRefusal(f"`by` is one of {', '.join(GROUPS)}, not {by!r}.")
    try:
        days = int(args.get("days") or 7)
    except (TypeError, ValueError):
        raise ToolRefusal("`days` is a whole number of days.") from None
    days = max(1, min(days, MAX_DAYS))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = await (_source or _from_the_log)(since, by)
    if by == "agent":
        names = await _agent_names()
        rows = [{**r, "agent_id": r["key"], "key": names.get(r["key"], r["key"])} for r in rows]
    total = {"cost_usd": round(sum(r["cost_usd"] for r in rows), 6),
             "input_tokens": sum(r["input_tokens"] for r in rows),
             "output_tokens": sum(r["output_tokens"] for r in rows),
             "calls": sum(r["calls"] for r in rows)}
    note = "cost_usd is estimated from each provider's listed prices; a model with no price counts $0."
    if any("charged_usd" in r for r in rows):
        # What the host took for its own models: a fact, kept apart from the estimate (cloud's ledger).
        total["charged_usd"] = round(sum(r.get("charged_usd") or 0.0 for r in rows), 6)
        note += " charged_usd is what was charged for Synapse's own models; the two are never added."
    return {"window": f"the last {days} day{'s' if days != 1 else ''} (since {since:%Y-%m-%d %H:%M} UTC)",
            "by": by, "rows": rows, "total": total, "note": note}


def entries() -> list[ProductTool]:
    return [ProductTool(
        name="get_usage",
        description=(
            "What this workspace spent on models over the last N days (default 7, at most 90): estimated dollars, "
            "tokens in and out, and model calls, summed by day, agent or model, with the total."
        ),
        input_schema={"type": "object", "properties": {
            "days": {"type": "integer", "minimum": 1, "maximum": MAX_DAYS, "description": "How many days back."},
            "by": {"type": "string", "enum": list(GROUPS), "description": "How to group it (default day)."},
        }},
        handler=_usage, kind="read", area="usage", min_role=Role.VIEWER, scope="read", idempotent=True,
    )]
