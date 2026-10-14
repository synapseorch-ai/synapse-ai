"""The AI builder reads the workspace from the workspace, never from a guess (BWA1, D274, decision 5).

BWA1's Nova Pro baseline asked the builder twelve questions about its own workspace and it got four right. With no
way to read channels, phone numbers, widgets, tool policy, knowledge, usage, the plan or the models, it reached for
the nearest tool and stated what it found as the answer: the MCP tool servers listed as "channels", "no knowledge
bases" from `list_skills` (one existed), the default model guessed from the agents.

`read_workspace(topic)` returns the truth for each of those, from the same stores the app's own screens read, and
says plainly when it cannot see something rather than leaving the model to fill the gap. Read-only, and it never
returns a secret's value: channel tokens, webhook secrets, provider keys and passwords are left out, and a
provider key is reported only as set or not set.

Each topic has a self-hosted reader here; a host registers its own (`set_workspace_reader`) where its truth lives
elsewhere — cloud's plan, credits and usage are its billing tables, not the engine's.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Awaitable, Callable

from core.builder_tools import register_builder_tool

Reader = Callable[[dict], Awaitable[dict]]

TOPICS: dict[str, str] = {
    "schedules": "every schedule: on or paused, when it runs, what it starts, when it last ran and how that went",
    "runs": "recent workflow runs, newest first, with why each failed one failed (status=failed for only those)",
    "channels": "connected chat channels (Slack, Telegram, Discord…): on or off, and which agents answer",
    "phone": "phone numbers: the number, on or off, and the agent that answers",
    "widgets": "website widgets: the agent that answers, the sites allowed, on or off",
    "tool_policy": "whether agents must ask before destructive, open-world or undeclared tools, by default and per agent",
    "knowledge": "knowledge bases, or that Knowledge is switched off",
    "usage": "what each workflow's runs used over the last `days` days (runs, credits, model dollars)",
    "plan": "the plan this workspace is on and the credits and model money left",
    "models": "the default model, the builder's models, and which providers have a key (never the key)",
    "agents": "the workspace's agents: name, type, model, how many tools",
    "workflows": "the workspace's workflows: name, how many steps, whether a schedule runs it",
}

_HOST: dict[str, Reader] = {}


def set_workspace_reader(topic: str, reader: Reader | None) -> None:
    """A host's own reader for `topic` (it gets the call's arguments); None puts back the self-hosted one."""
    if topic not in TOPICS:
        raise ValueError(f"unknown topic {topic!r}")
    if reader is None:
        _HOST.pop(topic, None)
    else:
        _HOST[topic] = reader


SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_workspace",
        "description": (
            "Read the truth about this workspace, from its own records, before you answer a question about it. "
            "Topics: " + "; ".join(f"`{k}` — {v}" for k, v in TOPICS.items()) + ". "
            "Answer from what it returns. When it says it cannot see something, say that; never fill the gap."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "enum": list(TOPICS)},
                "name": {"type": "string", "description": "Only things whose name contains this (any topic)."},
                "status": {"type": "string", "description": "runs: only this status, e.g. failed."},
                "days": {"type": "integer", "description": "usage: how many days back (default 7)."},
                "limit": {"type": "integer", "description": "runs: how many (default 20, at most 100)."},
            },
            "required": ["topic"],
        },
    },
}


def _named(items: list[dict], name: str | None, key: str = "name") -> list[dict]:
    if not name:
        return items
    needle = name.strip().lower()
    return [i for i in items if needle in str(i.get(key) or "").lower()]


def _when(schedule: dict) -> str:
    if schedule.get("schedule_type") == "cron" and schedule.get("cron_expression"):
        return f'cron "{schedule["cron_expression"]}" (UTC)'
    if schedule.get("interval_value"):
        return f'every {schedule["interval_value"]} {schedule.get("interval_unit") or "minutes"}'
    return "not set"


async def _agent_names() -> dict[str, str]:
    from core.routes.agents import load_user_agents

    return {a.get("id"): a.get("name") for a in await load_user_agents()}


async def _workflow_names() -> dict[str, str]:
    from core.routes.orchestrations import load_orchestrations

    return {o.get("id"): o.get("name") for o in await load_orchestrations()}


# ── self-hosted readers ───────────────────────────────────────────────────────

async def _schedules(args: dict) -> dict:
    from core.orchestration.run_view import list_runs
    from core.store import schedules as store

    agents, workflows = await _agent_names(), await _workflow_names()
    recent = await list_runs(limit=200)
    out = []
    for s in await store.load():
        target = (workflows if s.get("target_type") == "orchestration" else agents).get(s.get("target_id"))
        mine = [r for r in recent if r.get("session_id") == f"schedule_{s.get('id')}"]
        last = mine[0] if mine else None
        out.append({
            "name": s.get("name"), "on": bool(s.get("enabled")), "runs": _when(s),
            "starts": f'{s.get("target_type")} "{target or s.get("target_id")}"',
            "last_run_at": s.get("last_run_at"), "next_run_at": s.get("next_run_at"),
            "last_run": ({"status": last.get("status"), "error": last.get("error"), "started_at": last.get("started_at")}
                         if last else None),
        })
    return {"items": _named(out, args.get("name")),
            "note": "Times are UTC. last_run is the last workflow run the schedule started (a manual 'Run now' counts)."}


async def _runs(args: dict) -> dict:
    from core.orchestration.run_view import list_runs

    workflows = await _workflow_names()
    limit = max(1, min(int(args.get("limit") or 20), 100))
    status = (args.get("status") or "").strip().lower() or None
    rows = await list_runs(limit=limit, status=status)
    out = [{"workflow": workflows.get(r.get("orchestration_id")) or r.get("orchestration_id"),
            "status": r.get("status"), "started_at": r.get("started_at"), "ended_at": r.get("ended_at"),
            "error": r.get("error"), "waiting_for_a_person": bool(r.get("waiting_for_human")),
            "run_id": r.get("run_id")} for r in rows]
    return {"items": _named(out, args.get("name"), "workflow"),
            "note": "Newest first. `error` is why a failed or cancelled run stopped."}


async def _channels(args: dict, phone: bool = False) -> dict:
    from core.messaging import store
    from core.messaging.voice import PLATFORM as VOICE

    agents = await _agent_names()
    out = []
    for c in await store.list_channels():
        is_phone = c.get("platform") == VOICE
        if is_phone != phone:
            continue
        # An assignment can outlive its agent: say so, rather than hand back an id the model then guesses a name for.
        answering = [agents.get(a) or f"an agent that no longer exists ({a})"
                     for a in await store.get_agent_ids_for_channel(c.get("id"))]
        if phone:
            out.append({"name": c.get("name"), "number": (c.get("voice") or {}).get("number"),
                        "on": bool(c.get("enabled")), "answered_by": answering[:1] or None,
                        "language": (c.get("voice") or {}).get("language")})
        else:
            out.append({"name": c.get("name"), "platform": c.get("platform"), "on": bool(c.get("enabled")),
                        "status": c.get("status"), "agents": answering})
    note = "No phone number is connected." if phone and not out else (
        "No chat channel is connected." if not out else "")
    return {"items": _named(out, args.get("name")), "note": note}


async def _phone(args: dict) -> dict:
    return await _channels(args, phone=True)


async def _widgets(args: dict) -> dict:
    from core.chat_widgets.store import list_widgets

    agents = await _agent_names()
    out = [{"name": w.get("name"), "agent": agents.get(w.get("agent_id"), w.get("agent_id")),
            "allowed_sites": w.get("allowed_origins") or [], "on": bool(w.get("enabled", True)),
            "messages_per_day": w.get("daily_message_cap")} for w in await list_widgets()]
    return {"items": _named(out, args.get("name")), "note": "" if out else "No widget is set up."}


async def _tool_policy(args: dict) -> dict:
    from core.config import load_settings
    from core.routes.agents import load_user_agents
    from core.tool_policy import resolve_policy

    settings = load_settings()
    default = resolve_policy(None, settings)
    own = [{"agent": a.get("name"), "policy": resolve_policy(a, settings)} for a in await load_user_agents()
           if a.get("tool_policy") and not a.get("system")]
    return {"default": default, "agents_with_their_own": _named(own, args.get("name"), "agent"),
            "note": "Each category is allow (runs), confirm (asks a person first) or deny. open_world tools reach "
                    "systems outside this workspace; destructive ones change data that cannot be put back."}


async def _knowledge(args: dict) -> dict:
    from core.knowledge.config import knowledge_enabled

    if not knowledge_enabled():
        return {"items": [], "note": "Knowledge is switched off in this workspace, so there are no knowledge bases."}
    from core.knowledge.bases import list_bases

    out = [{"name": b.name, "description": b.description, "status": getattr(b, "status", None)}
           for b in await list_bases()]
    return {"items": _named(out, args.get("name")), "note": "" if out else "There are no knowledge bases yet."}


async def _usage(args: dict) -> dict:
    from core.orchestration.run_view import list_runs

    days = max(1, min(int(args.get("days") or 7), 90))
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    workflows = await _workflow_names()
    totals: dict[str, dict] = {}
    for r in await list_runs(limit=1000):
        if (r.get("started_at") or "") < since:
            continue
        name = workflows.get(r.get("orchestration_id")) or r.get("orchestration_id")
        t = totals.setdefault(name, {"workflow": name, "runs": 0, "model_dollars": 0.0})
        t["runs"] += 1
        t["model_dollars"] = round(t["model_dollars"] + float(r.get("total_cost_usd") or 0), 6)
    return {"items": _named(sorted(totals.values(), key=lambda t: -t["runs"]), args.get("name"), "workflow"),
            "window": f"the last {days} days (since {since[:10]})",
            "note": "Self-hosted has no credits: a run costs what its model providers charge, estimated here."}


async def _plan(args: dict) -> dict:
    return {"items": [], "note": "This is a self-hosted install: there is no plan and there are no credits. Runs "
                                 "cost only what your model providers charge."}


async def _models(args: dict) -> dict:
    from core.config import SECRET_SETTINGS, load_settings

    s = load_settings()
    keys = sorted(name[: -len("_key")] for name in SECRET_SETTINGS
                  if name.endswith("_key") and s.get(name) and not name.startswith(("login", "aws")))
    return {"default_model": s.get("model"), "builder_model": s.get("builder_model") or "(the default model)",
            "builder_model_for_quick_steps": s.get("builder_quick_model") or "(the builder model)",
            "providers_with_a_key": keys, "note": "Keys are never shown, only whether one is set."}


async def _agents(args: dict) -> dict:
    from core.routes.agents import load_user_agents

    out = [{"name": a.get("name"), "type": a.get("type"), "model": a.get("model") or "(the default model)",
            "tools": len(a.get("tools") or [])}
           for a in await load_user_agents() if not a.get("system") and a.get("type") != "builder"]
    return {"items": _named(out, args.get("name"))}


async def _workflows(args: dict) -> dict:
    from core.routes.orchestrations import load_orchestrations
    from core.store import schedules as store

    scheduled = {s.get("target_id") for s in await store.load() if s.get("target_type") == "orchestration"}
    out = [{"name": o.get("name"), "steps": len(o.get("steps") or []), "scheduled": o.get("id") in scheduled}
           for o in await load_orchestrations() if not o.get("system") and o.get("is_active") is not False]
    return {"items": _named(out, args.get("name")), "count": len(out)}


_SELF_HOSTED: dict[str, Reader] = {
    "schedules": _schedules, "runs": _runs, "channels": _channels, "phone": _phone, "widgets": _widgets,
    "tool_policy": _tool_policy, "knowledge": _knowledge, "usage": _usage, "plan": _plan, "models": _models,
    "agents": _agents, "workflows": _workflows,
}


async def read_workspace(args: dict, server_module: Any, events: list[dict]) -> dict:
    topic = (args.get("topic") or "").strip().lower()
    if topic not in TOPICS:
        return {"error": f"topic must be one of {', '.join(TOPICS)}."}
    reader = _HOST.get(topic) or _SELF_HOSTED[topic]
    try:
        result = await reader(args)
    except Exception as exc:  # noqa: BLE001 — a reader that fails says so; the model must not fill the gap
        return {"topic": topic, "error": f"I can't read {topic} right now ({type(exc).__name__}). Say so."}
    return {"topic": topic, **result}


register_builder_tool(SCHEMA, read_workspace)
