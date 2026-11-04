"""An upgrade leaves none of the old builder's agents behind as the person's own (F1351; REL3's 1.9.3 → 2.0
upgrade, 11-04: a release blocker for upgrades).

1.9.3 shipped its builder as agents of the product's own (`agent_native_builder_*`): an Agent Creator, a Requirements
Analyst and two Savers. 2.0's builder is other agents, and the seeder removed only the two ids its legacy list named
(`planner_create`, `planner_update`), while the importer brought every agent of an install's `user_agents.json`
across. So REL3's upgraded install kept four of them, with 1.9's prompts, among the person's own agents and in every
picker. The class: every stored `agent_native_builder*` id the bundle does not ship is the product's, never a
person's — left out at import, not counted as the person's work, and removed by the seeder. By its id alone: an
agent of the person's called "Builder …" has an ordinary id, and stays.
"""
from __future__ import annotations

import json

from sqlalchemy import select

from core.store import session
from core.store.importer import import_data_dir, store_is_empty
from core.store.models import AgentDB

#: 1.9.3's builder agents that 2.0 does not ship (v1.9.3's native_builder/agents/).
STALE = {
    "agent_native_builder_agent_creator": "Builder — Agent Creator",
    "agent_native_builder_analyst": "Builder — Requirements Analyst",
    "agent_native_builder_saver_create": "Builder - Create Saver",
    "agent_native_builder_saver_update": "Builder — Update Saver",
}
#: A person's own agent called "Builder …": its id is an ordinary one.
THEIRS = {"id": "agent_1700000123", "name": "Builder — Kitchen planner", "type": "conversational",
          "model": "gpt-4.1-mini", "system_prompt": "Plan kitchens.", "tools": []}


def _old(aid: str, name: str) -> dict:
    return {"id": aid, "name": name, "type": "conversational", "system_prompt": "1.9's own prompt.", "tools": []}


async def _ids() -> set[str]:
    async with session() as s:
        return set((await s.execute(select(AgentDB.id))).scalars())


async def _stored(*agents: dict) -> None:
    from core.store.resources import save_agent

    for agent in agents:
        await save_agent(agent)


async def test_the_seeder_removes_every_builder_agent_it_no_longer_ships():
    from core.native_builder import seed_native_builder
    from core.native_builder.seed import bundled_definitions

    await _stored(*(_old(a, n) for a, n in STALE.items()), THEIRS)
    done = await seed_native_builder()
    ids = await _ids()
    assert not set(STALE) & ids, set(STALE) & ids
    assert set(STALE) <= set(done["agents_removed"]), done
    assert THEIRS["id"] in ids, "a person's agent called Builder … is theirs: its id is an ordinary one"
    assert {a["id"] for a in bundled_definitions()["agents"]} <= ids


async def test_an_import_leaves_them_out(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / "user_agents.json").write_text(json.dumps([
        *(_old(a, n) for a, n in STALE.items()),
        _old("agent_native_builder", "Synapse AI Builder"),  # a bundled one, as 1.9.3 kept it: the seeder's
        THEIRS,
    ]), encoding="utf-8")
    counts = await import_data_dir(root)
    ids = await _ids()
    assert THEIRS["id"] in ids and counts["user_agents"] == 1, (ids, counts)
    assert not {i for i in ids if i.startswith("agent_native_builder")}, ids


async def test_only_the_products_agents_leave_the_store_empty():
    await _stored(*(_old(a, n) for a, n in STALE.items()))
    assert await store_is_empty() is True, "the old builder's agents are not the person's work"
    await _stored(THEIRS)
    assert await store_is_empty() is False
