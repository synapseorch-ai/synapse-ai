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

from core import http_tool

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
