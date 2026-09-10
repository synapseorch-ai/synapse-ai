"""The AI builder's HTTP surface.

The panel used to be handed a private vocabulary invented by the route, on top
of a translation layer that guessed what had been saved. It reads the engine's
own events now, so what these tests pin is that the stream arrives intact —
including `resource_saved`, which is the whole point of the change.
"""
from __future__ import annotations

import json

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
        assert legend["step_count"] == 21
        assert legend["entry_step"]["type"] == "evaluator"
        assert {r["label"] for r in legend["routes"]} == {"create", "update", "manage", "answer"}
        assert {a["role"] for a in legend["agents"]} == {
            "analyst", "agent_creator", "saver_create", "saver_update", "operator", "reporter",
        }
        # The answer path adds a step and a sub-agent but no gate: a question
        # has nothing to approve.
        assert len(legend["human_gates"]) == 3
        assert "evaluator" in legend["step_types"]

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
