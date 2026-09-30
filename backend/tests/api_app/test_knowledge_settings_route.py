"""Knowledge's settings reach the engine through the ordinary settings route (D153 #2)."""
from __future__ import annotations

from core.knowledge import config as kconfig


async def test_it_can_be_switched_on_through_the_settings_route(client):
    from core.config import load_settings

    assert kconfig.knowledge_enabled(load_settings()) is False
    resp = await client.post("/api/settings", json={"knowledge_enabled": True, "knowledge_max_file_mb": 50})

    assert resp.status_code == 200
    assert kconfig.knowledge_enabled(load_settings()) is True
    assert kconfig.knowledge_settings().max_file_mb == 50
