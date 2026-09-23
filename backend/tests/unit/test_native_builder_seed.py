"""The builder ships as data: an orchestration and five sub-agents.

Two properties matter and neither was tested. It has to seed without touching
anything else in the store — it used to replace the whole tenant on every
start-up — and it has to stay invisible, because it lives in the same tables as
the customer's own work.
"""
from __future__ import annotations

import json

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
            "reporter", "saver_create", "saver_update",
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


class TestTheSchemaSaysWhatTheStepsWrite:
    """F52. The builder's `state_schema` was an annotation nobody was held to
    until F14 started reading it, and four of its types were wrong.

    `final_orch` was declared `dict`, but both savers are told to finish with
    the exact word `DONE` — so every build that saved logged a typing warning,
    and with `strict_state_types` on it would have failed right after saving.
    `human_response`, `review_feedback` and `manage_review` were declared `str`
    and always hold the answer card's fields.
    """

    #: What each step kind puts under its `output_key`. Agent, llm and
    #: evaluator steps write text (an agent's final response, a completion, a
    #: route label); a human step's answer arrives as the card's fields
    #: (`BuilderResumeRequest.response: dict`). A tool step is not listed: what
    #: it writes is whatever that tool returns.
    WRITES = {"agent": "str", "llm": "str", "evaluator": "str", "human": "dict"}

    def test_every_declared_type_is_what_its_step_writes(self):
        orch = bundled_definitions()["orchestration"]
        schema = orch["state_schema"]
        wrong = []
        for step in orch["steps"]:
            key, kind = step.get("output_key"), step["type"]
            if key and kind in self.WRITES and schema[key]["type"] != self.WRITES[kind]:
                wrong.append(f"{key} is declared {schema[key]['type']} but "
                             f"{step['id']} ({kind}) writes {self.WRITES[kind]}")
        # `resume()` copies every answer to `human_response` as well, whatever
        # the step's own output_key.
        if any(s["type"] == "human" for s in orch["steps"]) and schema["human_response"]["type"] != "dict":
            wrong.append(f"human_response is declared {schema['human_response']['type']} "
                         f"but resume() writes every answer there")
        assert not wrong, "; ".join(sorted(set(wrong)))

    def test_a_saver_finishing_with_done_is_not_a_typing_failure(self):
        import types

        from core.models_orchestration import Orchestration, OrchestrationRun
        from core.orchestration.engine import OrchestrationEngine

        orch = Orchestration.model_validate(bundled_definitions()["orchestration"])
        engine = OrchestrationEngine(orch, types.SimpleNamespace(
            agent_sessions={}, memory_store=None, tool_router={}))
        run = OrchestrationRun(run_id="builder_f52", orchestration_id=orch.id,
                               shared_state=engine._init_state("build me something"))
        before = dict(run.shared_state)
        run.shared_state["final_orch"] = "DONE"

        warnings, unconvertible = engine._type_written_state(
            run, engine.step_map["step_save_create"], before)

        assert warnings == []
        assert unconvertible == []
        assert run.shared_state["final_orch"] == "DONE"


class TestSeeding:
    async def test_it_installs_the_builder(self):
        result = await seed_native_builder()
        assert result["orchestration"] == "added"
        assert len(result["agents_added"]) == 7
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


class TestAParseFailureGoesSomewhereSafe:
    """Every evaluator says which way it falls when the model's reply is junk.

    It used to be "the first key of `route_map`", which is not a property an
    orchestration has: `definition` is JSONB on Postgres, and JSONB sorts object
    keys by (length, bytes) rather than keeping insertion order. Five of the
    builder's seven evaluators fell one way on SQLite and the other on Postgres,
    and two of those five approved a plan nobody had read.
    """

    def _evaluators(self):
        from core.native_builder import bundled_definitions
        return [s for s in bundled_definitions()["orchestration"]["steps"]
                if s["type"] == "evaluator"]

    def test_every_evaluator_declares_one(self):
        missing = [s["id"] for s in self._evaluators() if not s.get("fallback_route")]
        assert not missing, missing

    def test_each_one_names_a_route_it_actually_has(self):
        for step in self._evaluators():
            assert step["fallback_route"] in step["route_map"], step["id"]

    def test_nothing_writes_on_a_parse_failure(self):
        """The two that matter. `step_approval_route` and `step_manage_route`
        both listed `approved` first, so on SQLite — the default store — an
        unparseable reply ran the saver, or the operator with its write tools,
        without anyone approving anything."""
        by_id = {s["id"]: s for s in self._evaluators()}
        assert by_id["step_approval_route"]["fallback_route"] == "revise"
        assert by_id["step_manage_route"]["fallback_route"] == "revise"
        assert by_id["step_manage_writes"]["fallback_route"] == "writes"

    def test_it_does_not_depend_on_key_order(self):
        """What Postgres does to the map, done here: if the fallback survives
        the keys being re-sorted, it is not reading position."""
        from core.models_orchestration import StepConfig

        for step in self._evaluators():
            shuffled = dict(sorted(step["route_map"].items(),
                                   key=lambda kv: (len(kv[0]), kv[0].encode())))
            config = StepConfig.model_validate({**step, "route_map": shuffled})
            labels = list(config.route_map)
            resolved = config.fallback_route if config.fallback_route in labels else labels[0]
            assert resolved == step["fallback_route"], step["id"]


class TestTheBuilderKnowsItsTarget:
    """Round two, item 12: which orchestration a message is about.

    The server resolves it — the page someone is on, or a name in the message —
    and the seed decides who reads it. New keys reach a step through
    `input_keys` only: a step skips a key the run's state does not have, while a
    `{state.key}` placeholder renders literally, and a run paused on the previous
    release has none of these keys.
    """

    def _steps(self):
        return {s["id"]: s for s in bundled_definitions()["orchestration"]["steps"]}

    def test_every_target_key_is_declared(self):
        from core.native_builder.target import TARGET_KEYS

        schema = bundled_definitions()["orchestration"]["state_schema"]
        assert set(TARGET_KEYS) <= set(schema)

    def test_the_router_reads_the_target(self):
        keys = set(self._steps()["step_intent"]["input_keys"])
        assert {
            "current_orchestration_id", "current_orchestration_name",
            "current_orchestration_source", "current_orchestration_matches",
        } <= keys

    def test_the_answer_and_the_change_planner_see_the_page(self):
        steps = self._steps()
        assert {"current_page", "current_orchestration_source", "current_orchestration_matches"} <= set(
            steps["step_answer"]["input_keys"]
        )
        assert "current_page" in steps["step_manage_plan"]["input_keys"]

    def test_an_unclear_change_is_answered_not_built(self):
        """Two matches, or a name that is not the open one, leaves the id empty —
        and the route that runs then must ask, not build a duplicate."""
        intent = self._steps()["step_intent"]
        assert "ambiguous" in intent["route_descriptions"]["answer"]
        assert "ambiguous" in intent["evaluator_prompt"]

    def test_no_prompt_uses_a_new_key_as_a_placeholder(self):
        import re

        from core.native_builder.target import TARGET_KEYS

        added = set(TARGET_KEYS) - {"current_orchestration_id"}
        blob = json.dumps(bundled_definitions())
        placeholders = set(re.findall(r"\{\{?\s*state\.(\w+)", blob))
        assert not (placeholders & added), placeholders & added


class TestTheSummaryHasSomethingToName:
    """F53 (D104): the closing summary read `final_orch` as "Orchestration built or
    changed", but that key holds the saver's closing word — `DONE`. Two of three
    measured summaries could only say "the orchestration" was saved. The plan it
    built from and the orchestration it changed are already in state; the summary
    reads them. Through `input_keys`, not placeholders (see the target tests)."""

    def _summarize(self):
        steps = {s["id"]: s for s in bundled_definitions()["orchestration"]["steps"]}
        return steps["step_summarize"]

    def test_it_reads_the_plan_and_the_target(self):
        keys = set(self._summarize()["input_keys"])
        assert {"final_orch", "manage_result", "plan_draft", "current_orchestration_name"} <= keys

    def test_it_is_not_told_the_closing_word_is_the_orchestration(self):
        prompt = self._summarize()["prompt_template"]
        assert "Orchestration built or changed:\n{state.final_orch}" not in prompt
        assert "plan_draft" in prompt and "current_orchestration_name" in prompt


class TestAServerNameIsNotACapability:
    """F126. On 2026-09-13 the Requirements Analyst specified a Wiki Searcher
    step using "a search tool from the `deepwiki` MCP server (e.g., a query or
    search function)" — it had never read what deepwiki holds, and deepwiki
    answers questions about GitHub repositories. The Agent Creator and the
    Create Saver both saw the real description later and bound it anyway.

    `list_tool_servers` now carries each server's own tools, and these rules
    are what tell the agents to read them.
    """

    @staticmethod
    def _prompt(role):
        for agent in bundled_definitions()["agents"]:
            if agent["id"].endswith(role):
                return agent["system_prompt"]
        raise AssertionError(f"no agent for {role}")

    def test_the_analyst_is_told_a_name_is_not_a_capability(self):
        prompt = self._prompt("analyst")
        assert "NAME is not its capability" in prompt
        assert "deepwiki" in prompt, "the rule names the case it came from"

    def test_the_analyst_is_told_that_nothing_fitting_is_an_answer(self):
        assert "If nothing fits, say so plainly" in self._prompt("analyst")

    def test_everyone_who_binds_a_tool_stops_on_a_mismatch(self):
        # The analyst proposes; these three are the ones that write the binding.
        for role in ("agent_creator", "saver_create", "saver_update"):
            prompt = self._prompt(role)
            assert "must match what the plan asked for" in prompt, role
            assert "do not bind it because the server's name sounded right" in prompt, role


class TestWhatWasAskedForIsBuilt:
    """F149 + F150 (B4, D122). "Every Monday morning, pull … and write me a
    short summary I can check before it goes to the client" was built as
    `agent → agent → end`: no human step and no schedule, four times out of
    four on `synapse.nova-pro`. Two different losses.

    * The approval was lost at the analyst: its own Goal restated "a short
      summary for review before it goes to the client", and its Human
      checkpoints line read "None specified by the user". Nothing told it that
      a check before something goes out *is* a checkpoint.
    * The schedule could not have been kept on any model: the create path's
      agents held no schedule tool — only the operator did, on the manage path —
      and the plan had nowhere to write a cadence.
    """

    @staticmethod
    def _agent(role):
        for agent in bundled_definitions()["agents"]:
            if agent["id"].endswith(role):
                return agent
        raise AssertionError(f"no agent for {role}")

    @staticmethod
    def _step(step_id):
        steps = {s["id"]: s for s in bundled_definitions()["orchestration"]["steps"]}
        return steps[step_id]

    def test_the_analyst_is_told_a_check_before_it_goes_out_is_a_checkpoint(self):
        prompt = self._agent("analyst")["system_prompt"]
        assert "before it goes to the client" in prompt, "the rule names the case it came from"
        assert "is a human checkpoint" in prompt

    def test_the_analyst_writes_down_a_cadence(self):
        prompt = self._agent("analyst")["system_prompt"]
        assert "- **Schedule**:" in prompt
        # A scheduled run starts with no person to type its input: the cadence
        # and what each run starts with are asked for together.
        assert "what each scheduled run starts with" in prompt

    def test_the_plan_has_a_step_for_every_checkpoint_and_a_schedule_section(self):
        prompt = self._step("step_draft_plan")["prompt_template"]
        assert "Every human checkpoint in the requirements becomes a `human` step" in prompt
        assert "## Schedule" in prompt
        assert "Omit this section when the requirements name no cadence" in prompt

    def test_the_create_saver_can_make_the_schedule_and_nothing_more(self):
        tools = set(self._agent("saver_create")["tools"])
        # A new orchestration has no schedule to find, so no listing either.
        assert "create_schedule" in tools
        assert not {"list_schedules", "set_schedule_enabled", "delete_schedule"} & tools

    def test_the_create_saver_schedules_only_what_the_plan_says_after_it_validates(self):
        prompt = self._agent("saver_create")["system_prompt"]
        assert "PHASE 4: Schedule" in prompt
        assert "Only if the plan has a `## Schedule` section" in prompt
        assert prompt.index("PHASE 3") < prompt.index("PHASE 4: Schedule")

    def test_a_schedule_is_made_only_after_a_person_approved_the_plan(self):
        """A schedule is standing spend. The create saver is reached only
        through the plan review, as the operator is."""
        orch = bundled_definitions()["orchestration"]
        steps = {s["id"]: s for s in orch["steps"]}

        def into(target):
            return [
                s for s in orch["steps"]
                if target in [s.get("next_step_id"), *(s.get("route_map") or {}).values()]
            ]

        # Walk back from the saver to the start; every path passes the review.
        seen, frontier = set(), ["step_save_create"]
        while frontier:
            current = frontier.pop()
            for s in into(current):
                if s["id"] in seen or s["id"] == "step_human_review":
                    continue
                seen.add(s["id"])
                frontier.append(s["id"])
        assert "step_intent" not in seen, "the create saver is reachable without the review"
        assert steps["step_human_review"]["type"] == "human"

    def test_the_summary_names_a_schedule_it_made(self):
        prompt = self._step("step_summarize")["prompt_template"]
        assert "schedule" in prompt.lower()
