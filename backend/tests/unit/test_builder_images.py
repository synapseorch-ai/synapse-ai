"""
An attachment reaching a builder step — and not reaching anything else.

The builder is an orchestration run, not a chat turn, and no orchestration step
consumed images at all. The obvious way to add them — a state key, declared in
the step's `input_keys` — is the wrong one twice over: `input_keys` entries are
injected into the prompt as text, so a base64 data URI would land in the context
window; and shared state is echoed to the browser twice at the end of a run,
checkpointed at every step boundary, returned by the run-detail endpoint and fed
to the memory summariser. All of that is fine for a model name. None of it is
fine for up to 20 MB of image.

So they travel on the engine, the way `global_variables` already does, and a
step opts in with `pass_images`. These tests pin both halves: that an opted-in
step gets them, and that nothing else does.
"""
import base64

import pytest

from core.models_orchestration import StepConfig, StepType
from core.orchestration.context import snapshot_inputs
from _fakes.images import png

PNG = png()
URI = "data:image/png;base64," + base64.b64encode(PNG).decode()


class TestOptingIn:
    def test_a_step_does_not_see_images_unless_it_asks(self):
        assert StepConfig(id="s", name="S", type=StepType.AGENT).pass_images is False

    def test_the_builder_asks_on_exactly_two_steps(self):
        """The analyst, who may be shown a picture of what to build, and the
        reporter, who may be asked what one is. Not the savers, and not the
        operator — they would pay for tokens they cannot use."""
        from core.native_builder import bundled_definitions

        steps = bundled_definitions()["orchestration"]["steps"]
        opted = {s["id"] for s in steps if s.get("pass_images")}
        assert opted == {"step_understand", "step_answer"}

    def test_the_field_survives_a_round_trip(self):
        """Orchestrations are stored as JSON, so a new field only exists if the
        model keeps it through validate → dump."""
        step = StepConfig(id="s", name="S", type=StepType.AGENT, pass_images=True)
        assert StepConfig.model_validate(step.model_dump()).pass_images is True


class TestTheyAreNotState:
    """The reason this is not a state key, pinned structurally.

    `build_origin_aware_context` renders every declared `input_keys` entry into
    the prompt as text, and `snapshot_inputs` renders the same set again as "what
    the agent saw" for replay into later turns. An image key declared on a step
    would reach a model through both.
    """

    def test_no_bundled_step_declares_an_image_key(self):
        from core.native_builder import bundled_definitions

        orch = bundled_definitions()["orchestration"]
        declared = {k for s in orch["steps"] for k in (s.get("input_keys") or [])}
        declared |= {s["output_key"] for s in orch["steps"] if s.get("output_key")}
        assert not [k for k in declared if "image" in k.lower()], declared

    def test_the_state_schema_has_no_image_key(self):
        """Not even underscore-prefixed. `_public_run_view` strips a fixed list
        of prefixes and this would not have been on it."""
        from core.native_builder import bundled_definitions

        schema = bundled_definitions()["orchestration"]["state_schema"]
        assert not [k for k in schema if "image" in k.lower()], list(schema)

    def test_the_snapshot_of_a_steps_inputs_carries_none(self):
        class Engine:
            step_map: dict = {}
            images = [URI]

        class Run:
            run_id = "run_1"
            session_id = "s"
            shared_state = {"user_input": "hello"}
            step_history: list = []

        step = StepConfig(id="step_x", name="X", type=StepType.AGENT,
                          prompt_template="Do the thing.")
        snap = snapshot_inputs(step, Run(), Engine())
        assert not any("base64" in v for v in snap.values())


class TestTheRequestChecksThemFirst:
    def test_it_rejects_what_chat_rejects(self):
        """Same validator, same limits — `core.images` is the one place that
        answers what an acceptable attachment is."""
        from pydantic import ValidationError

        from core.routes.builder import BuilderChatRequest

        with pytest.raises(ValidationError, match="Up to 5 images"):
            BuilderChatRequest(message="hi", images=[URI] * 6)

    def test_it_rejects_a_renamed_file(self):
        from pydantic import ValidationError

        from core.routes.builder import BuilderChatRequest

        not_a_png = "data:image/png;base64," + base64.b64encode(b"just text").decode()
        with pytest.raises(ValidationError):
            BuilderChatRequest(message="hi", images=[not_a_png])

    def test_it_accepts_a_real_one(self):
        from core.routes.builder import BuilderChatRequest

        assert BuilderChatRequest(message="hi", images=[URI]).images == [URI]


class TestTheFlagRefusesToBeANoOp:
    def test_a_non_agent_step_cannot_set_it(self):
        """Only the agent executor reads `pass_images`. Storing it on an `llm`
        step would look like it worked and do nothing."""
        import pytest as _pytest
        from pydantic import ValidationError

        with _pytest.raises(ValidationError, match="only read by agent steps"):
            StepConfig(id="s", name="S", type=StepType.LLM, pass_images=True)

    def test_an_agent_step_can(self):
        assert StepConfig(id="s", name="S", type=StepType.AGENT, pass_images=True).pass_images


# ── the builder sets it (F313) ───────────────────────────────────────────────

def _builder_server():
    import types
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _call(tool, args):
    import json
    from core.builder_tools import execute_builder_tool
    return json.loads(await execute_builder_tool(tool, args, _builder_server()))


async def _stored(orch_id):
    from core.models_orchestration import Orchestration
    from core.scale.context import resolve_orchestration
    data = await resolve_orchestration(orch_id)
    Orchestration.model_validate(data)  # what a run does first
    return {s["id"]: s for s in data["steps"]}


async def _two_steps():
    """An orchestration the builder wrote: an agent step and an llm step, both asked to see attachments."""
    import json
    from _fakes import seed as S
    await S.seed_agents([S.make_agent(id="agent_eyes")])
    made = await _call("create_orchestration", {
        "name": "Photo desk", "description": "d", "entry_step_id": "look",
        "steps_json": json.dumps([
            {"id": "look", "name": "Look", "type": "agent", "agent_id": "agent_eyes",
             "prompt_template": "Describe it.", "pass_images": True, "next_step_id": "write"},
            {"id": "write", "name": "Write", "type": "llm", "prompt_template": "Caption it.",
             "pass_images": True},
        ]),
    })
    return made.get("orch_id") or made.get("id") or made.get("orchestration", {}).get("id")


class TestTheBuilderCanSetIt:
    """The AI builder could not mark a step "Sees attachments": no tool offered it (F313)."""

    def test_both_step_tools_offer_it(self):
        from core.builder_tools import BUILDER_TOOL_SCHEMAS
        tools = {t["function"]["name"]: t["function"]["parameters"] for t in BUILDER_TOOL_SCHEMAS}
        assert tools["add_single_step"]["properties"]["pass_images"]["type"] == "boolean"
        items = tools["add_multiple_steps"]["properties"]["steps"]["items"]["properties"]
        assert items["pass_images"]["type"] == "boolean"

    def test_the_cheatsheet_and_both_json_descriptions_name_it(self):
        from core.builder_tools import PATCH_JSON_DESCRIPTION, STEPS_JSON_DESCRIPTION
        from core.native_builder import STEP_TYPE_CHEATSHEET
        for text in (STEPS_JSON_DESCRIPTION, PATCH_JSON_DESCRIPTION, STEP_TYPE_CHEATSHEET):
            assert "pass_images" in text

    async def test_a_non_agent_step_the_builder_writes_drops_it_and_still_loads(self):
        orch_id = await _two_steps()
        steps = await _stored(orch_id)
        assert steps["look"]["pass_images"] is True
        assert "pass_images" not in steps["write"]

    async def test_a_step_patched_away_from_agent_drops_it(self):
        import json
        orch_id = await _two_steps()
        await _call("update_step", {"orch_id": orch_id, "step_id": "look",
                                    "patch_json": json.dumps({"type": "llm", "prompt_template": "Describe it."})})
        steps = await _stored(orch_id)
        assert steps["look"]["type"] == "llm" and "pass_images" not in steps["look"]

    def test_defaults_add_nothing_to_a_step_that_did_not_ask(self):
        from core.builder_tools import _fill_step_defaults
        [agent, llm] = _fill_step_defaults([{"id": "a", "name": "A", "type": "agent"},
                                            {"id": "b", "name": "B", "type": "llm"}])
        assert "pass_images" not in agent and "pass_images" not in llm
