"""
A phone call's vendor side (VOX1a): ConversationRelay's frames, the tokens that
verify a call without the auth token (V6), the TwiML we answer with (golden
strings), and the Twilio REST checks against a mocked Twilio.
"""
import json

import httpx
import pytest

from core.messaging.markdown import to_speech
from core.messaging.voice import mask_number, relay, tokens
from core.messaging.voice.hooks import VoiceHost
from core.messaging.voice.provider import AnswerOptions, CallRefused
from core.messaging.voice.service import notice_for
from core.messaging.voice.twilio import TwilioProvider

ACCOUNT = "AC" + "1" * 32
NUMBER = "+15551230000"
CALL = "CA" + "2" * 32


class TestFrames:
    def test_setup(self):
        f = relay.parse(json.dumps({"type": "setup", "callSid": CALL, "from": "+14155550100", "to": NUMBER,
                                    "customParameters": {"resume": "1"}}))
        assert isinstance(f, relay.Setup)
        assert (f.call_sid, f.caller, f.called, f.params) == (CALL, "+14155550100", NUMBER, {"resume": "1"})

    @pytest.mark.parametrize("last, want", [(True, True), (False, False), ("true", True), ("false", False),
                                            (None, True)])
    def test_prompt_last_as_bool_or_string(self, last, want):
        raw = {"type": "prompt", "voicePrompt": "hello", "lang": "en-US"}
        if last is not None:
            raw["last"] = last
        f = relay.parse(json.dumps(raw))
        assert isinstance(f, relay.Prompt) and f.text == "hello" and f.last is want

    def test_interrupt(self):
        f = relay.parse(json.dumps({"type": "interrupt", "utteranceUntilInterrupt": "It is sunny",
                                    "durationUntilInterruptMs": "1200"}))
        assert isinstance(f, relay.Interrupt) and f.heard == "It is sunny" and f.duration_ms == 1200

    @pytest.mark.parametrize("raw", ["not json", "[1,2]", json.dumps({"no": "type"}), b"\xff"])
    def test_anything_unreadable_is_unknown_never_an_exception(self, raw):
        assert relay.parse(raw).type == "unknown"

    def test_only_text_and_end_go_out(self):
        assert json.loads(relay.text_frame("Hi ", last=False)) == {"type": "text", "token": "Hi ", "last": False}
        assert json.loads(relay.end_frame({"transfer": "billing"})) == {
            "type": "end", "handoffData": '{"transfer":"billing"}'}
        assert json.loads(relay.end_frame()) == {"type": "end"}


class TestTokens:
    def test_route_token(self):
        t = tokens.new_route_token()
        assert len(t) >= 30 and tokens.route_token_matches(t, t)
        assert not tokens.route_token_matches(t, t[:-1] + ("y" if t.endswith("x") else "x"))
        assert not tokens.route_token_matches("", "") and not tokens.route_token_matches(t, None)

    def test_a_socket_token_names_its_call_and_channel(self):
        key = b"k" * 32
        t = tokens.socket_token(key, channel_id="chn_1", call_sid=CALL, resume=True)
        claims = tokens.read_socket_token(key, t, channel_id="chn_1")
        assert claims["call_sid"] == CALL and claims["resume"] is True and claims["nonce"]

    @pytest.mark.parametrize("why", ["channel", "key", "tampered", "expired", "garbage"])
    def test_a_bad_socket_token_is_refused(self, why):
        key = b"k" * 32
        t = tokens.socket_token(key, channel_id="chn_1", call_sid=CALL, now=1000.0)
        kw = {"channel_id": "chn_1", "now": 1001.0}
        if why == "channel":
            kw["channel_id"] = "chn_2"
        elif why == "key":
            key = b"j" * 32
        elif why == "tampered":
            payload, sig = t.split(".")
            t = payload[:-2] + ("AA" if payload[-2:] != "AA" else "BB") + "." + sig
        elif why == "expired":
            kw["now"] = 1000.0 + tokens.SOCKET_TOKEN_TTL + 1
        else:
            t = "nonsense"
        with pytest.raises(tokens.BadSocketToken):
            tokens.read_socket_token(key, t, **kw)

    async def test_a_socket_token_is_single_use(self):
        host = VoiceHost()
        assert await host.claim_socket("n1") is True
        assert await host.claim_socket("n1") is False
        assert await host.claim_socket("") is False


class TestTwiML:
    def _p(self, http=None):
        return TwilioProvider(account_sid=ACCOUNT, api_key_sid="SKx", api_key_secret="s", http=http)

    def test_answer(self):
        xml = self._p().answer(ws_url="wss://h/ws?t=a&b", action_url="https://h/end", opts=AnswerOptions(
            notice="Hi, you're speaking with Ada, an AI assistant.", language="en-US"))
        assert xml == (
            '<?xml version="1.0" encoding="UTF-8"?><Response><Connect action="https://h/end">'
            '<ConversationRelay url="wss://h/ws?t=a&amp;b" welcomeGreeting="Hi, you\'re speaking with Ada, '
            'an AI assistant." welcomeGreetingInterruptible="none" interruptible="any" ignoreBackchannel="true" '
            'dtmfDetection="false" language="en-US"></ConversationRelay></Connect></Response>')

    def test_a_moved_call_has_no_greeting(self):
        xml = self._p().answer(ws_url="wss://h/ws?t=a", action_url="https://h/end", opts=AnswerOptions(
            notice="Hi", language="en-GB", voice="v1", tts_provider="Google", resume=True))
        assert xml == (
            '<?xml version="1.0" encoding="UTF-8"?><Response><Connect action="https://h/end">'
            '<ConversationRelay url="wss://h/ws?t=a" interruptible="any" ignoreBackchannel="true" '
            'dtmfDetection="false" language="en-GB" ttsProvider="Google" voice="v1">'
            '<Parameter name="resume" value="1"/></ConversationRelay></Connect></Response>')

    def test_who_hears_the_caller_is_written_out(self):
        """VOX1d: Twilio's own default depends on the account's age, so the pair is always ours."""
        xml = self._p().answer(ws_url="wss://h/ws?t=a", action_url="https://h/end", opts=AnswerOptions(
            notice="Hi", language="en-IN", transcription_provider="Deepgram", speech_model="nova-3-general"))
        assert xml == (
            '<?xml version="1.0" encoding="UTF-8"?><Response><Connect action="https://h/end">'
            '<ConversationRelay url="wss://h/ws?t=a" welcomeGreeting="Hi" welcomeGreetingInterruptible="none" '
            'interruptible="any" ignoreBackchannel="true" dtmfDetection="false" language="en-IN" '
            'transcriptionProvider="Deepgram" speechModel="nova-3-general"></ConversationRelay></Connect></Response>')

    def test_a_moved_call_hears_the_same_way(self):
        xml = self._p().answer(ws_url="wss://h/ws?t=a", action_url="https://h/end", opts=AnswerOptions(
            notice="Hi", language="en-IN", tts_provider="Google", transcription_provider="Google", speech_model="long",
            resume=True))
        assert xml == (
            '<?xml version="1.0" encoding="UTF-8"?><Response><Connect action="https://h/end">'
            '<ConversationRelay url="wss://h/ws?t=a" interruptible="any" ignoreBackchannel="true" '
            'dtmfDetection="false" language="en-IN" ttsProvider="Google" transcriptionProvider="Google" '
            'speechModel="long"><Parameter name="resume" value="1"/></ConversationRelay></Connect></Response>')

    def test_transfer(self):
        assert self._p().transfer_markup(number="+15550001111", caller_id=NUMBER, action_url="https://h/dialed") == (
            '<?xml version="1.0" encoding="UTF-8"?><Response><Dial callerId="+15551230000" timeout="20" '
            'action="https://h/dialed">+15550001111</Dial></Response>')

    def test_voicemail(self):
        assert self._p().voicemail_markup(action_url="https://h/recorded", language="en-US") == (
            '<?xml version="1.0" encoding="UTF-8"?><Response><Say language="en-US">Nobody could take your call. '
            'Please leave a message after the tone.</Say><Record maxLength="120" playBeep="true" '
            'action="https://h/recorded"/></Response>')

    def test_say_and_hang_up_escapes(self):
        assert self._p().say_and_hang_up("Q&A <closed>") == (
            '<?xml version="1.0" encoding="UTF-8"?><Response><Say language="en-US">Q&amp;A &lt;closed&gt;</Say>'
            '<Hangup/></Response>')
        assert self._p().hang_up_markup() == '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'

    def test_the_end_callback(self):
        end = self._p().parse_end({"CallSid": CALL, "SessionStatus": "ended", "SessionDuration": "75",
                                   "CallStatus": "in-progress", "HandoffData": '{"transfer":"billing"}'})
        assert (end.status, end.duration, end.handoff, end.caller_hung_up) == ("ended", 75, {"transfer": "billing"},
                                                                               False)
        assert self._p().parse_end({"HandoffData": "[1]", "CallStatus": "completed"}).handoff == {}
        assert self._p().parse_end({"CallStatus": "completed"}).caller_hung_up


class TestTheNotice:
    def test_default_names_the_agent(self):
        assert notice_for({}, agent_name="Ada") == "Hi, you're speaking with Ada, an AI assistant."

    def test_it_cannot_be_emptied(self):
        assert notice_for({"notice": "   "}, agent_name="Ada").startswith("Hi, you're speaking with Ada")

    def test_recording_is_always_said(self):
        assert notice_for({"notice": "Hello from {agent}.", "recording": True}, agent_name="Ada") == (
            "Hello from Ada. This call may be recorded.")
        assert notice_for({"notice": "Calls are recorded.", "recording": True}, agent_name="A") == "Calls are recorded."


class TestSpeech:
    @pytest.mark.parametrize("md, said", [
        ("**Yes**, see [the docs](https://x.y).", "Yes, see the docs."),
        ("## Hours\n- Mon to Fri", "Hours Mon to Fri"),
        ("Call `support` now 📞", "Call support now "),
        ("Go to https://example.com today", "Go to a link today"),
        ("file_name stays", "file_name stays"),
        ("Here's your checkout link: shop.example/checkout/8de333c5. Anything else?",
         "Here's your checkout link: a link Anything else?"),
        ("Visit www.harbor.example/returns today", "Visit a link today"),
        ("We are harbor.example on the web.", "We are harbor.example on the web."),
        ("It costs 3.50/month", "It costs 3.50/month"),
    ])
    def test_markdown_becomes_words(self, md, said):
        assert to_speech(md) == said

    def test_a_number_is_masked_for_logs(self):
        assert mask_number("+14155550123") == "+1••••••0123"
        assert mask_number("123") == "123"


def _twilio(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestVerifyingACall:
    """No REST lookup: `GET /Calls/{sid}` was a 404 for every real call (VOX1a P2). The webhook is read as is;
    the route token and, when the number has one, the SharedKey signature are what admit it."""

    def _form(self, **kw):
        form = {"CallSid": CALL, "AccountSid": ACCOUNT, "To": NUMBER, "From": "+14155550100"}
        form.update(kw)
        return form

    def _p(self, seen):
        def handler(req):
            seen.append(req.url.path)
            return httpx.Response(500)
        return TwilioProvider(account_sid=ACCOUNT, api_key_sid="SKx", api_key_secret="s", http=_twilio(handler))

    async def test_a_call_to_this_number_on_this_account(self):
        seen = []
        info = await self._p(seen).verify_incoming(self._form(), number=NUMBER)
        assert (info.call_sid, info.caller, info.called) == (CALL, "+14155550100", NUMBER)
        assert seen == [], "Twilio is not asked"

    async def test_a_browser_call_through_a_twiml_app_has_no_to(self):
        info = await self._p([]).verify_incoming(self._form(To="", From="client:front-desk"), number=NUMBER)
        assert info.caller == "client:front-desk" and info.called == NUMBER

    @pytest.mark.parametrize("change", [{"To": "+15559999999"}, {"AccountSid": "AC" + "9" * 32}, {"CallSid": ""},
                                        {"To": "", "From": "+14155550100"}])
    async def test_refusals(self, change):
        with pytest.raises(CallRefused):
            await self._p([]).verify_incoming(self._form(**change), number=NUMBER)


class TestSignedWebhooks:
    """SharedKeys sign every voice webhook with HMAC-SHA256 (measured on a real call, VOX1a P3)."""

    def test_a_signature_made_with_the_key_verifies(self):
        from core.messaging.voice.twilio import signature, verify_signature
        url, params = "https://h/api/messaging/twilio_voice/webhook/r/incoming", {"CallSid": CALL, "To": NUMBER}
        sig = signature("sekret", url, params)
        headers = {"x-twilio-signature": sig, "x-twilio-webhook-signature-settings": "webhooks_sharedkey_1;HMAC_SHA256"}
        assert verify_signature("sekret", url, params, headers)
        assert not verify_signature("other", url, params, headers)
        assert not verify_signature("sekret", url, {**params, "To": "+1"}, headers)
        assert not verify_signature("sekret", url + "x", params, headers)
        assert not verify_signature("sekret", url, params, {"x-twilio-signature": sig}), "the auth token's is not ours"

    async def test_start_makes_a_key_a_setting_and_a_rule_for_the_numbers_urls(self):
        calls = []

        def handler(req):
            body = json.loads(req.content or b"{}")
            calls.append((req.method, req.url.path, body))
            if req.url.path.endswith("/SharedKeys"):
                return httpx.Response(201, json={"id": "webhooks_sharedkey_1", "secret": "s3"})
            if req.url.path.endswith("/Settings"):
                return httpx.Response(201, json={"id": "webhooks_setting_1"})
            return httpx.Response(202, json={"operation": {"resourceId": "webhooks_rule_1"}, "rule": {"id": "webhooks_rule_1"}})
        p = TwilioProvider(account_sid=ACCOUNT, api_key_sid="SKx", api_key_secret="s", http=_twilio(handler))
        made = await p.start_signing("https://h/api/messaging/twilio_voice/webhook/r/", name="synapse chn_1")
        assert made == {"shared_key_id": "webhooks_sharedkey_1", "secret": "s3", "setting_id": "webhooks_setting_1",
                        "rule_id": "webhooks_rule_1"}
        assert calls[1][2]["signature"] == {"type": "SHARED_KEY", "sharedKeyId": "webhooks_sharedkey_1",
                                            "algorithm": "HMAC_SHA256"}
        assert calls[2][2]["matchType"] == "PREFIX_MATCH" and calls[2][2]["prefix"].endswith("/webhook/r/")

    async def test_a_restricted_key_is_refused_and_nothing_is_left_behind(self):
        deleted = []

        def handler(req):
            if req.method == "DELETE":
                deleted.append(req.url.path)
                return httpx.Response(204)
            if req.url.path.endswith("/SharedKeys"):
                return httpx.Response(201, json={"id": "k1", "secret": "s"})
            return httpx.Response(401, json={"message": "Authenticate"})
        p = TwilioProvider(account_sid=ACCOUNT, api_key_sid="SKx", api_key_secret="s", http=_twilio(handler))
        with pytest.raises(CallRefused) as e:
            await p.start_signing("https://h/x/", name="n")
        assert e.value.permission == "create webhook settings"
        assert deleted == ["/v1/Webhooks/SharedKeys/k1"]

    async def test_stop_removes_the_rule_before_the_key(self):
        deleted = []

        def handler(req):
            deleted.append(req.url.path.rsplit("/", 2)[-2])
            return httpx.Response(204)
        p = TwilioProvider(account_sid=ACCOUNT, api_key_sid="SKx", api_key_secret="s", http=_twilio(handler))
        await p.stop_signing({"shared_key_id": "k", "setting_id": "s", "rule_id": "r"})
        assert deleted == ["Rules", "Settings", "SharedKeys"]


class TestPointingANumber:
    async def test_point_keeps_what_was_there_and_restore_puts_it_back(self):
        posts = []
        before = {"phone_number": NUMBER, "voice_url": "https://old.example/voice", "voice_method": "POST",
                  "status_callback": "", "voice_application_sid": "", "voice_fallback_url": ""}

        def handler(req):
            if req.method == "GET":
                return httpx.Response(200, json=before)
            posts.append(dict(httpx.QueryParams(req.content.decode())))
            return httpx.Response(200, json={"voice_url": posts[-1].get("VoiceUrl")})
        p = TwilioProvider(account_sid=ACCOUNT, api_key_sid="SKx", api_key_secret="s", http=_twilio(handler))
        previous = await p.point_number("PN1", voice_url="https://us/incoming", status_url="https://us/status")
        assert previous["voice_url"] == "https://old.example/voice"
        assert posts[0]["VoiceUrl"] == "https://us/incoming" and posts[0]["StatusCallback"] == "https://us/status"
        await p.restore_number("PN1", previous)
        assert posts[1]["VoiceUrl"] == "https://old.example/voice"

    async def test_a_key_that_cannot_write_numbers_is_named(self):
        def handler(req):
            if req.method == "GET":
                return httpx.Response(200, json={"voice_url": ""})
            return httpx.Response(401, json={"message": "Authenticate"})
        p = TwilioProvider(account_sid=ACCOUNT, api_key_sid="SKx", api_key_secret="s", http=_twilio(handler))
        with pytest.raises(CallRefused) as e:
            await p.point_number("PN1", voice_url="https://us/i", status_url="https://us/s")
        assert e.value.permission == "change incoming phone numbers"
