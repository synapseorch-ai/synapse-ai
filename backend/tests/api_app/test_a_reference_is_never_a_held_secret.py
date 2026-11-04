"""A reference is a name, never a held secret (F1326, TRI1; BLD1's F1312b shot rig; eb 11-04).

A tool's header stored as `X-Api-Key: {{ global.SHOP_KEY }}` was held as a secret (`held_secrets` takes every header
string, and a reference passes `secret_like`), so a person who pasted a curl with that same reference had it replaced by
the mark before the planner read it, and was told to rotate a key they never typed. The class: any `{{ … }}` or `${…}`
reference kept where the workspace keeps secrets — a tool's headers, an MCP server's definition, a setting under a
secret-named key. A reference names a value kept elsewhere, as F1309's header check reads one: it is never held, on the
door's side or the output side, and a real held value beside it is still masked.

Every secret below is assembled at run time: a literal one in a source file trips secret scanners.
"""
from __future__ import annotations

import pytest

from core import held_secrets, secret_scrub
from core.native_builder import door

REAL = "shop-" + "real-key-" + "5521"
CURL = 'curl -H "X-Api-Key: {{ global.SHOP_KEY }}" https://shop.example.com/orders'
REFERENCES = ["{{ global.SHOP_KEY }}", "Bearer {{ global.SHOP_TOKEN }}", "{{global.SHOP_KEY}}", "${SHOP_SECRET}",
              "${TRACKER_TOKEN}", "{{ global.ACME_KEY }}"]


@pytest.fixture
async def kept():
    """References where the workspace keeps secrets, and one real value beside them."""
    from core.store.resources import save_mcp_server, save_tool
    from core.store.settings import save_setting

    await save_tool({"name": "shop_orders", "description": "The shop's orders.", "tool_type": "http", "method": "GET",
                     "url": "https://shop.example.com/orders",
                     "headers": {"X-Api-Key": "{{ global.SHOP_KEY }}", "Authorization": "Bearer {{ global.SHOP_TOKEN }}",
                                 "X-Unspaced": "{{global.SHOP_KEY}}", "X-Env": "${SHOP_SECRET}", "X-Old": REAL}})
    await save_mcp_server({"name": "tracker", "label": "Tracker", "server_type": "stdio", "command": "tracker-mcp",
                           "env": {"TRACKER_TOKEN": "${TRACKER_TOKEN}"}})
    await save_setting("acme_api_key", "{{ global.ACME_KEY }}")


class TestAReferenceIsNeverHeld:
    @pytest.mark.parametrize("said", REFERENCES)
    async def test_on_the_doors_side(self, client, kept, said):
        assert await secret_scrub.held_in(f"use {said} please") == []

    async def test_a_pasted_curl_keeps_its_reference_and_says_nothing(self, client, kept):
        checked = door.check(CURL, await secret_scrub.held_in(CURL))
        assert checked.text == CURL and checked.removed == 0 and checked.reason != "held", checked

    async def test_on_the_output_side(self, client, kept):
        matcher = await held_secrets.matcher()
        assert matcher is not None and matcher.find(" ".join(REFERENCES)) == []


class TestARealValueBesideItIsStillHeld:
    async def test_the_door_masks_it_and_keeps_the_reference(self, client, kept):
        text = f"{CURL} and the old key was {REAL}"
        held = await secret_scrub.held_in(text)
        assert held == [(REAL, "connection")], held
        checked = door.check(text, held)
        assert REAL not in checked.text and "{{ global.SHOP_KEY }}" in checked.text

    async def test_the_output_side_marks_it(self, client, kept):
        matcher = await held_secrets.matcher()
        assert matcher.find(f"sent with {REAL}") == [REAL]
