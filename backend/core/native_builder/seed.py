"""Idempotent seeding of the native builder orchestration and its sub-agents.

Reads bundled JSON definitions from this package and syncs them into the store.

The builder is the product demonstrating itself: an ordinary orchestration with
ordinary sub-agents, sitting in the same tables as the customer's own. What
keeps the two apart is the `system: "native_builder"` marker every bundled
definition carries — read paths filter on it, so the builder never offers its
own machinery as something to edit, delete, schedule or count against a plan.

`bundled_definitions` and `bundle_version` exist for the commercial edition,
which cannot seed from here: it has many organisations in one database and
mints its own ids per organisation. It takes the definitions, remaps them, and
stamps the version so it knows when to re-seed.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENTS_DIR = os.path.join(_HERE, "agents")
_ORCH_FILE = os.path.join(_HERE, "orchestration.json")

NATIVE_BUILDER_ORCH_ID = "orch_native_builder"
NATIVE_BUILDER_AGENT_ID = "agent_native_builder"

#: The value of the `system` marker on every bundled definition.
SYSTEM_MARKER = "native_builder"

#: Every builder run's id begins with this. It is what tells the rest of the
#: product that a run belongs to the builder panel — the run id and not the
#: orchestration id, because a resumed run emits no `orchestration_start` and
#: so never says which orchestration it is.
BUILDER_RUN_PREFIX = "builder_"

# Agent IDs that were shipped by earlier versions of the native builder and
# should be purged on startup. Kept here so stale entries get cleaned up from
# the store when users upgrade.
LEGACY_BUILDER_AGENT_IDS: set[str] = {
    "agent_native_builder_planner_create",
    "agent_native_builder_planner_update",
}


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _expand_cheatsheet(text: str) -> str:
    # Imported lazily to avoid a circular import (this module is imported from __init__.py).
    from . import STEP_TYPE_CHEATSHEET
    if not isinstance(text, str):
        return text
    return text.replace("{{STEP_TYPE_CHEATSHEET}}", STEP_TYPE_CHEATSHEET)


def _apply_cheatsheet_to_agent(agent: dict) -> dict:
    sp = agent.get("system_prompt")
    if isinstance(sp, str):
        agent["system_prompt"] = _expand_cheatsheet(sp)
    return agent


def _apply_cheatsheet_to_orch(orch: dict) -> dict:
    for step in orch.get("steps", []) or []:
        pt = step.get("prompt_template")
        if isinstance(pt, str):
            step["prompt_template"] = _expand_cheatsheet(pt)
    return orch


def _load_bundled_agents() -> list[dict]:
    if not os.path.isdir(_AGENTS_DIR):
        return []
    out: list[dict] = []
    for name in sorted(os.listdir(_AGENTS_DIR)):
        if name.endswith(".json"):
            out.append(_apply_cheatsheet_to_agent(_load_json(os.path.join(_AGENTS_DIR, name))))
    return out


def _load_bundled_orchestration() -> dict:
    return _apply_cheatsheet_to_orch(_load_json(_ORCH_FILE))


def bundled_definitions() -> dict[str, Any]:
    """The builder as shipped: `{"orchestration": {...}, "agents": [...]}`.

    Cheatsheet tokens already expanded, so what comes back is what would run.
    """
    return {"orchestration": _load_bundled_orchestration(), "agents": _load_bundled_agents()}


def bundle_version() -> str:
    """A short hash of the expanded definitions.

    Stamped on a seeded copy so a later release can tell whether that copy is
    the current one without diffing fifteen steps and five system prompts.
    """
    payload = json.dumps(bundled_definitions(), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


async def seed_native_builder() -> dict:
    """Sync the builder orchestration and sub-agents from the bundled definitions.

    Bundled entries always win, so tool-list and prompt fixes land on restart.
    Everything else in the store is left exactly as it is: the writes here are
    one row each, because this used to replace the whole tenant — which meant
    every start-up rewrote every agent and orchestration a customer owned.

    Returns: {"agents_added": [...], "agents_updated": [...], "agents_removed": [...],
              "orchestration": "added"|"updated"|"unchanged", "version": "..."}.
    """
    from core.routes.agents import load_user_agents
    from core.routes.orchestrations import load_orchestrations
    from core.store.resources import delete_agent, save_agent, save_orchestration

    bundled_agents = _load_bundled_agents()
    bundled_orch = _load_bundled_orchestration()

    existing_agents = {a.get("id"): a for a in await load_user_agents()}
    agents_added: list[str] = []
    agents_updated: list[str] = []
    agents_removed: list[str] = []

    for legacy_id in LEGACY_BUILDER_AGENT_IDS:
        if legacy_id in existing_agents:
            await delete_agent(legacy_id)
            agents_removed.append(legacy_id)

    for bundled in bundled_agents:
        aid = bundled["id"]
        current = existing_agents.get(aid)
        if current == bundled:
            continue
        await save_agent(bundled)
        (agents_updated if current is not None else agents_added).append(aid)

    existing_orch = next(
        (o for o in await load_orchestrations() if o.get("id") == bundled_orch["id"]), None
    )
    if existing_orch == bundled_orch:
        orch_status = "unchanged"
    else:
        await save_orchestration(bundled_orch)
        orch_status = "updated" if existing_orch is not None else "added"

    return {
        "agents_added": agents_added,
        "agents_updated": agents_updated,
        "agents_removed": agents_removed,
        "orchestration": orch_status,
        "version": bundle_version(),
    }
