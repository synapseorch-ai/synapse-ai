"""
An HTTP tool's arguments cannot move its request off the URL its author wrote (F1014).

`render_url` filled `{x}` and `{{x}}` with `str(value)` wherever they stood, host
included. A model steered by content it read could fill a keyed tool's host with
`evil.com/#` and the request — with the tool's stored auth header — went there; a
path value like `../../admin` or `x?admin=1` reached other endpoints of the same
API with its key; and a value holding `{y}` was filled a second time.

Now an argument's value is percent-encoded into its place, a bare `.`/`..` is
refused, a placeholder in the host may only be one DNS label, and the rendered
URL's scheme and host must be the ones the template allows. Globals are the
author's and stay raw. A tool may keep its path values raw (`url_values: "raw"`,
for an API that takes a path with slashes); the host check never turns off.
"""
from __future__ import annotations

import pytest

from core.http_tool import UnsafeUrl, build_request, render_url


def _url(template, args, globals_=None, **tool):
    return render_url(template, args, globals_ or {}, **tool)[0]


# ── path and query values stay in their place ────────────────────────────────

def test_a_path_value_cannot_climb_out_of_its_segment():
    assert _url("https://api.test/users/{id}", {"id": "../../admin"}) == "https://api.test/users/..%2F..%2Fadmin"


@pytest.mark.parametrize("value", [".", ".."])
def test_a_bare_dot_segment_is_refused(value):
    with pytest.raises(UnsafeUrl, match="segment"):
        _url("https://api.test/users/{id}/orders", {"id": value})


def test_a_query_value_cannot_add_parameters_or_a_fragment():
    assert _url("https://api.test/s?q={q}", {"q": "x&admin=1#f"}) == "https://api.test/s?q=x%26admin%3D1%23f"


def test_the_double_brace_form_is_encoded_too():
    assert _url("https://api.test/users/{{id}}", {"id": "a/b?c"}) == "https://api.test/users/a%2Fb%3Fc"


def test_plain_values_read_as_before():
    assert _url("https://api.test/things/{id}?n={n}", {"id": 7, "n": "abc-1_2.3~"}) == \
        "https://api.test/things/7?n=abc-1_2.3~"


def test_a_value_holding_braces_is_never_filled_again():
    assert _url("https://api.test/a/{x}", {"x": "{y}", "y": "secret"}) == "https://api.test/a/%7By%7D"
    assert _url("https://api.test/a/{{x}}", {"x": "{y}", "y": "secret"}) == "https://api.test/a/%7By%7D"


def test_the_authors_globals_stay_raw():
    assert _url("{{global.base}}/things/{id}", {"id": 9}, {"base": "https://api.test/v2"}) == \
        "https://api.test/v2/things/9"


# ── the host is the template's ───────────────────────────────────────────────

@pytest.mark.parametrize("host", ["evil.com", "evil.com/#", "evil.com:443", "127.0.0.1"])
def test_an_argument_cannot_choose_the_host(host):
    with pytest.raises(UnsafeUrl, match="host"):
        _url("https://{host}/v1/things", {"host": host})


def test_a_host_label_still_fills():
    assert _url("https://{tenant}.zendesk.com/api/v2", {"tenant": "acme-co"}) == "https://acme-co.zendesk.com/api/v2"


@pytest.mark.parametrize("tenant", ["evil.com/#", "a.evil", "acme@evil.com", ""])
def test_a_host_label_is_one_label_only(tenant):
    with pytest.raises(UnsafeUrl, match="host"):
        _url("https://{tenant}.zendesk.com/api/v2", {"tenant": tenant})


def test_userinfo_after_the_host_cannot_move_it():
    with pytest.raises(UnsafeUrl, match="host"):
        _url("https://api.test{p}", {"p": "@evil.com/x"})


def test_the_scheme_cannot_change():
    with pytest.raises(UnsafeUrl):
        _url("{scheme}://api.test/x", {"scheme": "file"})


# ── raw path values, by the tool's choice; never the host ────────────────────

def test_a_tool_may_keep_path_values_raw():
    assert _url("https://api.github.com/repos/o/r/contents/{path}", {"path": "docs/a.md"}, url_values="raw") == \
        "https://api.github.com/repos/o/r/contents/docs/a.md"


def test_raw_values_still_cannot_choose_the_host():
    with pytest.raises(UnsafeUrl, match="host"):
        _url("https://{h}/x", {"h": "evil.com"}, url_values="raw")
    with pytest.raises(UnsafeUrl, match="host"):
        _url("https://api.test{p}", {"p": "@evil.com/x"}, url_values="raw")


# ── a keyed tool and a hostile argument: no request is built ─────────────────

def test_a_keyed_tool_never_sends_its_key_to_an_argument_host():
    tool = {"method": "GET", "url": "https://{region}.api.example.com/v1/items",
            "headers": {"Authorization": "Bearer {{global.api_key}}"}}
    with pytest.raises(UnsafeUrl):
        build_request(tool, {"region": "evil.com/#"}, {"api_key": "sk-live-secret"})
    ok = build_request(tool, {"region": "eu"}, {"api_key": "sk-live-secret"})
    assert ok["url"] == "https://eu.api.example.com/v1/items"
    assert ok["headers"]["Authorization"] == "Bearer sk-live-secret"


def test_the_refusal_is_a_value_error_so_the_caller_reports_it():
    """The callers already turn a ValueError from build_request into the tool's
    error for the model (as MissingArgument does); nothing is sent."""
    assert issubclass(UnsafeUrl, ValueError)
