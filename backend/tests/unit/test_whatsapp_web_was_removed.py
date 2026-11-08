"""WhatsApp Web, the QR sign-in (`whatsapp_mode: "unofficial"`), is removed in 2.0 (F1355, Naveen 11-04).

It drove WhatsApp Web in a headed Chromium, the only part of Synapse that needed Playwright's full build, which every
install and upgrade downloaded for it (184 MiB, about 30 s). WhatsApp stays, through the Cloud API, its only mode. No
door makes or starts a WhatsApp Web channel again; one carried over from 1.x is kept, switched off, and says how to
reconnect it, never started, never retried, and no browser is opened for it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.messaging import service, store

REPO = Path(__file__).resolve().parents[3]
REMOVED = "WhatsApp Web (the QR sign-in) was removed in Synapse 2.0. Connect WhatsApp through the WhatsApp Cloud API."
PAUSED = "WhatsApp Web sign-in was removed in Synapse 2.0; reconnect this channel through the WhatsApp Cloud API."
WEB = {"whatsapp_mode": "unofficial"}
CLOUD = {"whatsapp_mode": "meta_api", "phone_number_id": "123", "access_token": "EAA-test"}


@pytest.fixture(autouse=True)
async def _store(test_app):
    from core.store import get_store

    await get_store()


async def _carried_over() -> dict:
    """A 1.x WhatsApp Web channel as the importer leaves it: the stored document, switched on, never saved through a
    door."""
    doc = {"id": "chn_old_wa", "name": "Shop WhatsApp", "platform": "whatsapp", "enabled": True, "status": "running",
           "last_error": None, "credentials": dict(WEB), "agent_id": "agent_x"}
    await store._save_raw([*(await store._load_raw()), doc])
    return doc


async def test_a_whatsapp_web_channel_is_refused_at_the_screens_save():
    with pytest.raises(service.ChannelRefused) as refused:
        await service.save({"name": "WA", "platform": "whatsapp", "credentials": dict(WEB)})
    assert (refused.value.status, refused.value.detail) == (422, REMOVED)
    assert [c for c in await store.list_channels() if c.get("platform") == "whatsapp"] == []


async def test_the_store_refuses_one_switched_on_from_any_door():
    with pytest.raises(ValueError, match=re.escape(REMOVED)):
        await store.save_channel({"name": "WA", "platform": "whatsapp", "enabled": True, "credentials": dict(WEB)})


async def test_a_cloud_api_channel_is_saved_as_before():
    saved = await service.save({"name": "WA", "platform": "whatsapp", "credentials": dict(CLOUD)})
    assert saved["id"] and saved["credentials"]["whatsapp_mode"] == "meta_api"


async def test_a_1x_whatsapp_web_channel_is_kept_switched_off_and_opens_no_browser(monkeypatch):
    from core.messaging import manager as manager_module

    old = await _carried_over()

    def no_adapter(platform):
        raise AssertionError(f"an adapter was built for a WhatsApp Web channel ({platform})")

    monkeypatch.setattr(manager_module, "_import_adapter", no_adapter)
    mgr = manager_module.MessagingManager(server_module=None)

    await mgr.start_channel(old["id"])

    after = await store.get_channel(old["id"])
    assert (after["status"], after["last_error"], after["enabled"]) == ("stopped", PAUSED, False)
    assert after["credentials"]["whatsapp_mode"] == "unofficial" and after["agent_id"] == "agent_x"   # kept as it was
    assert old["id"] not in mgr._adapters
    # Switched off, so the next start of everything leaves it alone: never retried.
    await mgr.start_all()
    assert (await store.get_channel(old["id"]))["status"] == "stopped"


async def test_turning_a_1x_whatsapp_web_channel_on_is_refused_and_it_stays_off(test_app, monkeypatch):
    from core.messaging import manager as manager_module

    old = await _carried_over()
    monkeypatch.setattr(manager_module, "_import_adapter",
                        lambda platform: (_ for _ in ()).throw(AssertionError("an adapter was built")))
    test_app.state.messaging_manager = manager_module.MessagingManager(server_module=None)

    with pytest.raises(service.ChannelRefused) as refused:
        await service.enable(old["id"], test_app.state)
    assert (refused.value.status, refused.value.detail) == (422, PAUSED)
    # Switching it off still saves: a carried-over channel can always be paused.
    await service.disable(old["id"], test_app.state)
    assert (await store.get_channel(old["id"]))["enabled"] is False


def test_nothing_is_left_of_the_browser_path():
    adapter = (REPO / "backend/core/messaging/adapters/whatsapp.py").read_text(encoding="utf-8")
    assert "playwright" not in adapter.lower() and "_start_unofficial" not in adapter
    screen = (REPO / "frontend/src/components/settings/MessagingTab.tsx").read_text(encoding="utf-8")
    for gone in ("'unofficial'", "Unofficial (Playwright)", "I understand the risks", "WhatsApp mode"):
        assert gone not in screen, f"the Channels screen still offers WhatsApp Web: {gone!r}"
    assert PAUSED in screen, "a carried-over WhatsApp Web channel must say how to reconnect it"
