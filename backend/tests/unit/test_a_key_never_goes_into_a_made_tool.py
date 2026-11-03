"""A key said in chat never goes into a made tool: it goes on the tool's Connect card (F1309, TRI1; CAP3's tier 1 P1
prod-02; 79's ruling 11-03).

The person pasted their Chirp token in chat; the planner's change made an HTTP tool with it — "Make the HTTP tool
chirp_api_createpost … Authenticates with bearer token … in the Authorization header" — and the maker wrote it into
the tool's header, where the workspace then held it in plaintext. Two doors close it: the change check refuses an op
whose fields or `says` carry a key, by shape or as a value the workspace holds, before the review and with nothing
kept (its words say a tool's key goes on its Connect card); and `create_http_tool` refuses a header holding one, so the
tool is made without it and its card asks for the key. A reference (`{{ global.X }}`) is a name and passes.

Every secret below is assembled at run time: a literal one in a source file trips secret scanners.
"""
from __future__ import annotations

import json

import pytest

from test_the_change_route_decides_in_code import _call, _check, _run, fresh, role  # noqa: F401 — fixtures

SAID = "chirp-" + "canary-token-" + "6620"
HELD = "tracker-" + "canary-token-" + "4410"


@pytest.fixture
async def held():
    from core.store.resources import save_mcp_server

    await save_mcp_server({"name": "tracker", "label": "Tracker", "server_type": "http",
                           "url": "http://localhost:9/mcp", "token": HELD})


class TestTheChangeCheck:
    async def test_a_made_tool_carrying_a_bearer_token_is_refused_and_nothing_keeps_it(self, role):
        out, _, run = await _check([{"op": "create", "target": {"kind": "tool", "name": "chirp_api_createpost"},
                                     "fields": {"what": "HTTP POST tool to http://localhost:9421/posts. Authenticates "
                                                        f"with bearer token {SAID} in the Authorization header."}}])
        assert out["outcome"] == "said" and out["calls"] == []
        assert "Keys never go in a change" in out["say"] and "Connect card" in out["say"]
        assert SAID not in json.dumps(out)

    async def test_a_value_the_workspace_holds_is_refused_in_its_says_too(self, role, held):
        out, _, _ = await _check([{"op": "create", "target": {"kind": "tool", "name": "tracker_lookup"},
                                   "fields": {"what": "Looks up a ticket by its id."},
                                   "says": f"Make a tracker tool that sends {HELD}"}])
        assert out["outcome"] == "said" and HELD not in json.dumps(out)

    async def test_a_tool_without_a_key_goes_on(self, role):
        out, _, _ = await _check([{"op": "create", "target": {"kind": "tool", "name": "chirp_api_createpost"},
                                   "fields": {"what": "HTTP POST tool to http://localhost:9421/posts; its key on its "
                                                      "Connect card."}}])
        assert "Keys never go in a change" not in json.dumps(out)


class TestMakingTheTool:
    @pytest.mark.parametrize("header", [f"Bearer {SAID}-{SAID}", HELD])
    async def test_a_header_holding_a_key_is_refused(self, role, held, header):
        run = await _run()
        out, _ = await _call("create_http_tool", {"name": "chirp_post", "url": "https://api.example.com/posts",
                                                  "method": "POST", "headers_json": json.dumps({"Authorization": header})},
                             run)
        assert "error" in out and "Connect card" in out["error"]
        from core.store.resources import load_tools

        assert "chirp_post" not in {t.get("name") for t in await load_tools()}

    async def test_a_reference_is_a_name_and_passes(self, role):
        run = await _run()
        out, _ = await _call("create_http_tool", {"name": "chirp_post", "url": "https://api.example.com/posts",
                                                  "method": "POST",
                                                  "headers_json": json.dumps({"Authorization": "Bearer {{ global.CHIRP }}"})},
                             run)
        assert "error" not in out, out
