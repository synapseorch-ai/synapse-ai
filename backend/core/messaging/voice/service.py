"""A call from ring to hang-up, as the routes see it (the self-hosted server's, and cloud's voice service's).

    incoming ─ verify (V6), admit (V22), answer: <Connect action=end><ConversationRelay url=ws?t=…>
    ws       ─ the one-time token, then `VoiceCall` runs the conversation
    end      ─ the session ended: transfer (<Dial action=dialed>), move (a fresh <Connect>, V10), or hang up
    dialed   ─ nobody took the transfer: voicemail (<Record action=recorded>)
    recorded ─ the message is in the customer's Twilio account; the bell says it is waiting
    status   ─ the number's call status callback: when the call itself ended
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from core.messaging.voice import PLATFORM, hooks, mask_number, session_id as voice_session_id
from core.messaging.voice.connect import provider_for, urls
from core.messaging.voice.provider import AnswerOptions, CallRefused
from core.messaging.voice.tokens import BadSocketToken, read_socket_token, route_token_matches, socket_token

log = logging.getLogger("voice")

UNAVAILABLE = "This line isn't available right now. Please try again later."
NO_AGENT = "This line has no one to answer it yet. Please try again later."
GOODBYE_AFTER_VOICEMAIL = "Thank you. Goodbye."
#: How many times one call may be moved or picked up again after a dropped socket.
MAX_RESUMES = 3
DEFAULT_NOTICE = "Hi, you're speaking with {agent}, an AI assistant{for_org}."
RECORDING_LINE = "This call may be recorded."


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def channel_for_route(route_token: str) -> dict | None:
    """The voice channel a route token names (V6 check 1), compared in constant time."""
    from core.messaging import store as channel_store

    found = None
    for ch in await channel_store.list_channels():
        if ch.get("platform") == PLATFORM and \
                route_token_matches(route_token, (ch.get("credentials") or {}).get("route_token")):
            found = ch
    return found


def notice_for(voice: dict, *, agent_name: str, org: str = "") -> str:
    """The opening notice (V25): editable, never empty, and it says so when the call is recorded."""
    text = str(voice.get("notice") or "").strip() or DEFAULT_NOTICE
    text = text.replace("{agent}", agent_name or "an assistant").replace(
        "{org}", org).replace("{for_org}", f" for {org}" if org else "")
    if voice.get("recording") and "record" not in text.lower():
        text = f"{text} {RECORDING_LINE}"
    return text


async def _agent_for(channel_id: str) -> dict | None:
    from core.messaging import store as channel_store
    from core.react_engine import resolve_agent_for_run

    agent_id = await channel_store.get_default_agent_id(channel_id)
    if not agent_id:
        return None
    try:
        agent = await resolve_agent_for_run(agent_id)
    except Exception:  # noqa: BLE001 — an agent deleted since it was assigned
        return None
    return agent if agent and agent.get("id") else None


def _running(channel: dict) -> bool:
    return bool(channel.get("enabled", True)) and channel.get("status") == "running"


async def incoming(channel: dict, form: dict) -> str:
    """Answer a ringing call: markup that connects it to our socket, or one sentence and a hang-up."""
    host = hooks.current()
    voice = dict(channel.get("voice") or {})
    provider = provider_for(await host.secrets(channel))
    language = voice.get("language") or "en-US"
    if not _running(channel):
        log.warning("a call to a voice channel that is %s", channel.get("status") if channel.get("enabled", True)
                    else "turned off")
        return provider.say_and_hang_up(UNAVAILABLE, language=language)
    try:
        info = await provider.verify_incoming(form, number=voice.get("number") or "")
    except CallRefused as exc:
        log.warning("refused a call on a voice channel: %s", exc.reason)  # never the body
        return provider.say_and_hang_up(UNAVAILABLE, language=language)
    agent = await _agent_for(channel["id"])
    if agent is None:
        return provider.say_and_hang_up(NO_AGENT, language=language)
    refusal = await host.admit(channel, info.caller, call_sid=info.call_sid)
    if refusal:
        await host.notify("run_failed", "A call could not be answered", body=refusal,
                          data={"door": "voice", "channel_id": channel["id"], "caller": info.caller})
        return provider.say_and_hang_up(refusal, language=language)
    from core.messaging.voice.call import new_record
    if await host.get_call(info.call_sid) is None:
        await host.save_call(new_record(
            call_sid=info.call_sid, channel_id=channel["id"], agent_id=agent["id"],
            session_id=voice_session_id(channel["id"], info.call_sid), caller=info.caller, called=info.called))
    log.info("answering a call from %s", mask_number(info.caller))
    return _connect(channel, provider, info.call_sid, agent_name=agent.get("name") or "", resume=False)


def _connect(channel: dict, provider, call_sid: str, *, agent_name: str, resume: bool) -> str:
    host = hooks.current()
    voice = dict(channel.get("voice") or {})
    to = urls(host.public_base(), (channel.get("credentials") or {}).get("route_token") or "")
    token = socket_token(host.signing_key(), channel_id=channel["id"], call_sid=call_sid, resume=resume)
    return provider.answer(ws_url=f"{to['ws']}?t={token}", action_url=to["end"], opts=AnswerOptions(
        notice=notice_for(voice, agent_name=agent_name, org=host.org_name(channel)),
        language=voice.get("language") or "en-US",
        voice=voice.get("voice") or "", tts_provider=voice.get("tts_provider") or "", resume=resume))


class SocketRefused(Exception):
    pass


async def socket_call(channel: dict, token: str, transport):
    """The `VoiceCall` this socket carries, once its token is checked (V6 check 3). Raises `SocketRefused`."""
    from core.messaging.voice.call import VoiceCall

    host = hooks.current()
    try:
        claims = read_socket_token(host.signing_key(), token, channel_id=channel["id"])
    except BadSocketToken as exc:
        raise SocketRefused(str(exc)) from exc
    if not await host.claim_socket(claims["nonce"]):
        raise SocketRefused("used")
    record = await host.get_call(claims["call_sid"])
    if record is None:
        raise SocketRefused("unknown call")
    agent = await _agent_for(channel["id"])
    if agent is None or agent["id"] != record.get("agent_id"):
        agent = {"id": record.get("agent_id"), "name": (agent or {}).get("name") or ""}
    try:
        started = datetime.fromisoformat(record["started_at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        started = time.time()
    voice = dict(channel.get("voice") or {})
    voice["status_url"] = urls(host.public_base(), (channel.get("credentials") or {}).get("route_token") or "")["status"]
    return VoiceCall(
        transport=transport, host=host, provider=provider_for(await host.secrets(channel)),
        channel={**channel, "voice": voice}, agent=agent, call_sid=claims["call_sid"],
        caller=record.get("caller") or "", called=record.get("called") or "",
        session_id=record.get("session_id") or voice_session_id(channel["id"], claims["call_sid"]),
        resume=claims["resume"], notice=notice_for(voice, agent_name=agent.get("name") or "",
                                                   org=host.org_name(channel)),
        started_at=time.monotonic() - max(0.0, time.time() - started))


async def end(channel: dict, form: dict) -> str:
    """The session ended (`<Connect action>`): what the call does next."""
    host = hooks.current()
    voice = dict(channel.get("voice") or {})
    provider = provider_for(await host.secrets(channel))
    done = provider.parse_end(form)
    record = await host.get_call(done.call_sid)
    if record is None:
        return provider.say_and_hang_up(UNAVAILABLE)
    # What we asked for when we ended the session is on the record. Twilio can report a session we ended
    # as failed with no HandoffData (64105 when the socket closed first); ours decides, never "dropped".
    ours = record.get("handoff") if isinstance(record.get("handoff"), dict) else None
    if not done.handoff and ours:
        done.handoff = dict(ours)
    segment = {"seconds": done.duration, "status": done.status, "at": _now_iso()}
    changes: dict = {"segments": [segment]}
    if done.error_code:
        changes["error"] = f"{done.error_code} {done.error_message}".strip()
    record = await host.update_call(done.call_sid, changes) or record

    number = str(voice.get("transfer_number") or "").strip()
    transferring = done.handoff.get("transfer") is not None and bool(number)
    moved = bool(done.handoff.get("reconnect"))
    dropped = done.status == "failed" and not done.handoff
    resumes = int(record.get("resumes") or 0)
    resuming = (not done.caller_hung_up and not transferring and (moved or dropped) and _running(channel)
                and resumes < MAX_RESUMES)
    # Our part of the call is over unless it picks up again on a fresh socket (a hand-off's leg is not ours).
    await host.settle(record, final=not resuming)

    if done.caller_hung_up:
        await host.update_call(done.call_sid, {"ended_at": _now_iso(),
                                               "outcome": record.get("outcome") or "caller_hung_up"})
        return provider.hang_up_markup()

    if transferring:
        await host.update_call(done.call_sid, {"transfer": {"reason": done.handoff.get("transfer"), "status": "dialing"}})
        await host.notify("human_input", f"A caller is being put through to {number}",
                          body=f"Reason: {done.handoff.get('transfer') or 'not given'}. The call's log has the transcript.",
                          data={"door": "voice", "channel_id": channel["id"], "call_sid": done.call_sid,
                                "caller": record.get("caller"), "session_id": record.get("session_id")})
        to = urls(host.public_base(), (channel.get("credentials") or {}).get("route_token") or "")
        return provider.transfer_markup(number=number, caller_id=voice.get("number") or "", action_url=to["dialed"])

    if resuming:
        # V10: a fresh socket on whichever process answers, the same conversation, no greeting.
        await host.update_call(done.call_sid, {"resumes": resumes + 1, "handoff": None, "outcome": None})
        agent = await _agent_for(channel["id"]) or {}
        return _connect(channel, provider, done.call_sid, agent_name=agent.get("name") or "", resume=True)

    await host.update_call(done.call_sid, {"ended_at": _now_iso(),
                                           "outcome": record.get("outcome") or ("failed" if dropped else "ended")})
    return provider.hang_up_markup()


async def dialed(channel: dict, form: dict) -> str:
    """How the transfer went (V17): answered, the call is theirs; otherwise voicemail."""
    host = hooks.current()
    voice = dict(channel.get("voice") or {})
    provider = provider_for(await host.secrets(channel))
    call_sid = str(form.get("CallSid") or "")
    record = await host.get_call(call_sid)
    if record is None:
        return provider.say_and_hang_up(UNAVAILABLE)
    status = str(form.get("DialCallStatus") or "")
    transfer = dict(record.get("transfer") or {})
    transfer["status"] = status
    if status == "completed":
        await host.update_call(call_sid, {"transfer": transfer, "outcome": "transferred", "ended_at": _now_iso()})
        return provider.hang_up_markup()
    await host.update_call(call_sid, {"transfer": transfer, "outcome": "voicemail"})
    to = urls(host.public_base(), (channel.get("credentials") or {}).get("route_token") or "")
    return provider.voicemail_markup(action_url=to["recorded"], language=voice.get("language") or "en-US")


async def recorded(channel: dict, form: dict) -> str:
    """A voicemail is waiting (V17): kept in the customer's Twilio account; the bell asks a person."""
    host = hooks.current()
    voice = dict(channel.get("voice") or {})
    provider = provider_for(await host.secrets(channel))
    call_sid = str(form.get("CallSid") or "")
    record = await host.get_call(call_sid)
    if record is None:
        return provider.say_and_hang_up(UNAVAILABLE)
    try:
        seconds = int(form.get("RecordingDuration") or 0)
    except (TypeError, ValueError):
        seconds = 0
    vm = {"recording_sid": str(form.get("RecordingSid") or ""), "url": str(form.get("RecordingUrl") or ""),
          "seconds": seconds}
    await host.update_call(call_sid, {"voicemail": vm, "outcome": "voicemail", "ended_at": _now_iso()})
    if seconds > 0:
        await host.notify("human_input", f"Voicemail from {record.get('caller') or 'a caller'}",
                          body=f"{seconds} seconds, in your Twilio account. Call them back.",
                          data={"door": "voice", "channel_id": channel["id"], "call_sid": call_sid,
                                "caller": record.get("caller"), "voicemail": vm,
                                "session_id": record.get("session_id")})
    return provider.say_and_hang_up(GOODBYE_AFTER_VOICEMAIL, language=voice.get("language") or "en-US")


async def status(channel: dict, form: dict) -> None:
    """The number's status callback (and a recording's): the call's own end, kept on its record."""
    host = hooks.current()
    call_sid = str(form.get("CallSid") or "")
    if not call_sid or await host.get_call(call_sid) is None:
        return
    changes: dict = {}
    if form.get("CallStatus") in ("completed", "busy", "failed", "no-answer", "canceled"):
        changes["ended_at"] = _now_iso()
        try:
            changes["call_seconds"] = int(form.get("CallDuration") or 0)
        except (TypeError, ValueError):
            pass
    if form.get("RecordingSid") and form.get("RecordingStatus"):
        changes["recording_sid"] = str(form.get("RecordingSid"))
    if changes:
        await host.update_call(call_sid, changes)
