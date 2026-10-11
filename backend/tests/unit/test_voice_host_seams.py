"""
The voice host's seams a second host needs (VOX1b, cloud's voice service): a
call is billed once, when it is over (`settle(final=…)`), not on every session
of a moved call; a caller's confirm is parked by the host (`park`), so cloud
asks through its own bell; the opening notice names the host's organisation;
and a disconnect says what Twilio would not remove (a trial account cannot
delete a SharedKey, and an account holds at most 50).
"""
import asyncio
import json

import httpx
import pytest

from core.messaging.voice import call as call_mod, connect, hooks, service
from core.messaging.voice.hooks import VoiceHost
from core.messaging.voice.relay import Frame, Prompt
from core.messaging.voice.twilio import TwilioProvider

ACCOUNT = "AC" + "1" * 32
NUMBER = "+15551230000"
CALL = "CA" + "3" * 32
CREDS = {"account_sid": ACCOUNT, "api_key_sid": "SKx", "api_key_secret": "s", "route_token": "r" * 32}


class RecordingHost(VoiceHost):
    """Keeps call records in memory and remembers every settle."""

    def __init__(self, org: str = ""):
        super().__init__()
        self.records: dict[str, dict] = {}
        self.settled: list[tuple[int, bool]] = []
        self.org = org

    def org_name(self, channel):
        return self.org

    def public_base(self):
        return "https://voice.example"

    async def settle(self, record, *, final):
        self.settled.append((int(record.get("seconds") or 0), final))

    async def notify(self, kind, title, *, body="", data=None):
        pass

    async def save_call(self, record):
        self.records[record["id"]] = dict(record)

    async def get_call(self, call_sid):
        return self.records.get(call_sid)

    async def update_call(self, call_sid, changes):
        rec = self.records.get(call_sid)
        if rec is None:
            return None
        for k, v in changes.items():
            rec[k] = (rec.get(k) or []) + list(v or []) if k in ("segments", "approvals", "first_words_ms") else v
        rec["seconds"] = sum(int(s.get("seconds") or 0) for s in rec.get("segments") or [])
        return rec


@pytest.fixture
def host(monkeypatch):
    h = RecordingHost()
    hooks.install(h)

    async def agent_for(channel_id):
        return {"id": "a1", "name": "Ada"}
    monkeypatch.setattr(service, "_agent_for", agent_for)
    yield h
    hooks.install(None)


def _channel(**voice):
    return {"id": "chn_1", "platform": "twilio_voice", "enabled": True, "status": "running",
            "credentials": dict(CREDS), "voice": {"number": NUMBER, **voice}}


def _record(host, **extra):
    rec = call_mod.new_record(call_sid=CALL, channel_id="chn_1", agent_id="a1", session_id=f"voice_chn_1_{CALL}",
                              caller="+14155550100", called=NUMBER)
    rec.update(extra)
    host.records[CALL] = rec
    return rec


def _end_form(*, status="ended", seconds=65, call_status="in-progress", handoff=None):
    form = {"CallSid": CALL, "SessionStatus": status, "SessionDuration": str(seconds), "CallStatus": call_status}
    if handoff is not None:
        form["HandoffData"] = json.dumps(handoff)
    return form


class TestABillIsSettledOnceTheCallIsOver:
    async def test_a_caller_who_hangs_up_ends_it(self, host):
        _record(host)
        await service.end(_channel(), _end_form(status="completed", call_status="completed"))
        assert host.settled == [(65, True)]

    async def test_the_agent_ending_it_ends_it(self, host):
        _record(host, handoff={"ended": "agent"})
        markup = await service.end(_channel(), _end_form())
        assert host.settled == [(65, True)] and "<Hangup/>" in markup

    async def test_a_move_is_not_the_end_and_the_next_session_is(self, host):
        _record(host, handoff={"reconnect": True})
        markup = await service.end(_channel(), _end_form(seconds=40))
        assert host.settled == [(40, False)], "a moved call carries on: nothing billed yet"
        assert "<ConversationRelay" in markup and host.records[CALL]["resumes"] == 1
        host.records[CALL]["handoff"] = {"ended": "agent"}
        await service.end(_channel(), _end_form(seconds=50))
        assert host.settled[-1] == (90, True), "the two sessions are one call, summed"

    async def test_a_dropped_socket_picks_up_again_until_the_last_try(self, host):
        _record(host)
        await service.end(_channel(), _end_form(status="failed"))
        assert host.settled == [(65, False)]
        host.records[CALL]["resumes"] = service.MAX_RESUMES
        await service.end(_channel(), _end_form(status="failed", seconds=5))
        assert host.settled[-1] == (70, True)

    async def test_a_hand_off_ends_our_part(self, host):
        _record(host, handoff={"transfer": "billing"})
        markup = await service.end(_channel(transfer_number="+15550001111"), _end_form())
        assert host.settled == [(65, True)] and "<Dial" in markup

    async def test_a_move_on_a_number_turned_off_is_the_end(self, host):
        _record(host, handoff={"reconnect": True})
        ch = _channel()
        ch["enabled"] = False
        markup = await service.end(ch, _end_form())
        assert host.settled == [(65, True)] and "<Hangup/>" in markup


class TestAdmissionNamesTheCall:
    async def test_admit_is_told_which_call_it_is(self, host, monkeypatch):
        """A host that counts live calls (cloud's org limit, V23) frees a call's place by its CallSid."""
        asked = []

        async def admit(channel, caller, call_sid=""):
            asked.append((caller, call_sid))
            return "All our lines are busy."
        monkeypatch.setattr(host, "admit", admit)
        form = {"CallSid": CALL, "From": "+14155550100", "To": NUMBER, "AccountSid": ACCOUNT}
        markup = await service.incoming(_channel(), form)
        assert asked == [("+14155550100", CALL)]
        assert "All our lines are busy." in markup and "<Hangup/>" in markup


class TestTheNoticeNamesTheHostsOrganisation:
    async def test_cloud_names_the_workspace(self, host):
        host.org = "Harbor Home"
        markup = service._connect(_channel(), TwilioProvider.from_credentials(CREDS), CALL, agent_name="Ada",
                                  resume=False)
        assert 'welcomeGreeting="Hi, you\'re speaking with Ada, an AI assistant for Harbor Home."' in markup

    async def test_self_hosted_names_no_one(self):
        hooks.install(None)
        assert VoiceHost().org_name({}) == ""
        markup = service._connect(_channel(), TwilioProvider.from_credentials(CREDS), CALL, agent_name="Ada",
                                  resume=False)
        assert 'welcomeGreeting="Hi, you\'re speaking with Ada, an AI assistant."' in markup


class FakeSocket:
    def __init__(self):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.said: list[str] = []

    async def receive(self):
        frame = await self.inbox.get()
        if frame is None:
            raise ConnectionError("closed")
        return frame

    async def say(self, text, *, last, interruptible=None):
        self.said.append(text)

    async def end(self, handoff=None):
        await self.inbox.put(None)


class ParkingHost(RecordingHost):
    def __init__(self, ask):
        super().__init__()
        self.ask = ask
        self.parked: list[dict] = []

    def max_minutes(self, channel):
        return 30

    async def run_turn(self, call, text, *, caller_system, extra_tools):
        yield {"type": "_step_progress", "turn": 1}
        yield self.ask

    async def park(self, call, *, event, message, progress, caller_system):
        self.parked.append({"session_id": call.session_id, "event": event, "message": message,
                            "progress": progress, "caller_system": caller_system,
                            "visitor": call.visitor.to_dict()})
        return "ap_9"


class TestAConfirmIsParkedByTheHost:
    async def test_the_host_parks_it_and_the_caller_is_told(self, monkeypatch):
        saved = []

        async def save_turn(session_id, agent_id, user, assistant, tools=None, **kw):
            saved.append((user, assistant))
        import core.session as session_mod
        monkeypatch.setattr(session_mod, "_save_conversation_turn", save_turn)

        async def never(**kw):
            raise AssertionError("a host that parks itself is never bypassed")
        from core import api_approvals
        monkeypatch.setattr(api_approvals, "park", never)

        ask = {"type": "human_input_required", "kind": "tool_approval", "tool_name": "pay_bill"}
        host = ParkingHost(ask)
        _record(host)
        socket = FakeSocket()
        channel = {"id": "chn_1", "platform": "twilio_voice", "visitor_tools": ["pay_bill"], "voice": {}}
        call = call_mod.VoiceCall(transport=socket, host=host, provider=None, channel=channel,
                                  agent={"id": "a1", "name": "Ada"}, call_sid=CALL, caller="+14155550100",
                                  called=NUMBER, session_id=f"voice_chn_1_{CALL}", resume=True)
        task = asyncio.create_task(call.run())
        await socket.inbox.put(Prompt("prompt", text="pay my bill", last=True))
        for _ in range(100):
            if socket.said:
                break
            await asyncio.sleep(0.01)
        await socket.inbox.put(Frame("closed"))
        await socket.inbox.put(None)
        await asyncio.wait_for(task, 5)
        assert socket.said[0] == call_mod.APPROVAL_LINE
        [p] = host.parked
        assert p["event"] is ask and p["message"] == "pay my bill" and p["progress"] == {"turn": 1}
        assert p["caller_system"].startswith(call_mod.VOICE_SUFFIX[:40])
        assert p["visitor"] == {"surface": "channel", "tools": ["pay_bill"], "approvals": "park"}
        assert host.records[CALL]["approvals"] == ["ap_9"]
        assert saved == [("pay my bill", call_mod.APPROVAL_LINE)]

    async def test_the_default_host_parks_through_api_approvals(self, monkeypatch):
        got = []

        async def park(**kw):
            got.append(kw)
            return "ap_1"
        from core import api_approvals
        monkeypatch.setattr(api_approvals, "park", park)

        class C:
            session_id, agent_id, agent_name, caller = "voice_c_CA1", "a1", "Ada", "+14155550100"
            visitor = call_mod.VoiceCall.__new__(call_mod.VoiceCall)
        from core import visitor as _visitor
        C.visitor = _visitor.VisitorScope("channel", frozenset(), approvals="park")
        out = await VoiceHost().park(C, event={"approval_id": "ap_1"}, message="m", progress=None,
                                     caller_system="sys")
        assert out == "ap_1"
        [kw] = got
        assert kw["door"] == "voice" and kw["hours"] == 24 and kw["resume"] == {"caller_system": "sys"}
        assert kw["caller"] == {"number": "+14155550100", "visitor": {"surface": "channel", "tools": [],
                                                                      "approvals": "park"}}


def _twilio(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestADisconnectSaysWhatWasLeft:
    async def test_a_trial_account_keeps_its_signing_key(self):
        def handler(req):
            return httpx.Response(401, json={"code": 20003, "message": "Authenticate"})
        p = TwilioProvider(account_sid=ACCOUNT, api_key_sid="SKx", api_key_secret="s", http=_twilio(handler))
        left = await p.stop_signing({"shared_key_id": "k", "setting_id": "s", "rule_id": "r"})
        assert left == ["rule_id", "setting_id", "shared_key_id"]

    async def test_a_removed_or_gone_key_leaves_nothing(self):
        codes = iter([204, 404, 204])

        def handler(req):
            return httpx.Response(next(codes))
        p = TwilioProvider(account_sid=ACCOUNT, api_key_sid="SKx", api_key_secret="s", http=_twilio(handler))
        assert await p.stop_signing({"shared_key_id": "k", "setting_id": "s", "rule_id": "r"}) == []

    async def test_an_unreachable_twilio_counts_as_left(self):
        def handler(req):
            raise httpx.ConnectError("down")
        p = TwilioProvider(account_sid=ACCOUNT, api_key_sid="SKx", api_key_secret="s", http=_twilio(handler))
        assert await p.stop_signing({"shared_key_id": "k"}) == ["shared_key_id"]

    async def test_disconnect_returns_the_leftovers_and_the_number(self, monkeypatch):
        saved = []

        class Provider:
            async def stop_signing(self, made):
                return ["shared_key_id"]

            async def restore_number(self, number_sid, previous):
                raise RuntimeError("refused")
        monkeypatch.setattr(connect, "provider_for", lambda creds: Provider())
        from core.messaging import store as channel_store

        ch = {"id": "chn_1", "platform": "twilio_voice", "credentials": {**CREDS, "signing_secret": "x"},
              "voice": {"number_sid": "PN1", "previous": {"voice_url": ""}, "signing": {"shared_key_id": "k"}}}

        async def get_channel(cid):
            return json.loads(json.dumps(ch))

        async def save_channel(doc):
            saved.append(doc)
            return doc
        monkeypatch.setattr(channel_store, "get_channel", get_channel)
        monkeypatch.setattr(channel_store, "save_channel", save_channel)
        hooks.install(None)
        left = await connect.disconnect(ch)
        assert left == ["shared_key_id", "number"]
        [doc] = saved
        assert "signing" not in doc["voice"] and "signing_secret" not in doc["credentials"]
        assert doc["voice"]["previous"] == {"voice_url": ""}, "a number not put back keeps where it pointed"
