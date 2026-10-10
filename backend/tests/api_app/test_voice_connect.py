"""
Connecting a phone number (VOX1a, V4/V5/V7): turning a `twilio_voice` channel on
checks the key and the number and points the number at this server, keeping
where it pointed before; turning it off or deleting it puts that back. The
channel API never shows the key's secret or the route token.
"""
import types

import httpx
import pytest

ACCOUNT = "AC" + "1" * 32
NUMBER = "+15551230000"
BASE = "https://calls.example.com"


@pytest.fixture
def twilio(monkeypatch):
    state = types.SimpleNamespace(voice_url="https://old.example/voice", app_sid="", writes=[], refuse_write=False,
                                  refuse_read=False, numbers=[NUMBER], restricted=False, signing=[], unsigned=[])

    def handler(req: httpx.Request):
        path = req.url.path
        if req.url.host == "webhooks.twilio.com":
            if req.method == "DELETE":
                state.unsigned.append(path.rsplit("/", 2)[-2])
                return httpx.Response(204)
            if state.restricted:
                return httpx.Response(401, json={"message": "Authenticate"})
            state.signing.append(path.rsplit("/", 1)[-1])
            if path.endswith("/SharedKeys"):
                return httpx.Response(201, json={"id": "webhooks_sharedkey_1", "secret": "sign-me"})
            if path.endswith("/Settings"):
                return httpx.Response(201, json={"id": "webhooks_setting_1"})
            return httpx.Response(202, json={"rule": {"id": "webhooks_rule_1"}})
        if state.refuse_read:
            return httpx.Response(401, json={"message": "Authenticate"})
        if path.endswith("/IncomingPhoneNumbers.json"):
            return httpx.Response(200, json={"incoming_phone_numbers": [
                {"sid": f"PN{i}", "phone_number": n, "voice_url": state.voice_url if i == 0 else "",
                 "voice_application_sid": ""} for i, n in enumerate(state.numbers)]})
        if "/IncomingPhoneNumbers/" in path and req.method == "GET":
            return httpx.Response(200, json={"phone_number": NUMBER, "voice_url": state.voice_url,
                                             "voice_method": "POST", "status_callback": "",
                                             "voice_application_sid": state.app_sid})
        if "/IncomingPhoneNumbers/" in path and req.method == "POST":
            if state.refuse_write:
                return httpx.Response(403, json={"message": "Forbidden"})
            form = dict(httpx.QueryParams(req.content.decode()))
            state.writes.append(form)
            state.voice_url = form.get("VoiceUrl", "")
            return httpx.Response(200, json={"voice_url": state.voice_url})
        return httpx.Response(404, json={})

    from core.messaging.voice import connect, hooks
    from core.messaging.voice.twilio import TwilioProvider

    monkeypatch.setattr(connect, "provider_for", lambda creds: TwilioProvider.from_credentials(
        creds, http=httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    hooks.install(None)
    monkeypatch.setattr(hooks.current(), "public_base", lambda: BASE)
    yield state
    hooks.install(None)


@pytest.fixture
def manager(test_app):
    import core.server as server
    from core.messaging.manager import MessagingManager
    m = MessagingManager(server_module=server)
    test_app.state.messaging_manager = m
    yield m
    test_app.state.messaging_manager = None


async def _number(client, **voice):
    r = await client.post("/api/messaging/channels", json={
        "name": "Front desk", "platform": "twilio_voice", "enabled": False,
        "credentials": {"account_sid": ACCOUNT, "api_key_sid": "SKx", "api_key_secret": "sekret-value"},
        "voice": {"number": NUMBER, **voice}, "senders_are_members": True})
    assert r.status_code == 200, r.text
    return r.json()


async def test_turning_it_on_points_the_number_and_off_puts_it_back(client, manager, twilio):
    from core.messaging import store as channel_store

    ch = await _number(client)
    assert ch["senders_are_members"] is False, "a caller is always a visitor"
    assert ch["credentials"]["api_key_secret"] == "" and ch["credentials_set"]["api_key_secret"] is True

    on = await client.post(f"/api/messaging/channels/{ch['id']}/enable")
    assert on.status_code == 200 and on.json()["status"] == "running", on.json()
    assert "route_token" not in on.json()["credentials"], "the route token is never shown"
    stored = await channel_store.get_channel(ch["id"])
    token = stored["credentials"]["route_token"]
    assert twilio.voice_url == f"{BASE}/api/messaging/twilio_voice/webhook/{token}/incoming"
    assert twilio.writes[0]["StatusCallback"] == f"{BASE}/api/messaging/twilio_voice/webhook/{token}/status"
    assert stored["voice"]["previous"]["voice_url"] == "https://old.example/voice"
    assert stored["voice"]["number_sid"] == "PN0"

    # On again: the stored "before" is still the original, never ourselves; signing is not made twice.
    await client.post(f"/api/messaging/channels/{ch['id']}/enable")
    assert len(twilio.signing) == 3
    assert (await channel_store.get_channel(ch["id"]))["voice"]["previous"]["voice_url"] == "https://old.example/voice"

    # A Standard key: every webhook under the number's URL is signed (V6, VOX1a P3), once.
    assert twilio.signing == ["SharedKeys", "Settings", "Rules"]
    assert stored["voice"]["signing"] == {"shared_key_id": "webhooks_sharedkey_1", "setting_id": "webhooks_setting_1",
                                          "rule_id": "webhooks_rule_1"}
    assert stored["credentials"]["signing_secret"] == "sign-me"
    assert "signing_secret" not in on.json()["credentials"] or on.json()["credentials"]["signing_secret"] == ""

    off = await client.post(f"/api/messaging/channels/{ch['id']}/disable")
    assert off.status_code == 200
    assert twilio.voice_url == "https://old.example/voice"
    assert twilio.unsigned == ["Rules", "Settings", "SharedKeys"]
    stored = await channel_store.get_channel(ch["id"])
    assert stored["enabled"] is False and "previous" not in stored["voice"] and "signing" not in stored["voice"]
    assert "signing_secret" not in stored["credentials"]
    assert stored["credentials"]["route_token"] == token, "the token outlives a disable"


async def test_deleting_it_puts_the_number_back(client, manager, twilio):
    ch = await _number(client)
    await client.post(f"/api/messaging/channels/{ch['id']}/enable")
    r = await client.delete(f"/api/messaging/channels/{ch['id']}")
    assert r.status_code == 200 and twilio.voice_url == "https://old.example/voice"


@pytest.mark.parametrize("case, error", [
    ("no_https", "public URL"),
    ("key", "Twilio refused the API key for read incoming phone numbers"),
    ("not_on_account", "not on this Twilio account"),
])
async def test_what_stops_it_is_the_channels_error(client, manager, twilio, monkeypatch, case, error):
    from core.messaging.voice import hooks

    if case == "no_https":
        monkeypatch.setattr(hooks.current(), "public_base", lambda: "http://localhost:8000")
    elif case == "key":
        twilio.refuse_read = True
    else:
        twilio.numbers = ["+15559999999"]
    ch = await _number(client)
    r = await client.post(f"/api/messaging/channels/{ch['id']}/enable")
    assert r.json()["status"] == "error" and error in r.json()["last_error"]
    assert "sekret-value" not in r.text


async def test_a_key_that_cannot_change_numbers_connects_with_the_url_to_paste(client, manager, twilio):
    """V7: degrade, don't fail."""
    from core.messaging import store as channel_store

    twilio.refuse_write = True
    ch = await _number(client)
    r = await client.post(f"/api/messaging/channels/{ch['id']}/enable")
    assert r.json()["status"] == "running"
    voice = (await channel_store.get_channel(ch["id"]))["voice"]
    assert voice["manual_url"].startswith(f"{BASE}/api/messaging/twilio_voice/webhook/")
    assert "change incoming phone numbers" in voice["point_error"]
    assert twilio.voice_url == "https://old.example/voice"


async def test_the_numbers_a_key_can_see(client, twilio):
    r = await client.post("/api/messaging/twilio_voice/numbers",
                          json={"account_sid": ACCOUNT, "api_key_sid": "SKx", "api_key_secret": "s"})
    assert r.status_code == 200
    assert r.json()["numbers"] == [{"sid": "PN0", "number": NUMBER, "voice_url": "https://old.example/voice",
                                    "voice_application_sid": ""}]
    twilio.refuse_read = True
    bad = await client.post("/api/messaging/twilio_voice/numbers",
                            json={"account_sid": ACCOUNT, "api_key_sid": "SKx", "api_key_secret": "s"})
    assert bad.status_code == 400 and "refused the API key" in bad.json()["detail"]


async def test_recent_calls_newest_first(client):
    from core.messaging.voice import hooks
    from core.messaging.voice.call import new_record

    host = hooks.current()
    for n, at in (("1", "2026-10-10T10:00:00+00:00"), ("2", "2026-10-10T11:00:00+00:00")):
        rec = new_record(call_sid="CA" + n * 32, channel_id="chn_v", agent_id="a", session_id="s", caller="+1",
                         called="+2")
        rec["started_at"] = at
        await host.save_call(rec)
    await host.update_call("CA" + "1" * 32, {"segments": [{"seconds": 30}, {"seconds": 12}]})
    r = await client.get("/api/messaging/twilio_voice/channels/chn_v/calls")
    calls = r.json()["calls"]
    assert [c["id"][2] for c in calls] == ["2", "1"] and calls[1]["seconds"] == 42


def test_fast_for_voice():
    from core.providers.voice_speed import fast_for_voice
    assert fast_for_voice("groq.openai/gpt-oss-20b") and fast_for_voice("synapse.nova-micro")
    assert not fast_for_voice("synapse.nova-pro") and not fast_for_voice("anthropic.claude-haiku-4-5")
    assert not fast_for_voice("") and not fast_for_voice(None)


async def test_a_restricted_key_connects_unsigned_and_says_so(client, manager, twilio):
    from core.messaging import store as channel_store

    twilio.restricted = True
    ch = await _number(client)
    r = await client.post(f"/api/messaging/channels/{ch['id']}/enable")
    assert r.json()["status"] == "running"
    stored = await channel_store.get_channel(ch["id"])
    assert "signing" not in stored["voice"] and "signing_secret" not in stored["credentials"]
    assert "Standard API key turns signing on" in stored["voice"]["signing_error"]
