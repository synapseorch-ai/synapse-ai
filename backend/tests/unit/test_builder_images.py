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

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
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
