"""Reading settings never changes them (S10).

`load_settings()` served the cached snapshot itself, and `GET /api/settings`
did `settings.pop("login_password_hash")` on what it was handed — so opening
the Settings page deleted the password hash from the live settings. Until the
cache refreshed (30 s), login read as not configured: the proxy's gate asked
the backend, heard "login is off", and let anyone on the network through with no
credentials at all. Measured on a scratch install, 2026-09-25.
"""
from core import settings_runtime
from core.config import load_settings


def _with(snapshot: dict):
    settings_runtime.install_provider()
    return settings_runtime.set_snapshot(snapshot)


async def test_the_settings_route_does_not_strip_the_live_hash():
    from core.routes.settings import get_settings

    token = _with({"login_enabled": True, "login_username": "owner", "login_password_hash": "h"})
    try:
        body = await get_settings()
        assert "login_password_hash" not in body
        assert load_settings()["login_password_hash"] == "h"
    finally:
        settings_runtime.reset(token)


async def test_no_reader_can_change_what_the_next_one_reads():
    token = _with({"login_enabled": True, "login_password_hash": "h"})
    try:
        first = load_settings()
        first.pop("login_password_hash")
        first["login_enabled"] = False
        again = load_settings()
        assert again["login_password_hash"] == "h" and again["login_enabled"] is True
    finally:
        settings_runtime.reset(token)
