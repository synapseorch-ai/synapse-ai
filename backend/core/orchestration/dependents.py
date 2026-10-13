"""What runs a workflow, so that deleting it never leaves them running nothing (F408).

A workflow is run by its workflow agent — which is how chat, a widget, a channel,
a phone number, the OpenAI-compatible endpoint and every other workflow reach
it — and by the schedules that target it. Deleting the workflow left all of them
in place: in the self-hosted product each then answered "Orchestration '…' not
found", and in cloud, which keeps a deleted workflow's row, they went on running
the workflow its owner had deleted.

C4 kept the builder's clean-up from removing a workflow something runs (F429);
this is the same rule at every other door that deletes one. The doors refuse,
naming what runs it, and the person decides: point those at another workflow, or
delete them first.

`what_runs` reads the engine's own stores. `refusal` is only the sentence, from
whatever a host found, so cloud — which reads its own tables — says it in the
same words.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.log import get_logger

_log = get_logger(__name__)


@dataclass(frozen=True)
class Dependent:
    """Something that runs a workflow, or uses the workflow agent that does.

    `kind` is "agent" or "schedule" for what runs it, and "widget", "channel",
    "schedule" or "workflow" for what uses one of its agents — those carry the
    agent's `id` in `via`.
    """

    kind: str
    name: str
    via: str | None = None
    id: str | None = None


def _gone(orchestration: dict) -> bool:
    return bool(orchestration.get("system")) or orchestration.get("is_active", True) is False


def _calls(orchestration: dict, agent_id: str) -> bool:
    """Whether one of the workflow's steps — a Loop body or a branch included — runs `agent_id`."""
    return any(step.get("agent_id") == agent_id for step in orchestration.get("steps") or [])


async def what_runs(orch_id: str) -> list[Dependent]:
    """What runs workflow `orch_id` in this tenant, and what uses the agents that do.

    The workflow agents are read as the delete has to read them: a failure there
    raises, so a delete is never let through on a read that did not happen. What
    uses them only fills in the sentence, and a store that cannot be read leaves
    its part out.
    """
    from core.store import schedules as schedule_store
    from core.store.resources import load_agents, load_orchestrations

    agents = [a for a in await load_agents()
              if a.get("type") == "orchestrator" and a.get("orchestration_id") == orch_id
              and not a.get("system")]
    schedules = await schedule_store.load()
    found: list[Dependent] = []
    if agents:
        try:
            from core.chat_widgets.store import list_widgets
            widgets = await list_widgets()
        except Exception as e:  # noqa: BLE001 — the agent alone is reason enough
            _log.warning(f"what runs {orch_id}: widgets not read: {e}")
            widgets = []
        workflows = [o for o in await load_orchestrations() if o.get("id") != orch_id and not _gone(o)]
        for agent in agents:
            name, via = agent.get("name") or agent["id"], agent["id"]
            found.append(Dependent("agent", name, id=via))
            found += [Dependent("widget", w.get("name") or w["id"], via=via)
                      for w in widgets if w.get("agent_id") == agent["id"]]
            try:
                from core.messaging.store import get_channels_for_agent
                found += [Dependent("channel", c.get("name") or c["id"], via=via)
                          for c in await get_channels_for_agent(agent["id"])]
            except Exception as e:  # noqa: BLE001
                _log.warning(f"what runs {orch_id}: channels not read: {e}")
            found += [Dependent("schedule", s.get("name") or s["id"], via=via)
                      for s in schedules if s.get("target_type") == "agent" and s.get("target_id") == agent["id"]]
            found += [Dependent("workflow", o.get("name") or o["id"], via=via)
                      for o in workflows if _calls(o, agent["id"])]
    found += [Dependent("schedule", s.get("name") or s["id"])
              for s in schedules if s.get("target_type") == "orchestration" and s.get("target_id") == orch_id]
    return found


def _join(items: list[str]) -> str:
    if len(items) < 2:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def refusal(name: str, dependents: list[Dependent]) -> str:
    """The sentence a refused delete answers with: what runs workflow `name`, and what to do.

    "'Invoice triage' was not deleted: the agent 'Invoice triage agent' runs it,
    and the widget 'Support' uses that agent. Point it at another workflow, or
    delete it, first."
    """
    runners = [d for d in dependents if d.via is None]
    parts = []
    for runner in runners:
        part = f"the {runner.kind} '{runner.name}' runs it"
        if runner.kind == "agent":
            uses = [f"the {d.kind} '{d.name}'" for d in dependents if d.via is not None and d.via == runner.id]
            if uses:
                part += f", and {_join(uses)} {'uses' if len(uses) == 1 else 'use'} that agent"
        parts.append(part)
    them = "it" if len(runners) == 1 else "them"
    return f"'{name}' was not deleted: {'; '.join(parts)}. Point {them} at another workflow, or delete {them}, first."
