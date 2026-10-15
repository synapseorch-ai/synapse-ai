"""Teams: a lead and members working in parallel (TEAM1a, D283; 20-agent-teams.md Part A).

A Team agent is a `delegate` whose `team` block says `mode: "team"` (Q1). Without the block it is
today's delegate, one member at a time through `delegate_to_agent` (D204).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: The keys and their shipped defaults. `core.config._DEFAULTS` carries the same values; a test holds them equal.
DEFAULTS: dict[str, Any] = {
    # The most members at once, whatever a Team agent asks for (Q2a: default 10, the owner's to change).
    "team_at_once_ceiling": 10,
}


@dataclass(frozen=True)
class TeamConfig:
    """A Team agent's settings as this install allows them."""

    at_once: int
    task_budget: int
    spend_limit_usd: float | None


def config_of(agent: Mapping[str, Any], *, settings: Mapping[str, Any] | None = None) -> TeamConfig | None:
    """The team settings of an agent whose members work as a team; None for every other agent."""
    if (agent or {}).get("type") != "delegate":
        return None
    block = agent.get("team")
    if not isinstance(block, Mapping) or block.get("mode") != "team":
        return None
    if settings is None:
        from core.config import load_settings
        settings = load_settings()
    from core.models import AgentTeam
    knobs = AgentTeam(**block)
    ceiling = settings.get("team_at_once_ceiling")
    ceiling = DEFAULTS["team_at_once_ceiling"] if ceiling is None else max(1, int(ceiling))
    return TeamConfig(at_once=min(knobs.at_once, ceiling), task_budget=knobs.task_budget,
                      spend_limit_usd=knobs.spend_limit_usd)
