"""An agent's Boards section saves, and Boards switch on through the settings route (BOARD1a, D280).

`POST /api/agents` validates against the engine's `Agent`, which drops what it does not declare (F265): an
undeclared `boards` would be lost at every save, and an agent's "the board of the workflow or schedule it runs
in" switch could never be turned off.
"""
from _fakes import seed as S


async def _save(client, **fields):
    resp = await client.post("/api/agents", json={**S.make_agent(id="agent_boards"), **fields})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _stored(client):
    return {a["id"]: a for a in (await client.get("/api/agents")).json()}["agent_boards"]


async def test_its_own_board_turned_off_is_kept(client):
    answered = await _save(client, boards={"own": False})
    assert answered["boards"] == {"own": False}
    assert (await _stored(client))["boards"] == {"own": False}


async def test_an_agent_saved_without_it_reads_as_on(client):
    await _save(client)
    assert ((await _stored(client)).get("boards") or {}).get("own", True) is True


async def test_boards_switch_on_through_the_settings_route(client):
    from core.boards import config as bconfig
    from core.config import load_settings

    assert bconfig.boards_enabled(load_settings()) is False
    resp = await client.post("/api/settings", json={"boards_enabled": True, "boards_max_open_cards": 300})
    assert resp.status_code == 200
    knobs = bconfig.board_settings(load_settings())
    assert knobs.enabled is True and knobs.max_open_cards == 300
