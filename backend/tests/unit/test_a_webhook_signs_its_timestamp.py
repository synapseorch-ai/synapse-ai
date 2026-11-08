"""A webhook's signature covers its timestamp (F425).

`X-Synapse-Signature` is HMAC-SHA256 of the body alone, and `X-Synapse-Timestamp`
rides outside it. A receiver that follows the usual advice (refuse a delivery
older than five minutes) was checking a header anyone could rewrite: C2 replayed
a captured delivery's body with a fresh timestamp and it verified.

`X-Synapse-Signature-V2` signs `"{timestamp}.{body}"`; the old header stays, as
it was, for receivers written against it. Each attempt is signed afresh, so a
retry carries its own time (cloud's sender already did; this one reused the
first attempt's). Cloud restates this signer byte for byte
(`api/control/webhook_signing.py`), and its test compares the two.
"""
import hashlib
import hmac
import json

import httpx
import pytest

from core.scale import webhook

SECRET = "s3cret"
PAYLOAD = {"run_id": "run_1", "status": "completed", "shared_state": {"a": 1}}


def _mac(data: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), data, hashlib.sha256).hexdigest()


@pytest.fixture
def posts(monkeypatch):
    """Every POST the sender makes, answered by `answers` in turn (then 200)."""
    sent: list[dict] = []
    answers: list[int] = []

    class Capture:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, url, content, headers):
            sent.append({"body": content, "headers": dict(headers)})
            return httpx.Response(answers.pop(0) if answers else 200)

    async def no_wait(_):
        return None

    monkeypatch.setattr(httpx, "AsyncClient", Capture)
    monkeypatch.setattr(webhook.asyncio, "sleep", no_wait)
    sent_answers = (sent, answers)
    return sent_answers


async def test_v2_signs_the_timestamp_and_the_body(posts):
    sent, _ = posts
    assert await webhook.deliver_webhook("http://x", PAYLOAD, secret=SECRET)
    [one] = sent
    body, h = one["body"], one["headers"]
    assert body == json.dumps(PAYLOAD, default=str).encode()
    assert h["X-Synapse-Signature-V2"] == _mac(h["X-Synapse-Timestamp"].encode() + b"." + body)


async def test_the_old_header_is_kept_for_receivers_written_against_it(posts):
    sent, _ = posts
    await webhook.deliver_webhook("http://x", PAYLOAD, secret=SECRET)
    [one] = sent
    assert one["headers"]["X-Synapse-Signature"] == _mac(one["body"])


async def test_a_replayed_body_under_a_new_timestamp_does_not_verify(posts):
    sent, _ = posts
    await webhook.deliver_webhook("http://x", PAYLOAD, secret=SECRET)
    [one] = sent
    forged = str(int(one["headers"]["X-Synapse-Timestamp"]) + 3600).encode()
    assert one["headers"]["X-Synapse-Signature-V2"] != _mac(forged + b"." + one["body"])


async def test_each_attempt_is_signed_with_its_own_time(posts, monkeypatch):
    sent, answers = posts
    answers.extend([500, 503])
    clock = iter([1_791_000_000, 1_791_000_002, 1_791_000_006])
    monkeypatch.setattr(webhook.time, "time", lambda: next(clock))
    assert await webhook.deliver_webhook("http://x", PAYLOAD, secret=SECRET)
    assert [s["headers"]["X-Synapse-Timestamp"] for s in sent] == ["1791000000", "1791000002", "1791000006"]
    for s in sent:
        ts = s["headers"]["X-Synapse-Timestamp"].encode()
        assert s["headers"]["X-Synapse-Signature-V2"] == _mac(ts + b"." + s["body"])


async def test_no_secret_no_signature(posts):
    sent, _ = posts
    await webhook.deliver_webhook("http://x", PAYLOAD)
    [one] = sent
    assert "X-Synapse-Signature" not in one["headers"]
    assert "X-Synapse-Signature-V2" not in one["headers"]
    assert one["headers"]["X-Synapse-Timestamp"].isdigit()


def test_the_tolerance_receivers_are_told_is_five_minutes():
    assert webhook.SIGNATURE_TOLERANCE_SECONDS == 300
