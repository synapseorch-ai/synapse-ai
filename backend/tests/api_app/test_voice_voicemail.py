"""A voicemail is played from the call's log (F480, VOX1e, D263; VOX1f, D273).

The log showed "The caller left a voicemail (0:18). It is in your Twilio account RE1a6…" and nothing could play it
(Naveen: "either show the voicemail here or get the redirect url here"; he chose playback). The app's own route
reads the recording from Twilio with the number's key and passes it on, never stored (V26).
"""
import httpx
import pytest

ACCOUNT = "AC" + "1" * 32
CALL = "CA" + "4" * 32
REC = "RE" + "b" * 32


@pytest.fixture
async def number(monkeypatch):
    from core.messaging import store as channel_store
    from core.messaging.voice import call as call_mod, connect, hooks
    from core.messaging.voice.twilio import TwilioProvider

    state = {"code": 200, "asked": []}

    def handler(req: httpx.Request):
        state["asked"].append((req.url.path, req.headers.get("authorization", "")))
        if state["code"] != 200:
            return httpx.Response(state["code"], json={"message": "no"})
        return httpx.Response(200, content=b"ID3 a voicemail", headers={"content-type": "audio/mpeg"})
    monkeypatch.setattr(connect, "provider_for", lambda creds: TwilioProvider.from_credentials(
        creds, http=httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    hooks.install(None)
    ch = await channel_store.save_channel({
        "name": "Front desk", "platform": "twilio_voice",
        "credentials": {"account_sid": ACCOUNT, "api_key_sid": "SKx", "api_key_secret": "s", "route_token": "r" * 32},
        "voice": {"number": "+15551230000"}})
    record = call_mod.new_record(call_sid=CALL, channel_id=ch["id"], agent_id="a1", session_id=f"voice_{ch['id']}_{CALL}",
                                 caller="+14155550100", called="+15551230000")
    record["voicemail"] = {"recording_sid": REC, "url": f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT}/Recordings/{REC}",
                           "seconds": 18}
    await hooks.current().save_call(record)
    yield state
    hooks.install(None)


async def test_it_is_played_from_twilio_with_the_numbers_key(client, number):
    r = await client.get(f"/api/messaging/twilio_voice/calls/{CALL}/voicemail")
    assert r.status_code == 200, r.text
    assert r.content == b"ID3 a voicemail" and r.headers["content-type"] == "audio/mpeg"
    assert "no-store" in r.headers["cache-control"], "never kept, not even by the browser (V26)"
    [(path, auth)] = number["asked"]
    assert path.endswith(f"/Recordings/{REC}.mp3") and auth.startswith("Basic ")


async def test_a_key_that_may_not_read_recordings_says_which_permission_to_add(client, number):
    number["code"] = 403
    r = await client.get(f"/api/messaging/twilio_voice/calls/{CALL}/voicemail")
    assert r.status_code == 409
    assert "/twilio/voice/recordings/read" in r.json()["detail"]


async def test_a_call_with_no_voicemail_has_none_to_play(client, number):
    r = await client.get(f"/api/messaging/twilio_voice/calls/{'CA' + '5' * 32}/voicemail")
    assert r.status_code == 404
    assert number["asked"] == []
