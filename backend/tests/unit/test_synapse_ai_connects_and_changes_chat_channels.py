"""Synapse AI connects chat channels and changes them from cards (F1263, REACH2; D340 R4, Naveen 10-30: "connect to
channels").

A channel is connected on a Connect card (`channel_connect`, `prepare_connection`'s kind `channel`): the platform's
fields for the person to type, its token a password input, never a model's (F1093). Teams and WhatsApp call this app,
so their card carries the address to paste. One that is connected is turned on or off, removed, tested with one real
message (said before the press, never retried), answered by the agents named, and given run notices, each through the
Channels screen's own code (`core.messaging.service`). A phone number is the Phone screen's. In the app only, a
builder's.
"""
from __future__ import annotations

import json

import pytest

from _fakes.knowledge_env import use_saved_settings
from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.messaging import store
from core.models_orchestration import OrchestrationRun
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks
from core.product_tools.roles import Role


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


class _Manager:
    """The running adapters, as the routes and the tools see them: what was started and stopped."""

    def __init__(self):
        self.started, self.stopped = [], []

    async def start_channel(self, channel_id):
        self.started.append(channel_id)
        await store.update_channel_status(channel_id, "running")

    async def stop_channel(self, channel_id):
        self.stopped.append(channel_id)

    async def get_status(self):
        return await store.list_channels()


@pytest.fixture(autouse=True)
async def assistant_run(monkeypatch, test_app):
    import core.server as server
    from core.store import get_store

    await get_store()
    use_saved_settings(monkeypatch)
    manager = _Manager()
    monkeypatch.setattr(server, "messaging_manager", manager)
    test_app.state.messaging_manager = manager
    confirm.set_claim_store(None)
    builder_access.forget_runs()
    await SharedState(OrchestrationRun(run_id="builder_ch", orchestration_id="orch_native_builder",
                                       shared_state={builder_access.CALLER_KEY: {"user_id": None}})).checkpoint()
    yield manager
    test_app.state.messaging_manager = None
    hooks.set_role_source(None)
    builder_access.forget_runs()


async def _call(tool, args):
    raw, events = await execute_builder_tool_with_events(tool, args, _Server(), run_id="builder_ch")
    return json.loads(raw), events


async def _prepare(args):
    said, events = await _call("prepare_action", args)
    return said, [e for e in events if e.get("type") in ("action_needed", "connection_needed")]


async def _press(client, card, i=0):
    res = await client.post("/api/assistant/act", json=card["buttons"][i])
    assert res.status_code == 200, res.text
    return res.json()["result"]


async def _telegram(**extra):
    return await store.save_channel({"name": "Support bot", "platform": "telegram", "mode": "polling",
                                     "credentials": {"bot_token": "123456:SECRET-TOKEN"}, **extra})


# ── connecting one ──────────────────────────────────────────────────────────────────────────────────────────────────

async def test_a_channel_is_connected_on_a_card_whose_token_the_person_types(seed_agent):
    agent = await seed_agent(name="Front desk")
    said, [card] = await _prepare({"action": "channel_connect", "name": "Support bot", "platform": "telegram",
                                   "agent": "front desk"})
    assert card["type"] == "connection_needed" and card["kind"] == "channel"
    assert card["title"] == 'Connect Telegram "Support bot"'
    channel = card["channel"]
    assert channel["agent"] == {"id": agent["id"], "name": "Front desk"} and channel["mode"] == "polling"
    assert [(f["key"], f["secret"]) for f in channel["fields"]] == [("bot_token", True)]
    assert '"Connect"' in said["say"] and await store.list_channels() == []  # nothing until the person connects it


async def test_the_change_route_s_op_puts_up_the_same_card():
    from core.native_builder import change_ops as C

    assert C.issues("channel_connect", {"kind": "channel", "name": "Support bot"}, {"platform": "telegram"}) == []
    assert any("needs platform" in f for f in C.issues("channel_connect", {"kind": "channel", "name": "x"}, {}))
    said, events = await _call("prepare_connection", {"kind": "channel", "title": "Connect Slack",
                                                      "platform": "slack", "name": "Team"})
    [card] = [e for e in events if e.get("type") == "connection_needed"]
    assert [f["key"] for f in card["channel"]["fields"]] == ["bot_token", "app_token"]


async def test_teams_and_whatsapp_carry_the_address_to_paste_once_one_is_set():
    from core.routes.settings import save_settings

    said, cards = await _prepare({"action": "channel_connect", "name": "Ops", "platform": "teams"})
    assert cards == [] and "public address" in said["error"] and "channels" in said["error"]

    await save_settings({"messaging_webhook_base_url": "https://synapse.acme.com/"})
    _, [card] = await _prepare({"action": "channel_connect", "name": "Ops", "platform": "whatsapp"})
    channel = card["channel"]
    assert channel["webhook_base"] == "https://synapse.acme.com" and "Meta" in channel["paste_into"]
    secret = {f["key"] for f in channel["fields"] if f["secret"]}
    assert secret == {"access_token", "app_secret"}


async def test_a_phone_number_is_the_phone_screen_s():
    said, cards = await _prepare({"action": "channel_connect", "name": "Line", "platform": "twilio_voice"})
    assert cards == [] and "Phone screen" in said["error"]


# ── one that is connected ───────────────────────────────────────────────────────────────────────────────────────────

async def test_on_and_off_are_cards_through_the_screen_s_code(client, assistant_run):
    ch = await _telegram(enabled=False)
    _, [card] = await _prepare({"action": "channel_on", "channel": "support bot"})
    assert card["rows"] == [{"label": "On", "before": "off", "after": "on"}]
    await _press(client, card)
    assert assistant_run.started == [ch["id"]] and (await store.get_channel(ch["id"]))["enabled"] is True

    said, cards = await _prepare({"action": "channel_on", "channel": "Support bot"})
    assert cards == [] and "already on" in said["error"]
    _, [card] = await _prepare({"action": "channel_off", "channel": "Support bot"})
    await _press(client, card)
    assert assistant_run.stopped == [ch["id"]] and (await store.get_channel(ch["id"]))["enabled"] is False


async def test_a_removal_is_a_danger_card(client):
    ch = await _telegram()
    _, [card] = await _prepare({"action": "channel_delete", "channel": "Support bot"})
    assert card["danger"] is True and "typing it again" in card["detail"]
    await _press(client, card)
    assert await store.get_channel(ch["id"]) is None


async def test_a_test_sends_one_real_message_said_before_and_never_again(client, monkeypatch):
    from core.messaging import senders

    sent = []

    async def send(platform, credentials, conversation_id, text):
        sent.append((platform, conversation_id, text))
    monkeypatch.setattr(senders, "send", send)

    await _telegram()
    said, cards = await _prepare({"action": "channel_test", "channel": "Support bot"})
    assert cards == [] and "Message the bot once first" in said["error"]

    ch = await _telegram(name="Sales bot", default_conversation_id="98765")
    _, [card] = await _prepare({"action": "channel_test", "channel": "Sales bot"})
    assert card["detail"] == "Sends one test message to 98765 on Telegram."
    done = await _press(client, card)
    assert sent == [("telegram", "98765", "Test message from Synapse — this channel is connected.")]
    assert "98765" in done["say"] and ch["id"] == done.get("channel_id", ch["id"])

    async def refused(platform, credentials, conversation_id, text):
        sent.append("tried")
        raise RuntimeError("Unauthorized: 123456:SECRET-TOKEN")
    monkeypatch.setattr(senders, "send", refused)
    sent.clear()
    _, [card] = await _prepare({"action": "channel_test", "channel": "Sales bot"})
    res = await client.post("/api/assistant/act", json=card["buttons"][0])
    assert res.status_code != 200 and "It did not send" in res.text and "SECRET-TOKEN" not in res.text
    assert sent == ["tried"]  # once: never retried


async def test_who_answers_is_named_and_shown_as_it_changes(client):
    from _fakes.seed import make_agent, seed_agents

    desk, sales = await seed_agents([make_agent(name="Front desk"), make_agent(name="Sales")])
    ch = await _telegram()
    await store.set_assignments(ch["id"], [desk["id"]], default_agent_id=desk["id"])

    _, [card] = await _prepare({"action": "channel_agents", "channel": "Support bot", "agents": ["Front desk", "Sales"],
                                "agent": "Sales"})
    assert card["rows"] == [
        {"label": "Answered by", "before": "Front desk", "after": "Front desk, Sales"},
        {"label": "Answers first", "before": "Front desk", "after": "Sales"},
    ]
    await _press(client, card)
    assert set(await store.get_agent_ids_for_channel(ch["id"])) == {desk["id"], sales["id"]}
    assert await store.get_default_agent_id(ch["id"]) == sales["id"]


async def test_run_notices_go_to_a_channel_and_stop(client):
    from core.config import load_settings

    ch = await _telegram()
    _, [card] = await _prepare({"action": "channel_run_notifications", "channel": "Support bot"})
    assert card["rows"] == [{"label": "Run notices also go to", "before": None, "after": "Support bot"}]
    await _press(client, card)
    assert load_settings()["run_notification_channel_id"] == ch["id"]

    _, [card] = await _prepare({"action": "channel_run_notifications_off", "channel": "Support bot"})
    await _press(client, card)
    assert load_settings()["run_notification_channel_id"] == ""


async def test_a_plan_reads_each_channel_s_id_for_an_approval_on_it():
    ch = await _telegram()
    read, _ = await _call("read_workspace", {"topic": "channels"})
    assert [(i["name"], i["id"]) for i in read["items"]] == [("Support bot", ch["id"])]


def test_the_channel_tools_are_the_app_s_own_and_a_builder_s():
    from core.product_tools import areas, registry

    areas.ensure_loaded()
    for name in ("set_channel_enabled", "delete_channel", "test_channel", "set_channel_agents",
                 "set_run_notification_channel"):
        tool = registry.get(name)
        assert tool.app_only and tool.min_role == Role.BUILDER and tool.kind == "act", name
    assert registry.get("delete_channel").irreversible


def test_the_channels_screen_is_reached_and_the_phone_screen_is_not():
    from core import feature_census

    rows = {s.id: s for s in feature_census.CENSUS}
    assert rows["messaging"].cant == "" and "prepare_connection" in rows["messaging"].tools
    assert rows["phone"].cant


async def test_the_screen_s_routes_say_what_they_said(client):
    """One place (F1189): the routes call the service and answer as before."""
    res = await client.delete("/api/messaging/channels/ch_nope")
    assert res.status_code == 404 and res.json()["detail"] == "Channel not found"
    ch = await _telegram()
    res = await client.put(f"/api/messaging/channels/{ch['id']}/assignments", json={"agent_ids": ["agent_ghost"]})
    assert res.status_code == 422 and res.json()["detail"] == "Unknown agent(s): agent_ghost"
    listed = (await client.get("/api/messaging/channels")).json()
    assert "SECRET-TOKEN" not in str(listed)
