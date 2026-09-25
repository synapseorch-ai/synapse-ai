"""
Webhook mode's pure halves: signature verification, payload parsing, senders.

These are the functions every receiver leans on — the engine's generic route
and any embedder's HTTP layer — so they are held to their platform's documented
rules with real crypto, not mocks: the Slack HMAC is computed, the Discord
signature is made with a real Ed25519 key.
"""
import hashlib
import hmac
import json

import pytest

from core.messaging import inbound, senders, verify


# ── Slack signatures ─────────────────────────────────────────────────────────

SECRET = "8f742231b10e8888abcd99yyyzzz85a5"


def _slack_sign(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(
        secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
    ).hexdigest()
    return f"v0={digest}"


def test_slack_accepts_its_own_signature():
    body = b'{"type":"event_callback"}'
    ts = "1700000000"
    sig = _slack_sign(SECRET, ts, body)
    assert verify.verify_slack(SECRET, ts, sig, body, now=1700000000.0)


def test_slack_rejects_wrong_secret_and_tampered_body():
    body = b'{"type":"event_callback"}'
    ts = "1700000000"
    sig = _slack_sign(SECRET, ts, body)
    assert not verify.verify_slack("other-secret", ts, sig, body, now=1700000000.0)
    assert not verify.verify_slack(SECRET, ts, sig, body + b" ", now=1700000000.0)


def test_slack_rejects_a_replayed_timestamp():
    body = b"{}"
    ts = "1700000000"
    sig = _slack_sign(SECRET, ts, body)
    late = 1700000000.0 + verify.SLACK_TIMESTAMP_TOLERANCE_SECONDS + 1
    assert not verify.verify_slack(SECRET, ts, sig, body, now=late)


def test_slack_rejects_garbage_without_raising():
    assert not verify.verify_slack(SECRET, "not-a-number", "v0=zz", b"{}")
    assert not verify.verify_slack("", "1700000000", "v0=zz", b"{}")


# ── Discord signatures ───────────────────────────────────────────────────────

def _discord_keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
    )

    private = Ed25519PrivateKey.generate()
    public_hex = private.public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw
    ).hex()
    return private, public_hex


def test_discord_accepts_a_real_signature():
    private, public_hex = _discord_keypair()
    body = b'{"type":1}'
    ts = "1700000000"
    sig = private.sign(ts.encode() + body).hex()
    assert verify.verify_discord(public_hex, sig, ts, body)


def test_discord_rejects_wrong_key_and_garbage():
    private, _ = _discord_keypair()
    _, other_public = _discord_keypair()
    body = b'{"type":1}'
    ts = "1700000000"
    sig = private.sign(ts.encode() + body).hex()
    assert not verify.verify_discord(other_public, sig, ts, body)
    assert not verify.verify_discord("zz-not-hex", sig, ts, body)
    assert not verify.verify_discord(other_public, "zz-not-hex", ts, body)


# ── Telegram secret token ────────────────────────────────────────────────────

def test_telegram_compares_the_shared_secret():
    assert verify.verify_telegram("s3cret", "s3cret")
    assert not verify.verify_telegram("s3cret", "wrong")
    assert not verify.verify_telegram("", "")


def test_a_non_ascii_header_is_refused_not_raised():
    """F224: `hmac.compare_digest` raises on a non-ASCII str, and every one of
    these headers is the sender's to choose — the route answered 500."""
    import time

    ts = str(int(time.time()))
    assert not verify.verify_telegram("s3cret", "sé")
    assert not verify.verify_slack(SECRET, ts, "v0=é", b"{}")


# ── Meta (WhatsApp Cloud API) signatures ─────────────────────────────────────

def _meta_sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_meta_accepts_its_own_signature():
    body = b'{"object":"whatsapp_business_account","entry":[]}'
    assert verify.verify_meta("app-secret", _meta_sign("app-secret", body), body)


def test_meta_rejects_wrong_secret_tampered_body_and_garbage():
    body = b'{"entry":[]}'
    good = _meta_sign("app-secret", body)
    assert not verify.verify_meta("other-secret", good, body)
    assert not verify.verify_meta("app-secret", good, body + b" ")
    # The bare hex without Meta's `sha256=` prefix is not what Meta sends.
    assert not verify.verify_meta("app-secret", good.removeprefix("sha256="), body)
    assert not verify.verify_meta("app-secret", "sha256=zz", body)
    assert not verify.verify_meta("app-secret", "sha256=é", body)
    assert not verify.verify_meta("app-secret", "", body)
    # No secret configured is "cannot verify", never "verified" — even against
    # a signature made with the empty key.
    assert not verify.verify_meta("", _meta_sign("", body), body)


# ── Payload parsing ──────────────────────────────────────────────────────────

def test_telegram_update_parses_to_a_message():
    update = {
        "update_id": 991,
        "message": {
            "text": "hello",
            "chat": {"id": 4242},
            "from": {"id": 77},
        },
    }
    msg = inbound.parse("telegram", update)
    assert msg.conversation_id == "4242"
    assert msg.user_id == "77"
    assert msg.text == "hello"
    assert msg.event_id == "991"


def test_telegram_non_message_updates_are_noise():
    assert inbound.parse("telegram", {"update_id": 1}) is None
    assert inbound.parse("telegram", {"update_id": 1, "message": {"chat": {"id": 1}}}) is None


def test_slack_dm_parses_and_bot_echo_does_not():
    def payload(**event):
        return {
            "type": "event_callback",
            "event_id": "Ev123",
            "team_id": "T999",
            "event": {"type": "message", "channel_type": "im", "channel": "D1",
                      "user": "U1", "ts": "1.2", **event},
        }

    msg = inbound.parse("slack", payload(text="hi there"))
    assert (msg.conversation_id, msg.user_id, msg.text) == ("D1", "U1", "hi there")
    assert msg.external_workspace_id == "T999"
    assert msg.thread_ts == "1.2"

    assert inbound.parse("slack", payload(text="hi", bot_id="B1")) is None
    assert inbound.parse("slack", payload(text="hi", subtype="message_changed")) is None


def test_slack_mention_strips_the_bot_and_url_verification_answers():
    payload = {
        "type": "event_callback",
        "event_id": "Ev124",
        "team_id": "T999",
        "event": {"type": "app_mention", "channel": "C7", "user": "U2",
                  "text": "<@U0BOT> summarize this", "ts": "9.9"},
    }
    msg = inbound.parse("slack", payload)
    assert msg.text == "summarize this"
    assert msg.conversation_id == "C7"

    assert inbound.slack_url_verification(
        {"type": "url_verification", "challenge": "abc"}
    ) == "abc"
    assert inbound.slack_url_verification(payload) is None


def test_discord_ping_and_slash_command():
    assert inbound.discord_is_ping({"type": 1})

    payload = {
        "type": 2,
        "id": "555",
        "application_id": "app1",
        "token": "tok",
        "guild_id": "G1",
        "channel_id": "C1",
        "member": {"user": {"id": "U9"}},
        "data": {"name": "ask", "options": [{"name": "message", "value": "what's up"}]},
    }
    msg = inbound.parse("discord", payload)
    assert (msg.conversation_id, msg.user_id, msg.text) == ("C1", "U9", "what's up")
    assert msg.interaction == {"application_id": "app1", "token": "tok"}
    assert msg.external_workspace_id == "G1"
    assert msg.event_id == "555"


# ── Senders (httpx faked at the seam) ────────────────────────────────────────

class _Response:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = text or json.dumps(self._body)

    def json(self):
        return self._body


class _FakeClient:
    """Stands in for httpx.AsyncClient; records calls, replays canned answers."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)

    async def patch(self, url, **kwargs):
        self.calls.append(("PATCH", url, kwargs))
        return self.responses.pop(0)


@pytest.fixture
def fake_client(monkeypatch):
    def install(*responses):
        client = _FakeClient(responses)
        monkeypatch.setattr(senders, "_client", lambda: client)
        return client

    return install


async def test_telegram_send_falls_back_to_plain_text_on_parse_error(fake_client):
    client = fake_client(
        _Response(400, {"description": "can't parse entities"}),
        _Response(200, {"ok": True}),
    )
    await senders.send_telegram_raw({"bot_token": "T"}, "42", "*bad*")
    assert len(client.calls) == 2
    assert "parse_mode" in client.calls[0][2]["json"]
    assert "parse_mode" not in client.calls[1][2]["json"]


async def test_slack_send_reads_failure_from_the_body_not_the_status(fake_client):
    fake_client(_Response(200, {"ok": False, "error": "channel_not_found"}))
    with pytest.raises(senders.SendError, match="channel_not_found"):
        await senders.send_slack_raw({"bot_token": "xoxb"}, "C1", "hi")


async def test_discord_send_uses_the_bot_token(fake_client):
    client = fake_client(_Response(200, {}))
    await senders.send_discord_raw({"bot_token": "B"}, "C1", "hi")
    method, url, kwargs = client.calls[0]
    assert url.endswith("/channels/C1/messages")
    assert kwargs["headers"]["Authorization"] == "Bot B"


async def test_formatted_send_chunks_and_raises_for_unknown_platform(fake_client):
    fake_client(_Response(200, {"ok": True}))
    await senders.send("slack", {"bot_token": "x"}, "C1", "hello")
    with pytest.raises(senders.SendError):
        await senders.send("teams", {}, "C1", "hello")
    assert senders.supports_rest_send("telegram")
    assert not senders.supports_rest_send("whatsapp")


async def test_missing_token_is_an_error_before_any_request(fake_client):
    client = fake_client()
    with pytest.raises(senders.SendError, match="bot_token"):
        await senders.send_telegram_raw({}, "42", "hi")
    assert client.calls == []
