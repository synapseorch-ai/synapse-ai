"""The two tokens that verify a call without the account's auth token (V6).

* **The route token** names the channel in every URL Twilio is given (``…/webhook/{route_token}/…``). It is
  minted once per channel, kept server-only, and compared in constant time.
* **The socket token** is in the WebSocket URL of the TwiML we return to a verified call only. It is signed
  (HMAC over channel, CallSid, expiry and a nonce), so any process holding the signing key can check it, and it is
  single-use through the host's ``claim_socket`` (in memory here; cloud's is shared). It is valid for 60 s.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

#: How long the TwiML's socket URL may be used after we hand it out.
SOCKET_TOKEN_TTL = 60


def new_route_token() -> str:
    return secrets.token_urlsafe(24)


def route_token_matches(given: str, stored: str | None) -> bool:
    return bool(stored) and bool(given) and hmac.compare_digest(given.encode(), str(stored).encode())


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def socket_token(key: bytes, *, channel_id: str, call_sid: str, resume: bool = False,
                 now: float | None = None) -> str:
    """A signed, one-time token for this call's socket."""
    body = {"c": channel_id, "s": call_sid, "e": int((now or time.time()) + SOCKET_TOKEN_TTL),
            "n": secrets.token_hex(8), "r": bool(resume)}
    payload = _b64(json.dumps(body, separators=(",", ":")).encode())
    sig = _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


class BadSocketToken(Exception):
    """The socket's token is forged, stale, for another channel, or already used."""


def read_socket_token(key: bytes, token: str, *, channel_id: str, now: float | None = None) -> dict:
    """The token's claims if it is genuine, unexpired and for this channel. Raises `BadSocketToken`.

    Single use is the caller's to enforce (``VoiceHost.claim_socket(nonce)``), because cloud's tasks share it.
    """
    try:
        payload, sig = token.split(".", 1)
        want = _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, want):
            raise BadSocketToken("signature")
        body = json.loads(_unb64(payload))
    except (ValueError, TypeError) as exc:
        raise BadSocketToken("malformed") from exc
    if body.get("c") != channel_id:
        raise BadSocketToken("channel")
    if int(body.get("e") or 0) < (now or time.time()):
        raise BadSocketToken("expired")
    return {"channel_id": body["c"], "call_sid": body.get("s") or "", "nonce": body.get("n") or "",
            "resume": bool(body.get("r"))}
