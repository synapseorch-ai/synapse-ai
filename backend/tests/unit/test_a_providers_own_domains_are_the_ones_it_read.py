"""A provider's own domains are the ones the research names and read, never a guess by name (F1244; TRI1's triage of
Naveen's prod chat, class 4).

"Twitter / X" has its API at api.x.com. Code's name rule judged a page the provider's own only when its domain spelt
the provider's name, and it dropped one-letter words, so x.com was never Twitter's own, the spec on api.x.com/2 was
dropped, and "build the tool, I'll add the keys later" got "can't make a tool for it yet". A table of renames would
need keeping up forever (Naveen, 10-30): the Researcher names the provider's own domains (`own_domains`), as its own
docs show them, and code accepts a named one only where this run read a page on it: fetched and returned
(`read_web_page`, the look-up's own read), never a search result or an address the model wrote. A key goes only to a
host on an accepted domain (F943). A host where customers have their own subdomains (a store on myshopify.com, a
bucket on amazonaws.com) is its own site by `core.sites`' one rule, so reading one never makes the others trusted, and
the shared suffix itself is never accepted. An `api` or `none` option that names no own domain it read is sent back
once to say them, so a model that leaves them out isn't silently demoted to `unclear`.
"""
from __future__ import annotations

import pytest

from core import builder_research as br

X_API = {
    "base_url": "https://api.x.com/2",
    "auth": {"in": "header", "name": "Authorization", "scheme": "Bearer"},
    "endpoints": [{"method": "POST", "path": "/tweets", "does": "Posts a tweet"}],
    "docs_url": "https://docs.x.com/x-api/posts/create-post",
}


@pytest.fixture(autouse=True)
def fresh():
    br._LOOSE.clear()
    br._LOOSE[""] = br._fresh()
    yield
    br._LOOSE.clear()


async def _read(*urls):
    for url in urls:
        await br._read_host(url)


async def _report(provider, *, own=None, sources=(), api=None, how="api"):
    option = {"provider": provider, "how": how, "detail": "Posts it", "sources": list(sources)}
    if own is not None:
        option["own_domains"] = list(own)
    if api is not None:
        option["api"] = api
    return await br.report_findings({"goal": "post it", "options": [option]}, None, [])


class TestTheResearchNamesThem:
    async def test_twitter_x_on_api_x_com_is_kept(self):
        await _read("https://docs.x.com/x-api/posts/create-post")
        out = await _report("Twitter / X", own=["x.com"], sources=["https://docs.x.com/x-api/posts/create-post"],
                            api=X_API)
        [found] = out["options"]
        assert found["how"] == "api" and found["own_domains"] == ["x.com"]
        assert found["api"]["base_url"] == "https://api.x.com/2"

    @pytest.mark.parametrize("provider, read, own, base", [
        ("Brevo (formerly Sendinblue)", "https://developers.sendinblue.com/reference", "sendinblue.com",
         "https://api.sendinblue.com/v3"),
        ("Sendinblue", "https://developers.brevo.com/reference", "brevo.com", "https://api.brevo.com/v3"),
        ("HubSpot", "https://developers.hubspot.com/docs/api/crm", "hubapi.com", "https://api.hubapi.com/crm/v3"),
        ("Square", "https://developer.squareup.com/reference/square", "squareup.com", "https://connect.squareup.com/v2"),
    ])
    async def test_a_renamed_or_second_domain_the_research_read_is_kept(self, provider, read, own, base):
        # HubSpot's docs are on hubspot.com and its API on hubapi.com: both read, both named.
        await _read(read, f"https://{own}/")
        out = await _report(provider, own=[own, br._registrable_domain(br._host(read))], sources=[read],
                            api={**X_API, "base_url": base, "docs_url": read})
        [found] = out["options"]
        assert found["how"] == "api" and found["api"]["base_url"] == base, found

    async def test_a_registry_anchor_still_counts(self):
        br._LOOSE[""]["own_domains"] = {br._slug("Mailchimp"): ["mailchimp.com"]}
        await _read("https://mailchimp.com/developer/")
        out = await _report("Mailchimp", own=["mailchimp.com"], sources=["https://mailchimp.com/developer/"],
                            api={**X_API, "base_url": "https://us1.api.mailchimp.com/3.0",
                                 "docs_url": "https://mailchimp.com/developer/"})
        assert out["options"][0]["api"]["base_url"] == "https://us1.api.mailchimp.com/3.0"


class TestOnlyWhatWasRead:
    async def test_a_domain_named_but_never_read_is_not_own(self):
        await _read("https://help.example.org/twitter-api")
        out = await _report("Twitter / X", own=["x.com", "example.org"], sources=["https://help.example.org/twitter-api"],
                            api=X_API)
        [found] = out["options"]
        assert "x.com" not in found["own_domains"] and "api" not in found

    async def test_a_page_the_model_cites_but_never_read_is_no_source(self):
        out = await _report("Twitter / X", own=["x.com"], sources=["https://docs.x.com/x-api"], api=X_API)
        assert "error" in out or "api" not in out["options"][0]

    async def test_a_spec_off_the_named_domains_is_dropped(self):
        await _read("https://docs.x.com/x-api")
        out = await _report("Twitter / X", own=["x.com"], sources=["https://docs.x.com/x-api"],
                            api={**X_API, "base_url": "https://api.evil.com/2"})
        [found] = out["options"]
        assert "api" not in found and "own domain" in found["detail"]


class TestASharedHostIsEachTenantsOwn:
    async def test_one_store_read_trusts_neither_the_suffix_nor_another_store(self):
        await _read("https://acme.myshopify.com/pages/api")
        out = await _report("Acme Store", own=["myshopify.com", "acme.myshopify.com"],
                            sources=["https://acme.myshopify.com/pages/api"],
                            api={**X_API, "base_url": "https://evil.myshopify.com/admin/api"})
        [found] = out["options"]
        assert "myshopify.com" not in found["own_domains"] and "api" not in found

    async def test_a_cloud_tenant_read_is_that_tenant_only(self):
        await _read("https://abc.execute-api.us-east-1.amazonaws.com/docs")
        out = await _report("Acme", own=["amazonaws.com"], sources=["https://abc.execute-api.us-east-1.amazonaws.com/docs"],
                            api={**X_API, "base_url": "https://xyz.execute-api.us-east-1.amazonaws.com/v1"})
        assert "api" not in (out.get("options") or [{}])[0]


class TestAModelThatLeavesThemOut:
    async def test_an_api_with_no_own_domain_read_is_sent_back_once_to_say_them(self):
        await _read("https://docs.x.com/x-api")
        out = await _report("Twitter / X", sources=["https://docs.x.com/x-api"], api=X_API)
        assert "own_domains" in out.get("error", ""), out

    async def test_saying_unclear_instead_is_taken(self):
        out = await _report("Twitter / X", how="unclear")
        assert out["options"][0]["how"] == "unclear"

    def test_the_tool_asks_for_them(self):
        props = br.FINDINGS_SCHEMA["function"]["parameters"]["properties"]["options"]["items"]["properties"]
        assert "own_domains" in props and "read" in props["own_domains"]["description"]


class TestALookAlikeIsWarnedOnTheCard:
    """F951's look-alike, under Naveen's rule: one the research named and read is accepted, and the Connect card says
    above the key field that it doesn't look like the provider's own (code's name rule, a warning, never a gate)."""

    async def test_a_look_alike_named_and_read_is_kept(self):
        await _read("https://mailchimp.xyz/docs")
        out = await _report("Mailchimp", own=["mailchimp.xyz"], sources=["https://mailchimp.xyz/docs"],
                            api={**X_API, "base_url": "https://api.mailchimp.xyz/3.0", "docs_url": "https://mailchimp.xyz/docs"})
        assert out["options"][0]["api"]["base_url"] == "https://api.mailchimp.xyz/3.0"

    @pytest.mark.parametrize("provider, lock, unsure", [
        ("Mailchimp", "https://api.mailchimp.xyz/3.0", ["api.mailchimp.xyz"]),
        ("Mailchimp", "https://__dc__.api.mailchimp.com/3.0", []),
        ("Twitter / X", "https://api.x.com/2", []),
        ("Brevo (formerly Sendinblue)", "https://api.sendinblue.com/v3", []),
    ])
    async def test_the_card_says_where_the_key_goes_and_warns_on_a_look_alike(self, provider, lock, unsure):
        from core.builder_connect import prepare_connection
        from core.store.resources import save_tool

        await save_tool({"name": "made_tool", "description": "x", "tool_type": "http", "method": "GET",
                         "url": lock + "/items", "base_lock": lock, "headers": {},
                         "inputSchema": {"type": "object", "properties": {}}})
        await prepare_connection({"kind": "tool_secret", "title": f"Connect {provider}", "why": "x",
                                  "tools": ["made_tool"], "provider": provider, "header": "Authorization",
                                  "scheme": "Bearer"}, None, events := [])
        [card] = [e for e in events if e.get("type") == "connection_needed"]
        assert card["tool"]["sends_to"] == [lock.split("//")[1].split("/")[0].removeprefix("__dc__.")]
        assert card["tool"]["unsure"] == unsure
