"""The builder ships as data: an orchestration and its sub-agents.

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

    def test_every_step_id_is_its_own(self):
        """Two steps of one id: the engine's step map keeps the last, and every route to the first goes to the
        second (SAI2-P2 nearly shipped a second `step_approval_route`)."""
        ids = [s["id"] for s in bundled_definitions()["orchestration"]["steps"]]
        assert len(ids) == len(set(ids)), sorted({i for i in ids if ids.count(i) > 1})

    def test_every_agent_names_its_role(self):
        """Ids are minted per organisation in the commercial edition, so a
        re-seed matches on the role. A missing one would orphan that agent."""
        roles = [a["system_role"] for a in bundled_definitions()["agents"]]
        assert sorted(roles) == [
            "change_planner", "classifier", "classifier_at_plan", "maker", "orchestrator", "plan_researcher",
            "planner", "prompt_writer", "reporter", "researcher", "talker",
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
        assert "step_turn" in blob

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
            # An agent that finishes by calling a tool (E2) writes that tool's object, not text.
            writes = "dict" if kind == "agent" and step.get("finish_tools") else self.WRITES.get(kind)
            if key and writes and schema[key]["type"] != writes:
                wrong.append(f"{key} is declared {schema[key]['type']} but "
                             f"{step['id']} ({kind}) writes {writes}")
        # `resume()` copies every answer to `human_response` as well, whatever
        # the step's own output_key.
        if any(s["type"] == "human" for s in orch["steps"]) and schema["human_response"]["type"] != "dict":
            wrong.append(f"human_response is declared {schema['human_response']['type']} "
                         f"but resume() writes every answer there")
        assert not wrong, "; ".join(sorted(set(wrong)))

    @pytest.mark.parametrize("step_id,key,value", [
        # The planner finishes on a tool (E2): its Plan is an object. The save (E1) answers in JSON text.
        ("step_plan", "plan_draft", {"finish": "submit_plan", "kind": "create", "title": "t"}),
        ("step_save", "build_saved", '{"outcome": "saved", "made": {}, "effect": ""}'),
        ("step_plan_full", "final_orch", "STOPPED: the plan is full"),
    ])
    def test_what_the_build_route_writes_is_not_a_typing_failure(self, step_id, key, value):
        import types

        from core.models_orchestration import Orchestration, OrchestrationRun
        from core.orchestration.engine import OrchestrationEngine

        orch = Orchestration.model_validate(bundled_definitions()["orchestration"])
        engine = OrchestrationEngine(orch, types.SimpleNamespace(
            agent_sessions={}, memory_store=None, tool_router={}))
        run = OrchestrationRun(run_id="builder_f52", orchestration_id=orch.id,
                               shared_state=engine._init_state("build me something"))
        before = dict(run.shared_state)
        run.shared_state[key] = value

        warnings, unconvertible = engine._type_written_state(run, engine.step_map[step_id], before)

        assert warnings == []
        assert unconvertible == []
        assert run.shared_state[key] == value


class TestSeeding:
    async def test_it_installs_the_builder(self):
        result = await seed_native_builder()
        assert result["orchestration"] == "added"
        # 8 since the Researcher (F577, BWA1); 9 with P2's change planner and maker; 7 since P3's planner and
        # prompt writer took the analyst's, the agent creator's and both savers' places; 8 with P3b's plan researcher
        # (the build's own, an E2 finish on report_findings; P5 may fold the two researchers into one); 9 with the front
        # door's classifier (SAI2-P4: its one call, an E2 finish on classify_request); 10 with talk's, which holds
        # no tool; 11 with the classifier at a waiting plan (F1241's follow-up: its one call requires `continues`).
        assert len(result["agents_added"]) == 11
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

        agent = await get_agent("agent_native_builder_planner")
        await save_agent({**agent, "system_prompt": "drifted"})

        result = await seed_native_builder()
        assert result["agents_updated"] == ["agent_native_builder_planner"]
        assert (await get_agent("agent_native_builder_planner"))["system_prompt"] != "drifted"

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


class TestOnlyTheApplyStepCanDestroy:
    """Two layers guard a deletion. The registry's own rule is one; this is the
    other, and it is the one that matters — a delete is made only by the apply
    step (code, through the registry), and only after a human has read a review
    that named the deletion and approved it. Since SAI2-P2 no agent holds a
    delete at all: the operator, which did, is gone."""

    DESTRUCTIVE = {"delete_agent", "delete_orchestration", "delete_custom_tool", "delete_schedule"}

    def test_no_sub_agent_holds_a_delete(self):
        for agent in bundled_definitions()["agents"]:
            held = self.DESTRUCTIVE & set(agent.get("tools") or [])
            assert not held, f"{agent['system_role']} holds {held}"

    def test_the_apply_step_makes_all_of_them(self):
        """Every delete is a change op, and the one step that makes a change's calls is `step_apply`. The only
        other steps that force a write are the two approvals, which write the run's own record, and the build's
        save (SAI2-P3), which makes, never deletes: its calls are only these. P3b's `step_make_tools` is that save
        run up to its first calls, the tools it makes from a read API (F944)."""
        from core.builder_access import WRITES
        from core.native_builder import build_save, change_ops

        assert self.DESTRUCTIVE <= set(change_ops.WRITE_TOOLS)
        steps = {s["id"]: s for s in bundled_definitions()["orchestration"]["steps"]}
        writing = {s["id"] for s in bundled_definitions()["orchestration"]["steps"]
                   if s["type"] == "tool" and s.get("forced_tool") in WRITES}
        # And the wait's answer and its give-up (F946), which remove only the tools that build made and never saved.
        assert writing == {"step_plan_approve", "step_save", "step_make_tools", "step_connect_answer",
                           "step_connect_give_up", "step_change_approve", "step_apply"}
        assert {steps[s]["forced_tool"] for s in ("step_connect_answer", "step_connect_give_up")} == {
            "read_connect_answer"}
        assert steps["step_make_tools"]["forced_tool"] == "save_build"
        import inspect
        source = inspect.getsource(build_save.save_calls) + inspect.getsource(build_save._tool_call)
        # The board the person asked for is a shared board its workflow is given to work (F1214): changed on purpose.
        made = {"create_agent", "create_orchestration", "update_orchestration", "set_orchestration_meta",
                "create_board", "give_board_access", "create_schedule", "create_http_tool"}
        assert {t for t in made if f'"{t}"' in source} == made and "delete" not in source

    def test_nothing_inherits_the_default_tool_set_by_accident(self):
        """A sub-agent that does not skip the defaults gets every tool in the
        workspace on top of its declared list — which for the operator would
        mean tools no plan ever mentioned."""
        for agent in bundled_definitions()["agents"]:
            if not agent.get("tools"):
                continue  # the orchestrator shell declares none and runs none
            assert agent.get("skip_default_tools") is True, agent["system_role"]

    def test_the_apply_step_is_reached_only_through_a_human_approval(self):
        """The path to it: check → human → gate → approve → apply. Remove the human step and the builder can
        delete things unprompted."""
        orch = bundled_definitions()["orchestration"]
        by_id = {s["id"]: s for s in orch["steps"]}

        def targets(s):
            return [s.get("next_step_id"), *(s.get("route_map") or {}).values(),
                    *(s.get("switch_cases") or {}).values(), s.get("switch_default_step_id"),
                    s.get("if_true_step_id"), s.get("if_false_step_id")]

        def reaching(step_id):
            return [s for s in orch["steps"] if step_id in targets(s)]

        # Only the approval reaches the apply, through its own check (an approval that answered an error goes to
        # say why, never on), and only the gate reaches the approval.
        assert [s["id"] for s in reaching("step_apply")] == ["step_change_recorded"]
        recorded = by_id["step_change_recorded"]
        assert recorded["type"] == "if_else" and recorded["if_false_step_id"] == "step_apply"
        assert recorded["if_true_step_id"] == "step_change_failed"
        assert [s["id"] for s in reaching("step_change_recorded")] == ["step_change_approve"]
        # After the yes, a switch sends one new thing to the maker and every change to the approval: it is reached
        # only from the gate's yes side.
        [after] = reaching("step_change_approve")
        assert after["id"] == "step_after_yes" and after["switch_default_step_id"] == "step_change_approve"
        [gate] = reaching("step_after_yes")
        # A gate decided in code (F754): the approval is only on its yes side, and its input came from a human.
        assert gate["type"] == "if_else"
        assert gate["if_true_step_id"] == "step_after_yes" != gate["if_false_step_id"], gate["id"]
        feeders = reaching(gate["id"])
        assert feeders and all(by_id[f["id"]]["type"] == "human" for f in feeders), gate["id"]


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
        # The two approvals are decided in code since F754, and a condition that cannot be read counts as false
        # (`IfElseStepExecutor`): their false side is the redraft, never the saver or the apply.
        from core.native_builder import bundled_definitions
        steps = {s["id"]: s for s in bundled_definitions()["orchestration"]["steps"]}
        assert steps["step_approval_route"]["if_false_step_id"] == "step_plan_answer"
        assert steps["step_manage_route"]["if_false_step_id"] == "step_change"
        # A no is read in code (SAI2-P3): an answer that cannot be read goes back to the planner, never on.
        answered = steps["step_plan_answer_route"]
        assert answered["type"] == "switch" and answered["switch_default_step_id"] == "step_plan_context"
        assert "step_save" not in answered["switch_cases"].values()
        # A build check whose outcome cannot be read says it failed and ends.
        checked = steps["step_plan_route"]
        assert checked["type"] == "switch" and checked["switch_default_step_id"] == "step_build_failed"
        assert steps["step_build_failed"]["next_step_id"] == "step_end"
        # The change route's own split is code too (SAI2-P2): a check whose outcome cannot be read says what it
        # found and ends; it never falls to the review, let alone the apply.
        route = steps["step_check_route"]
        assert route["type"] == "switch" and route["switch_default_step_id"] == "step_change_said"
        assert steps["step_change_said"]["next_step_id"] == "step_end"

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
        keys = set(self._steps()["step_classify"]["input_keys"])
        assert {
            "current_orchestration_id", "current_orchestration_name",
            "current_orchestration_source", "current_orchestration_matches",
        } <= keys

    def test_the_answer_and_the_change_planner_see_the_page(self):
        steps = self._steps()
        # The answer that reads the workspace (SAI2-P4); the help's answer sees the page too.
        assert {"current_page", "current_orchestration_source", "current_orchestration_matches"} <= set(
            steps["step_answer_workspace"]["input_keys"]
        )
        assert "current_page" in steps["step_answer_help"]["input_keys"]
        assert "current_page" in steps["step_change"]["input_keys"]

    def test_an_unclear_change_is_answered_not_built(self):
        """Two matches, or a name that is not the open one, leaves the id empty —
        and the route that runs then must ask, not build a duplicate."""
        # SAI2-P4: classify reads the source, code sends an ambiguous build to the answer (`classify._entry`, tested
        # in test_classify_request), and the answer asks which one.
        steps = self._steps()
        assert "ambiguous" in steps["step_classify"]["prompt_template"]
        assert "ambiguous" in steps["step_answer_workspace"]["prompt_template"]

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

    # F490 (BWA1, D274) replaced the model's summary with the run's own record of its writes, so what F53 asked for
    # — the summary names what was built, never "the orchestration" — now holds by construction, and these say so.

    def test_it_prints_the_runs_own_record_not_a_models_recollection(self):
        summarize = self._summarize()
        assert summarize["type"] == "print" and summarize["print_content"] == "{state.run_changes}"
        assert summarize["output_key"] == "summary"

    def test_it_names_what_was_built_by_its_own_name(self):
        from core.orchestration import run_writes

        state: dict = {}
        run_writes.record(state, {"type": "resource_saved", "kind": "orchestration", "action": "created",
                                  "id": "o1", "name": "Vendor review"})
        assert state["run_changes"] == 'Built the workflow "Vendor review".'



class TestAServerNameIsNotACapability:
    """F126. On 2026-09-13 the Requirements Analyst specified a Wiki Searcher
    step using "a search tool from the `deepwiki` MCP server (e.g., a query or
    search function)" — it had never read what deepwiki holds, and deepwiki
    answers questions about GitHub repositories. The Agent Creator and the
    Create Saver both saw the real description later and bound it anyway.

    Since SAI2-P3 the planner chooses every tool from the workspace's catalogue as code reads it, each with its own
    description, and these rules tell it to choose by that; nothing after it binds a tool the plan did not name.
    """

    @staticmethod
    def _prompt(role):
        for agent in bundled_definitions()["agents"]:
            if agent["system_role"] == role:
                return agent["system_prompt"]
        raise AssertionError(f"no agent for {role}")

    def test_the_planner_is_told_a_name_is_not_a_capability(self):
        prompt = self._prompt("planner")
        assert "A tool's name is not its capability" in prompt
        assert "deepwiki" in prompt, "the rule names the case it came from"

    def test_the_planner_is_told_that_nothing_fitting_is_an_answer(self):
        assert "If nothing here does the job" in self._prompt("planner")

    async def test_the_pack_gives_each_tool_its_own_description(self):
        from core.native_builder.build_plan import read_plan_context
        from core.store.resources import save_tool

        await save_tool({"name": "deepwiki_ask", "description": "Answers questions about a GitHub repository.",
                         "tool_type": "http", "method": "GET", "url": "https://w.example/ask", "headers": {},
                         "inputSchema": {"type": "object", "properties": {}}})
        pack = await read_plan_context({}, type("S", (), {"agent_sessions": {}, "tool_router": {}})(), [])
        assert {"name": "deepwiki_ask", "does": "Answers questions about a GitHub repository."} in pack["tools"]

    def test_what_binds_a_tool_is_code_and_binds_only_what_the_plan_named(self):
        from core.native_builder import objects as O
        from core.native_builder.build_save import save_calls

        plan = O.Plan.from_draft({"kind": "create", "title": "t", "summary": "s", "steps": [
            {"id": "ask", "type": "tool", "name": "Ask", "does": "x", "tool": "deepwiki_ask"}]})
        [call] = save_calls(plan, {}, {})
        steps = json.loads(call["args"]["steps_json"])
        assert [s.get("forced_tool") for s in steps if s["type"] == "tool"] == ["deepwiki_ask"]
        assert not {a["system_role"] for a in bundled_definitions()["agents"]} & {
            "agent_creator", "saver_create", "saver_update"}


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
            if agent["system_role"] == role:
                return agent
        raise AssertionError(f"no agent for {role}")

    @staticmethod
    def _step(step_id):
        steps = {s["id"]: s for s in bundled_definitions()["orchestration"]["steps"]}
        return steps[step_id]

    def test_the_planner_is_told_a_check_before_it_goes_out_is_a_checkpoint(self):
        prompt = self._agent("planner")["system_prompt"]
        assert "before it goes to the client" in prompt, "the rule names the case it came from"
        assert "is a human step right before the step that sends it" in prompt

    def test_the_planner_writes_down_a_cadence(self):
        prompt = self._agent("planner")["system_prompt"]
        # A scheduled run starts with no person to type its input: the cadence
        # and what each run starts with are asked for together.
        assert "Add a schedule only when the person named when it runs" in prompt
        assert "what each run starts with" in prompt

    def test_the_plan_has_a_place_for_a_schedule_and_the_card_shows_it_only_when_there_is_one(self):
        from core.native_builder import objects as O
        from core.native_builder.render import render

        assert set(O.Schedule.model_fields) >= {"cron", "says", "input"}
        base = {"kind": "create", "title": "t", "summary": "s", "steps": [
            {"id": "a", "type": "llm", "name": "A", "does": "x", "prompt": "p"}]}
        scheduled = {**base, "schedule": {"cron": "0 9 * * 1", "says": "Mondays"}}
        assert "## Schedule" in render(O.Plan.from_draft(scheduled))
        assert "## Schedule" not in render(O.Plan.from_draft(base))

    def test_the_save_can_make_the_schedule_and_nothing_more(self):
        from core.native_builder import objects as O
        from core.native_builder.build_save import save_calls

        base = {"kind": "create", "title": "t", "summary": "s", "steps": [
            {"id": "a", "type": "llm", "name": "A", "does": "x", "prompt": "p"}]}
        tools = [c["tool"] for c in save_calls(O.Plan.from_draft(
            {**base, "schedule": {"cron": "0 9 * * 1", "says": "Mondays", "input": "This week's notes"}}), {}, {})]
        assert tools == ["create_orchestration", "create_schedule"]
        assert not {"list_schedules", "set_schedule_enabled", "delete_schedule", "update_schedule"} & set(tools)
        assert [c["tool"] for c in save_calls(O.Plan.from_draft(base), {}, {})] == ["create_orchestration"]

    def test_the_save_schedules_only_what_the_plan_says_after_the_workflow(self):
        from core.native_builder import objects as O
        from core.native_builder.build_save import save_calls

        plan = O.Plan.from_draft({"kind": "create", "title": "t", "summary": "s", "steps": [
            {"id": "a", "type": "llm", "name": "A", "does": "x", "prompt": "p"}],
            "schedule": {"cron": "0 9 * * 1", "says": "Mondays", "input": "This week's notes"}})
        made, sched = save_calls(plan, {}, {})
        assert sched["args"]["target_id"] == made["makes"] and "enabled" not in sched["args"]
        assert sched["args"]["prompt"] == "This week's notes"

    def test_a_schedule_is_made_only_after_a_person_approved_the_plan(self):
        """A schedule is standing spend. The create saver is reached only
        through the plan review, as the operator is."""
        orch = bundled_definitions()["orchestration"]
        steps = {s["id"]: s for s in orch["steps"]}

        def into(target):
            # if_else branches too (D122): with only next/route_map the walk
            # stops at the first if_else and passes without checking anything.
            return [
                s for s in orch["steps"]
                if target in [s.get("next_step_id"), s.get("if_true_step_id"),
                              s.get("if_false_step_id"), *(s.get("route_map") or {}).values(),
                              *(s.get("switch_cases") or {}).values(), s.get("switch_default_step_id")]
            ]

        # Walk back from the save to the start; every path passes the review.
        seen, frontier = set(), ["step_save"]
        while frontier:
            current = frontier.pop()
            for s in into(current):
                if s["id"] in seen or s["id"] == "step_human_review":
                    continue
                seen.add(s["id"])
                frontier.append(s["id"])
        assert seen and "step_intent" not in seen, "the save is reachable without the review"
        assert steps["step_human_review"]["type"] == "human"

    def test_the_summary_names_a_schedule_it_made(self):
        # F490: the summary is the run's own record; F155's words come from the schedule the run saved — that it was
        # made, when it runs (UTC), and that it waits, paused, for a person to turn it on.
        from core.orchestration import run_writes

        assert self._step("step_summarize")["print_content"] == "{state.run_changes}"
        state: dict = {}
        run_writes.record(state, {"type": "resource_saved", "kind": "schedule", "action": "created", "id": "s1",
                                  "name": "Friday report", "resource": {"enabled": False,
                                                                        "cron_expression": "0 16 * * 5"}})
        said = state["run_changes"]
        assert '"Friday report"' in said and '"0 16 * * 5" (UTC)' in said
        assert "paused: turn it on under Schedules" in said
        # The build's own ending says the same record, with what reading it back found after it.
        # F1228: then what each part does and what is missing or off, from the plan's parts in code.
        assert self._step("step_build_done")["print_content"] == (
            "{state.run_changes}{state.build_saved.effect}{state.build_saved.does}")


class TestAFullPlanAndAStoppedBuild:
    """F154 and F156 (D122), in the seed's wiring."""

    @staticmethod
    def _steps():
        return {s["id"]: s for s in bundled_definitions()["orchestration"]["steps"]}

    @staticmethod
    def _lands(steps, label):
        """Where a route lands past the door for a person who may change things (ASSIST1, F682): from classify's
        route (SAI2-P4) through its switch and the role door."""
        entry = {"create": "step_plan_room", "update": "step_fetch_existing", "manage": "step_change_context"}[label]
        target = steps["step_classify_route"]["switch_cases"][entry]
        if target == "step_may_change":
            gate = steps["step_may_change"]
            target = steps[gate["if_true_step_id"]]["switch_cases"][label]
        if target == "step_effort":
            # A build's effort check (F1153) goes on to the entry it names when it offers nothing.
            target = steps["step_effort_route"]["switch_cases"][entry]
        return target

    def test_a_create_is_checked_against_the_plan_before_anything_is_paid_for(self):
        steps = self._steps()
        assert self._lands(steps, "create") == "step_plan_room"
        room = steps["step_plan_room"]
        assert room["type"] == "if_else"
        assert room["if_true_step_id"] == "step_plan_context"
        full = steps[room["if_false_step_id"]]
        assert full["type"] == "print" and full["output_key"] == "final_orch"
        assert "{state.plan_room.reason}" in full["print_content"]
        assert full["next_step_id"] == "step_summarize"
        schema = bundled_definitions()["orchestration"]["state_schema"]
        assert schema["plan_room"]["type"] == "dict"

    def test_an_update_is_not_refused_for_orchestrations(self):
        # An update adds no orchestration; only a create is checked.
        assert self._lands(self._steps(), "update") == "step_fetch_existing"

    def test_the_save_cleans_up_after_itself_and_its_ending_says_what_it_did(self):
        """`finish_build`'s job (F156: what a stopped build started is removed) is the save's own now: it removes
        what its pass made and says why (`test_a_build_is_saved_in_code`)."""
        steps = self._steps()
        save = steps["step_save"]
        assert (save["type"], save["forced_tool"], save["next_step_id"]) == ("tool", "save_build", "step_build_done")
        assert steps["step_build_done"]["next_step_id"] == "step_end"
        assert "finish_build" not in {s.get("forced_tool") for s in steps.values()}
