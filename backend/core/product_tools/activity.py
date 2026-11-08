"""What has been going on in the workspace, as product tools (ASSIST1, D291, F680).

Naveen's questions for the assistant: "what's the status of my workflows", "did the agent finish the job", "what did
the other agent do", "summarise today". The builder's `read_workspace` answered what exists and how it is set; these
answer what happened, from the records the run page, the bell and Chat read:

* `summarise_activity`: a window (a day by default): runs by status, each failure and why, what waits on a person,
  the schedules that ran, what it cost;
* `agent_activity`: an agent's recent conversations (the last question and answer, keys masked), only those the
  caller may open by the host's rule (`set_conversation_rule`, F1361), and the workflows that use it, with how each
  one's last run went.

One run step by step is MCP2's `get_run`; what waits is its `list_waiting`. Reads, anyone's (a viewer's too); a host
whose truth lives elsewhere (cloud's credits) registers its own reader for the assistant's topic
(`builder_readers.set_workspace_reader`).
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable
from typing import Any

from core.product_tools.errors import ToolRefusal
from core.product_tools.registry import ProductTool

#: How much of a conversation's last question and answer a read carries.
_TEXT = 300
_CONVERSATIONS = 5
_WINDOW_RUNS = 1000

#: A host's rule for whose conversations a read may carry (F1361): given the caller and session ids, the ones it may
#: not open, as its Chat decides it. None: every person's conversation is the caller's (self-hosted, one person).
ConversationRule = Callable[[Any, list[str]], Awaitable[set[str]]]
_conversation_rule: ConversationRule | None = None

#: Said when the host's rule cannot answer: the read is refused, never every conversation shown.
NOT_READ = "This agent's conversations can't be read right now. Try again in a moment."


def set_conversation_rule(rule: ConversationRule | None) -> None:
    """A host's own rule for which conversations a caller may open; None puts back self-hosted's (all of them)."""
    global _conversation_rule
    _conversation_rule = rule


async def _openable(caller: Any, sessions: list[dict]) -> list[dict]:
    """`sessions` less those the host's rule says `caller` may not open, asked before any is read."""
    if _conversation_rule is None or not sessions:
        return sessions
    try:
        hidden = await _conversation_rule(caller, [str(s["session_id"]) for s in sessions])
    except Exception as exc:  # noqa: BLE001 — any failure of the host's rule refuses the read
        raise ToolRefusal(NOT_READ) from exc
    return [s for s in sessions if str(s["session_id"]) not in hidden]


def _clip(text: Any) -> str | None:
    if not text:
        return None
    from core.secret_scrub import scrub_credentials

    clean, _ = scrub_credentials(str(text))
    return clean if len(clean) <= _TEXT else clean[:_TEXT] + "…"


def _days(args: dict, default: int = 1) -> int:
    try:
        return max(1, min(int(args.get("days") or default), 90))
    except (TypeError, ValueError):
        return default


def _after(stamp: Any, since: dt.datetime) -> bool:
    """Whether an ISO time (a `Z` or an offset, or none, read as UTC) is at or after `since`."""
    if not stamp:
        return False
    try:
        when = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return False
    return (when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)) >= since


async def _workflow_names() -> dict[str, str]:
    from core.routes.orchestrations import load_orchestrations

    return {o.get("id"): o.get("name") for o in await load_orchestrations()}


async def summarise_activity(caller: Any, args: dict[str, Any]) -> dict:
    from core.orchestration.run_view import list_runs
    from core.store import schedules as store

    days = _days(args)
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    names = await _workflow_names()
    rows = [r for r in await list_runs(limit=_WINDOW_RUNS) if _after(r.get("started_at"), since)]
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.get("status") or "unknown"] = counts.get(r.get("status") or "unknown", 0) + 1

    def named(r: dict) -> str:
        return names.get(r.get("orchestration_id")) or r.get("orchestration_id") or "?"

    schedules = [{"schedule": s.get("name"), "last_ran_at": s.get("last_run_at")} for s in await store.load()
                 if _after(s.get("last_run_at"), since)]
    from core.product_tools import run_cost

    paid = run_cost.total(list((await run_cost.costs(rows)).values()))
    return {
        "window": f"the last {days} day{'s' if days != 1 else ''} (since {since:%Y-%m-%d %H:%M} UTC)",
        "runs": counts,
        "failed": [{"workflow": named(r), "why": r.get("error") or "(no reason was kept)", "run_id": r.get("run_id"),
                    "started_at": r.get("started_at")} for r in rows if r.get("status") == "failed"][:20],
        "waiting_for_a_person": [{"workflow": named(r), "run_id": r.get("run_id"), "since": r.get("started_at")}
                                 for r in rows if r.get("waiting_for_human")][:20],
        "running": [{"workflow": named(r), "run_id": r.get("run_id")} for r in rows
                    if r.get("status") == "running"][:20],
        "schedules_that_ran": schedules,
        # What was paid, as the host counts it (F843): credits where it charges them, model dollars always.
        **paid,
        "note": "Workflow runs only; chat conversations are in agent_activity. Tool calls an agent stopped on are in "
                "what waits (`waiting`).",
    }


async def _find_agent(name: str) -> dict | None:
    from core.routes.agents import load_user_agents

    needle = (name or "").strip().lower()
    if not needle:
        return None
    agents = [a for a in await load_user_agents() if not a.get("system") and a.get("type") != "builder"]
    exact = [a for a in agents if str(a.get("name") or "").lower() == needle or a.get("id") == name]
    return (exact or [a for a in agents if needle in str(a.get("name") or "").lower()] or [None])[0]


def _a_persons_conversation(session: dict) -> bool:
    """What Chat's History lists (`routes/sessions.py`): not the builder's own, a run's, or an API call's."""
    sid, agent = str(session.get("session_id") or ""), str(session.get("agent_id") or "")
    return not (sid.startswith(("builder:", "orch_", "oai_", "helperc_")) or agent.startswith(("subtask_",
                                                                                                "agent_native_builder")))


async def agent_activity(caller: Any, args: dict[str, Any]) -> dict:
    from core.orchestration.run_view import list_runs
    from core.routes.orchestrations import load_orchestrations
    from core.session import _get_conversation_history, list_chat_sessions
    from core.store import schedules as store

    agent = await _find_agent(str(args.get("name") or args.get("agent") or ""))
    if agent is None:
        raise ToolRefusal("No agent by that name in this workspace. read_workspace topic `agents` lists them.")

    talks = []
    persons = [s for s in await list_chat_sessions(agent_id=agent["id"]) if _a_persons_conversation(s)]
    for s in (await _openable(caller, persons))[:_CONVERSATIONS]:
        turns = await _get_conversation_history(s["session_id"], agent["id"])
        last = turns[-1] if turns else {}
        talks.append({"when": s.get("last_updated"), "turns": s.get("turn_count"),
                      "first_question": _clip(s.get("first_user_message")),
                      "last_question": _clip(last.get("user")), "last_answer": _clip(last.get("assistant")),
                      **({"from_a_schedule": True} if str(s["session_id"]).startswith("sched") else {})})

    using = [o for o in await load_orchestrations()
             if not o.get("system") and any(st.get("agent_id") == agent["id"] for st in o.get("steps") or [])]
    flows = []
    for o in using[:10]:
        [last] = (await list_runs(limit=1, orchestration_id=o["id"])) or [None]
        flows.append({"workflow": o.get("name"), "last_run": None if last is None else {
            "status": last.get("status"), "started_at": last.get("started_at"), "error": last.get("error"),
            "run_id": last.get("run_id")}})
    schedules = [s.get("name") for s in await store.load()
                 if s.get("target_type") == "agent" and s.get("target_id") == agent["id"]]
    return {"agent": agent.get("name"), "conversations": talks, "workflows": flows, "schedules": schedules,
            "note": "Newest conversations first, of those this person may open; keys in them are masked. A "
                    "workflow's own step-by-step record is topic `run`."}


_DAYS = {"days": {"type": "integer", "minimum": 1, "maximum": 90, "description": "How many days back (default 1)."}}


def entries() -> list[ProductTool]:
    return [
        ProductTool(
            name="summarise_activity",
            description=("What happened in this workspace over the last `days` days (default 1, today): workflow "
                         "runs by status, each failure and why, what waits on a person, schedules that ran, cost."),
            input_schema={"type": "object", "properties": dict(_DAYS)},
            handler=summarise_activity, kind="read", area="activity"),
        ProductTool(
            name="agent_activity",
            description=("What an agent has been doing: its recent conversations (the last question and answer) and "
                         "the workflows that use it, with how each one's last run went."),
            input_schema={"type": "object", "properties": {
                "name": {"type": "string", "description": "The agent's name (or part of it)."}},
                "required": ["name"]},
            handler=agent_activity, kind="read", area="activity"),
    ]
