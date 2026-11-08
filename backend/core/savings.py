"""Where the workspace could spend less, each with its estimate and the change that makes it (F1261, REACH1; D340 R2).

Naveen, 10-30: "usage to give suggestions", each a ready change card. Usage is read as the person, through the
product's own read (`get_usage`), so it is what the Usage screen counts. A model's guess at savings is not one: every
suggestion here is decided by a rule in code, priced from the workspace's own usage, and carries the op that makes it
(Synapse AI's change card, with the person's yes and undo). A suggestion with no priced usage behind it gives no
number, and says so.

* **A model swap** (`model`): an agent whose model costs more than F1251's best fit for what it needs (`pick`: its
  tools, whether its model reasons, own key first), when that fit is cheaper. The estimate is the agent's own tokens
  over the window, priced at both models' listed rates: `set_agent_model`. With no cheaper fit that has a listed
  price, a costly agent is still named, with what it costs a month and no number saved (F1297).
* **A schedule that repeats itself** (`schedule`): of its last runs (at least `RUNS_SEEN`), `SAME_SHARE` or more
  answered exactly as the run before, or failed. The estimate is those runs' own cost: `set_schedule`, its new time
  the person's to say.
* **Prompt caching off** (`caching`): `settings_set prompt_cache_enabled`. No number: what it saves depends on how
  much of each prompt repeats, which the usage log does not hold.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

#: A schedule's runs read, and how many must be like the one before for it to count as repeating itself.
RUNS_SEEN = 10
MIN_RUNS = 5
SAME_SHARE = 0.8
#: Below this a saving is not offered: the card would cost the person more attention than it saves.
MIN_MONTHLY_USD = 0.5
MONTH_DAYS = 30


def _per_month(usd: float, days: int) -> float:
    return round(usd * MONTH_DAYS / max(days, 1), 2)


def _priced(entry: Any, tokens_in: int, tokens_out: int) -> float | None:
    if entry is None or entry.price_in is None or entry.price_out is None:
        return None
    return (tokens_in * entry.price_in + tokens_out * entry.price_out) / 1_000_000


async def _agent_spend(days: int) -> dict[str, dict]:
    """Each agent's spend over the window, by its id, from the product's own usage read as the person (`get_usage`,
    F1297): what the Usage screen counts, chats included, and a host's own figures, which it answers only for a
    bound caller (cloud's source read nothing when called around the registry)."""
    from core.builder_readers import _product_read

    got = await _product_read("get_usage", {"by": "agent", "days": days})
    return {str(r.get("agent_id") or r.get("key")): r for r in got.get("rows") or []}


async def model_swaps(days: int) -> list[dict]:
    from core.native_builder import models_menu as MM
    from core.routes.agents import load_user_agents

    rows = await _agent_spend(days)
    menu = await MM.menu()
    if not menu:
        return []
    by_id = {e.id: e for e in menu}
    out = []
    for agent in await load_user_agents():
        row = rows.get(agent.get("id"))
        if agent.get("system") or not row or not row.get("calls") or not row.get("cost_usd"):
            continue
        now = agent.get("model") or MM.workspace_model()
        current = by_id.get(now)
        needs = "reasoning" if current is not None and current.reasoning else "simple"
        fit = MM.pick(needs, has_tools=bool(agent.get("tools")), menu=menu, default=MM.workspace_model())
        if isinstance(fit, MM.Picked) and fit.id == now:
            continue
        tokens = int(row["input_tokens"]), int(row["output_tokens"])
        would = _priced(by_id.get(fit.id), *tokens) if isinstance(fit, MM.Picked) else None
        if would is None:
            # No cheaper fit with a listed price (F1297): the costly agent is named, never left out, with no number.
            name, spent = agent.get("name") or agent["id"], _per_month(float(row["cost_usd"]), days)
            if spent >= MIN_MONTHLY_USD:
                out.append({"kind": "model", "agent": name, "now": now, "saves_usd_month": None, "op": None,
                            "rule": f"{name} costs about ${spent:.2f} a month on {now} at its last {days} days' "
                                    "rate; no cheaper model with a listed price fits what it needs here",
                            "no_estimate": "a model with no listed price here can't be priced against it"})
            continue
        # Like for like: both at the listed rates, the same tokens; the log's own figure only where the current
        # model has no listed price.
        was = _priced(current, *tokens)
        was = float(row["cost_usd"]) if was is None else was
        if would >= was:
            continue
        monthly = _per_month(was - would, days)
        if monthly < MIN_MONTHLY_USD:
            continue
        out.append({"kind": "model", "agent": agent.get("name") or agent["id"], "now": now, "to": fit.id,
                    "why": fit.reason, "saves_usd_month": monthly,
                    "rule": f"the best fit for what it needs costs less; its last {days} days' tokens at that "
                            "model's rates",
                    "op": {"op": "set_agent_model", "target": {"kind": "agent", "id": agent["id"]},
                           "fields": {"model": fit.id}}})
    return out


def _answer(view: dict | None) -> Any:
    return None if not view else view.get("response")


async def repeating_schedules(days: int) -> list[dict]:
    from core.orchestration.run_view import list_runs, read_run
    from core.product_tools import run_cost
    from core.store import schedules as store

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    recent = [r for r in await list_runs(limit=1000) if (r.get("started_at") or "") >= since]
    out = []
    for s in await store.load():
        if not s.get("enabled"):
            continue
        mine = [r for r in recent if r.get("session_id") == f"schedule_{s.get('id')}"][:RUNS_SEEN]
        if len(mine) < MIN_RUNS:
            continue
        answers = [_answer(await read_run(r["run_id"])) for r in mine]
        # Newest first: a run "like the one before" answered exactly as the next older one, or failed.
        idle = [r for i, r in enumerate(mine) if r.get("status") == "failed"
                or (i + 1 < len(mine) and answers[i] is not None and answers[i] == answers[i + 1])]
        if len(idle) / len(mine) < SAME_SHARE:
            continue
        paid = await run_cost.costs(idle)
        usd = sum(run_cost.total([paid.get(r.get("run_id")) or run_cost._own(r)]).get("model_dollars") or 0.0
                  for r in idle)
        span_days = max(1, days)
        out.append({"kind": "schedule", "schedule": s.get("name") or s["id"],
                    "saves_usd_month": _per_month(usd, span_days) if usd else None,
                    "rule": f"{len(idle)} of its last {len(mine)} runs answered exactly as the run before or failed "
                            f"(at least {SAME_SHARE:.0%} counts)",
                    "op": {"op": "set_schedule", "target": {"kind": "schedule", "id": s["id"]},
                           "fields": {}, "ask": "how often it should run instead"}})
    return out


async def caching() -> list[dict]:
    from core.product_tools import settings

    now = await settings.current()
    if "prompt_cache_enabled" not in now or now.get("prompt_cache_enabled") is not False:
        return []
    return [{"kind": "caching", "saves_usd_month": None,
             "rule": "prompt caching is off: a repeated start of a prompt is billed in full each time",
             "no_estimate": "what it saves depends on how much of each prompt repeats, which the usage log does "
                            "not hold",
             "op": {"op": "settings_set", "target": {"kind": "settings", "id": "workspace"},
                    "fields": {"prompt_cache_enabled": True}}}]


#: How many of the costliest agents and workflows a savings answer names.
TOP = 5


async def top_spenders(days: int) -> dict:
    """Who spent most in the window (F1311): the agents, each with its model, what it spent and what that is a month
    (Synapse AI's own left out), and the workflows whose runs cost most. Said whether or not a saving clears the
    floor: prod-08's "cut my costs" was answered with nothing to save and no one named, for $0.36 under the floor."""
    from core.native_builder import models_menu as MM
    from core.orchestration.run_view import list_runs
    from core.product_tools import run_cost
    from core.routes.agents import load_user_agents

    spend = await _agent_spend(days)
    agents = {a["id"]: a for a in await load_user_agents() if a.get("id") and not a.get("system")
              and a.get("type") != "builder"}
    named = sorted(((agents[i], r) for i, r in spend.items() if i in agents and float(r.get("cost_usd") or 0) > 0),
                   key=lambda pair: -float(pair[1]["cost_usd"]))[:TOP]
    out_agents = [{"agent": a.get("name") or a["id"], "model": a.get("model") or MM.workspace_model(),
                   "spent_usd": round(float(r["cost_usd"]), 6), "usd_month": _per_month(float(r["cost_usd"]), days),
                   "calls": int(r.get("calls") or 0),
                   **({"charged_usd": r["charged_usd"]} if "charged_usd" in r else {})} for a, r in named]
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    from core.routes.orchestrations import load_orchestrations

    names = {o.get("id"): o.get("name") for o in await load_orchestrations()}
    rows = [r for r in await list_runs(limit=1000) if (r.get("started_at") or "") >= since]
    paid = await run_cost.costs(rows)
    by_flow: dict[str, list[dict]] = {}
    for r in rows:
        by_flow.setdefault(names.get(r.get("orchestration_id")) or r.get("orchestration_id"), []).append(
            paid.get(r.get("run_id")) or run_cost._own(r))
    flows = [{"workflow": name, "runs": len(figures), **run_cost.total(figures)} for name, figures in by_flow.items()]
    flows = [f for f in flows if (f.get("model_dollars") or 0) > 0 or (f.get("credits") or 0) > 0]
    flows.sort(key=lambda f: -((f.get("credits") or 0) or (f.get("model_dollars") or 0)))
    return {"agents": out_agents, "workflows": flows[:TOP]}


async def find(days: int = MONTH_DAYS) -> dict:
    days = max(1, min(int(days), 90))
    items = [*await model_swaps(days), *await repeating_schedules(days), *await caching()]
    items.sort(key=lambda i: -(i.get("saves_usd_month") or 0))
    return {"items": items, "top_spenders": await top_spenders(days), "window": f"the last {days} days",
            "note": "Each item is code's rule, priced from this workspace's own usage; the op makes it, with the "
                    "person's yes. A schedule's new time is theirs to say. No number means none could be priced. "
                    "`top_spenders` are who spent most in the window, whether or not a saving clears the floor: "
                    "start from them."}
