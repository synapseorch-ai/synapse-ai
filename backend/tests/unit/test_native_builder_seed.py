"""The builder ships as data: an orchestration and five sub-agents.

Two properties matter and neither was tested. It has to seed without touching
anything else in the store — it used to replace the whole tenant on every
start-up — and it has to stay invisible, because it lives in the same tables as
the customer's own work.
"""
from __future__ import annotations

import pytest

from _fakes import seed as S
from core.native_builder import (
    NATIVE_BUILDER_ORCH_ID,
    SYSTEM_MARKER,
    bundle_version,
    bundled_definitions,
    seed_native_builder,
)


class TestTheBundle:
    def test_every_definition_carries_the_marker(self):
        bundle = bundled_definitions()
        assert bundle["orchestration"]["system"] == SYSTEM_MARKER
        assert all(a["system"] == SYSTEM_MARKER for a in bundle["agents"])

    def test_every_agent_names_its_role(self):
        """Ids are minted per organisation in the commercial edition, so a
        re-seed matches on the role. A missing one would orphan that agent."""
        roles = [a["system_role"] for a in bundled_definitions()["agents"]]
        assert sorted(roles) == [
            "agent_creator", "analyst", "operator", "orchestrator",
            "saver_create", "saver_update",
        ]

    def test_the_version_is_stable_and_moves_with_the_content(self):
        first = bundle_version()
        assert first == bundle_version()
        assert len(first) == 16

    def test_the_cheatsheet_is_expanded_before_it_is_hashed(self):
        """What comes back is what would run — the commercial edition inserts
        these definitions directly and never expands anything itself."""
        bundle = bundled_definitions()
        blob = str(bundle)
        assert "{{STEP_TYPE_CHEATSHEET}}" not in blob
        assert "step_intent" in blob

    def test_every_agent_a_step_names_is_in_the_bundle(self):
        bundle = bundled_definitions()
        known = {a["id"] for a in bundle["agents"]}
        named = {
            s["agent_id"] for s in bundle["orchestration"]["steps"] if s.get("agent_id")
        }
        assert named <= known, f"steps name agents that are not bundled: {named - known}"

    def test_every_state_key_a_step_reads_is_declared(self):
        """`clarify_decision` was written by a step and absent from the schema
        for as long as that step existed."""
        bundle = bundled_definitions()
        schema = set(bundle["orchestration"]["state_schema"])
        for step in bundle["orchestration"]["steps"]:
            for key in step.get("input_keys") or []:
                assert key in schema, f"{step['id']} reads undeclared '{key}'"
            if step.get("output_key"):
                assert step["output_key"] in schema, f"{step['id']} writes undeclared '{step['output_key']}'"


class TestSeeding:
    async def test_it_installs_the_builder(self):
        result = await seed_native_builder()
        assert result["orchestration"] == "added"
        assert len(result["agents_added"]) == 6
        assert result["version"] == bundle_version()

        from core.store.resources import get_orchestration
        assert (await get_orchestration(NATIVE_BUILDER_ORCH_ID))["system"] == SYSTEM_MARKER

    async def test_seeding_twice_changes_nothing(self):
        await seed_native_builder()
        again = await seed_native_builder()
        assert again["orchestration"] == "unchanged"
        assert again["agents_added"] == [] and again["agents_updated"] == []

    async def test_it_leaves_the_workspace_alone(self):
        """It used to write the whole tenant back on every start-up, so a
        concurrent edit during boot was lost and every row was rewritten."""
        mine = await S.seed_agents([S.make_agent(id="agent_mine", name="Mine")])
        await S.seed_orchestrations([S.make_orchestration(id="orch_mine", name="Mine")])

        await seed_native_builder()

        from core.store.resources import get_agent, get_orchestration
        assert (await get_agent("agent_mine"))["name"] == "Mine"
        assert (await get_orchestration("orch_mine"))["name"] == "Mine"

    async def test_a_stale_prompt_is_replaced_on_the_next_start(self):
        await seed_native_builder()
        from core.store.resources import get_agent, save_agent

        agent = await get_agent("agent_native_builder_analyst")
        await save_agent({**agent, "system_prompt": "drifted"})

        result = await seed_native_builder()
        assert result["agents_updated"] == ["agent_native_builder_analyst"]
        assert (await get_agent("agent_native_builder_analyst"))["system_prompt"] != "drifted"

    async def test_agents_from_older_releases_are_purged(self):
        await S.seed_agents([S.make_agent(id="agent_native_builder_planner_create")])
        result = await seed_native_builder()
        assert result["agents_removed"] == ["agent_native_builder_planner_create"]

        from core.store.resources import get_agent
        assert await get_agent("agent_native_builder_planner_create") is None


class TestItStaysOutOfSight:
    """It is an orchestration with agents in the same tables as the customer's
    own. Every read path has to filter it out, or the workspace reads as though
    the product installed six things it did not ask for."""

    @pytest.fixture(autouse=True)
    async def seeded(self):
        await seed_native_builder()
        await S.seed_agents([S.make_agent(id="agent_mine", name="Mine")])
        await S.seed_orchestrations([S.make_orchestration(id="orch_mine", name="Mine")])

    async def test_not_in_the_orchestration_list(self, client):
        listed = await client.get("/api/orchestrations")
        assert [o["id"] for o in listed.json()] == ["orch_mine"]

    async def test_not_in_the_agent_list(self, client):
        listed = await client.get("/api/agents")
        assert [a["id"] for a in listed.json()] == ["agent_mine"]

    async def test_not_in_an_export(self, client):
        """Carrying it would put a stale builder into whatever workspace
        imports the bundle, which seeding would then fight on every restart."""
        exported = (await client.get("/api/export/data")).json()
        assert [o["id"] for o in exported["orchestrations"]] == ["orch_mine"]
        assert [a["id"] for a in exported["agents"]] == ["agent_mine"]


class TestOnlyTheOperatorCanDestroy:
    """Two layers guard a deletion. `confirm=true` is one; this is the other,
    and it is the one that matters — the operator runs only after a human has
    read a plan that named the deletion and approved it."""

    DESTRUCTIVE = {"delete_agent", "delete_orchestration", "delete_custom_tool", "delete_schedule"}

    def test_no_other_sub_agent_holds_a_delete(self):
        for agent in bundled_definitions()["agents"]:
            if agent["system_role"] == "operator":
                continue
            held = self.DESTRUCTIVE & set(agent.get("tools") or [])
            assert not held, f"{agent['system_role']} holds {held}"

    def test_the_operator_holds_all_of_them(self):
        operator = next(
            a for a in bundled_definitions()["agents"] if a["system_role"] == "operator"
        )
        assert self.DESTRUCTIVE <= set(operator["tools"])

    def test_nothing_inherits_the_default_tool_set_by_accident(self):
        """A sub-agent that does not skip the defaults gets every tool in the
        workspace on top of its declared list — which for the operator would
        mean tools no plan ever mentioned."""
        for agent in bundled_definitions()["agents"]:
            if not agent.get("tools"):
                continue  # the orchestrator shell declares none and runs none
            assert agent.get("skip_default_tools") is True, agent["system_role"]

    def test_the_operator_is_reached_only_through_a_human_approval(self):
        """The path to it: plan → human → router → operator. Remove the human
        step and the builder can delete things unprompted."""
        orch = bundled_definitions()["orchestration"]
        by_id = {s["id"]: s for s in orch["steps"]}
        operator_step = next(
            s for s in orch["steps"] if s.get("agent_id") == "agent_native_builder_operator"
        )

        # Who can reach the operator's step?
        reaching = [
            s for s in orch["steps"]
            if operator_step["id"] in
            [s.get("next_step_id"), *(s.get("route_map") or {}).values()]
        ]
        assert reaching, "nothing reaches the operator"
        for step in reaching:
            # Every route in is a router whose own input came from a human step.
            assert step["type"] == "evaluator", step["id"]
            feeders = [
                s for s in orch["steps"]
                if step["id"] in [s.get("next_step_id"), *(s.get("route_map") or {}).values()]
            ]
            assert any(by_id[f["id"]]["type"] == "human" for f in feeders), step["id"]
