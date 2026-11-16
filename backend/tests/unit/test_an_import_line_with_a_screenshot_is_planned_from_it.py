"""The import starter's line sent with a screenshot and nothing pasted under it is planned from the screenshot (F1637).

The panel lets the line through when an image is attached, but the server answered "Nothing was pasted under the line"
before anything read the image: someone with no export, who brought a picture of the workflow instead, was told to
paste code they do not have. Now the route reads line plus screenshot as an ask to rebuild what the screenshot shows
(`export.screenshot_ask`): the run's message says so under the line, the planner sees the image as it sees any the
person sends, and code routes it to a new build. Nothing is stored apart from the images' own lane.

Near misses, each as before: the bare line with no image is still answered in code; the line with pasted code and an
image is read as an export; the line with the person's own words and an image keeps their words; an image with no line
is an ordinary message.
"""
from __future__ import annotations

import base64

import pytest

from _fakes.images import png
from core.native_builder import export, read_turn

LEAD = export.IMPORT_LEAD
URI = "data:image/png;base64," + base64.b64encode(png()).decode()
PY_EXPORT = '''from agents import Agent, Runner

triage = Agent(name="Triage", instructions="Sort the tickets.")
'''
OWN_WORDS = "A bot that sorts our support emails into refunds and questions."


async def _run_message(monkeypatch, message: str, images: list[str]) -> str:
    """The message the builder's run starts with, for what the panel sent."""
    from core.native_builder import seed
    from core.orchestration.engine import OrchestrationEngine
    from core.routes import builder

    await seed.seed_native_builder()
    started: dict = {}

    async def fake_run(self, *, initial_input, run_id, session_id, initial_state):
        started.update(state=initial_state)
        if False:
            yield {}

    monkeypatch.setattr(OrchestrationEngine, "run", fake_run)
    request = builder.BuilderChatRequest(message=message, client_session_id="c1", images=images)
    _run_id, agen = await builder.prepare_builder_run(request, object(), panel=True)
    async for _ in agen:
        pass
    return str(started["state"]["user_message"])


class TestTheLineWithAScreenshotIsAnAskToRebuildIt:
    async def test_the_run_is_asked_to_rebuild_what_the_screenshot_shows(self, monkeypatch):
        said = await _run_message(monkeypatch, LEAD, [URI])
        assert export.lead_present(said) and not export.lead_only(said), said
        assert "screenshot" in said.lower(), said
        assert export.read_export(said) is None

    async def test_it_is_never_answered_as_nothing_pasted(self, monkeypatch):
        said = await _run_message(monkeypatch, LEAD, [URI])
        assert read_turn.decide(said, previous={}, facts={}).route != "export_missing"

    def test_the_shared_words_need_an_image(self):
        assert export.screenshot_ask(LEAD, has_images=True) != LEAD
        assert export.screenshot_ask(LEAD, has_images=False) == LEAD


class TestEverythingElseIsAsBefore:
    async def test_the_bare_line_with_no_image(self, monkeypatch):
        said = await _run_message(monkeypatch, LEAD, [])
        assert export.lead_only(said)
        assert read_turn.decide(said, previous={}, facts={}).route == "export_missing"

    async def test_the_line_with_pasted_code_and_an_image(self, monkeypatch):
        said = await _run_message(monkeypatch, LEAD + "\n\n" + PY_EXPORT, [URI])
        assert "screenshot" not in said.lower() and "Sort the tickets." in said

    async def test_the_line_with_their_own_words_and_an_image(self, monkeypatch):
        said = await _run_message(monkeypatch, LEAD + "\n\n" + OWN_WORDS, [URI])
        assert OWN_WORDS in said and "screenshot" not in said.lower()

    async def test_an_image_with_no_line(self, monkeypatch):
        said = await _run_message(monkeypatch, "Build this workflow.", [URI])
        assert said == "Build this workflow."


def _routed(message: str, kind: str):
    from core.native_builder.classify import override
    from core.native_builder.objects import Request

    return override(Request(kind=kind, needs="none"), message=message,
                    state={"may_change": True, "may_act": True}, inventory={}, connectable=set())


class TestCodeChoosesTheBuild:
    """A weak quick model never turns an import with something under the line into an answer."""

    @pytest.mark.parametrize("kind", ["answer", "research", "change"])
    def test_the_screenshot_ask_is_a_new_build(self, kind):
        routed = _routed(export.screenshot_ask(LEAD, has_images=True), kind)
        assert (routed.kind, routed.route) == ("build", "step_plan_room"), routed

    @pytest.mark.parametrize("kind", ["answer", "research"])
    def test_their_own_words_under_the_line_are_a_new_build(self, kind):
        routed = _routed(LEAD + "\n\n" + OWN_WORDS, kind)
        assert routed.kind == "build", routed

    def test_a_message_with_no_line_keeps_the_quick_models_answer(self):
        assert _routed("What does a screenshot of a workflow show?", "answer").kind == "answer"


def test_the_planner_still_sees_the_persons_images():
    from core.native_builder import bundled_definitions

    steps = {s["id"]: s for s in bundled_definitions()["orchestration"]["steps"]}
    assert steps["step_plan"].get("pass_images") is True
