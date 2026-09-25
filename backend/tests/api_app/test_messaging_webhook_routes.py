"""
Teams and WhatsApp deliveries reach their own webhook routes (F206).

The generic `POST /api/messaging/{platform}/webhook/{channel_id}` was registered
before the Teams and WhatsApp routes, and FastAPI matches in registration
order — so a real Teams or WhatsApp delivery was answered `404 "Unknown
platform"` by the generic route, and neither platform could ever take an
inbound message (both are webhook-only). What these tests hold:

* the specific routes are registered first, and a delivery reaches them;
* a Teams delivery hands the Bot Framework its `Authorization` header, and a
  refused token is a 401 — the route used to pass the body where the SDK wants
  the header, so it could never have authenticated anything;
* a WhatsApp delivery is refused unless Meta's `X-Hub-Signature-256` matches
  the channel's App Secret, checked on the raw bytes before anything is parsed —
  the route used to check nothing, so opening it alone would have let anyone
  who learned the URL speak as any phone number.

The manager is put on `app.state` by hand, as in the assignment tests: the test
app runs no lifespan.
"""
import asyncio
import hashlib
import hmac
import json

import pytest
import pytest_asyncio

from core.messaging import store as channel_store


@pytest_asyncio.fixture
async def manager(test_app):
    import core.server as server
    from core.messaging.manager import MessagingManager

    m = MessagingManager(server_module=server)
    test_app.state.messaging_manager = m
    try:
        yield m
    finally:
        test_app.state.messaging_manager = None


class _FakeTeams:
    def __init__(self, refuse: bool = False):
        self.calls = []
        self.refuse = refuse

    async def process_request(self, auth_header, body):
        self.calls.append((auth_header, body))
        if self.refuse:
            raise PermissionError("Unauthorized Access. Request is not authorized")
        return None


class _FakeWhatsApp:
    def __init__(self):
        self.payloads = []

    async def process_webhook(self, data):
        self.payloads.append(data)


def _meta_signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def _whatsapp_channel(app_secret: str | None = "s3cret"):
    creds = {"whatsapp_mode": "meta_api", "phone_number_id": "1", "access_token": "EAA"}
    if app_secret is not None:
        creds["app_secret"] = app_secret
    return await channel_store.save_channel(
        {"name": "WA", "platform": "whatsapp", "credentials": creds}
    )


async def _eventually(predicate, timeout: float = 2.0):
    """The WhatsApp route answers first and dispatches in the background."""
    deadline = asyncio.get_event_loop().time() + timeout
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            return False
        await asyncio.sleep(0.02)
    return True


def test_specific_routes_are_registered_before_the_generic_one():
    from core.routes.messaging import router

    posts = [r.path for r in router.routes if "POST" in getattr(r, "methods", set())]
    generic = posts.index("/api/messaging/{platform}/webhook/{channel_id}")
    assert posts.index("/api/messaging/teams/webhook/{channel_id}") < generic
    assert posts.index("/api/messaging/whatsapp/webhook/{channel_id}") < generic


class TestTeams:
    async def test_a_delivery_reaches_the_adapter_with_its_token(self, client, manager):
        fake = _FakeTeams()
        manager.register_teams_adapter("chn_teams1", fake)
        activity = {"type": "message", "text": "hello", "conversation": {"id": "c1"}}

        resp = await client.post(
            "/api/messaging/teams/webhook/chn_teams1",
            json=activity,
            headers={"Authorization": "Bearer eyJ.token"},
        )

        assert resp.status_code == 200, resp.text
        assert fake.calls == [("Bearer eyJ.token", activity)]

    async def test_a_refused_token_is_a_401(self, client, manager):
        manager.register_teams_adapter("chn_teams2", _FakeTeams(refuse=True))

        resp = await client.post(
            "/api/messaging/teams/webhook/chn_teams2",
            json={"type": "message"},
            headers={"Authorization": "Bearer forged"},
        )

        assert resp.status_code == 401

    async def test_a_channel_that_is_not_running_is_a_404(self, client, manager):
        resp = await client.post("/api/messaging/teams/webhook/chn_nope", json={})
        assert resp.status_code == 404
        assert resp.json()["detail"] != "Unknown platform"


async def test_the_teams_adapter_gives_the_sdk_the_token_and_an_activity():
    """The adapter half, where the Bot Framework SDK is installed (the
    messaging extras). Measured against botbuilder 4.17.1 on 2026-09-25: an
    empty or forged token raises `PermissionError` from `process_activity`;
    the old call — the body where the header goes — raised `AttributeError`."""
    pytest.importorskip("botbuilder.schema")
    from botbuilder.schema import Activity

    from core.messaging.adapters.teams import TeamsAdapter

    class _Bot:
        async def process_activity(self, auth, activity, logic):
            self.call = (auth, activity, logic)

    adapter = TeamsAdapter(
        {"id": "chn_t", "name": "T", "platform": "teams",
         "credentials": {"app_id": "a", "app_password": "p"}},
        manager=None,
    )
    adapter._adapter_bot = _Bot()
    await adapter.process_request("Bearer t", {"type": "message", "text": "hi"})

    auth, activity, logic = adapter._adapter_bot.call
    assert auth == "Bearer t"
    assert isinstance(activity, Activity) and activity.text == "hi"
    assert logic == adapter._on_turn


class TestWhatsApp:
    async def test_a_signed_delivery_is_dispatched(self, client, manager):
        ch = await _whatsapp_channel()
        fake = _FakeWhatsApp()
        manager.register_whatsapp_adapter(ch["id"], fake)
        body = json.dumps({"entry": [{"changes": [{"value": {"messages": []}}]}]}).encode()

        resp = await client.post(
            f"/api/messaging/whatsapp/webhook/{ch['id']}",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _meta_signature("s3cret", body),
            },
        )

        assert resp.status_code == 200, resp.text
        assert await _eventually(lambda: fake.payloads == [json.loads(body)])

    async def test_an_unsigned_or_forged_delivery_is_refused(self, client, manager):
        ch = await _whatsapp_channel()
        fake = _FakeWhatsApp()
        manager.register_whatsapp_adapter(ch["id"], fake)
        body = b'{"entry": []}'

        for headers in (
            {},
            {"X-Hub-Signature-256": _meta_signature("wrong", body)},
            {"X-Hub-Signature-256": "garbage"},
        ):
            resp = await client.post(
                f"/api/messaging/whatsapp/webhook/{ch['id']}",
                content=body,
                headers={"Content-Type": "application/json", **headers},
            )
            assert resp.status_code == 401, headers

        # A signature over other bytes does not cover these.
        resp = await client.post(
            f"/api/messaging/whatsapp/webhook/{ch['id']}",
            content=b'{"entry": [1]}',
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _meta_signature("s3cret", body),
            },
        )
        assert resp.status_code == 401
        await asyncio.sleep(0.05)
        assert fake.payloads == []

    async def test_a_channel_without_an_app_secret_says_so(self, client, manager):
        ch = await _whatsapp_channel(app_secret=None)
        manager.register_whatsapp_adapter(ch["id"], _FakeWhatsApp())
        body = b'{"entry": []}'

        resp = await client.post(
            f"/api/messaging/whatsapp/webhook/{ch['id']}",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _meta_signature("", body),
            },
        )

        assert resp.status_code == 401
        assert "App Secret" in resp.json()["detail"]

    async def test_another_platforms_channel_is_not_found(self, client, manager):
        ch = await channel_store.save_channel(
            {"name": "TG", "platform": "telegram", "credentials": {"bot_token": "1:x"}}
        )
        resp = await client.post(
            f"/api/messaging/whatsapp/webhook/{ch['id']}", content=b"{}",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 404

    async def test_the_verify_handshake_still_answers(self, client, manager):
        from core.messaging.adapters.whatsapp import WhatsAppAdapter

        ch = await _whatsapp_channel()
        adapter = WhatsAppAdapter(ch, manager)
        manager.register_whatsapp_adapter(ch["id"], adapter)

        resp = await client.get(
            f"/api/messaging/whatsapp/webhook/{ch['id']}",
            params={"hub.mode": "subscribe", "hub.verify_token": "synapse_verify",
                    "hub.challenge": "42"},
        )

        assert resp.status_code == 200
        assert resp.text == "42"
