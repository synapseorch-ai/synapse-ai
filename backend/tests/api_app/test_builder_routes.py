"""The AI builder's HTTP surface.

The panel used to be handed a private vocabulary invented by the route, on top
of a translation layer that guessed what had been saved. It reads the engine's
own events now, so what these tests pin is that the stream arrives intact —
including `resource_saved`, which is the whole point of the change.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time

import pytest

from _fakes import seed as S
from core.native_builder import bundle_version, seed_native_builder


def tool_call(tool: str, **args) -> str:
    """A tool call in the shape the ReAct engine parses.

    `_fakes.fake_llm.tool_call` takes the tool name positionally as `name`,
    which collides with the `name` argument these builder tools take.
    """
    return json.dumps({"tool": tool, "arguments": args})


def route(label: str) -> str:
    """An evaluator's routing decision.

    Evaluators only accept a `route_<label>` tool call — a bare label string
    does not parse, and the step then falls back to the FIRST entry in its
    `route_map`. So a test that returns a bare "create" is exercising the
    fallback, not the routing.
    """
    return tool_call(f"route_{label}", reasoning="test")


async def stream(client, path: str, body: dict) -> list[dict]:
    """POST an SSE endpoint and collect its decoded events."""
    events: list[dict] = []
    async with client.stream("POST", path, json=body) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


@pytest.fixture
async def builder():
    await seed_native_builder()


class TestTheStreamIsTheEnginesOwn:
    async def test_a_run_starts_and_reports_its_steps(self, client, builder, fake_llm):
        fake_llm.set_default("create")
        events = await stream(client, "/api/builder/chat", {
            "message": "build me something", "client_session_id": "conv-1",
        })
        types = [e["type"] for e in events]
        assert "orchestration_start" in types
        assert "step_start" in types

    async def test_internal_bookkeeping_is_not_streamed(self, client, builder, fake_llm):
        """Underscore-prefixed events are mid-step progress markers for the
        engine's own crash recovery, not something a panel can render."""
        fake_llm.set_default("create")
        events = await stream(client, "/api/builder/chat", {"message": "hi"})
        assert not [e for e in events if e["type"].startswith("_")]

    async def test_a_missing_builder_is_reported_rather_than_raised(self, client, fake_llm):
        events = await stream(client, "/api/builder/chat", {"message": "hi"})
        assert events[0]["type"] == "orchestration_error"
        assert "not installed" in events[0]["error"]

    async def test_a_clarifying_question_arrives_with_its_run_id(self, client, builder, fake_llm):
        """The panel answers on /api/builder/resume, which needs the run id and
        the field name — both come from the engine's own event."""
        fake_llm.script([
            "create",           # step_intent
            "What should it do?",  # analyst
            "clarify",          # step_clarify_route
        ])
        events = await stream(client, "/api/builder/chat", {"message": "build something"})

        asked = next(e for e in events if e["type"] == "human_input_required")
        assert asked["run_id"].startswith("builder_")
        assert asked["fields"][0]["name"] == "answer"
        assert asked["prompt"]


class TestResourceSavedReachesThePanel:
    """This is what replaced the regex. The event has to survive the whole way:
    dispatcher → step executor → engine → route → wire."""

    @pytest.fixture
    async def writing_builder(self):
        """The builder, reduced to one step that writes something.

        The shipped definition only reaches a writing agent after a plan and a
        human approval, which is a lot of scaffolding to prove one event
        travels. The route loads the builder by id and streams what the engine
        yields, so a smaller definition under that id exercises the same path.
        """
        from core.store.resources import save_orchestration

        await save_orchestration({
            "id": "orch_native_builder",
            "system": "native_builder",
            "name": "Synapse AI Builder",
            "description": "",
            "entry_step_id": "step_write",
            "state_schema": {"user_message": {"type": "str", "default": ""}},
            "trigger": "manual",
            "steps": [
                {
                    "id": "step_write", "name": "Write", "type": "tool",
                    "forced_tool": "create_agent",
                    "prompt_template": "Create an agent called Researcher.",
                    "input_keys": ["user_message"], "output_key": "final_orch",
                    "next_step_id": "step_end",
                },
                {"id": "step_end", "name": "End", "type": "end"},
            ],
        })

    async def test_a_write_is_announced_with_the_whole_resource(
        self, client, writing_builder, fake_llm
    ):
        fake_llm.set_default(tool_call(
            "create_agent", name="Researcher", description="d",
            type="conversational", tools=[], system_prompt="p",
        ))
        events = await stream(client, "/api/builder/chat", {"message": "make an agent"})

        saved = [e for e in events if e["type"] == "resource_saved"]
        assert saved, [e["type"] for e in events]
        assert (saved[0]["kind"], saved[0]["action"]) == ("agent", "created")
        # The whole resource, not the 500-character truncation the model saw.
        assert saved[0]["resource"]["system_prompt"] == "p"
        assert saved[0]["id"].startswith("agent_")

    async def test_it_arrives_after_the_tool_result_it_belongs_to(
        self, client, writing_builder, fake_llm
    ):
        fake_llm.set_default(tool_call(
            "create_agent", name="Researcher", description="d",
            type="conversational", tools=[], system_prompt="p",
        ))
        events = await stream(client, "/api/builder/chat", {"message": "go"})
        types = [e["type"] for e in events]
        assert types.index("tool_result") < types.index("resource_saved")

    async def test_it_names_the_step_that_did_it(self, client, writing_builder, fake_llm):
        fake_llm.set_default(tool_call(
            "create_agent", name="Researcher", description="d",
            type="conversational", tools=[], system_prompt="p",
        ))
        events = await stream(client, "/api/builder/chat", {"message": "go"})
        saved = next(e for e in events if e["type"] == "resource_saved")
        assert saved["step_name"] == "Write"


class TestTheConversationIsScoped:
    async def test_the_session_id_carries_the_client_key(self, client, builder, fake_llm):
        """It was the literal string "builder" for every conversation — one
        shared tool cache and one shared history across every user."""
        fake_llm.set_default("create")
        await stream(client, "/api/builder/chat", {
            "message": "hi", "client_session_id": "conv-abc",
        })
        sessions = [c.get("session_id") for c in fake_llm.calls if c.get("session_id")]
        assert sessions
        assert all(s.endswith(":conv-abc") for s in sessions), sessions

    async def test_two_conversations_do_not_share_a_session(self, client, builder, fake_llm):
        fake_llm.set_default("create")
        await stream(client, "/api/builder/chat", {"message": "a", "client_session_id": "one"})
        first = {c.get("session_id") for c in fake_llm.calls if c.get("session_id")}
        fake_llm.reset()
        fake_llm.set_default("create")
        await stream(client, "/api/builder/chat", {"message": "b", "client_session_id": "two"})
        second = {c.get("session_id") for c in fake_llm.calls if c.get("session_id")}
        assert first and second and not (first & second)


class TestTheModelChoice:
    async def test_the_caller_picks_the_model_for_the_whole_run(self, client, builder, fake_llm):
        fake_llm.set_default("create")
        await stream(client, "/api/builder/chat", {"message": "hi", "model": "picked-model"})
        models = {c.get("current_model") for c in fake_llm.calls if c.get("current_model")}
        assert models == {"picked-model"}, models

    async def test_the_workspace_default_applies_when_the_caller_does_not_pick(
        self, client, builder, fake_llm
    ):
        saved = await client.post("/api/settings", json={
            "agent_name": "Synapse", "builder_model": "workspace-builder-model",
        })
        assert saved.status_code == 200

        fake_llm.set_default("create")
        await stream(client, "/api/builder/chat", {"message": "hi"})
        models = {c.get("current_model") for c in fake_llm.calls if c.get("current_model")}
        assert models == {"workspace-builder-model"}, models

    async def test_builder_model_round_trips_through_settings(self, client):
        await client.post("/api/settings", json={"agent_name": "Synapse", "builder_model": "some-model"})
        assert (await client.get("/api/settings")).json()["builder_model"] == "some-model"


class TestSeeHowImBuilt:
    async def test_the_legend_describes_the_shipped_definition(self, client):
        """Rendered from the seed rather than written down, so it cannot drift
        from the thing it describes."""
        legend = (await client.get("/api/builder/legend")).json()

        assert legend["name"] == "Synapse AI Builder"
        # 24 since D122: the plan-room check and its refusal (F154), and finish_build (F156).
        assert legend["step_count"] == 24
        assert legend["entry_step"]["type"] == "evaluator"
        assert {r["label"] for r in legend["routes"]} == {"create", "update", "manage", "answer"}
        assert {a["role"] for a in legend["agents"]} == {
            "analyst", "agent_creator", "saver_create", "saver_update", "operator", "reporter",
        }
        # The answer path adds a step and a sub-agent but no gate: a question
        # has nothing to approve.
        assert len(legend["human_gates"]) == 3
        assert "evaluator" in legend["step_types"]

    async def test_the_legend_carries_the_orchestration_the_canvas_draws(self, client):
        """The modal draws the builder on a read-only canvas instead of a
        screenshot, so the legend hands over every step and the agents their
        `agent_id`s name — and nothing that would let two steps sit on top of
        each other."""
        from core.native_builder import bundled_definitions

        canvas = (await client.get("/api/builder/legend")).json()["canvas"]
        shipped = bundled_definitions()["orchestration"]

        assert canvas["orchestration"]["entry_step_id"] == shipped["entry_step_id"]
        assert canvas["orchestration"]["steps"] == shipped["steps"]
        names = {a["id"]: a["name"] for a in canvas["agents"]}
        assert all(s["agent_id"] in names for s in shipped["steps"] if s.get("agent_id"))
        # Names only: a sub-agent's prompt is not the canvas's business.
        assert all(set(a) == {"id", "name"} for a in canvas["agents"])
        positions = [(s.get("position_x"), s.get("position_y")) for s in shipped["steps"]]
        assert len(set(positions)) == len(positions)

    async def test_duplicating_gives_the_workspace_an_editable_copy(self, client, builder):
        result = (await client.post("/api/builder/duplicate")).json()
        assert result["status"] == "created"
        assert len(result["agent_ids"]) == 7

        from core.store.resources import get_orchestration
        copy = await get_orchestration(result["orchestration_id"])
        # The customer's, not another instance of ours.
        assert "system" not in copy
        assert copy["name"].endswith("(Copy)")

        # Every step points at a copied agent, not at the original.
        named = {s["agent_id"] for s in copy["steps"] if s.get("agent_id")}
        assert named == set(result["agent_ids"]) - {
            aid for aid in result["agent_ids"] if aid not in named
        }
        assert named <= set(result["agent_ids"])
        assert not any(a.startswith("agent_native_builder") for a in named)

    async def test_the_copy_is_visible_where_the_original_is_not(self, client, builder):
        result = (await client.post("/api/builder/duplicate")).json()

        listed = (await client.get("/api/orchestrations")).json()
        assert [o["id"] for o in listed] == [result["orchestration_id"]]

        agents = {a["id"] for a in (await client.get("/api/agents")).json()}
        assert agents == set(result["agent_ids"])

    async def test_duplicating_twice_collides_with_nothing(self, client, builder):
        first = (await client.post("/api/builder/duplicate")).json()
        second = (await client.post("/api/builder/duplicate")).json()
        assert first["orchestration_id"] != second["orchestration_id"]
        assert not set(first["agent_ids"]) & set(second["agent_ids"])

    async def test_the_seed_version_is_reported_for_the_copy_to_be_compared_against(self):
        assert len(bundle_version()) == 16


class TestAQuestionGetsAnAnswer:
    """The `answer` fast path.

    "How many runs failed this week?" used to go down the `manage` branch, which
    drafts a plan and asks a human to approve it before anything runs — an
    approval step for a lookup. It now routes straight to a read-only agent
    whose reply *is* the run's summary.
    """

    async def test_a_question_skips_the_plan_and_the_gate(self, client, builder, fake_llm):
        fake_llm.script([
            route("answer"),               # step_intent
            "You have three agents.",      # step_answer — the reporter's reply
        ])
        events = await stream(client, "/api/builder/chat", {"message": "what agents do I have?"})

        steps = [e.get("step_name") for e in events if e["type"] == "step_start"]
        assert "Answer the Question" in steps
        # No plan, no approval, no operator.
        assert "Approve the Change" not in steps
        assert "Apply the Change" not in steps
        assert "Plan the Change" not in steps
        assert not [e for e in events if e["type"] == "human_input_required"]

    async def test_the_answer_is_the_summary_the_panel_renders(self, client, builder, fake_llm):
        """`step_answer` writes `summary` and goes straight to the end, bypassing
        `step_summarize` — the panel renders `final_state.summary`, so the
        reporter's own words reach the user rather than a paraphrase of them."""
        fake_llm.script([
            route("answer"),
            "Four of thirty-seven runs failed in the last 7 days.",
        ])
        events = await stream(client, "/api/builder/chat", {"message": "how many runs failed?"})

        done = next(e for e in events if e["type"] == "orchestration_complete")
        assert "Four of thirty-seven" in done["final_state"]["summary"]

        steps = [e.get("step_name") for e in events if e["type"] == "step_start"]
        assert "Summarise Result" not in steps

    async def test_the_reporter_cannot_write(self, builder):
        """The whole safety argument for skipping the gate: the destination has
        no write tools, so a misrouted request degrades to being answered rather
        than being done without approval."""
        from core.native_builder import bundled_definitions

        reporter = next(
            a for a in bundled_definitions()["agents"] if a["system_role"] == "reporter"
        )
        assert reporter["skip_default_tools"] is True
        assert all(
            not t.startswith(("create_", "update_", "delete_", "attach_", "import_", "set_"))
            for t in reporter["tools"]
        ), reporter["tools"]
        # Variable *values* are previews, and an agent that exists to quote what
        # it read is the wrong holder of them.
        assert "list_variables" not in reporter["tools"]


class TestAReadOnlyPlanHasNothingToApprove:
    async def test_it_skips_the_gate_and_answers(self, client, builder, fake_llm):
        fake_llm.script([
            route("manage"),                           # step_intent
            "- list_recent_runs — see what failed",    # step_manage_plan
            route("read_only"),                        # step_manage_writes
            "Nothing failed yesterday.",               # step_answer
        ])
        events = await stream(client, "/api/builder/chat", {"message": "anything fail yesterday?"})

        steps = [e.get("step_name") for e in events if e["type"] == "step_start"]
        assert "Plan the Change" in steps
        assert "Answer the Question" in steps
        assert "Approve the Change" not in steps
        assert "Apply the Change" not in steps
        assert not [e for e in events if e["type"] == "human_input_required"]

    async def test_a_writing_plan_still_stops_at_the_gate(self, client, builder, fake_llm):
        fake_llm.script([
            route("manage"),                                 # step_intent
            "- delete_schedule — remove the nightly brief",  # step_manage_plan
            route("writes"),                                 # step_manage_writes
        ])
        events = await stream(client, "/api/builder/chat", {"message": "delete the nightly brief"})

        asked = next(e for e in events if e["type"] == "human_input_required")
        assert asked["fields"][0]["name"] == "feedback"
        steps = [e.get("step_name") for e in events if e["type"] == "step_start"]
        assert "Apply the Change" not in steps

    async def test_an_unreadable_decision_falls_back_to_the_gate(self, client, builder, fake_llm):
        """An evaluator that cannot parse its own reply falls back to the first
        label in `route_map`. `writes` is first for exactly this reason: the
        accident has to be the one that asks a human."""
        fake_llm.script([
            route("manage"),
            "- delete_schedule — remove the nightly brief",
            "not a route at all",       # step_manage_writes, unparseable
        ])
        events = await stream(client, "/api/builder/chat", {"message": "delete something"})

        assert [e for e in events if e["type"] == "human_input_required"]


class TestAnAttachmentReachesTheStepAndNothingElse:
    """Images ride on the engine rather than in shared state.

    `orchestration_complete` carries `final_state` — the whole of shared state —
    and the `final` event carries it a second time. A reserved state key would
    have shipped megabytes of base64 down this stream on every build, and into
    every checkpoint besides.
    """

    PNG = "data:image/png;base64," + base64.b64encode(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    ).decode()

    async def test_the_opted_in_step_is_given_them(self, client, builder, fake_llm):
        fake_llm.script([route("answer"), "That is a picture of a cat."])
        await stream(client, "/api/builder/chat", {
            "message": "what is this?", "images": [self.PNG],
        })
        # `step_answer` sets pass_images; the reporter's own call carries them.
        assert any(c.get("images") for c in fake_llm.calls), [
            sorted(c) for c in fake_llm.calls
        ]

    async def test_a_step_that_did_not_ask_is_not_given_them(self, client, builder, fake_llm):
        """The negative half, driven through an *agent* step.

        `pass_images` is only read by the agent executor, so routing this
        through `step_manage_plan` (an `llm` step) would pass whether the opt-in
        existed or not. `step_manage_apply` is an agent step without it.
        """
        fake_llm.script([
            route("manage"),
            "- create_skill — add a summarising skill",
            route("writes"),
        ])
        events = await stream(client, "/api/builder/chat", {
            "message": "add a summarising skill", "images": [self.PNG],
        })
        # It pauses at the gate; answer it so the operator actually runs.
        run_id = next(e for e in events if e["type"] == "human_input_required")["run_id"]
        fake_llm.reset()
        fake_llm.script([route("approved"), "Created the skill."])
        after = await stream(client, "/api/builder/resume", {
            "run_id": run_id, "response": {"feedback": "approve"},
        })

        # The assertion is worthless unless the agent step actually ran.
        steps = [e.get("step_name") for e in after if e["type"] == "step_start"]
        assert "Apply the Change" in steps, steps
        assert not any(c.get("images") for c in fake_llm.calls)

    async def test_no_image_bytes_reach_the_browser(self, client, builder, fake_llm):
        fake_llm.script([route("answer"), "A cat."])
        events = await stream(client, "/api/builder/chat", {
            "message": "what is this?", "images": [self.PNG],
        })
        assert events
        wire = json.dumps(events)
        assert "base64" not in wire
        assert self.PNG[:40] not in wire

    async def test_a_model_that_cannot_see_them_refuses_rather_than_discarding(
        self, client, builder
    ):
        """Four providers discard images with no warning at all, so the run would
        answer as though nothing had been attached. The frontend disables the
        attach button for these; this is for everything that is not the
        frontend."""
        response = await client.post("/api/builder/chat", json={
            "message": "what is this?", "images": [self.PNG], "model": "cli.claude",
        })
        assert response.status_code == 422
        assert "cannot see images" in response.json()["detail"]

    async def test_too_many_is_refused_with_the_reason(self, client, builder):
        response = await client.post("/api/builder/chat", json={
            "message": "hi", "images": [self.PNG] * 6,
        })
        assert response.status_code == 422
        assert "Up to 5 images" in json.dumps(response.json())


class TestTheBuilderKnowsWhatYouMean:
    """The page someone is on, or an orchestration they name, reaches the router.

    Round two, item 12. Five times in one testing round the router was asked to
    change "the extended research orchestration" and chose `create`, quoting its
    own rule: an update needs `current_orchestration_id`, and it was empty.
    """

    MESSAGE = "update the second step to get the perfect json on the extended research orchestration"

    @staticmethod
    async def _orchestration(orch_id: str, name: str, **extra) -> None:
        from core.store.resources import save_orchestration

        await save_orchestration({
            "id": orch_id, "name": name, "description": "", "trigger": "manual",
            "entry_step_id": "step_end", "state_schema": {},
            "steps": [{"id": "step_end", "name": "End", "type": "end"}],
            **extra,
        })

    @staticmethod
    async def _final_state(client, fake_llm, body: dict) -> dict:
        fake_llm.script([route("answer"), "Which one did you mean?"])
        events = await stream(client, "/api/builder/chat", body)
        return next(e for e in events if e["type"] == "orchestration_complete")["final_state"]

    async def test_the_open_orchestration_reaches_the_router(self, client, builder, fake_llm):
        await self._orchestration("orch_ext", "extended research")
        fake_llm.script([route("answer"), "It has one step."])
        await stream(client, "/api/builder/chat", {
            "message": "add a step that emails me", "current_orchestration_id": "orch_ext",
        })
        router_prompt = fake_llm.calls[0]["prompt_msg"]
        assert "[current_orchestration_name]:\nextended research" in router_prompt
        assert "[current_orchestration_source]:\nviewing" in router_prompt

    async def test_a_named_orchestration_resolves_without_a_page(self, client, builder, fake_llm):
        await self._orchestration("orch_ext", "extended research")
        await self._orchestration("orch_weekly", "Weekly Report")
        state = await self._final_state(client, fake_llm, {"message": self.MESSAGE})
        assert state["current_orchestration_id"] == "orch_ext"
        assert state["current_orchestration_name"] == "extended research"
        assert state["current_orchestration_source"] == "named"

    async def test_the_builder_itself_is_never_a_target(self, client, builder, fake_llm):
        """It exists, so the old check — does this id exist? — let it through."""
        state = await self._final_state(client, fake_llm, {
            "message": "add a step to this", "current_orchestration_id": "orch_native_builder",
        })
        assert state["current_orchestration_id"] == ""
        assert state["current_page"] == ""

    async def test_a_retired_orchestration_is_not_a_target(self, client, builder, fake_llm):
        await self._orchestration("orch_old", "old digest", is_active=False)
        state = await self._final_state(client, fake_llm, {
            "message": "update the old digest orchestration", "current_orchestration_id": "orch_old",
        })
        assert state["current_orchestration_id"] == ""
        assert state["current_orchestration_source"] == ""

    async def test_an_unsaved_draft_id_is_dropped(self, client, builder, fake_llm):
        state = await self._final_state(client, fake_llm, {
            "message": "add a step", "current_orchestration_id": "orch_temp_not_saved",
        })
        assert state["current_orchestration_id"] == ""

    async def test_a_reply_picks_from_the_list_the_last_turn_asked_about(
        self, client, builder, fake_llm
    ):
        """F16. The builder asked which one; "the second one" answers it, and the
        router sees the conversation the request was made in."""
        await self._orchestration("orch_ext", "extended research")
        await self._orchestration("orch_ext_2", "Extended Research")
        first = await self._final_state(client, fake_llm, {"message": self.MESSAGE, "client_session_id": "pick"})
        assert first["current_orchestration_source"] == "ambiguous"

        fake_llm.reset()
        second = await self._final_state(client, fake_llm, {
            "message": "the second one", "client_session_id": "pick",
            "history": [{"role": "user", "content": self.MESSAGE},
                        {"role": "assistant", "content": "Which one did you mean?"}],
        })
        assert (second["current_orchestration_id"], second["current_orchestration_source"]) == (
            "orch_ext_2", "picked",
        )
        assert f"[chat_history]:\nuser: {self.MESSAGE}" in fake_llm.calls[0]["prompt_msg"]

    async def test_another_conversation_does_not_answer_the_question(
        self, client, builder, fake_llm
    ):
        await self._orchestration("orch_ext", "extended research")
        await self._orchestration("orch_ext_2", "Extended Research")
        await self._final_state(client, fake_llm, {"message": self.MESSAGE, "client_session_id": "one"})
        fake_llm.reset()
        other = await self._final_state(client, fake_llm, {"message": "the second one", "client_session_id": "two"})
        assert other["current_orchestration_id"] == ""

    async def test_an_ambiguous_name_resolves_nothing(self, client, builder, fake_llm):
        await self._orchestration("orch_ext", "extended research")
        await self._orchestration("orch_ext_2", "Extended Research")
        state = await self._final_state(client, fake_llm, {"message": self.MESSAGE})
        assert state["current_orchestration_id"] == ""
        assert state["current_orchestration_source"] == "ambiguous"
        assert "orch_ext" in state["current_orchestration_matches"]
        assert "orch_ext_2" in state["current_orchestration_matches"]

    async def test_the_open_page_is_described_for_the_answer_and_the_planner(
        self, client, builder, fake_llm
    ):
        await self._orchestration("orch_ext", "extended research")
        state = await self._final_state(client, fake_llm, {
            "message": "what does this one do?", "current_orchestration_id": "orch_ext",
        })
        assert "extended research" in state["current_page"]
        assert "orch_ext" in state["current_page"]

    async def test_a_run_paused_before_these_keys_existed_still_resumes(
        self, client, builder, fake_llm
    ):
        """A release that adds state keys meets runs paused on the one before.

        Resume restores the checkpointed state — which has none of the new keys
        — and re-reads the definition from the store, which the new seed has
        replaced. It has to carry on from the step it paused at.
        """
        import copy

        import core.server as server
        from core.models_orchestration import Orchestration
        from core.native_builder import bundled_definitions
        from core.native_builder.target import TARGET_KEYS
        from core.orchestration.engine import OrchestrationEngine
        from core.store.resources import get_orchestration, save_orchestration

        added = set(TARGET_KEYS) - {"current_orchestration_id"}
        shipped = bundled_definitions()["orchestration"]
        previous = copy.deepcopy(shipped)
        for key in added:
            previous["state_schema"].pop(key, None)
        for step in previous["steps"]:
            step["input_keys"] = [k for k in step.get("input_keys") or [] if k not in added]
        assert previous != shipped, "nothing was added, so nothing is being tested"
        await save_orchestration(previous)

        # Started the way the previous release started it: no target keys at all.
        fake_llm.script([route("create"), "What should the new step check?", route("clarify")])
        engine = OrchestrationEngine(Orchestration.model_validate(previous), server)
        events = [e async for e in engine.run(
            initial_input="add a step",
            run_id="builder_paused_before_release",
            session_id="builder:test:paused",
            initial_state={"user_message": "add a step", "current_orchestration_id": ""},
        )]
        asked = next(e for e in events if e["type"] == "human_input_required")

        await seed_native_builder()  # the new release
        assert (await get_orchestration("orch_native_builder"))["state_schema"].keys() >= added

        fake_llm.reset()
        fake_llm.script([
            "Requirements: check the final JSON has quality and content.",  # step_understand
            route("draft"),                                                   # step_clarify_route
            "## Overview\nOne new step.",                                     # step_draft_plan
        ])
        after = await stream(client, "/api/builder/resume", {
            "run_id": asked["run_id"], "response": {"answer": "quality and content"},
        })

        steps = [e.get("step_name") for e in after if e["type"] == "step_start"]
        draft = next(s["name"] for s in shipped["steps"] if s["id"] == "step_draft_plan")
        assert draft in steps, steps
        assert next(e for e in after if e["type"] == "human_input_required")
        assert not [e for e in after if e["type"] in ("orchestration_error", "step_error")]
        prompts = "\n".join(str(c.get("prompt_msg", "")) for c in fake_llm.calls)
        assert "{state.current_" not in prompts



class TestTheRunIsDrivenByThePump:
    """The builder's run has to be stoppable, and that is a property of how it
    is driven, not of the panel.

    `core/orchestration/runner.py` says every orchestration entry point goes
    through it; the builder was the one that did not. It iterated
    `engine.run(...)` inside the StreamingResponse, so the run never reached
    `_active_tasks`, `POST /api/orchestrations/runs/{id}/cancel` had no task to
    cancel, its events were never journalled, and the response task dying —
    a reload, a closed tab — took the run with it.

    Read `_active_tasks`, never the stream, to find a run that is still going:
    httpx's ASGITransport buffers a response body whole, so not one frame is
    readable until the run has ended. The frames are asserted afterwards.
    """

    @pytest.fixture
    def gated_llm(self, monkeypatch):
        """An LLM call that parks until the test lets it go.

        Cancelling a run that has already finished proves nothing, and the fake
        LLM answers instantly, so the window is held open deliberately rather
        than raced for.
        """
        started, release = asyncio.Event(), asyncio.Event()

        async def gate(*args, **kwargs):
            started.set()
            await release.wait()
            return "create"

        import core.llm_providers as _llm
        import core.react_engine as _re
        monkeypatch.setattr(_llm, "generate_response", gate, raising=False)
        monkeypatch.setattr(_re, "llm_generate_response", gate, raising=False)
        return started, release

    @staticmethod
    async def _until(predicate, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.02)
        return False

    @staticmethod
    def _builder_runs() -> list[str]:
        from core.orchestration.runner import _active_tasks
        return [r for r in _active_tasks if r.startswith("builder_")]

    async def _mid_step(self, client, started, events: list[dict]) -> asyncio.Task:
        """Start a builder run and return once it is inside its first model call."""
        async def consume():
            async with client.stream("POST", "/api/builder/chat",
                                     json={"message": "build something"}) as response:
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        events.append(json.loads(line[6:]))

        reader = asyncio.create_task(consume())
        assert await self._until(started.is_set), "the run never reached a model call"
        return reader

    async def test_a_running_builder_is_registered_and_cancellable(
        self, client, builder, gated_llm
    ):
        started, release = gated_llm
        events: list[dict] = []
        reader = await self._mid_step(client, started, events)
        try:
            running = self._builder_runs()
            assert running, (
                "a builder run is inside a model call and the pump's registry is "
                "empty — POST /api/orchestrations/runs/{id}/cancel has no task to "
                "cancel, so the panel has nothing to stop"
            )
            run_id = running[0]

            cancelled = await client.post(f"/api/orchestrations/runs/{run_id}/cancel")
            assert cancelled.status_code == 200
            await asyncio.wait_for(reader, timeout=15)
        finally:
            release.set()
            if not reader.done():
                reader.cancel()

        assert any(
            e["type"] == "orchestration_error" and e.get("error") == "Cancelled"
            for e in events
        ), [e["type"] for e in events]
        assert not self._builder_runs(), "the registry still holds a finished run"

    async def test_the_run_outlives_the_browser(self, client, builder, gated_llm):
        """A reload must not kill a build: the run finishes and stays resumable.

        The task being detached is not enough on its own: spawned inside the
        response body, a real build stalled where it stood the moment the
        browser went away, reporting `running` for ever. Spawning from the
        handler is what this pins; `test_response_bodies_start_no_work.py`
        holds the rule for every route, and `test_outlives_the_client.py`
        hangs up on a real server.
        """
        from core.orchestration.runner import _active_tasks
        from core.orchestration.state import SharedState

        started, release = gated_llm
        events: list[dict] = []
        reader = await self._mid_step(client, started, events)
        running = self._builder_runs()
        assert running, "the run was never registered, so nothing can outlive anything"
        run_id = running[0]

        reader.cancel()                     # the browser goes away
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await reader
        await asyncio.sleep(0.05)

        task = _active_tasks.get(run_id)
        assert task is not None and not task.done(), (
            f"'{run_id}' died with the response it was streaming to"
        )

        release.set()                       # and it carries on without a reader
        assert await self._until(lambda: run_id not in _active_tasks, timeout=30), (
            f"'{run_id}' never finished once its reader had gone"
        )
        restored = await SharedState.restore(run_id)
        assert restored.run.status in ("paused", "completed"), restored.run.status
        assert restored.run.step_history, "the run recorded no completed step"

    async def test_two_pumps_for_one_run_are_refused(self, client, builder, gated_llm):
        """Two pumps interleave one run's journal and duplicate its work — the
        guard the other resume routes already carry."""
        started, release = gated_llm
        events: list[dict] = []
        reader = await self._mid_step(client, started, events)
        try:
            running = self._builder_runs()
            assert running, "the run was never registered"
            second = await client.post(
                "/api/builder/resume",
                json={"run_id": running[0], "response": {"answer": "x"}},
            )
            assert second.status_code == 409, second.status_code
        finally:
            release.set()
            if not reader.done():
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await reader
