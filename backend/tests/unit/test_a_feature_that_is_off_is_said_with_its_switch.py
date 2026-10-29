"""F1203 (BLD1; CAP3's F-i): a feature that is off is said with where it is turned on, and offered as a switch.

Boards and Knowledge are off by default. A card for either was refused in words that named no switch, and the build
planner was told "no board tools" and planned without the board. Now one sentence (`core.features.off_sentence`) says
what is off and where; `turn_on_feature` is its switch, an admin's, in the app only, saved as Settings saves it; and
Synapse AI's card for it (`feature_on`) is offered to one who may press it, also in place of a card that needed it.
"""
from __future__ import annotations

import json

import pytest

from _fakes.knowledge_env import use_saved_settings
from core import builder_access, features
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.orchestration.state import SharedState
from core.product_tools import areas, confirm, hooks, registry
from core.product_tools.caller import Caller
from core.product_tools.errors import ToolRefusal
from core.product_tools.roles import Role


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def run(monkeypatch):
    from core.store import get_store

    use_saved_settings(monkeypatch)
    await get_store()
    confirm.set_claim_store(None)
    builder_access.forget_runs()
    await SharedState(OrchestrationRun(run_id="builder_ft", orchestration_id="orch_native_builder",
                                       shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}})).checkpoint()
    yield
    hooks.set_role_source(None)
    builder_access.forget_runs()
    features.set_where("boards", None)


def _as(role: Role):
    async def source(user_id):
        return role
    hooks.set_role_source(source)


async def _prepare(args):
    raw, events = await execute_builder_tool_with_events("prepare_action", args, _Server(), run_id="builder_ft")
    return json.loads(raw), [e for e in events if e.get("type") == "action_needed"]


class TestTheSentence:
    def test_it_says_what_is_off_and_where(self):
        assert features.off_sentence("boards") == ("Boards are switched off in this workspace: turn them on in "
                                                   "Settings › General › Boards.")
        assert features.off_sentence("knowledge") == ("Knowledge is switched off in this workspace: turn it on in "
                                                      "Settings › General › Knowledge.")

    def test_a_host_says_where_its_switch_is(self):
        features.set_where("boards", "Settings › Features › Boards (Team plan)")
        assert features.off_sentence("boards").endswith("in Settings › Features › Boards (Team plan).")

    async def test_the_product_tools_refuse_in_it(self):
        from core.product_tools import boards, knowledge

        with pytest.raises(ToolRefusal, match="turn them on in Settings › General › Boards"):
            await boards._on()
        with pytest.raises(ToolRefusal, match="turn it on in Settings › General › Knowledge"):
            await knowledge._base("FAQ")


class TestTheSwitch:
    def test_it_is_an_admins_in_the_app_only(self):
        areas.ensure_loaded()
        tool = registry.get("turn_on_feature")
        assert tool.min_role == Role.ADMIN and tool.app_only and tool.kind == "act"

    @pytest.mark.parametrize("feature", ["boards", "knowledge"])
    async def test_it_turns_one_on_as_settings_does(self, feature):
        areas.ensure_loaded()
        assert not await features.is_on(feature)
        got = await registry.invoke("turn_on_feature", Caller(role=Role.ADMIN, via="assistant"), {"feature": feature})
        assert await features.is_on(feature) and got["on"] is True
        again = await registry.invoke("turn_on_feature", Caller(role=Role.ADMIN, via="assistant"), {"feature": feature})
        assert "already on" in again["note"]

    async def test_a_builder_may_not(self):
        areas.ensure_loaded()
        with pytest.raises(ToolRefusal):
            await registry.invoke("turn_on_feature", Caller(role=Role.BUILDER, via="assistant"), {"feature": "boards"})
        assert not await features.is_on("boards")


class TestTheCards:
    async def test_the_switch_is_a_card(self):
        _as(Role.ADMIN)
        said, [card] = await _prepare({"action": "feature_on", "feature": "knowledge"})
        assert card["title"] == "Turn on Knowledge" and "Settings › General › Knowledge" in card["detail"]
        assert card["buttons"][0]["tool"] == "turn_on_feature" and not await features.is_on("knowledge")

    async def test_a_card_that_needs_it_offers_its_switch_instead(self):
        _as(Role.OWNER)
        said, cards = await _prepare({"action": "board_create", "name": "Bugs"})
        assert said["error"].startswith(features.off_sentence("boards")) and "A card is up" in said["error"]
        assert [c["title"] for c in cards] == ["Turn on Boards"]

    async def test_one_who_may_not_turn_it_on_is_told_who_may(self):
        _as(Role.BUILDER)
        said, cards = await _prepare({"action": "board_create", "name": "Bugs"})
        assert cards == [] and "Ask an admin of this workspace to turn Boards on." in said["error"]

    async def test_one_already_on_gets_no_switch(self, client):
        from core.routes.settings import save_settings

        _as(Role.ADMIN)
        await save_settings({"boards_enabled": True})
        said, cards = await _prepare({"action": "feature_on", "feature": "boards"})
        assert cards == [] and said["error"] == "Boards is already on."
