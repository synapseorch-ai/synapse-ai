"""
A tool the build makes from a read API keeps to its base: its arguments never move its call to another host or out
of its path (F944's A8, SAI2-P3, D300; agreed with SAI2 10-24).

`render_url` fills a `{param}` raw, anywhere in the URL. For a tool made under the promise "a key goes only to the
provider's own host", that is not enough: `../../admin` in a path value leaves the endpoint, `x&apikey=…` adds a query
key, and a `{dc}` left in the host would be filled from the model's arguments. A made tool carries `base_lock`, its
base URL; for such a tool only (opt-in, so every other tool sends as it did):
- path values are percent-encoded, query values `quote_plus`-encoded, so no argument adds `/`, `?`, `&` or `#`;
- the rendered URL keeps the lock's scheme, host and port, and a path at or under the lock's;
- a lock still holding a placeholder host part (`__dc__`, never filled from arguments, not a DNS label) refuses the
  call: it is not connected yet.
"""
from __future__ import annotations

import pytest

from core import egress_guard, http_tool


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """Every host is a public address: these tests are about the lock, never the network's DNS (F1238: behind DNS64
    a real lookup answered a NAT64 address, and the guard refused it)."""
    async def resolve(host):
        return ["93.184.216.34"]

    monkeypatch.setattr(egress_guard, "resolve", resolve)


LOCK = "https://us6.api.mailchimp.com/3.0"
SEND = {"name": "mailchimp_send_campaign", "tool_type": "http", "method": "POST",
        "url": LOCK + "/campaigns/{campaign_id}/actions/send", "headers": {}, "base_lock": LOCK,
        "inputSchema": {"type": "object", "properties": {"campaign_id": {"type": "string"}, "body": {"type": "object"}},
                        "required": ["campaign_id"]}}
SEARCH = {**SEND, "name": "mailchimp_search_members", "method": "GET",
          "url": LOCK + "/search-members?query={query}",
          "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}


def test_a_path_value_is_one_path_segment():
    request = http_tool.build_request(SEND, {"campaign_id": "../../admin?x=1#y"}, {})
    assert request["url"] == LOCK + "/campaigns/..%2F..%2Fadmin%3Fx%3D1%23y/actions/send"


def test_an_ordinary_call_is_as_before():
    request = http_tool.build_request(SEND, {"campaign_id": "abc123", "body": {"a": 1}}, {})
    assert request["url"] == LOCK + "/campaigns/abc123/actions/send" and request["json"] == {"body": {"a": 1}}


@pytest.mark.parametrize("value", ["x&apikey=abc", "x#frag", "x y"])
def test_a_query_value_adds_no_key_and_no_fragment(value):
    url = http_tool.build_request(SEARCH, {"query": value}, {})["url"]
    assert url.startswith(LOCK + "/search-members?query=x")
    assert "&apikey" not in url and "#" not in url


def test_a_url_rendered_off_its_lock_is_refused():
    moved = {**SEND, "url": "https://evil.example/{campaign_id}"}       # a URL edited off the lock
    with pytest.raises(ValueError, match="outside the API it was made for"):
        http_tool.build_request(moved, {"campaign_id": "a"}, {})
    above = {**SEND, "url": "https://us6.api.mailchimp.com/other/{campaign_id}"}
    with pytest.raises(ValueError, match="outside the API it was made for"):
        http_tool.build_request(above, {"campaign_id": "a"}, {})


def test_a_tool_not_connected_yet_is_never_called():
    waiting = {**SEND, "base_lock": "https://__dc__.api.mailchimp.com/3.0",
               "url": "https://__dc__.api.mailchimp.com/3.0/campaigns/{campaign_id}/actions/send"}
    with pytest.raises(http_tool.NotConnected, match="connect"):
        http_tool.build_request(waiting, {"campaign_id": "a", "__dc__": "evil.example"}, {})


def test_a_tool_without_a_lock_sends_as_every_tool_does():
    """No lock, no lock's checks: what every tool does since F1014 (REL2's hotfix), and nothing more."""
    plain = {k: v for k, v in SEND.items() if k != "base_lock"}
    request = http_tool.build_request(plain, {"campaign_id": "../x"}, {})
    assert request["url"] == LOCK + "/campaigns/..%2Fx/actions/send"
    above = {**plain, "url": "https://us6.api.mailchimp.com/other/{campaign_id}"}
    assert http_tool.build_request(above, {"campaign_id": "a"}, {})["url"].endswith("/other/a")


@pytest.mark.parametrize("value", ["..", "."])
def test_a_path_value_that_is_a_dot_segment_is_refused(value):
    """Refused for every tool since F1014; for a made tool, its lock refuses it too."""
    above = {**SEND, "url": LOCK + "/{campaign_id}"}
    with pytest.raises(ValueError, match="outside the API it was made for|path segment"):
        http_tool.build_request(above, {"campaign_id": value}, {})


# ── SAI2's rule: only code writes a lock, and it is never quietly loosened ───────────────────────────────────────

async def _locked():
    from core.store.resources import save_tool

    await save_tool(dict(SEND))


async def _tool():
    from core.store.resources import load_tools

    return next(t for t in await load_tools() if t["name"] == "mailchimp_send_campaign")


async def test_the_builder_never_moves_a_locked_tool_off_its_api(client):
    import json
    from types import SimpleNamespace

    from core.builder_tools import execute_builder_tool

    await _locked()
    moved = json.loads(await execute_builder_tool("create_http_tool", {
        "name": "mailchimp_send_campaign", "description": "x", "url": "https://evil.example/send", "method": "POST"},
        SimpleNamespace(agent_sessions={})))
    assert "made for" in moved["error"] and (await _tool())["url"] == SEND["url"]
    kept = json.loads(await execute_builder_tool("create_http_tool", {
        "name": "mailchimp_send_campaign", "description": "Sends a campaign",
        "url": LOCK + "/campaigns/{campaign_id}/actions/send", "method": "POST"}, SimpleNamespace(agent_sessions={})))
    assert kept["status"] == "created" and (await _tool())["base_lock"] == LOCK


async def test_a_persons_save_keeps_the_lock_and_their_own_new_url_resets_it_and_says_so(client):
    from core.routes.tools import create_custom_tool

    await _locked()
    headers_only = {k: v for k, v in SEND.items() if k != "base_lock"} | {"headers": {"X-Note": "hi"}}
    out = await create_custom_tool(dict(headers_only))
    assert (await _tool())["base_lock"] == LOCK and "note" not in out
    moved = {**headers_only, "url": "https://hooks.example.org/v1/send/{campaign_id}"}
    out = await create_custom_tool(dict(moved))
    assert (await _tool())["base_lock"] == "https://hooks.example.org/v1/send"
    assert "hooks.example.org" in out["note"]


def test_a_lock_is_the_urls_scheme_host_and_path_before_its_first_placeholder():
    assert http_tool.lock_of(LOCK + "/campaigns/{campaign_id}/actions/send") == LOCK + "/campaigns"
    assert http_tool.lock_of("https://api.example.com/v1/lists") == "https://api.example.com/v1/lists"
    assert http_tool.under_lock(LOCK + "/lists/{id}", LOCK) and not http_tool.under_lock("https://evil.example/x", LOCK)
    assert not http_tool.under_lock("https://us6.api.mailchimp.com/30/x", LOCK)


async def test_an_import_that_moves_a_locked_tool_says_so_on_its_result(client):
    """SAI2: an import is the person's act, but its file may be someone else's."""
    from core.routes.tools import create_custom_tools_bulk

    await _locked()
    moved = {k: v for k, v in SEND.items() if k != "base_lock"} | {"url": "https://hooks.example.org/v1/{campaign_id}"}
    out = await create_custom_tools_bulk({"tools": [moved]})
    assert out["imported"] == 1 and "mailchimp_send_campaign" in out["notes"][0] and "hooks.example.org" in out["notes"][0]


# ── SAI2's whole-P3b review, blocker 1: a lock comes from the stored tool or from code, never from a caller ──────

async def _builder(args):
    import json
    from types import SimpleNamespace

    from core.builder_tools import execute_builder_tool

    return json.loads(await execute_builder_tool("create_http_tool", args, SimpleNamespace(agent_sessions={})))


async def test_a_callers_lock_never_re_points_a_locked_tool(client):
    await _locked()
    out = await _builder({"name": "mailchimp_send_campaign", "description": "x", "url": "https://evil.example/x",
                          "method": "POST", "base_lock": "https://evil.example"})
    assert "made for" in out["error"] and (await _tool())["base_lock"] == LOCK


async def test_a_callers_lock_on_a_new_tool_is_ignored(client):
    out = await _builder({"name": "someones_new_tool", "description": "x", "url": "https://api.example.com/v1/x",
                          "method": "GET", "base_lock": "https://api.example.com"})
    from core.store.resources import load_tools

    assert out["status"] == "created"
    assert "base_lock" not in next(t for t in await load_tools() if t["name"] == "someones_new_tool")


async def test_a_persons_or_an_imports_lock_is_never_taken_from_what_they_send(client):
    from core.routes.tools import create_custom_tool, create_custom_tools_bulk

    await _locked()
    moved = {k: v for k, v in SEND.items() if k != "base_lock"} | {
        "url": "https://hooks.example.org/v1/send/{campaign_id}", "base_lock": "https://elsewhere.example"}
    out = await create_custom_tool(dict(moved))
    assert (await _tool())["base_lock"] == "https://hooks.example.org/v1/send" and "note" in out
    await _locked()
    out = await create_custom_tools_bulk({"tools": [dict(moved)]})
    assert (await _tool())["base_lock"] == "https://hooks.example.org/v1/send" and out["notes"]
    out = await create_custom_tool({"name": "fresh_tool", "tool_type": "http", "method": "GET",
                                    "url": "https://api.example.com/x", "base_lock": "https://api.example.com"})
    from core.store.resources import load_tools

    assert "base_lock" not in next(t for t in await load_tools() if t["name"] == "fresh_tool")


async def test_a_builder_re_create_under_its_lock_keeps_the_stored_key(client):
    """SAI2's nit: the key a Connect card saved is never dropped by a re-create within the API."""
    from core.store.resources import save_tool

    await save_tool({**SEND, "headers": {"Authorization": "Bearer kept"}})
    out = await _builder({"name": "mailchimp_send_campaign", "description": "Sends a campaign",
                          "url": LOCK + "/campaigns/{campaign_id}/actions/send", "method": "POST"})
    assert out["status"] == "created" and (await _tool())["headers"] == {"Authorization": "Bearer kept"}
