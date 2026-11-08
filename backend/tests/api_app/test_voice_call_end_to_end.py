"""
A whole phone call through the real app (VOX1a): Twilio's webhooks over HTTP,
ConversationRelay as a fake client on a real WebSocket, the real engine with a
scripted model, and Twilio's REST API mocked.

ring → verify (V6) → TwiML → socket (one-time token) → turns streamed in
phrases → end_call → the end callback; a transfer nobody answers → voicemail
→ the bell; a move (V10) resumes the same conversation; a caller's confirm
tool parks in the bell (V16) and the answer keeps the call's transcript.
"""
import asyncio
import json
import re
import types

import httpx
import pytest
import websockets

from _fakes import seed as S
from _fakes.fake_llm import Streamed, tool_call
from _fakes.live_server import serve

ACCOUNT = "AC" + "1" * 32
NUMBER = "+15551230000"
CALLER = "+14155550100"
ROUTE = "r" * 32


def _call_sid(n=1):
    return "CA" + str(n) * 32


@pytest.fixture
def twilio(monkeypatch):
    """Twilio's REST API: every call the test names is real and ringing; anything else is a 404."""
    state = types.SimpleNamespace(calls={}, requests=[])

    def handler(req: httpx.Request):
        state.requests.append((req.method, req.url.path))
        m = re.search(r"/Calls/(CA\w+)\.json$", req.url.path)
        if m and req.method == "GET":
            sid = m.group(1)
            if sid in state.calls:
                return httpx.Response(200, json={"sid": sid, "to": NUMBER, "from": CALLER,
                                                 "status": state.calls[sid]})
            return httpx.Response(404, json={"code": 20404, "message": "The requested resource was not found"})
        return httpx.Response(404, json={})

    from core.messaging.voice import connect, service
    from core.messaging.voice.twilio import TwilioProvider

    def provider_for(creds):
        return TwilioProvider.from_credentials(creds, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(connect, "provider_for", provider_for)
    monkeypatch.setattr(service, "provider_for", provider_for)
    return state


@pytest.fixture
async def phone(test_app, twilio, monkeypatch):
    """A running voice channel answered by a seeded agent, served for real (HTTP + WebSocket)."""
    from core.messaging import store as channel_store
    from core.messaging.voice import hooks

    agent = S.make_agent(name="Ada", tools=["all"], skip_default_tools=True, planning=False)
    await S.seed_agents([agent])
    ch = await channel_store.save_channel({
        "name": "Front desk", "platform": "twilio_voice",
        "credentials": {"account_sid": ACCOUNT, "api_key_sid": "SKx", "api_key_secret": "s", "route_token": ROUTE},
        "voice": {"number": NUMBER, "number_sid": "PN1", "transfer_number": "+15550001111"},
        "visitor_tools": []})
    await channel_store.assign_channel(ch["id"], agent["id"])
    await channel_store.update_channel_status(ch["id"], "running")
    hooks.install(None)
    async with serve(test_app, ws="websockets") as live:
        monkeypatch.setattr(hooks.current(), "public_base", lambda: live.url)
        yield types.SimpleNamespace(live=live, channel=ch, agent=agent, base=f"/api/messaging/twilio_voice/webhook/{ROUTE}")
    hooks.install(None)


async def _post(phone, path, form):
    async with phone.live.client() as c:
        return await c.post(phone.base + path, data=form)


async def _ring(phone, twilio, sid):
    twilio.calls[sid] = "ringing"
    r = await _post(phone, "/incoming", {"CallSid": sid, "AccountSid": ACCOUNT, "To": NUMBER, "From": CALLER})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    return r.text


def _socket_url(phone, twiml):
    m = re.search(r'url="wss://[^"]*?(/api/messaging/twilio_voice/webhook/[^"]+)"', twiml)
    assert m, twiml
    return "ws://" + phone.live.url.split("://", 1)[1] + m.group(1).replace("&amp;", "&")


class Relay:
    """ConversationRelay's side of the socket."""

    def __init__(self, ws):
        self.ws = ws

    async def send(self, **frame):
        await self.ws.send(json.dumps(frame))

    async def say(self, text):
        await self.send(type="prompt", voicePrompt=text, lang="en-US", last=True)

    async def hear(self, timeout=10.0):
        """Frames until our talk cycle closes (a text with last) or the session ends."""
        out = []
        while True:
            frame = json.loads(await asyncio.wait_for(self.ws.recv(), timeout))
            out.append(frame)
            if frame["type"] == "end" or (frame["type"] == "text" and frame.get("last")):
                return out


def _words(frames):
    return "".join(f.get("token", "") for f in frames if f["type"] == "text")


async def test_a_whole_call(phone, twilio, fake_llm):
    from core.messaging.voice import hooks
    from core.store import sessions as _sessions

    sid = _call_sid(1)
    twiml = await _ring(phone, twilio, sid)
    assert 'welcomeGreeting="Hi, you&apos;re speaking with Ada, an AI assistant. How can I help?"' in twiml or \
        "welcomeGreeting=\"Hi, you're speaking with Ada, an AI assistant. How can I help?\"" in twiml
    assert 'welcomeGreetingInterruptible="none"' in twiml and f'action="{phone.live.url}{phone.base}/end"' in twiml
    assert twilio.requests == [], "no REST lookup: it 404s for real calls (VOX1a P2)"
    url = _socket_url(phone, twiml)

    fake_llm.script([Streamed(["It is sunny ", "and 21 degrees, ", "in Paris today."]),
                     tool_call("end_call", goodbye="Goodbye!"), tool_call("end_call", goodbye="Goodbye!")])
    async with websockets.connect(url) as ws:
        relay = Relay(ws)
        await relay.send(type="setup", callSid=sid, **{"from": CALLER, "to": NUMBER})
        await relay.say("What's the weather?")
        first = await relay.hear()
        assert _words(first) == "It is sunny and 21 degrees, in Paris today."
        assert len([f for f in first if f["type"] == "text"]) >= 2, "it went out in phrases"
        await relay.say("Thanks, bye")
        closing = await relay.hear()
        assert _words(closing) == "Is there anything else I can help you with?", "the call asks first (VOX1d)"
        await relay.say("No, that's all")
        second = await relay.hear()
        while second[-1]["type"] != "end":
            second += await relay.hear()
        assert _words(second) == "Goodbye!", "no filler before a goodbye"
        assert second[-1] == {"type": "end", "handoffData": '{"ended":"agent"}'}

    # The prompt the model got is the agent's own plus the voice suffix; the call offers end_call.
    system = fake_llm.calls[0].get("system_prompt") or json.dumps(fake_llm.calls[0], default=str)
    assert "phone call" in system
    r = await _post(phone, "/end", {"CallSid": sid, "SessionStatus": "ended", "SessionDuration": "42",
                                    "CallStatus": "in-progress", "HandoffData": '{"ended":"agent"}'})
    assert "<Hangup/>" in r.text

    record = await hooks.current().get_call(sid)
    assert record["outcome"] == "agent_ended" and record["seconds"] == 42 and record["caller"] == CALLER
    row = await _sessions.get(record["session_id"], phone.agent["id"])
    said = [m["content"] for m in row["messages"]]
    assert said[0] == "What's the weather?" and said[1] == "It is sunny and 21 degrees, in Paris today."

    # The socket token was single use.
    with pytest.raises(websockets.InvalidStatus):
        async with websockets.connect(url):
            pass


async def test_refusals(phone, twilio):
    from core.messaging import store as channel_store

    async with phone.live.client() as c:
        r = await c.post(f"/api/messaging/twilio_voice/webhook/{'x' * 32}/incoming", data={"CallSid": _call_sid(2)})
    assert r.status_code == 404, "a route token names no channel"

    elsewhere = await _post(phone, "/incoming", {"CallSid": _call_sid(3), "AccountSid": ACCOUNT, "To": "+15559999999"})
    assert "This line isn't available right now" in elsewhere.text and "<Hangup/>" in elsewhere.text
    assert "ConversationRelay" not in elsewhere.text

    forged_token = _socket_url(phone, await _ring(phone, twilio, _call_sid(4))).replace("t=", "t=x")
    with pytest.raises(websockets.InvalidStatus):  # refused before the upgrade
        async with websockets.connect(forged_token):
            pass

    await channel_store.unassign_channel(phone.channel["id"], phone.agent["id"])
    nobody = await _ring(phone, twilio, _call_sid(5))
    assert "no one to answer it yet" in nobody and "ConversationRelay" not in nobody


async def test_a_transfer_nobody_takes_becomes_a_voicemail_in_the_bell(phone, twilio, fake_llm):
    from core.messaging.voice import hooks
    from core.notifications import hub

    sid = _call_sid(6)
    url = _socket_url(phone, await _ring(phone, twilio, sid))
    fake_llm.script([tool_call("transfer_call", reason="billing question")])
    async with websockets.connect(url) as ws:
        relay = Relay(ws)
        await relay.send(type="setup", callSid=sid)
        await relay.say("I need a person")
        frames = await relay.hear()
        while frames[-1]["type"] != "end":
            frames += await relay.hear()
        assert frames[-1] == {"type": "end", "handoffData": '{"transfer":"billing question"}'}

    dial = await _post(phone, "/end", {"CallSid": sid, "SessionStatus": "ended", "SessionDuration": "20",
                                       "CallStatus": "in-progress", "HandoffData": '{"transfer":"billing question"}'})
    assert f'<Dial callerId="{NUMBER}" timeout="20" action="{phone.live.url}{phone.base}/dialed">+15550001111</Dial>' \
        in dial.text
    vm = await _post(phone, "/dialed", {"CallSid": sid, "DialCallStatus": "no-answer"})
    assert "<Record " in vm.text and f'action="{phone.live.url}{phone.base}/recorded"' in vm.text
    bye = await _post(phone, "/recorded", {"CallSid": sid, "RecordingSid": "RE1", "RecordingUrl": "https://api/RE1",
                                           "RecordingDuration": "14"})
    assert "Thank you. Goodbye." in bye.text

    record = await hooks.current().get_call(sid)
    assert record["outcome"] == "voicemail" and record["voicemail"]["recording_sid"] == "RE1"
    assert record["transfer"] == {"reason": "billing question", "status": "no-answer"}
    items = hub.list()
    assert any(i["title"] == f"Voicemail from {CALLER}" and i["kind"] == "human_input" for i in items)
    assert any(i["title"].startswith("A caller is being put through") for i in items)


async def test_a_moved_call_resumes_the_same_conversation_without_a_greeting(phone, twilio, fake_llm):
    from core.messaging.voice import call as call_mod

    sid = _call_sid(7)
    url = _socket_url(phone, await _ring(phone, twilio, sid))
    fake_llm.script([Streamed(["Your order ships Monday."]), Streamed(["Yes, Monday the 12th."])])
    async with websockets.connect(url) as ws:
        relay = Relay(ws)
        await relay.send(type="setup", callSid=sid)
        await relay.say("When does my order ship?")
        await relay.hear()
        await asyncio.sleep(0.1)
        assert call_mod.move_all() == 1
        frames = await relay.hear()
        while frames[-1]["type"] != "end":
            frames += await relay.hear()
        assert frames[-1] == {"type": "end", "handoffData": '{"reconnect":true}'}

    twiml = (await _post(phone, "/end", {"CallSid": sid, "SessionStatus": "ended", "SessionDuration": "15",
                                         "CallStatus": "in-progress", "HandoffData": '{"reconnect":true}'})).text
    assert "welcomeGreeting" not in twiml and '<Parameter name="resume" value="1"/>' in twiml
    async with websockets.connect(_socket_url(phone, twiml)) as ws:
        relay = Relay(ws)
        await relay.send(type="setup", callSid=sid, customParameters={"resume": "1"})
        await relay.say("Monday?")
        assert _words(await relay.hear()) == "Yes, Monday the 12th."
    # The second socket's turn read the first one's: the conversation carried over.
    history = json.dumps(fake_llm.calls[-1], default=str)
    assert "Your order ships Monday." in history


async def test_a_callers_confirm_tool_parks_in_the_bell_and_the_transcript_is_kept(phone, twilio, fake_llm,
                                                                                    monkeypatch, client):
    import core.react_engine as re_mod
    from mcp.types import ToolAnnotations

    from core.messaging import store as channel_store
    from core.notifications import hub
    from core.store import sessions as _sessions
    from core.tools import VirtualTool

    refund = VirtualTool("refund", "refunds an order", {"type": "object"},
                         ToolAnnotations(readOnlyHint=False, destructiveHint=True))

    async def aggregated(server_module, agent, custom_tools, **_):
        ollama = [{"type": "function", "function": {"name": "refund", "description": "x", "parameters": {}}}]
        return [refund], {"refund": {}}, ollama, "['refund']"
    monkeypatch.setattr(re_mod, "aggregate_all_tools", aggregated)
    agent = dict(phone.agent, tool_policy={"destructive": "confirm"})
    await S.seed_agents([agent])
    ch = await channel_store.get_channel(phone.channel["id"])
    ch["visitor_tools"] = ["refund"]
    await channel_store.save_channel(ch)

    sid = _call_sid(8)
    url = _socket_url(phone, await _ring(phone, twilio, sid))
    fake_llm.script([tool_call("refund", order="A1")])
    async with websockets.connect(url) as ws:
        relay = Relay(ws)
        await relay.send(type="setup", callSid=sid)
        await relay.say("Refund order A1")
        frames = await relay.hear()
        if "approve" not in _words(frames):
            frames += await relay.hear()  # the filler came first, while the call was checked
        assert _words(frames).endswith("I've asked someone on the team to approve that. "
                                       "They'll follow up with you.")
    ask = [i for i in hub.list() if i["kind"] == "tool_approval"][-1]
    session_id = f"voice_{phone.channel['id']}_{sid}"
    assert ask["data"]["caller"] == CALLER and ask["data"]["door"] == "voice"
    assert ask["data"]["answer_url"] == f"/api/messaging/twilio_voice/approvals/{session_id}"

    fake_llm.script(["Okay, I won't refund it."])
    r = await client.post(ask["data"]["answer_url"], json={"approve": False, "reason": "not eligible",
                                                           "approval_id": ask["data"]["approval_id"]})
    assert r.status_code == 200, r.text
    for _ in range(100):
        if any(i["kind"] == "run_completed" for i in hub.list()):
            break
        await asyncio.sleep(0.05)
    done = [i for i in hub.list() if i["kind"] == "run_completed"][-1]
    assert done["title"].endswith(f"call {CALLER} back")
    row = await _sessions.get(session_id, agent["id"])
    assert row is not None, "a call's conversation is its transcript: never deleted on settle"
    assert "Okay, I won't refund it." in [m["content"] for m in row["messages"]]


async def test_a_confirm_answered_while_the_caller_is_on_the_line_is_said_on_the_call(phone, twilio, fake_llm,
                                                                                         monkeypatch, client):
    """F474 (VOX1e): answered during the call, the answer is spoken on it, after the call's own lead-in, and
    saved into the call's transcript once; nothing resumes behind it, so the bell never asks for a call-back."""
    import core.react_engine as re_mod
    from mcp.types import ToolAnnotations

    from core.messaging import store as channel_store
    from core.messaging.voice import call as call_mod
    from core.notifications import hub
    from core.store import sessions as _sessions
    from core.tools import VirtualTool

    refund = VirtualTool("refund", "refunds an order", {"type": "object"},
                         ToolAnnotations(readOnlyHint=False, destructiveHint=True))

    async def aggregated(server_module, agent, custom_tools, **_):
        ollama = [{"type": "function", "function": {"name": "refund", "description": "x", "parameters": {}}}]
        return [refund], {"refund": {}}, ollama, "['refund']"
    monkeypatch.setattr(re_mod, "aggregate_all_tools", aggregated)
    agent = dict(phone.agent, tool_policy={"destructive": "confirm"})
    await S.seed_agents([agent])
    ch = await channel_store.get_channel(phone.channel["id"])
    ch["visitor_tools"] = ["refund"]
    await channel_store.save_channel(ch)

    sid = _call_sid(9)
    url = _socket_url(phone, await _ring(phone, twilio, sid))
    session_id = f"voice_{phone.channel['id']}_{sid}"
    fake_llm.script([tool_call("refund", order="A1")])
    async with websockets.connect(url) as ws:
        relay = Relay(ws)
        await relay.send(type="setup", callSid=sid)
        await relay.say("Refund order A1")
        frames = await relay.hear()
        if "approve" not in _words(frames):
            frames += await relay.hear()
        ask = [i for i in hub.list() if i["kind"] == "tool_approval"][-1]

        fake_llm.script(["Okay, I won't refund it."])
        r = await client.post(ask["data"]["answer_url"], json={"approve": False, "reason": "not eligible",
                                                               "approval_id": ask["data"]["approval_id"]})
        assert r.status_code == 200, r.text
        # The call says it once its own words are done: here the notice, the filler and the approval's line,
        # about 11 s at the speed it reckons (real time, unlike the loop tests).
        heard = await relay.hear(timeout=20)
        while "refund it" not in _words(heard):
            heard += await relay.hear()
        assert _words(heard) == call_mod.REFUSED_LINE + "Okay, I won't refund it."
        # The caller hangs up the moment it is said: the note that marks it as the approval's still lands.

    await asyncio.sleep(0.3)
    assert not [i for i in hub.list() if i["kind"] == "run_completed"], "the caller heard it: no call-back asked"
    row = await _sessions.get(session_id, agent["id"])
    said = [m["content"] for m in row["messages"]]
    assert said.count("Okay, I won't refund it.") == 1
    assert row["messages"][-1].get("answered_approval") == {"id": ask["data"]["approval_id"], "approved": False}
    # The Phone tab's log reads it through the history, as the filler note (VOX1c).
    history = (await client.get(f"/api/sessions/{session_id}/history", params={"agent_id": agent["id"]})).json()
    assert history["turns"][-1]["answered_approval"] == {"id": ask["data"]["approval_id"], "approved": False}
    assert "approval" not in history["turns"][-1], "never drawn as a question nobody answered (F247)"


async def test_a_signed_number_refuses_an_unsigned_webhook(phone, twilio):
    """V6 with a Standard key: every webhook carries the number's SharedKey signature (VOX1a P3)."""
    from core.messaging import store as channel_store
    from core.messaging.voice.twilio import signature

    ch = await channel_store.get_channel(phone.channel["id"])
    ch["credentials"]["signing_secret"] = "sign-me"
    ch["voice"]["signing"] = {"shared_key_id": "webhooks_sharedkey_1", "setting_id": "s", "rule_id": "r"}
    await channel_store.save_channel(ch)
    form = {"CallSid": _call_sid(9), "AccountSid": ACCOUNT, "To": NUMBER, "From": CALLER}
    url = phone.live.url + phone.base + "/incoming"
    assert (await _post(phone, "/incoming", form)).status_code == 403

    async with phone.live.client() as c:
        forged = await c.post(phone.base + "/incoming", data=form, headers={
            "X-Twilio-Signature": signature("not-the-key", url, form),
            "X-Twilio-Webhook-Signature-Settings": "webhooks_sharedkey_1;HMAC_SHA256"})
        assert forged.status_code == 403
        signed = await c.post(phone.base + "/incoming", data=form, headers={
            "X-Twilio-Signature": signature("sign-me", url, form),
            "X-Twilio-Webhook-Signature-Settings": "webhooks_sharedkey_1;HMAC_SHA256"})
    assert signed.status_code == 200 and "<ConversationRelay" in signed.text


async def test_a_session_we_ended_is_never_resumed_even_if_twilio_says_it_failed(phone, twilio):
    """Naveen's first real call: the agent said goodbye, Twilio reported the session failed with no HandoffData
    (64105), and the end callback resumed the call into "are you still there?" loops."""
    from core.messaging.voice import hooks

    sid = _call_sid(10)
    await _ring(phone, twilio, sid)
    await hooks.current().update_call(sid, {"outcome": "agent_ended", "handoff": {"ended": "agent"}})
    r = await _post(phone, "/end", {"CallSid": sid, "SessionStatus": "failed", "SessionDuration": "30",
                                    "CallStatus": "in-progress", "ErrorCode": "64105",
                                    "ErrorMessage": "Websocket ended"})
    assert "<Hangup/>" in r.text and "ConversationRelay" not in r.text
    assert (await hooks.current().get_call(sid))["outcome"] == "agent_ended"


async def test_a_socket_that_really_dropped_is_picked_up_again(phone, twilio):
    sid = _call_sid(11)
    await _ring(phone, twilio, sid)
    r = await _post(phone, "/end", {"CallSid": sid, "SessionStatus": "failed", "SessionDuration": "30",
                                    "CallStatus": "in-progress", "ErrorCode": "64105"})
    assert '<Parameter name="resume" value="1"/>' in r.text
