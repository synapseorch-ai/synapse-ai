"""
An API the research found carries a spec code can make its tool from, on the provider's own host only (F943, SAI2-P3,
D300; the hand-off agreed with SAI2 in pro drafts/sai2/P3B-HANDOFF.md).

Before, `report_findings` said only `how: "api"` and a page, so the build could say "Mailchimp has a public API, but I
can't make a tool for it here yet" and nothing more (F769). Now an `api` option may carry its spec: base URL, auth (a
header, its name and scheme), 1–5 endpoints and its docs page. Code holds the spec to the provider's own domain
before a key is ever given to its host:

- `own_domains` is computed by code: the Registry anchors for the provider ∪ the registrable domains of the own pages
  read (A3);
- base_url is https, on one of those domains; one `{param}` may stand as the leftmost host label only (Mailchimp's
  `{dc}`), named in `host_params` (A1);
- no path climbs out of base_url: no `..`, `//` or encoded slash (A5);
- auth is a header with Bearer, Token or none; a key in the URL, or Basic sign-in, is no way in here yet (A4);
- the spec holds no key.

A spec that fails is dropped, and the option stays a lead (`how: "api"` with no spec) for the research to read again;
it is never half-used. An option with no spec is a lead as before (A2).
"""
from __future__ import annotations

import pytest

from core import builder_research as br

DOCS = "https://mailchimp.com/developer/marketing/api/campaigns/"
MAILCHIMP = {
    "base_url": "https://{dc}.api.mailchimp.com/3.0",
    "host_params": [{"name": "dc", "says": "the part of your API key after the dash, like us21"}],
    "auth": {"in": "header", "name": "Authorization", "scheme": "Bearer"},
    "endpoints": [{"method": "POST", "path": "/campaigns/{campaign_id}/actions/send", "does": "Sends a campaign"},
                  {"method": "GET", "path": "/lists", "does": "Lists the audiences"}],
    "docs_url": DOCS,
}


@pytest.fixture(autouse=True)
def read_mailchimps_docs():
    br._LOOSE.clear()
    br._LOOSE[""] = {**br._fresh(), "read_hosts": ["mailchimp.com", "mailchimp.xyz"]}
    yield
    br._LOOSE.clear()


async def _report(api=None, *, sources=(DOCS,), how="api", provider="Mailchimp"):
    option = {"provider": provider, "how": how, "detail": "Sends the newsletter", "sources": list(sources)}
    if api is not None:
        option["api"] = api
    out = await br.report_findings({"goal": "send the newsletter", "options": [option]}, None, [])
    [found] = out["options"]
    return found


async def test_mailchimps_spec_is_kept_with_its_own_domain():
    found = await _report(MAILCHIMP)
    assert found["how"] == "api" and found["own_domains"] == ["mailchimp.com"]
    assert found["api"]["base_url"] == "https://{dc}.api.mailchimp.com/3.0"
    assert found["api"]["host_params"] == [{"name": "dc", "says": "the part of your API key after the dash, like us21"}]
    assert found["api"]["auth"] == {"in": "header", "name": "Authorization", "scheme": "Bearer"}
    assert [e["path"] for e in found["api"]["endpoints"]] == ["/campaigns/{campaign_id}/actions/send", "/lists"]


async def test_an_api_with_no_spec_is_a_lead_as_before():
    found = await _report(None)
    assert found["how"] == "api" and "api" not in found and found["own_domains"] == ["mailchimp.com"]


async def test_the_registrys_anchors_are_own_domains_too():
    br._LOOSE[""]["own_domains"] = {br._slug("Mailchimp"): ["mailchimp.com", "intuit.com"]}
    found = await _report(MAILCHIMP)
    assert found["own_domains"] == ["intuit.com", "mailchimp.com"]


@pytest.mark.parametrize("base_url, why", [
    ("https://{x}.evil.com/3.0", "own"),                      # SAI2's A1 tests
    ("https://api.{x}.com/3.0", "leftmost"),
    ("https://{a}.{b}.mailchimp.com/3.0", "leftmost"),
    ("https://{dc}.mailchimp.xyz/3.0", "own"),                # a look-alike under another ending
    ("http://{dc}.api.mailchimp.com/3.0", "https"),
    ("https://{region}.api.mailchimp.com/3.0", "host_params"),  # a placeholder the spec does not name
    ("https://{dc}.api.mailchimp.com/3.0/../admin", "climb"),   # A5, in base_url's own path
    ("https://{dc}.api.mailchimp.com/3.0?apikey=abc", "query"),
    # SAI2's review of 5e006e33: a login rides in a URL, and the stored tool would keep it.
    ("https://user@api.mailchimp.com/3.0", "login"),
    ("https://user:pass@api.mailchimp.com/3.0", "login"),
    ("https://api.mailchimp.com:8443/3.0", "port"),
])
async def test_a_base_url_off_the_providers_own_host_is_never_used(base_url, why):
    found = await _report({**MAILCHIMP, "base_url": base_url})
    assert found["how"] == "api" and "api" not in found, "a lead again, never half a spec"
    assert why in found["detail"]


@pytest.mark.parametrize("path", ["/lists/../../admin", "//evil.com/lists", "/lists%2F..%2Fadmin", "lists",
                                  "https://evil.com/lists", "/lists?apikey=abc"])
async def test_an_endpoint_never_climbs_out_of_its_base(path):
    found = await _report({**MAILCHIMP, "endpoints": [{"method": "GET", "path": path, "does": "x"}]})
    assert "api" not in found and "endpoint" in found["detail"]


@pytest.mark.parametrize("endpoints", [[], [{"method": "GET", "path": f"/e{i}", "does": "x"} for i in range(6)],
                                       [{"method": "TRACE", "path": "/lists", "does": "x"}]])
async def test_one_to_five_endpoints_by_a_known_method(endpoints):
    found = await _report({**MAILCHIMP, "endpoints": endpoints})
    assert "api" not in found and "endpoint" in found["detail"]


@pytest.mark.parametrize("auth, says", [
    ({"in": "query", "name": "apikey", "scheme": ""}, "in the URL"),
    ({"in": "header", "name": "Authorization", "scheme": "Basic"}, "username and password"),
])
async def test_a_key_in_the_url_or_a_basic_sign_in_is_no_way_in_yet(auth, says):
    found = await _report({**MAILCHIMP, "auth": auth})
    assert found["how"] == "unclear" and "api" not in found and says in found["detail"]


@pytest.mark.parametrize("auth", [{"in": "header", "name": "X Api Key", "scheme": "Bearer"},
                                  {"in": "header", "name": "Authorization", "scheme": "Digest"}])
async def test_a_header_is_one_token_with_a_known_scheme(auth):
    found = await _report({**MAILCHIMP, "auth": auth})
    assert "api" not in found and "auth" in found["detail"]


async def test_a_spec_holding_a_key_is_never_kept():
    keyed = {**MAILCHIMP, "endpoints": [{"method": "GET", "path": "/lists", "does": "x"}],
             "auth": {"in": "header", "name": "Authorization", "scheme": "Bearer"},
             "docs_url": DOCS, "example": "Authorization: Bearer sk_live_" + "Z" * 24}
    found = await _report(keyed)
    assert "api" not in found and "key" in found["detail"]
    assert "sk_live_" not in str(found)


async def test_the_docs_page_is_one_the_research_read_on_the_providers_own_site():
    found = await _report({**MAILCHIMP, "docs_url": "https://mailchimp.xyz/docs"})
    assert "api" not in found and "docs" in found["detail"]


async def test_an_api_with_no_own_page_read_is_unclear_as_before():
    found = await _report(MAILCHIMP, sources=("https://mailchimp.xyz/docs",))
    assert found["how"] == "unclear" and "api" not in found


def test_the_researcher_is_told_the_spec():
    props = br.FINDINGS_SCHEMA["function"]["parameters"]["properties"]["options"]["items"]["properties"]
    spec = props["api"]["properties"]
    assert set(spec) >= {"base_url", "host_params", "auth", "endpoints", "docs_url"}
    assert "{dc}" in spec["base_url"]["description"]


# ── A7: each endpoint names its tool (P3B-HANDOFF.md, F944) ───────────────────────────────────────────────────────

async def test_each_endpoint_names_its_tool_under_the_providers_name():
    endpoints = [{"method": "POST", "path": "/campaigns/{campaign_id}/actions/send", "does": "Sends", "tool": "send_campaign"},
                 {"method": "GET", "path": "/lists", "does": "Lists the audiences", "tool": "mailchimp_list_audiences"},
                 {"method": "GET", "path": "/lists/{list_id}/members", "does": "Lists members"}]
    found = await _report({**MAILCHIMP, "endpoints": endpoints})
    assert [e["tool"] for e in found["api"]["endpoints"]] == [
        "mailchimp_send_campaign", "mailchimp_list_audiences", "mailchimp_get_lists_members"]


@pytest.mark.parametrize("tool", ["send campaign", "Send-It", "1send", "x" * 80])
async def test_a_tool_name_is_an_identifier(tool):
    found = await _report({**MAILCHIMP, "endpoints": [{"method": "GET", "path": "/lists", "does": "x", "tool": tool}]})
    assert "api" not in found and "tool name" in found["detail"]


async def test_two_endpoints_never_share_a_tool_name():
    endpoints = [{"method": "GET", "path": "/lists", "does": "x", "tool": "lists"},
                 {"method": "POST", "path": "/lists", "does": "y", "tool": "lists"}]
    found = await _report({**MAILCHIMP, "endpoints": endpoints})
    assert "api" not in found and "tool name" in found["detail"]


# ── SAI2's whole-P3b review, 2: on shared hosting, "own" is the tenant, never the host ───────────────────────────

@pytest.mark.parametrize("a, b", [("acme.workers.dev", "evil.workers.dev"), ("acme.vercel.app", "evil.vercel.app"),
                                  ("acme.github.io", "evil.github.io"), ("api.acme.herokuapp.com", "evil.herokuapp.com"),
                                  ("acme.azurewebsites.net", "evil.azurewebsites.net")])
def test_a_shared_host_never_vouches_for_another_tenant(a, b):
    assert br._registrable_domain(a) != br._registrable_domain(b)
    assert br._registrable_domain("docs." + a.split(".", 1)[-1] if a.count(".") > 2 else a) == br._registrable_domain(a)


def test_an_ordinary_domain_is_as_before():
    assert br._registrable_domain("api.stripe.com") == "stripe.com"
    assert br._registrable_domain("dev.tesco.co.uk") == "tesco.co.uk"


async def test_a_spec_on_another_tenant_of_a_shared_host_is_not_kept():
    br._LOOSE[""] = {**br._fresh(), "read_hosts": ["acme.workers.dev"]}
    spec = {**MAILCHIMP, "base_url": "https://evil.workers.dev/v1", "host_params": [],
            "docs_url": "https://acme.workers.dev/docs"}
    found = await _report(spec, sources=("https://acme.workers.dev/docs",), provider="Acme")
    assert "api" not in found and "own domain" in found["detail"]


async def test_a_placeholder_in_the_base_path_is_not_kept():
    """SAI2's whole-P3b review, 7: only the start of the host may hold one."""
    found = await _report({**MAILCHIMP, "base_url": "https://{dc}.api.mailchimp.com/{version}"})
    assert "api" not in found and "path holds a placeholder" in found["detail"]


@pytest.mark.parametrize("name", ["body", "data", "global"])
async def test_a_path_placeholder_never_takes_a_name_the_tool_already_uses(name):
    found = await _report({**MAILCHIMP, "endpoints": [{"method": "GET", "path": f"/lists/{{{name}}}", "does": "x"}]})
    assert "api" not in found and "endpoint" in found["detail"]
