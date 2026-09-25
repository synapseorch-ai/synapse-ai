"""
Webhook signature verification — pure functions, importable from anywhere.

Each platform authenticates a webhook delivery differently, and every receiver
— the engine's own routes in webhook mode, or an embedder's HTTP layer that
imports no other engine code — has to apply exactly the same rules. Pure
``bytes + secret -> bool`` functions are the shape that allows both: nothing
here touches the store, the settings, or an event loop.

The rules implemented, per platform:

* **Slack** signs ``v0:{timestamp}:{body}`` with the app's signing secret
  (HMAC-SHA256, hex, prefixed ``v0=``). The timestamp is bounded to five
  minutes to stop replay of a captured delivery.
* **Discord** signs ``{timestamp}{body}`` with the application's Ed25519 key;
  the public half is on the developer portal. ``cryptography`` is imported
  lazily — it is not a declared dependency of the engine, only ever present
  transitively, and a missing library must read as "cannot verify", never as
  "verified".
* **Telegram** has no signature at all. What it offers is a ``secret_token``
  registered at ``setWebhook`` time and echoed back in the
  ``X-Telegram-Bot-Api-Secret-Token`` header — a shared secret compared in
  constant time. The webhook URL itself should also be unguessable; this check
  is the second factor, not the first.
* **Meta** (WhatsApp Cloud API) signs the raw body with the app secret
  (HMAC-SHA256, hex, prefixed ``sha256=``) in ``X-Hub-Signature-256``. There
  is no timestamp, so there is no replay window to bound.

Every function returns ``False`` rather than raising on malformed input: to a
receiver, "the header was garbage" and "the signature was wrong" call for the
same 401, and an exception on attacker-controlled bytes is a foothold.
"""
from __future__ import annotations

import hashlib
import hmac
import time

#: Slack's own documented replay window.
SLACK_TIMESTAMP_TOLERANCE_SECONDS = 60 * 5


def verify_slack(
    signing_secret: str,
    timestamp: str,
    signature: str,
    body: bytes,
    *,
    now: float | None = None,
) -> bool:
    """True when ``signature`` is Slack's HMAC for ``body`` at ``timestamp``."""
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs((now if now is not None else time.time()) - ts) > SLACK_TIMESTAMP_TOLERANCE_SECONDS:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    digest = hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"v0={digest}", signature)


def verify_discord(public_key_hex: str, signature_hex: str, timestamp: str, body: bytes) -> bool:
    """True when ``signature_hex`` is a valid Ed25519 signature over
    ``timestamp + body`` for the application whose public key is given."""
    if not public_key_hex or not signature_hex or timestamp is None:
        return False
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return False
    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        key.verify(bytes.fromhex(signature_hex), timestamp.encode() + body)
        return True
    except (InvalidSignature, ValueError):
        return False


def verify_telegram(secret_token: str, header_token: str) -> bool:
    """Constant-time comparison of the ``setWebhook`` secret against the header."""
    if not secret_token or not header_token:
        return False
    return hmac.compare_digest(secret_token, header_token)


def verify_meta(app_secret: str, signature: str, body: bytes) -> bool:
    """True when ``signature`` is Meta's ``X-Hub-Signature-256`` for ``body``."""
    if not app_secret or not signature:
        return False
    digest = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    # Bytes, not str: `compare_digest` raises on a non-ASCII str, and the
    # header is the sender's to choose.
    return hmac.compare_digest(f"sha256={digest}".encode(), signature.encode("utf-8", "replace"))
