"""Every setting of an agent, and how Synapse AI reaches it (F1259; Naveen 10-30: "it should be able to build team and
browser agents and with helper if needed and all those things").

A plan could make an agent, a team and a browser agent, and a change could set an agent's name, model, tools, skills,
instructions and tool policy. Nothing set a team's way of working, helpers, a code agent's repos, an orchestrator's
workflow, turn limits, reasoning, output length, planning, boards or a browser agent's recordings. This is the one
table of every setting the agent editor has, each with the plan field that sets it on a new agent and the change op
that sets it on one that exists, or a stated reason it stays the editor's; a test walks `core.models.Agent` (and its
team, browser and boards parts) and `objects.NewAgent` both ways, so a setting added to either without a row fails.

`editor_problem` is the one check the agent editor's save makes of a whole agent (its own model, `core.models.Agent`):
the builder's update runs it too, so nothing a person couldn't save is saved.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Setting:
    #: The field, `part.field` for one inside the team, browser or boards part.
    field: str
    #: The editor's section, as the agent editor names it.
    section: str
    #: The NewAgent field a plan sets it with; None when a plan doesn't.
    plan: str | None = None
    #: The change op that sets it on an agent that exists; None when no change does.
    op: str | None = None
    #: Why it stays the editor's, said to the person; None when a plan and an op both reach it.
    cant: str | None = None
    #: Set by code, never by a person (an id, a provider read from the model).
    code: bool = False


SETTINGS: tuple[Setting, ...] = (
    Setting("id", "", code=True),
    Setting("provider", "", code=True),
    Setting("avatar", "Basics", cant="An agent's picture is chosen in its editor (Basics)."),
    Setting("name", "Basics", plan="name", op="rename_agent"),
    Setting("description", "Basics", plan="purpose", op="set_agent_settings"),
    Setting("type", "Basics", plan="type",
            cant="An agent's kind is set when it is made: a different kind is a new agent, made by a build."),
    Setting("system_prompt", "Instructions", plan="purpose", op="set_agent_instructions"),
    Setting("model", "Model", plan="needs", op="set_agent_model"),
    Setting("reasoning_effort", "Model", plan="reasoning_effort", op="set_agent_settings"),
    Setting("max_output_tokens", "Model", plan="max_output_tokens", op="set_agent_settings"),
    Setting("tools", "Tools", plan="tools", op="set_agent_tools"),
    Setting("skills", "Skills", plan="skills", op="set_agent_skills"),
    Setting("tool_policy", "Tool policy", op="set_tool_policy",
            cant="A new agent's tool policy is the workspace's; a change sets one for an agent that exists."),
    Setting("skip_default_tools", "Tools", plan="skip_default_tools", op="set_agent_settings"),
    Setting("memory", "Memory", plan="memory", op="set_agent_settings"),
    Setting("knowledge", "Knowledge", plan="knowledge", op="set_agent_settings"),
    Setting("boards", "Boards", plan="boards_own", op="set_agent_settings"),
    Setting("boards.own", "Boards", plan="boards_own", op="set_agent_settings"),
    Setting("planning", "Planning", plan="planning", op="set_agent_settings"),
    # Helpers are turned on by a plan's `helpers` and the op (spawn_subtask in its tools); they run on its own model.
    Setting("helper_model", "Helpers",
            cant="Helpers run on the agent's own model; a different model for them is picked in its editor (Helpers)."),
    Setting("max_turns", "Limits", plan="max_turns", op="set_agent_settings"),
    Setting("turn_limit_mode", "Limits", plan="turn_limit_mode", op="set_agent_settings"),
    Setting("delegate_agent_ids", "Team", plan="members", op="set_agent_team"),
    Setting("team", "Team", plan="team", op="set_agent_team"),
    Setting("team.mode", "Team", plan="team", op="set_agent_team"),
    Setting("team.at_once", "Team", plan="team", op="set_agent_team"),
    Setting("team.task_budget", "Team", plan="team", op="set_agent_team"),
    Setting("team.spend_limit_usd", "Team", plan="team", op="set_agent_team"),
    Setting("browser", "Browser", plan="browser", op="set_agent_browser"),
    Setting("browser.sites", "Browser", plan="browser", op="set_agent_browser"),
    Setting("browser.sessions", "Browser", plan="browser", op="set_agent_browser"),
    Setting("browser.recordings", "Browser", plan="browser", op="set_agent_browser"),
    Setting("browser.action_budget", "Browser", plan="browser", op="set_agent_browser"),
    Setting("browser.mode", "Browser", plan="browser", op="set_agent_browser"),
    Setting("repos", "Code", plan="repos", op="set_agent_code"),
    Setting("db_configs", "Code", plan="db_configs", op="set_agent_code"),
    Setting("orchestration_id", "Workflow", plan="workflow",
            cant="An orchestrator agent's workflow is set when it is made; another workflow is another agent."),
)

#: NewAgent's own fields that are code's or a plan's reasoning, never an editor setting: what a model needs (code picks
#: the model), the person's words for one, and the card's reasons.
PLAN_ONLY = frozenset({"model_wish", "long_inputs", "stronger_helps", "why", "servers"})


#: NewAgent fields that set part of another setting: `helpers` puts spawn_subtask in its tools.
PLAN_PARTS = {"helpers": "tools"}


def by_field() -> dict[str, Setting]:
    return {s.field: s for s in SETTINGS}


def cant(field: str) -> str | None:
    """Why a setting stays the editor's, in the person's words, or None."""
    row = by_field().get(field)
    return row.cant if row is not None else None


def editor_problem(agent: dict[str, Any], only: set[str] | frozenset[str] | None = None) -> str | None:
    """What the agent editor's own model (`core.models.Agent`) refuses in this agent's settings, in one sentence; None
    when it saves. The editor's save starts with it, so the builder's update does too (F1259). `only`: the fields an
    update sets, the ones it speaks to, so an agent saved before a rule (a field it lacks) is still changed."""
    from pydantic import ValidationError

    from core.models import Agent

    try:
        # The editor's own fields only: a saved agent also keeps bookkeeping (when it was made, `system`) its model
        # has no field for.
        Agent.model_validate({k: v for k, v in agent.items() if k in Agent.model_fields})
    except ValidationError as exc:
        for error in exc.errors():
            loc = [str(p) for p in error.get("loc") or ()]
            if only is None or (loc and loc[0] in only):
                return f"That can't be saved: {'.'.join(loc) or 'the agent'}: {error.get('msg')}."
    return None
