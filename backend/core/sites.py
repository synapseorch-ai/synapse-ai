"""A host's site: its registrable domain, the one rule everything that groups hosts by owner uses (BRW1, D328).

`dev.tesco.co.uk` and `www.tesco.co.uk` are one site, `tesco.co.uk`; `acme.workers.dev` and `evil.workers.dev` are
two, as their tenants are; on a cloud that puts a tenant several labels deep (`*.amazonaws.com`) each host is its own.
Used by the browser's gate (which socket and send belong to the page's own site), the sessions it keeps (one per
site; a cookie only ever goes to its own) and the builder's research (which pages are a provider's own). No public
suffix list is installed, so the suffixes that matter are named here.
"""
from __future__ import annotations

#: Two-part public suffixes, so `dev.tesco.co.uk`'s registrable name is "tesco".
TWO_PART_SUFFIXES = {"co.uk", "org.uk", "ac.uk", "com.au", "net.au", "co.in", "co.jp", "com.br", "co.nz", "com.sg",
                     "co.za", "com.mx"}
#: A country's own second level (`co.kr`, `gov.uk`, `com.cn`): two-part too, for any two-letter country.
COUNTRY_SECOND_LEVELS = frozenset({"co", "com", "net", "org", "gov", "ac", "edu"})

#: Hosts on which each tenant has its own name (SAI2's whole-P3b review, 2): `acme.workers.dev` and `evil.workers.dev`
#: are two owners, as `tesco.co.uk` and `asda.co.uk` are, so the tenant's label is part of the registrable domain.
SHARED_HOST_SUFFIXES = {"workers.dev", "pages.dev", "vercel.app", "netlify.app", "github.io", "gitlab.io",
                        "herokuapp.com", "onrender.com", "fly.dev", "web.app", "firebaseapp.com", "appspot.com",
                        "azurewebsites.net", "cloudfront.net", "azure-api.net", "ngrok.io", "ngrok-free.app",
                        "glitch.me", "replit.app", "repl.co", "myshopify.com", "azurestaticapps.net"}
#: Clouds that put a tenant more than one label deep (`abc.execute-api.us-east-1.amazonaws.com`,
#: `acme.blob.core.windows.net`): there each host is its own, and no host vouches for another (SAI2's re-check of 2).
WHOLE_HOST_SUFFIXES = ("amazonaws.com", "windows.net", "cloudapp.azure.com")


def whole_host(labels: list[str]) -> bool:
    host = ".".join(labels)
    return any(host.endswith("." + s) for s in WHOLE_HOST_SUFFIXES)


def suffix_labels(labels: list[str]) -> int:
    """How many of a host's last labels are its public suffix: 2 for `co.uk`, `co.kr` or `workers.dev`, else 1."""
    if len(labels) < 3:
        return 1
    last_two = ".".join(labels[-2:])
    if last_two in TWO_PART_SUFFIXES or last_two in SHARED_HOST_SUFFIXES:
        return 2
    return 2 if len(labels[-1]) == 2 and labels[-1].isalpha() and labels[-2] in COUNTRY_SECOND_LEVELS else 1


def registrable_domain(host: str) -> str:
    """`learn.microsoft.com` → "microsoft.com"; `dev.tesco.co.uk` → "tesco.co.uk"; `acme.workers.dev` stays itself."""
    labels = [x for x in (host or "").lower().rstrip(".").removeprefix("www.").split(".") if x]
    if whole_host(labels):
        return ".".join(labels)
    return ".".join(labels[-1 - suffix_labels(labels):])


def same_site(a: str, b: str) -> bool:
    return bool(a) and bool(b) and registrable_domain(a) == registrable_domain(b)
