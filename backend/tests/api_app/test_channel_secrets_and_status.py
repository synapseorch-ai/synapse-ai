"""
A channel with a bad token says so, and no channel response carries a secret
(F207).

Measured by the OSS test pass: a Telegram channel with a bogus token showed
RUNNING for ever, `last_error: null`, nothing logged — polling started in a
background task, `start()` returned at once, and the task's `InvalidToken` died
unread; only TEST revealed "Unauthorized". And `GET /api/messaging/channels`
served `credentials.bot_token` (Teams' `app_password` too), F135's and F201's
class.

Now the token is checked before the adapter counts as running (Telegram's
`getMe`, Discord's login, Slack's `auth.test`), a background task that dies
marks its channel `error` with the reason, and every channel response blanks
secret credentials and says which are set — a blank secret on save keeps the
stored one, since the screen no longer has it to send back.
"""
import asyncio

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
        for cid in list(m._adapters):
            await m.stop_channel(cid)
        test_app.state.messaging_manager = None


async def _telegram(token="123456:bogus-token"):
    return await channel_store.save_channel({
        "name": "TG", "platform": "telegram", "mode": "polling",
        "credentials": {"bot_token": token, "webhook_secret": "minted-by-the-server"},
    })


async def _settled(channel_id, until, timeout=2.0):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        ch = await channel_store.get_channel(channel_id)
        if until(ch) or loop.time() > deadline:
            return ch
        await asyncio.sleep(0.02)


class TestNoResponseCarriesASecret:
    async def test_the_list_blanks_secrets_and_says_which_are_set(self, client, manager):
        ch = await channel_store.save_channel({
            "name": "Discord", "platform": "discord",
            "credentials": {"bot_token": "MTE.secret", "channel_id": "12345"},
        })
        await _telegram()

        listed = (await client.get("/api/messaging/channels")).json()

        body = str(listed)
        assert "MTE.secret" not in body and "bogus-token" not in body
        assert "minted-by-the-server" not in body and "webhook_secret" not in body
        discord = next(c for c in listed if c["id"] == ch["id"])
        assert discord["credentials"]["bot_token"] == ""
        assert discord["credentials"]["channel_id"] == "12345"   # not a secret
        assert discord["credentials_set"] == {"bot_token": True}

    async def test_the_save_and_enable_answers_carry_none(self, client, manager, monkeypatch):
        import core.messaging.manager as mgr

        async def no_start(self, channel_id):
            return None

        monkeypatch.setattr(mgr.MessagingManager, "start_channel", no_start)
        resp = await client.post("/api/messaging/channels", json={
            "name": "Teams", "platform": "teams",
            "credentials": {"app_id": "app-1", "app_password": "p@ss-secret"},
        })
        assert resp.status_code == 200
        assert "p@ss-secret" not in resp.text
        assert resp.json()["credentials_set"] == {"app_password": True}

        enabled = await client.post(f"/api/messaging/channels/{resp.json()['id']}/enable")
        assert "p@ss-secret" not in enabled.text

    async def test_a_blank_secret_on_save_keeps_the_stored_one(self, client, manager):
        ch = await _telegram("123456:the-real-token")

        resp = await client.post("/api/messaging/channels", json={
            "id": ch["id"], "name": "Renamed", "platform": "telegram", "mode": "polling",
            "credentials": {"bot_token": ""},
        })

        assert resp.status_code == 200
        stored = await channel_store.get_channel(ch["id"])
        assert stored["name"] == "Renamed"
        assert stored["credentials"]["bot_token"] == "123456:the-real-token"
        # The server-minted secret the screen never sees survives too.
        assert stored["credentials"]["webhook_secret"] == "minted-by-the-server"

    async def test_a_new_secret_replaces_it(self, client, manager):
        ch = await _telegram("123456:old")

        await client.post("/api/messaging/channels", json={
            "id": ch["id"], "name": "TG", "platform": "telegram",
            "credentials": {"bot_token": "123456:new"},
        })

        assert (await channel_store.get_channel(ch["id"]))["credentials"]["bot_token"] == "123456:new"


class TestABadTokenIsAnError:
    async def test_telegram_checks_the_token_before_it_says_running(self, manager, monkeypatch):
        from telegram.error import InvalidToken
        from telegram.ext import Application

        async def refused(self):
            raise InvalidToken("Unauthorized")

        monkeypatch.setattr(Application, "initialize", refused)
        ch = await _telegram()

        await manager.start_channel(ch["id"])
        stored = await _settled(ch["id"], lambda c: c["status"] != "running")

        assert stored["status"] == "error"
        assert "Unauthorized" in (stored["last_error"] or "")
        assert ch["id"] not in manager._adapters

    async def test_discord_logs_in_before_it_says_running(self, manager, monkeypatch):
        import discord

        async def refused(self, token):
            raise discord.LoginFailure("Improper token has been passed.")

        monkeypatch.setattr(discord.Client, "login", refused)
        ch = await channel_store.save_channel({
            "name": "D", "platform": "discord", "credentials": {"bot_token": "bogus"},
        })

        await manager.start_channel(ch["id"])
        stored = await _settled(ch["id"], lambda c: c["status"] != "running")

        assert stored["status"] == "error"
        assert "Improper token" in (stored["last_error"] or "")

    async def test_a_polling_task_that_dies_later_marks_the_channel(self, manager, monkeypatch):
        from telegram.ext import Application

        async def fine(self):
            return None

        async def dies(self):
            raise RuntimeError("Conflict: terminated by other getUpdates request")

        monkeypatch.setattr(Application, "initialize", fine)
        monkeypatch.setattr(Application, "start", dies)
        ch = await _telegram()

        await manager.start_channel(ch["id"])
        stored = await _settled(ch["id"], lambda c: c["status"] == "error")

        assert stored["status"] == "error"
        assert "terminated by other getUpdates" in stored["last_error"]
        assert ch["id"] not in manager._adapters
        # And the list says so, rather than "running" because an adapter object
        # still sits in the manager.
        assert next(c for c in await manager.get_status() if c["id"] == ch["id"])["status"] == "error"

    async def test_the_reason_never_quotes_the_token(self, client, manager, monkeypatch, caplog):
        """python-telegram-bot's own refusal quotes the token ("The token `…`
        was rejected by the server."), so the recorded reason, the served
        reason and the log line all carried it."""
        from telegram.error import InvalidToken
        from telegram.ext import Application

        async def refused(self):
            raise InvalidToken(f"The token `{self.bot.token}` was rejected by the server.")

        monkeypatch.setattr(Application, "initialize", refused)
        ch = await _telegram("123456:quoted-back-to-us")

        await manager.start_channel(ch["id"])

        stored = await channel_store.get_channel(ch["id"])
        assert stored["status"] == "error"
        assert "quoted-back-to-us" not in stored["last_error"]
        assert "was rejected by the server" in stored["last_error"]
        assert "quoted-back-to-us" not in (await client.get("/api/messaging/channels")).text
        assert "quoted-back-to-us" not in caplog.text

