"""
Data seeders — write agents / orchestrations / API keys into the sandboxed
SYNAPSE_DATA_DIR so route handlers load them exactly as in production.

These call the same persistence helpers the app uses (``save_user_agents``,
``save_orchestrations``, ``generate_api_key``), whose JsonStore updates its
in-memory cache on write, so a subsequent ``load_*`` returns the seeded data
immediately (no cache-TTL race).
"""
from __future__ import annotations

import uuid
from typing import Any


# ── builders ─────────────────────────────────────────────────────────────────
def make_agent(**overrides: Any) -> dict:
    """A minimal, valid conversational agent with no tools/repos/db."""
    agent = {
        "id": overrides.pop("id", f"agent_{uuid.uuid4().hex[:8]}"),
        "name": "Test Agent",
        "description": "A test agent.",
        "avatar": "default",
        "type": "conversational",
        "tools": [],
        "repos": [],
        "db_configs": [],
        "system_prompt": "You are a helpful test assistant.",
        "model": None,
        "max_turns": 3,
        "delegate_agent_ids": [],
    }
    agent.update(overrides)
    return agent


def make_orchestrator_agent(orchestration_id: str, **overrides: Any) -> dict:
    return make_agent(
        type="orchestrator",
        orchestration_id=orchestration_id,
        name=overrides.pop("name", "Test Orchestrator"),
        **overrides,
    )


def make_orchestration(**overrides: Any) -> dict:
    """A single-step 'print' orchestration — runs with no LLM by default.

    Callers that want an LLM/agent step pass their own ``steps``/``entry_step_id``.
    """
    step_id = overrides.pop("entry_step_id", "step_1")
    orch = {
        "id": overrides.pop("id", f"orch_{uuid.uuid4().hex[:8]}"),
        "name": "Test Orchestration",
        "description": "A test orchestration.",
        "avatar": "default",
        "entry_step_id": step_id,
        "trigger": "manual",
        "steps": overrides.pop("steps", [
            {
                "id": step_id,
                "name": "Say Hello",
                "type": "print",
                "print_content": "Hello from the test orchestration.",
                "output_key": "greeting",
                "next_step_id": None,
            }
        ]),
        "edges": overrides.pop("edges", []),
    }
    orch.update(overrides)
    return orch


# ── persistence ──────────────────────────────────────────────────────────────
def seed_agents(agents: list[dict]) -> list[dict]:
    from core.routes.agents import save_user_agents
    save_user_agents(agents)
    return agents


def seed_orchestrations(orchs: list[dict]) -> list[dict]:
    from core.routes.orchestrations import save_orchestrations
    save_orchestrations(orchs)
    return orchs


def seed_api_key(name: str = "test") -> tuple[str, dict]:
    """Create a real API key. Returns (raw_key, record). raw_key -> Bearer token."""
    from core.api_keys import generate_api_key
    return generate_api_key(name)
