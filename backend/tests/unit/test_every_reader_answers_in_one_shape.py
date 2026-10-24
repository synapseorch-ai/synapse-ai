"""Every `read_workspace` topic answers in one shape, on self-hosted and cloud alike (SAI2-P5, D300).

DESIGN's issue "self-hosted trails cloud": the same builder reads its workspace through readers a host may replace
(cloud's `plan` and `widgets`), so a topic could answer in one shape here and another there, and a prompt or a code
check written against one would misread the other. `builder_readers.SHAPES` is the contract: for each topic, the
keys an answer has (`?` marks one a host may leave out) and the keys of each of its `items`. This test reads every
topic of a seeded self-hosted workspace and holds each answer to it; cloud's twin
(`api/tests/test_the_builder_answers_from_cloud.py`) holds its own readers to the same `SHAPES`.
"""
from __future__ import annotations

import pytest

from core import builder_access
from core.builder_readers import SHAPES, TOPICS, shape_problems

from test_the_assistant_reads_what_happened import _ago, _read, _run, _workflow, viewer  # noqa: F401


@pytest.fixture(autouse=True)
def settings_from_the_store():
    from core import settings_runtime
    from core.config import get_settings_provider, set_settings_provider

    previous = get_settings_provider()
    settings_runtime.reset_state()
    settings_runtime.install_provider()
    yield
    settings_runtime.reset_state()
    set_settings_provider(previous)


@pytest.fixture
async def workspace():
    """One of everything a topic reads."""
    from core.chat_widgets.store import save_widget
    from core.messaging import store as channels
    from core.routes.agents import save_user_agents
    from core.routes.settings import save_settings
    from core.store import schedules

    await save_settings({"boards_enabled": True})
    await save_user_agents([{"id": "agent_desk", "name": "Front desk", "type": "conversational",
                             "model": "gpt-4.1-mini", "tools": ["send_email"],
                             "tool_policy": {"open_world": "confirm"}}])
    await _workflow(id="orch_inv", name="Invoice chaser")
    await schedules.save({"id": "sched_inv", "name": "Nightly invoices", "enabled": True,
                          "target_type": "orchestration", "target_id": "orch_inv", "prompt": "",
                          "schedule_type": "cron", "cron_expression": "0 2 * * *"})
    await _run("run_ok", "orch_inv", "completed", started=_ago(hours=2), cost=0.01,
               history=[{"step_id": "s1", "step_name": "Draft", "step_type": "agent", "status": "completed",
                         "output_key": "draft"}], state={"draft": "Dear Acme"})
    await _run("run_bad", "orch_inv", "failed", started=_ago(hours=1), error="Slack refused")
    await _run("run_wait", "orch_inv", "paused", started=_ago(minutes=5), waiting_for_human=True,
               human_prompt="Send it?")
    await channels.save_channel({"id": "chn_tg", "name": "Patient chat", "platform": "telegram", "enabled": True})
    await channels.assign_channel("chn_tg", "agent_desk")
    await channels.save_channel({"id": "chn_ph", "name": "Front line", "platform": "twilio_voice", "enabled": True,
                                 "voice": {"number": "+14155550142", "language": "en-US"}})
    await save_widget({"id": "w1", "name": "Website helper", "agent_id": "agent_desk",
                       "allowed_origins": ["https://harbordental.example"], "enabled": True})


ARGS = {"run": {"run_id": "run_ok"}, "agent_activity": {"name": "Front desk"}}


def test_every_topic_has_a_shape():
    assert set(SHAPES) == set(TOPICS)


@pytest.mark.parametrize("topic", sorted(TOPICS))
async def test_each_topic_answers_in_its_shape(topic, workspace, viewer):
    builder_access.forget_runs()
    got = await _read(**{"topic": topic, **ARGS.get(topic, {})})
    assert "error" not in got, got
    assert shape_problems(topic, got) == [], got


def test_a_drifted_answer_is_named():
    assert shape_problems("agents", {"topic": "agents", "items": [{"name": "A", "type": "t", "model": "m"}]}) == [
        "agents: an item has no `tools`"]
    assert shape_problems("agents", {"topic": "agents", "items": [], "extra": 1}) == [
        "agents: `extra` is not in its shape"]
    assert shape_problems("plan", {"topic": "plan", "items": []}) == ["plan: has no `note`"]


async def test_a_hosts_reader_that_drifts_is_caught():
    """The kind of drift the twin catches: a host's plan reader answering in its own words."""
    from core import builder_readers

    async def plan(args):
        return {"plan": "Team", "credits_left": 24000}

    builder_readers.set_workspace_reader("plan", plan)
    try:
        got = await _read(topic="plan")
    finally:
        builder_readers.set_workspace_reader("plan", None)
    assert shape_problems("plan", got) == ["plan: has no `items`", "plan: has no `note`",
                                           "plan: `credits_left` is not in its shape",
                                           "plan: `plan` is not in its shape"]
