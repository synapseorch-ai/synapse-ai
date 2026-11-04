"""A tool's address with a login is refused as one, at every door, and nothing of it is kept (F1312b × F1343; eb
11-04).

TRI1's door reads a URL's password as a key (F1343), so update_http_tool's change with `https://alice:pw@…` was
refused with the generic key sentence, and create_http_tool, which had no login check, would have kept one. Now one
check, before any key check (`builder_tools.holds_a_login`, `LOGIN_REFUSAL`): create_http_tool by the builder and by
an MCP client, and a new HTTP tool in a change, are refused with the login sentence; the answer never shows the
address or its password, no tool is saved, and the change's review, event and card keep nothing of it. An `@` in a
path or a query is no login.
"""
from __future__ import annotations

import json

import pytest

from core.builder_tools import LOGIN_REFUSAL, holds_a_login
from test_a_change_makes_the_http_tools_it_uses import (  # noqa: F401 — fixtures
    SHOP, _call, _change, _public_dns, _run, _tools, fresh)

PASSWORD = "p" + "w9" + "x"
LOGGED_IN = f"https://alice:{PASSWORD}@shop.tidewell.example/orders"


def _never_said(said: str) -> None:
    assert PASSWORD not in said and "alice:" not in said and "@shop" not in said, said


def _args(url: str = LOGGED_IN) -> dict:
    return {"name": "fetch_new_orders", "description": "Fetches the shop's new orders", "method": "GET", "url": url}


async def test_the_builders_create_http_tool_refuses_it_and_saves_nothing(client):
    out, _ = await _call("create_http_tool", _args(), await _run())
    said = json.dumps(out)
    assert LOGIN_REFUSAL in said, said
    _never_said(said)
    assert "fetch_new_orders" not in await _tools()


async def test_an_mcp_clients_create_http_tool_refuses_it_and_saves_nothing(client):
    from core.product_tools import areas, registry
    from core.product_tools.caller import Caller
    from core.product_tools.errors import ToolRefusal
    from core.product_tools.roles import Role

    areas.ensure_loaded()
    try:
        out = await registry.invoke("create_http_tool", Caller(role=Role.OWNER, via="mcp", user_id="u1"), _args())
        said = json.dumps(out, default=str)
    except ToolRefusal as refused:
        said = str(refused)
    assert LOGIN_REFUSAL in said, said
    _never_said(said)
    assert "fetch_new_orders" not in await _tools()


async def test_a_new_tool_in_a_change_is_refused_as_a_login_and_kept_nowhere(client):
    _, checked, applied, events = await _change({"ops": [{
        "op": "create_tool", "target": {"kind": "tool", "name": "fetch_new_orders"},
        "fields": {"description": "Fetches the shop's new orders", "method": "GET", "url": LOGGED_IN}}]})
    assert checked["outcome"] == "said" and applied is None, checked
    assert LOGIN_REFUSAL in checked["say"], checked
    _never_said(json.dumps(checked, default=str))
    _never_said(json.dumps(events, default=str))
    assert "fetch_new_orders" not in await _tools()


@pytest.mark.parametrize("url,login", [
    (LOGGED_IN, True),
    ("https://ops:‹credential removed›@portal.example:9411/x", True),  # its password masked by the door
    ("https://alice@shop.tidewell.example/orders", True),
    ("https://shop.tidewell.example/users/alice@example.com", False),  # an @ in the path
    ("https://shop.tidewell.example/orders?by=alice@example.com", False),  # an @ in the query
    ("mailto:alice@example.com", False),
])
def test_only_a_user_part_before_the_host_is_a_login(url, login):
    assert holds_a_login(url) is login


async def test_an_at_in_a_path_is_not_refused_as_a_login(client):
    out, _ = await _call("create_http_tool", _args(f"{SHOP}/users/alice@example.com"), await _run())
    assert LOGIN_REFUSAL not in json.dumps(out), out
