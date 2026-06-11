"""
Webhook delivery with HMAC-SHA256 signing and exponential-backoff retry.
"""
import asyncio
import hashlib
import hmac
import json
import time


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
    Signs with HMAC-SHA256 if secret is provided (X-Synapse-Signature header).
    Returns True on 2xx response, False after max_retries exhausted.
    """
    import httpx

    body = json.dumps(payload, default=str).encode()

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Synapse-Webhook/1.0",
        "X-Synapse-Timestamp": str(int(time.time())),
    }

    if secret:
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Synapse-Signature"] = f"sha256={sig}"

    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(webhook_url, content=body, headers=headers)
                if resp.status_code < 300:
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

    return False
