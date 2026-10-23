"""A page is a provider's own only on the provider's own registrable domain (SAI2, D300; Naveen 2026-10-23).

F667's rule let a research verdict (`api`, `none`) stand only on a page of the provider's own, but it decided "own"
by a substring of the whole host: `mailchimp.evil.com`, `evilmailchimp.io` and `stripe-docs.attacker.net` all counted
as Mailchimp's or Stripe's own. Today that only shapes advice; P3b uses the same rule to decide which host is given a
person's key for an HTTP tool. So the name is read where a domain's owner is: the registrable name (the label before
the public suffix). It must be the provider's whole name, its brand word (Microsoft Teams on `microsoft.com`, F667),
or the whole name with a common product affix (`goshippo.com` for Shippo).
"""
from __future__ import annotations

import pytest

from core.builder_research import _providers_own_page


@pytest.mark.parametrize("provider, host", [
    ("Mailchimp", "mailchimp.com"),
    ("Mailchimp", "us1.api.mailchimp.com"),
    ("Stripe", "docs.stripe.com"),
    ("Shippo", "docs.goshippo.com"),          # its site is goshippo.com
    ("USPS", "developers.usps.com"),
    ("Microsoft Teams", "learn.microsoft.com"),  # F667's brand word
    ("Google Calendar", "developers.google.com"),
    ("Tesco Groceries", "dev.tesco.co.uk"),
    ("Calendly", "developer.calendly.com"),
    ("Duffel", "duffel.com"),
])
def test_the_providers_own_domain_counts(provider, host):
    assert _providers_own_page(provider, host)


@pytest.mark.parametrize("provider, host", [
    ("Mailchimp", "mailchimp.evil.com"),       # the name is a subdomain of someone else's domain
    ("Mailchimp", "evilmailchimp.io"),         # the name inside another registrable name
    ("Stripe", "stripe-docs.attacker.net"),
    ("Stripe", "stripe.com.evil.net"),
    ("Microsoft Teams", "teamsbot.io"),
    ("Board", "boardtools.example"),           # QA1c's invented "Board" provider
    ("Shippo", "shippoapi-docs.xyz"),
])
def test_a_look_alike_domain_is_not_the_providers_own(provider, host):
    assert not _providers_own_page(provider, host)
