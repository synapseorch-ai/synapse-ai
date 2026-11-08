"""
What the Phone tab offers (VOX1c): the languages and voices a number may use
(VOX1d's list), the models fast enough for a call (V15's quiet note), and
whether Twilio can reach this server at all.
"""


async def test_what_the_phone_tab_offers(client, monkeypatch):
    from core.messaging.voice import hooks
    from core.providers.voice_speed import FAST_FOR_VOICE

    hooks.install(None)
    monkeypatch.setattr(hooks.current(), "public_base", lambda: "https://calls.example.com")
    try:
        r = await client.get("/api/messaging/twilio_voice/options")
        body = r.json()
        assert r.status_code == 200 and body["available"] is True and body["reason"] == ""
        assert body["fast_for_voice"] == list(FAST_FOR_VOICE)
        assert any(lang["code"] == "en-IN" and lang["name"] for lang in body["languages"])

        monkeypatch.setattr(hooks.current(), "public_base", lambda: "http://localhost:8000")
        body = (await client.get("/api/messaging/twilio_voice/options")).json()
        assert body["available"] is False and "backend directly" in body["reason"]
    finally:
        hooks.install(None)
