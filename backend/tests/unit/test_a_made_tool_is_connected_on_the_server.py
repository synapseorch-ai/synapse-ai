"""
A tool the build made is connected by the server, never by the browser alone (F945, SAI2-P3, D300; A8 and SAI2's
additions 3 and 4 in P3B-HANDOFF.md).

One Connect card per provider names every tool the build made for it, the header its key goes in, its scheme
(`Bearer`, `Token`, or the key alone) and what its host needs (Mailchimp's `dc`, "the part of your API key after the
dash"). The card's save goes to `/api/tools/connect`, which, for each of those tools:
- fills each account part of its host only with one DNS label, and checks the provider's domain again on the result
  (a value cannot move the host off it);
- writes the key into its header with the scheme, through the tools' own save path (no other store);
- never sends the key back, and refuses one with a line break in it (a header it could split).
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from core import http_tool
from core.store.resources import load_tools, save_tool

WAITING = {"name": "mailchimp_send_campaign", "tool_type": "http", "method": "POST",
           "url": "https://__dc__.api.mailchimp.com/3.0/campaigns/{campaign_id}/actions/send",
           "base_lock": "https://__dc__.api.mailchimp.com/3.0", "headers": {},
           "inputSchema": {"type": "object", "properties": {"campaign_id": {"type": "string"}}}}
LISTS = {**WAITING, "name": "mailchimp_list_audiences", "method": "GET", "url": "https://__dc__.api.mailchimp.com/3.0/lists"}
BODY = {"tools": ["mailchimp_send_campaign", "mailchimp_list_audiences"], "header": "Authorization",
        "scheme": "Bearer", "key": "abc123-us6", "host_params": {"dc": "us6"}}


@pytest.fixture(autouse=True)
async def made():
    await save_tool(dict(WAITING))
    await save_tool(dict(LISTS))


async def _tool(name):
    return next(t for t in await load_tools() if t["name"] == name)


async def _connect(body):
    from core.routes.tools import connect_made_tools

    return await connect_made_tools(dict(body))


async def test_one_card_connects_every_tool_of_the_provider_on_its_own_host(client):
    out = await _connect(BODY)
    assert out["status"] == "connected" and "abc123" not in str(out)
    for name in BODY["tools"]:
        tool = await _tool(name)
        assert tool["base_lock"] == "https://us6.api.mailchimp.com/3.0"
        assert tool["url"].startswith("https://us6.api.mailchimp.com/3.0/")
        assert tool["headers"] == {"Authorization": "Bearer abc123-us6"}
    request = http_tool.build_request(await _tool("mailchimp_send_campaign"), {"campaign_id": "c1"}, {})
    assert request["url"] == "https://us6.api.mailchimp.com/3.0/campaigns/c1/actions/send"


@pytest.mark.parametrize("value", ["evil.example/#", "a.b", "US6!", "", "-us6", "x" * 70])
async def test_an_account_part_is_one_dns_label_and_the_host_stays_the_providers(client, value):
    with pytest.raises(HTTPException) as refused:
        await _connect({**BODY, "host_params": {"dc": value}})
    assert refused.value.status_code == 422 and "dc" in refused.value.detail
    assert (await _tool("mailchimp_send_campaign"))["base_lock"] == WAITING["base_lock"], "nothing changed"


async def test_every_account_part_is_filled_or_none_is(client):
    with pytest.raises(HTTPException, match="dc"):
        await _connect({**BODY, "host_params": {}})


@pytest.mark.parametrize("key", ["abc\r\nX-Evil: 1", "", "abc\ndef"])
async def test_a_key_that_could_split_its_header_is_refused(client, key):
    with pytest.raises(HTTPException) as refused:
        await _connect({**BODY, "key": key})
    assert refused.value.status_code == 422 and "abc" not in refused.value.detail
    assert (await _tool("mailchimp_send_campaign"))["headers"] == {}


async def test_only_a_tool_the_build_made_is_connected_here(client):
    await save_tool({"name": "someones_tool", "tool_type": "http", "method": "GET", "url": "https://api.example.com/x",
                     "headers": {}})
    with pytest.raises(HTTPException) as refused:
        await _connect({**BODY, "tools": ["someones_tool"]})
    assert refused.value.status_code == 422
    with pytest.raises(HTTPException):
        await _connect({**BODY, "tools": ["no_such_tool"]})


async def test_the_card_names_the_providers_tools_its_header_scheme_and_host_parts(client):
    from core.builder_connect import prepare_connection

    out = await prepare_connection({"kind": "tool_secret", "title": "Connect Mailchimp", "why": "x",
                                    "tools": BODY["tools"], "provider": "Mailchimp", "header": "Authorization",
                                    "scheme": "Bearer", "host_params": [{"name": "dc", "says": "after the dash"}]},
                                   None, events := [])
    assert out["status"] == "card_shown"
    [card] = [e for e in events if e.get("type") == "connection_needed"]
    assert card["tool"] == {"id": "mailchimp_send_campaign", "name": "Mailchimp", "header": "Authorization",
                            "scheme": "Bearer", "tools": BODY["tools"],
                            "host_params": [{"name": "dc", "says": "after the dash"}]}


async def test_connecting_keeps_the_builders_tags_a_claim(client):
    """SAI2's whole-P3b review, 8: the card's save is not the person tagging the tool."""
    await save_tool({**WAITING, "annotations_by": "builder", "annotations": {"readOnlyHint": False}})
    await _connect(BODY)
    assert (await _tool("mailchimp_send_campaign")).get("annotations_by") == "builder"
