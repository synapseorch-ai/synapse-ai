"""The AI builder makes no tool that places a phone call (F497, BWA1, D274).

Outbound calls are V20 "Later": an AI voice calling someone needs their consent first (TCPA/FCC 24-17, AB 2905,
SHAKEN/STIR, TRAI), which VOX2 brings. No agent tool dials out, but an HTTP tool pointed at a voice provider's
call-placing endpoint would, and the builder could be asked to make one ("call my landlord"). Naveen's call: refuse
it in code until VOX2. A tool a person saves in the editor is theirs (D204).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.builder_guard import PLACES_CALLS, places_calls

TWILIO_CALLS = "https://api.twilio.com/2010-04-01/Accounts/{{account_sid}}/Calls.json"


@pytest.mark.parametrize("url, method, dials", [
    (TWILIO_CALLS, "POST", True),
    ("https://api.twilio.com/2010-04-01/Accounts/AC123/Calls", "post", True),
    (TWILIO_CALLS, "GET", False),                                    # reads the call log
    ("https://api.twilio.com/2010-04-01/Accounts/AC123/Messages.json", "POST", False),
    ("https://api.twilio.com.example.org/Calls.json", "POST", False),  # not Twilio's host
    ("https://api.telnyx.com/v2/calls", "POST", True),
    ("https://api.nexmo.com/v1/calls", "POST", True),
    ("https://api-us.vonage.com/v1/calls", "POST", True),
    ("https://api.plivo.com/v1/Account/MA123/Call/", "POST", True),
    ("https://voice.bandwidth.com/api/v2/accounts/1/calls", "POST", True),
    ("https://calling.api.sinch.com/calling/v1/callouts", "POST", True),
    ("https://api.vapi.ai/call", "POST", True),
    ("https://api.bland.ai/v1/calls", "POST", True),
    ("https://api.retellai.com/v2/create-phone-call", "POST", True),
    ("https://api.example.com/calls", "POST", False),                # an API of the person's own
    ("", "POST", False),
])
def test_a_call_placing_endpoint_is_known_by_its_host_and_path(url, method, dials):
    assert places_calls(url, method) is dials


async def _builder(tool: str, args: dict) -> dict:
    from core.builder_tools import execute_builder_tool
    return json.loads(await execute_builder_tool(tool, args, SimpleNamespace(agent_sessions={})))


async def test_the_builder_refuses_to_make_one_and_saves_nothing():
    from core.routes.tools import load_custom_tools

    got = await _builder("create_http_tool", {
        "name": "call_landlord", "description": "Calls the landlord.", "method": "POST", "url": TWILIO_CALLS})

    assert got == {"error": PLACES_CALLS}
    assert "call_landlord" not in {t["name"] for t in await load_custom_tools()}


async def test_an_import_leaves_out_only_the_operation_that_dials():
    from core.routes.tools import load_custom_tools

    spec = json.dumps({
        "openapi": "3.0.0",
        "info": {"title": "Twilio"},
        "servers": [{"url": "https://api.twilio.com"}],
        "paths": {
            "/2010-04-01/Accounts/{AccountSid}/Calls.json": {
                "get": {"operationId": "listCall"},
                "post": {"operationId": "createCall"},
            },
            "/2010-04-01/Accounts/{AccountSid}/Messages.json": {"post": {"operationId": "createMessage"}},
        },
    })

    got = await _builder("import_openapi", {"spec_text": spec})

    assert got["left_out"] == ["createcall"] and got["why"] == PLACES_CALLS
    assert set(got["tool_names"]) == {"listcall", "createmessage"}
    assert "createcall" not in {t["name"] for t in await load_custom_tools()}


async def test_an_import_of_nothing_but_a_call_is_refused():
    spec = json.dumps({
        "openapi": "3.0.0", "info": {"title": "Dialer"}, "servers": [{"url": "https://api.telnyx.com"}],
        "paths": {"/v2/calls": {"post": {"operationId": "dial"}}},
    })
    assert await _builder("import_openapi", {"spec_text": spec}) == {"error": PLACES_CALLS}


async def test_a_persons_own_tool_is_theirs():
    """The editor's save is untouched (D204): the guard is the builder's, not a new refusal of what people save."""
    from core.routes.tools import create_custom_tool, load_custom_tools

    await create_custom_tool({"name": "my_dialer", "description": "d", "tool_type": "http", "method": "POST",
                              "url": TWILIO_CALLS, "headers": {}, "inputSchema": {"type": "object"}})

    assert "my_dialer" in {t["name"] for t in await load_custom_tools()}
