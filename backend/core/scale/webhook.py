"""
Webhook delivery with HMAC-SHA256 signing and exponential-backoff retry.

Each attempt carries `X-Synapse-Timestamp` (its send time, whole seconds) and,
when a secret was given, two signatures:

- `X-Synapse-Signature-V2: sha256=<HMAC-SHA256(secret, "{timestamp}.{body}")>`
  covers the timestamp too (F425). A receiver recomputes it over the raw body
  and the header's timestamp, compares in constant time, and refuses a delivery
  whose timestamp is more than `SIGNATURE_TOLERANCE_SECONDS` from its own clock:
  a captured delivery replayed later then fails, whatever timestamp it is given.
- `X-Synapse-Signature: sha256=<HMAC-SHA256(secret, body)>`, the first scheme,
  kept as it was for receivers written against it. It does not cover the
  timestamp, so a receiver that checks only it cannot tell a replay.

Cloud restates this byte for byte (`api/control/webhook_signing.py`); its test
signs the same payload both ways and compares the headers.
"""
import asyncio
import hashlib
import hmac
import json
import time

from core.scale.metrics import record_webhook

#: How far from its own clock a receiver should accept `X-Synapse-Timestamp`.
SIGNATURE_TOLERANCE_SECONDS = 300


def signed_headers(body: bytes, secret: str | None) -> dict[str, str]:
    """The headers one attempt is sent with, signed at the time it is sent."""
    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Synapse-Webhook/1.0",
        "X-Synapse-Timestamp": timestamp,
    }
    if secret:
        key = secret.encode()
        sig = hmac.new(key, body, hashlib.sha256).hexdigest()
        headers["X-Synapse-Signature"] = f"sha256={sig}"
        sig_v2 = hmac.new(key, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
        headers["X-Synapse-Signature-V2"] = f"sha256={sig_v2}"
    return headers


async def deliver_webhook(
    webhook_url: str,
    payload: dict,
    secret: str | None = None,
    max_retries: int = 3,
    backoff_base: float = 2.0,
    timeout: float = 30.0,
) -> bool:
    """
    POST payload to webhook_url.
    Signs each attempt with HMAC-SHA256 if secret is provided (`signed_headers`).
    Returns True on 2xx response, False after max_retries exhausted.
    """
    import httpx

    body = json.dumps(payload, default=str).encode()

    for attempt in range(max_retries):
        # Signed afresh: a retry carries its own time, so a receiver's
        # tolerance applies to when it was sent, not to the first try.
        headers = signed_headers(body, secret)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(webhook_url, content=body, headers=headers)
                if resp.status_code < 300:
                    record_webhook(True)
                    return True
                print(
                    f"[webhook] {webhook_url} returned {resp.status_code} "
                    f"(attempt {attempt + 1}/{max_retries})",
                    flush=True,
                )
        except Exception as e:
            print(
                f"[webhook] {webhook_url} error: {e} "
                f"(attempt {attempt + 1}/{max_retries})",
                flush=True,
            )

        if attempt < max_retries - 1:
            delay = backoff_base ** attempt
            await asyncio.sleep(delay)

    record_webhook(False)
    return False
