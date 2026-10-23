"""F935 tightened: a look-alike under another ending, or behind a common word, is not the provider's own (F951, SAI2).

F935 read the provider's name where a domain's owner is, the registrable name, but compared labels only: the ending
was never looked at (`mailchimp.xyz` passed), and a first word of three letters was enough ("Open Dental" accepted
`open.<anything>`). SAI2-P3 found both while building P3a; P3b gives a person's key to the host this rule accepts.

Now:
- **Without an anchor,** the ending is a common one (`.com`, `.io`, `.ai`, `.app`, a country's, a two-part suffix such
  as `co.uk`), and a brand word stands only with five letters or more and when it isn't a common word.
- **With an anchor,** it decides: the domains the provider's own MCP Registry entry vouches for (its namespace's
  domain, its website, its server's host), verified by the Registry, so a look-alike under a good ending
  (`stripe.io` beside Stripe's `stripe.com`) is not the provider's own either.
"""
from __future__ import annotations

import pytest

from core.builder_research import _providers_own_page, _server_domains


@pytest.mark.parametrize("provider, host", [
    ("Mailchimp", "mailchimp.com"),
    ("Mailchimp", "us1.api.mailchimp.com"),
    ("Stripe", "docs.stripe.com"),
    ("Shippo", "docs.goshippo.com"),
    ("USPS", "developers.usps.com"),
    ("Microsoft Teams", "learn.microsoft.com"),
    ("Google Calendar", "developers.google.com"),
    ("Tesco Groceries", "dev.tesco.co.uk"),
    ("Calendly", "developer.calendly.com"),
    ("Duffel", "duffel.com"),
    ("Open Dental", "opendental.com"),
    ("Notion", "developers.notion.so"),
    ("Hugging Face", "huggingface.co"),
    ("Mistral", "docs.mistral.ai"),
    ("Linear", "linear.app"),
])
def test_the_providers_own_domain_still_counts(provider, host):
    assert _providers_own_page(provider, host)


@pytest.mark.parametrize("provider, host", [
    ("Mailchimp", "mailchimp.xyz"),             # an ending nobody's own site needs
    ("Stripe", "stripe.top"),
    ("Mailchimp", "mailchimp.online"),
    ("Open Dental", "open.io"),                 # "Open" is a common word, not a brand (F935 accepted it)
    ("Open Dental", "docs.open.co"),
    ("Open Dental", "openhealth.com"),
    ("Smart Scheduler", "smart.io"),            # a common word, however good the ending
])
def test_a_look_alike_under_another_ending_or_a_common_word_is_not(provider, host):
    assert not _providers_own_page(provider, host)


def test_an_anchor_from_the_providers_registry_entry_decides():
    stripe = {"stripe.com"}
    assert _providers_own_page("Stripe", "docs.stripe.com", anchors=stripe)
    assert not _providers_own_page("Stripe", "stripe.io", anchors=stripe), "a good ending is not enough beside an anchor"
    shippo = {"shippo.com", "goshippo.com"}          # the namespace's domain and its website
    assert _providers_own_page("Shippo", "docs.goshippo.com", anchors=shippo)


def test_a_registry_entry_vouches_for_its_namespace_website_and_server_host():
    server = {"name": "com.shippo/shippo-mcp", "website": "https://goshippo.com/mcp",
              "remote_url": "https://mcp.goshippo.com/sse"}
    assert _server_domains(server) == {"shippo.com", "goshippo.com"}
    # A GitHub namespace vouches for no domain of the provider's: only its website and server host count.
    assert _server_domains({"name": "io.github.acme/acme-mcp", "website": None,
                            "remote_url": "https://mcp.acme.dev/mcp"}) == {"acme.dev"}
