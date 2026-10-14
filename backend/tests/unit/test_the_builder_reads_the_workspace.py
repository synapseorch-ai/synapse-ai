"""The AI builder reads the workspace from the workspace (BWA1, D274, decision 5).

BWA1's Nova Pro baseline asked twelve questions about the workspace and got four right: with no way to read
channels, phone numbers, widgets, tool policy, knowledge, usage, the plan or the models, the builder answered from
the nearest tool or from nothing. `read_workspace` reads each from the same stores the screens read. These pin:

- each topic answers from what is stored, in the words a person reads (on/paused, when, which agent answers);
- no secret ever comes back: a channel's token, a provider key — a key is only "set";
- what cannot be seen is said (Knowledge off, no plan on a self-hosted install), never filled;
- a host's reader replaces the self-hosted one for its topic; a reader that fails says so;
- the builder's answering agent, operator and analyst hold the tool.
"""
from __future__ import annotations

import json

import pytest

from core import builder_readers
from core.builder_tools import execute_builder_tool_with_events

TOKEN = "1234567890:" + "AAF" + "x" * 30
OPENAI = "sk-" + "proj-" + "Q" * 40


@pytest.fixture(autouse=True)
def settings_from_the_store():
    """`load_settings()` reaches the store once the lifespan installs the provider; the same wiring here."""
    from core import settings_runtime
    from core.config import set_settings_provider

    settings_runtime.reset_state()
    settings_runtime.install_provider()
    yield
    settings_runtime.reset_state()
    set_settings_provider(None)


async def _read(**args) -> dict:
    raw, _ = await execute_builder_tool_with_events("read_workspace", args, server_module=None)
    return json.loads(raw)


async def test_schedules_say_on_or_paused_and_when():
    from core.store import schedules as store
    await store.save({"id": "sched_a", "name": "Every night at 2", "enabled": True, "target_type": "orchestration",
                      "target_id": "orch_x", "prompt": "", "schedule_type": "cron", "cron_expression": "0 2 * * *"})
    await store.save({"id": "sched_b", "name": "Monday invoices", "enabled": False, "target_type": "orchestration",
                      "target_id": "orch_y", "prompt": "", "schedule_type": "cron", "cron_expression": "0 9 * * 1"})
    got = await _read(topic="schedules")
    by_name = {i["name"]: i for i in got["items"]}
    assert by_name["Every night at 2"]["on"] is True and by_name["Monday invoices"]["on"] is False
    assert by_name["Every night at 2"]["runs"] == 'cron "0 2 * * *" (UTC)'
    assert [i["name"] for i in (await _read(topic="schedules", name="monday"))["items"]] == ["Monday invoices"]


async def test_channels_and_phone_numbers_never_carry_a_secret():
    from core.messaging import store
    await store.save_channel({"id": "chn_tg", "name": "Patient chat", "platform": "telegram", "enabled": True,
                              "credentials": {"bot_token": TOKEN}})
    await store.save_channel({"id": "chn_ph", "name": "Front desk line", "platform": "twilio_voice", "enabled": True,
                              "credentials": {"api_key_secret": TOKEN},
                              "voice": {"number": "+14155550142", "language": "en-US"}})
    channels, phone = await _read(topic="channels"), await _read(topic="phone")
    assert [(c["name"], c["platform"], c["on"]) for c in channels["items"]] == [("Patient chat", "telegram", True)]
    assert phone["items"][0]["number"] == "+14155550142" and phone["items"][0]["on"] is True
    assert TOKEN not in json.dumps(channels) + json.dumps(phone)


async def test_widgets_say_their_agent_and_sites():
    from core.chat_widgets.store import save_widget
    await save_widget({"id": "w1", "name": "Website helper", "agent_id": "agent_none",
                       "allowed_origins": ["https://harbordental.example"], "enabled": True})
    got = await _read(topic="widgets")
    assert got["items"][0]["allowed_sites"] == ["https://harbordental.example"] and got["items"][0]["on"] is True


async def test_the_tool_policy_is_read_as_set():
    from core.routes.settings import save_settings
    await save_settings({"tool_policy_defaults": {"open_world": "confirm"}})
    got = await _read(topic="tool_policy")
    assert got["default"]["open_world"] == "confirm" and got["default"]["destructive"] == "allow"


async def test_models_say_a_key_is_set_never_what_it_is():
    from core.routes.settings import save_settings
    await save_settings({"model": "gpt-4.1-mini", "openai_key": OPENAI})
    got = await _read(topic="models")
    assert got["default_model"] == "gpt-4.1-mini" and "openai" in got["providers_with_a_key"]
    assert OPENAI not in json.dumps(got) and OPENAI[-4:] not in json.dumps(got)


async def test_what_cannot_be_seen_is_said():
    from core.routes.settings import save_settings
    await save_settings({"knowledge_enabled": False})
    assert "switched off" in (await _read(topic="knowledge"))["note"]
    assert "no plan" in (await _read(topic="plan"))["note"]


async def test_a_hosts_reader_replaces_the_self_hosted_one():
    async def plan(args):
        return {"plan": "Team", "credits_left": 24000}
    builder_readers.set_workspace_reader("plan", plan)
    try:
        assert (await _read(topic="plan"))["plan"] == "Team"
    finally:
        builder_readers.set_workspace_reader("plan", None)
    assert "no plan" in (await _read(topic="plan"))["note"]


async def test_a_reader_that_fails_says_so_and_an_unknown_topic_is_refused():
    async def broken(args):
        raise RuntimeError("store down")
    builder_readers.set_workspace_reader("usage", broken)
    try:
        assert "can't read usage" in (await _read(topic="usage"))["error"]
    finally:
        builder_readers.set_workspace_reader("usage", None)
    assert "topic must be one of" in (await _read(topic="passwords"))["error"]


def test_the_answering_agent_the_operator_and_the_analyst_hold_it():
    from core.native_builder import bundled_definitions
    held = {a["system_role"] for a in bundled_definitions()["agents"] if "read_workspace" in (a.get("tools") or [])}
    assert {"reporter", "operator", "requirements_analyst"} <= held or {"reporter", "operator", "analyst"} <= held, held


@pytest.mark.parametrize("topic", ["runs", "usage", "agents", "workflows"])
async def test_an_empty_workspace_answers_with_nothing_not_an_error(topic):
    got = await _read(topic=topic)
    assert "error" not in got and got["items"] == []
