"""A bot check that refuses SynapseBot names whose it is and the honest way on (F1632).

Knowledge read a challenge as "The server answered 403.", which says nothing a person can act on. The refusal's own
headers name the protection (Cloudflare's challenge header, a vendor's server or cookie headers), and the words say
SynapseBot does not take browser checks and what to do instead. A plain 403 with no vendor's sign keeps its plain words:
a site that refuses is not called a bot check without a sign of one.
"""
from __future__ import annotations

import httpx
import pytest

from core import web_refusals


def _headers(**h: str) -> httpx.Headers:
    return httpx.Headers({k.replace("_", "-"): v for k, v in h.items()})


@pytest.mark.parametrize("status,headers,vendor", [
    (403, _headers(cf_mitigated="challenge", server="cloudflare"), "Cloudflare"),
    (403, _headers(server="cloudflare", cf_ray="8c0ffee-LHR"), "Cloudflare"),
    (503, _headers(server="AkamaiGHost"), "Akamai"),
    (403, _headers(x_datadome="protected"), "DataDome"),
    (403, _headers(set_cookie="_px3=abc; Path=/"), "PerimeterX"),
    (429, _headers(x_iinfo="12-345", set_cookie="incap_ses_1=x"), "Imperva"),
    (403, _headers(x_amzn_waf_action="block"), "AWS WAF"),
])
def test_a_refusal_with_a_vendors_sign_names_the_vendor_and_the_way_on(status, headers, vendor):
    said = web_refusals.bot_check(status, headers)
    assert said and vendor in said and f"HTTP {status}" in said, said
    assert "SynapseBot" in said and "as a file" in said and "Firecrawl" in said, said


@pytest.mark.parametrize("status,headers", [
    (403, _headers(server="nginx")),  # a plain refusal: no vendor's sign
    (200, _headers(server="cloudflare", cf_ray="8c0ffee-LHR")),  # a page served through Cloudflare is just a page
    (404, _headers(server="cloudflare")),  # missing, not a bot check
    (500, _headers(server="AkamaiGHost")),  # an error, not a refusal
])
def test_a_page_with_no_bot_check_is_never_called_one(status, headers):
    assert web_refusals.bot_check(status, headers) is None


def test_cloudflares_challenge_header_is_a_bot_check_whatever_the_status():
    said = web_refusals.bot_check(200, _headers(cf_mitigated="challenge"))
    assert said and "Cloudflare" in said and "browser check" in said, said
