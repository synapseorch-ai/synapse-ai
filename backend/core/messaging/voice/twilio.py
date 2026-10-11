"""Twilio: ConversationRelay for the call, the REST API with the customer's API key (never the auth token, V4).

Every request to Twilio authenticates with an API key SID and secret (a Restricted key with Calls and Incoming
Phone Numbers, or a Standard key) on the customer's Account SID.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from xml.sax.saxutils import escape, quoteattr

import httpx

from core.messaging.voice import PLATFORM, mask_number
from core.messaging.voice.provider import (
    AnswerOptions, CallInfo, CallProvider, CallRefused, Number, SessionEnd, handoff_of,
)

log = logging.getLogger("voice.twilio")

API = "https://api.twilio.com/2010-04-01"
WEBHOOKS_API = "https://webhooks.twilio.com/v1/Webhooks"
#: How long a transfer rings before it counts as unanswered (then voicemail, V17).
TRANSFER_TIMEOUT = 20
#: The longest voicemail, in seconds.
VOICEMAIL_SECONDS = 120


def _xml(body: str) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><Response>' + body + "</Response>"


def _attrs(values: dict) -> str:
    return " ".join(f"{k}={quoteattr(str(v))}" for k, v in values.items() if v not in (None, ""))


def _same_number(a: str | None, b: str | None) -> bool:
    def digits(n):
        return "".join(ch for ch in str(n or "") if ch.isdigit())
    return bool(digits(a)) and digits(a) == digits(b)


def signature(secret: str, url: str, params: dict) -> str:
    """Twilio's webhook signature: HMAC-SHA256 over the URL and each POST parameter (sorted) as name+value."""
    base = url + "".join(k + str(v) for k, v in sorted(params.items()))
    return base64.b64encode(hmac.new(secret.encode(), base.encode(), hashlib.sha256).digest()).decode()


def verify_signature(secret: str, url: str, params: dict, headers) -> bool:
    """A webhook signed with this number's SharedKey (V6). Constant-time; the settings header names the key."""
    given = headers.get("x-twilio-signature") or ""
    used = headers.get("x-twilio-webhook-signature-settings") or ""
    if not given or not used.endswith(";HMAC_SHA256"):
        return False
    return hmac.compare_digest(given, signature(secret, url, params))


class TwilioProvider(CallProvider):
    platform = PLATFORM

    def __init__(self, *, account_sid: str, api_key_sid: str, api_key_secret: str,
                 http: httpx.AsyncClient | None = None):
        self.account_sid = account_sid
        self._auth = (api_key_sid, api_key_secret)
        self._http = http

    @classmethod
    def from_credentials(cls, creds: dict, http: httpx.AsyncClient | None = None) -> "TwilioProvider":
        return cls(account_sid=str(creds.get("account_sid") or ""), api_key_sid=str(creds.get("api_key_sid") or ""),
                   api_key_secret=str(creds.get("api_key_secret") or ""), http=http)

    async def _request(self, method: str, path: str, **kw) -> tuple[int, dict]:
        url = f"{API}/Accounts/{self.account_sid}{path}"
        if self._http is not None:
            resp = await self._http.request(method, url, auth=self._auth, **kw)
        else:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.request(method, url, auth=self._auth, **kw)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        return resp.status_code, body if isinstance(body, dict) else {}

    @staticmethod
    def _refusal(code: int, body: dict, what: str) -> CallRefused:
        detail = body.get("message") or f"HTTP {code}"
        if code in (401, 403):
            return CallRefused(f"Twilio refused the API key for {what} ({detail}).", permission=what)
        return CallRefused(f"Twilio could not {what} ({detail}).")

    # ── A call ───────────────────────────────────────────────────────────────

    async def verify_incoming(self, form: dict, *, number: str) -> CallInfo:
        """The call this webhook names, if it is this account's and for this number.

        No REST lookup: `GET /Calls/{sid}` answered 404 for every real call, ringing or after (VOX1a P2,
        2026-10-10), so it cannot tell a real call from a forged one. What admits a call is the route token
        and, when the number signs its webhooks, the SharedKey signature (`verify_signature`).

        A browser call through a TwiML App (Twilio's Voice SDK) carries no `To`; it is accepted, since only
        the account's own access tokens can place one.
        """
        call_sid = str(form.get("CallSid") or "")
        if not call_sid.startswith("CA"):
            raise CallRefused("no CallSid")
        if form.get("AccountSid") and form.get("AccountSid") != self.account_sid:
            raise CallRefused("another account's call")
        caller = str(form.get("From") or "")
        to = str(form.get("To") or "")
        if not (to == "" and caller.startswith("client:")) and not _same_number(to, number):
            raise CallRefused("not this channel's number")
        return CallInfo(call_sid=call_sid, caller=caller, called=to or number)

    def answer(self, *, ws_url: str, action_url: str, opts: AnswerOptions) -> str:
        relay = {
            "url": ws_url,
            # The notice is the greeting and is always heard in full (V14, V25). A moved call (V10) has none.
            "welcomeGreeting": "" if opts.resume else opts.notice,
            "welcomeGreetingInterruptible": "" if opts.resume else "none",
            "interruptible": "any",
            "ignoreBackchannel": "true",
            "dtmfDetection": "false",
            "language": opts.language or "en-US",
            "ttsProvider": opts.tts_provider,
            "voice": opts.voice,
            "transcriptionProvider": opts.transcription_provider,
            "speechModel": opts.speech_model,
        }
        params = '<Parameter name="resume" value="1"/>' if opts.resume else ""
        return _xml(f"<Connect {_attrs({'action': action_url})}>"
                    f"<ConversationRelay {_attrs(relay)}>{params}</ConversationRelay></Connect>")

    def parse_end(self, form: dict) -> SessionEnd:
        try:
            duration = int(form.get("SessionDuration") or 0)
        except (TypeError, ValueError):
            duration = 0
        return SessionEnd(
            call_sid=str(form.get("CallSid") or ""), status=str(form.get("SessionStatus") or ""),
            duration=duration, call_status=str(form.get("CallStatus") or ""),
            handoff=handoff_of(form.get("HandoffData")), error_code=str(form.get("ErrorCode") or ""),
            error_message=str(form.get("ErrorMessage") or ""))

    def transfer_markup(self, *, number: str, caller_id: str, action_url: str) -> str:
        dial = _attrs({"callerId": caller_id, "timeout": TRANSFER_TIMEOUT, "action": action_url})
        return _xml(f"<Dial {dial}>{escape(number)}</Dial>")

    def voicemail_markup(self, *, action_url: str, language: str) -> str:
        say = _attrs({"language": language or "en-US"})
        rec = _attrs({"maxLength": VOICEMAIL_SECONDS, "playBeep": "true", "action": action_url})
        return _xml(f"<Say {say}>Nobody could take your call. Please leave a message after the tone.</Say>"
                    f"<Record {rec}/>")

    def say_and_hang_up(self, text: str, *, language: str = "en-US") -> str:
        return _xml(f"<Say {_attrs({'language': language or 'en-US'})}>{escape(text)}</Say><Hangup/>")

    def hang_up_markup(self) -> str:
        return _xml("<Hangup/>")

    async def hangup(self, call_sid: str) -> None:
        code, body = await self._request("POST", f"/Calls/{call_sid}.json", data={"Status": "completed"})
        if code >= 300:
            log.warning("hang-up refused for a call (%s): %s", code, body.get("message"))

    async def start_recording(self, call_sid: str, *, status_url: str) -> str:
        code, body = await self._request("POST", f"/Calls/{call_sid}/Recordings.json", data={
            "RecordingChannels": "dual", "RecordingStatusCallback": status_url,
            "RecordingStatusCallbackMethod": "POST"})
        if code >= 300:
            raise self._refusal(code, body, "record calls")
        return str(body.get("sid") or "")

    # ── Signed webhooks (V6, VOX1a P3) ───────────────────────────────────────
    # SharedKeys (public beta, 2026-09-08) sign the HTTP webhooks with a key we hold, so the auth token is never
    # needed. Measured: every voice webhook is signed (HMAC-SHA256); the ConversationRelay WebSocket handshake
    # is not (it stays on the auth token), so the socket keeps its one-time token. Only a Main or Standard key
    # may create one; a Restricted key is refused, and the number then runs on its tokens alone.

    async def _webhooks(self, method: str, path: str, **kw) -> tuple[int, dict]:
        url = f"{WEBHOOKS_API}{path}"
        if self._http is not None:
            resp = await self._http.request(method, url, auth=self._auth, **kw)
        else:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.request(method, url, auth=self._auth, **kw)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        return resp.status_code, body if isinstance(body, dict) else {}

    async def start_signing(self, prefix: str, *, name: str) -> dict:
        """A SharedKey, a Setting that signs with it, and a Rule for URLs under `prefix`.

        Returns ``{"secret", "shared_key_id", "setting_id", "rule_id"}``. Raises `CallRefused` (naming the
        permission) when the key may not create them; anything made before the refusal is removed.
        """
        made: dict = {}
        try:
            code, key = await self._webhooks("POST", "/SharedKeys", json={"friendlyName": name})
            if code != 201:
                raise self._refusal(code, key, "create signing keys (use a Standard API key)")
            made.update(shared_key_id=key.get("id") or "", secret=key.get("secret") or "")
            code, setting = await self._webhooks("POST", "/Settings", json={
                "friendlyName": name, "auth": {"source": "INLINE", "value": {"scheme": "NONE"}},
                "signature": {"type": "SHARED_KEY", "sharedKeyId": made["shared_key_id"], "algorithm": "HMAC_SHA256"}})
            if code not in (200, 201):
                raise self._refusal(code, setting, "create webhook settings")
            made["setting_id"] = setting.get("id") or ""
            code, rule = await self._webhooks("POST", "/Rules", json={
                "matchType": "PREFIX_MATCH", "prefix": prefix,
                "settings": [{"settingId": made["setting_id"], "percentage": 100}]})
            if code not in (200, 201, 202):
                raise self._refusal(code, rule, "create webhook rules")
            made["rule_id"] = (rule.get("rule") or {}).get("id") or (rule.get("operation") or {}).get("resourceId") \
                or rule.get("id") or ""
            return made
        except Exception:
            await self.stop_signing(made)
            raise

    def verify_webhook(self, made: dict, *, url: str, params: dict, headers) -> bool:
        return bool((made or {}).get("secret")) and verify_signature(made["secret"], url, params, headers)

    async def stop_signing(self, made: dict) -> list[str]:
        """Remove what `start_signing` made, rule first (a referenced key cannot be deleted). Never raises.

        Returns the keys of `made` Twilio would not remove: a trial account may create these but not delete them
        (20003), and an account holds at most 50 SharedKeys, so the person is told rather than left guessing.
        """
        left: list[str] = []
        for path, key in (("/Rules/", "rule_id"), ("/Settings/", "setting_id"), ("/SharedKeys/", "shared_key_id")):
            if (made or {}).get(key):
                try:
                    code, body = await self._webhooks("DELETE", path + made[key])
                    if code >= 300 and code != 404:
                        log.warning("could not remove a webhook %s (%s): %s", key, code, body.get("message"))
                        left.append(key)
                except Exception as exc:  # noqa: BLE001 — cleanup is best-effort; the log says what is left
                    log.warning("could not remove a webhook %s: %s", key, exc)
                    left.append(key)
        return left

    # ── Numbers ──────────────────────────────────────────────────────────────

    async def list_numbers(self) -> list[Number]:
        code, body = await self._request("GET", "/IncomingPhoneNumbers.json", params={"PageSize": 200})
        if code != 200:
            raise self._refusal(code, body, "read incoming phone numbers")
        return [Number(sid=n.get("sid") or "", number=n.get("phone_number") or "",
                       voice_url=n.get("voice_url") or "", voice_application_sid=n.get("voice_application_sid") or "")
                for n in body.get("incoming_phone_numbers") or []]

    async def point_number(self, number_sid: str, *, voice_url: str, status_url: str) -> dict:
        code, cur = await self._request("GET", f"/IncomingPhoneNumbers/{number_sid}.json")
        if code != 200:
            raise self._refusal(code, cur, "read incoming phone numbers")
        previous = {k: cur.get(k) or "" for k in (
            "voice_url", "voice_method", "voice_fallback_url", "status_callback", "voice_application_sid")}
        code, body = await self._request("POST", f"/IncomingPhoneNumbers/{number_sid}.json", data={
            "VoiceUrl": voice_url, "VoiceMethod": "POST", "StatusCallback": status_url,
            "StatusCallbackMethod": "POST", "VoiceApplicationSid": ""})
        if code != 200:
            raise self._refusal(code, body, "change incoming phone numbers")
        log.info("pointed %s at the voice channel", mask_number(cur.get("phone_number")))
        return previous

    async def restore_number(self, number_sid: str, previous: dict) -> None:
        data = {"VoiceUrl": previous.get("voice_url") or "", "VoiceMethod": previous.get("voice_method") or "POST",
                "StatusCallback": previous.get("status_callback") or "",
                "VoiceApplicationSid": previous.get("voice_application_sid") or ""}
        code, body = await self._request("POST", f"/IncomingPhoneNumbers/{number_sid}.json", data=data)
        if code != 200:
            raise self._refusal(code, body, "change incoming phone numbers")
