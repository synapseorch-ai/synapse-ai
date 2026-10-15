"""The AI builder researches a goal the workspace has no tool for (F577, BWA1, D274).

"Book a train and a flight", "buy shipping labels": research a goal scored 0/12 on both models BWA1 measured, because
the builder planned with what the workspace had or said it could not, and never looked for a provider. Naveen's
call (2026-10-15): a route in the builder first, with three sources in this order:

1. this product's Apps catalog — `find_in_catalog`, read through `builder_connect`'s host catalog (cloud's public
   Apps catalog; self-hosted has none);
2. the official MCP Registry — `search_mcp_registry`. Its search is a substring match and noisy ("stripe" returns
   third-party servers beside Stripe's own), so whether a server is the provider's own is decided here, in code,
   by its reverse-DNS namespace against the provider's domain, never by the model;
3. the web, through the built-in scraper (`scrape_url` on a search results page, then the provider's docs) — no
   fetcher of the builder's own.

`report_findings` ends the research: the options, each with how it would connect, what the person needs and the
pages it rests on, as the findings card shows them. Nothing here connects, books or buys anything.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote, urlsplit

from core.builder_tools import register_builder_tool

REGISTRY = "https://registry.modelcontextprotocol.io/v0/servers"
#: How many Registry entries a search reads, and hands back.
_REGISTRY_READ, _REGISTRY_KEEP = 30, 8
_HOW = ("mcp", "api", "none", "unclear")


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 1]


def _slug(text: str) -> str:
    return "".join(_words(text))


# ── 1. The Apps catalog ────────────────────────────────────────────────────────────────────────────────────────────

CATALOG_SCHEMA = {
    "type": "function",
    "function": {
        "name": "find_in_catalog",
        "description": (
            "Look a provider or a kind of service up in this product's Apps catalog: apps it can connect in a click "
            "(sign-in, or a key). Search here first. An app found here is the easiest way to connect."
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "A provider's name, or what it does"}},
            "required": ["query"],
        },
    },
}


async def find_in_catalog(args: dict, server_module: Any, events: list[dict]) -> dict:
    from core.builder_connect import _read_options

    words = set(_words(args.get("query") or ""))
    catalog = (await _read_options()).get("catalog") or []
    found = [{k: e.get(k) for k in ("slug", "name", "auth", "url")} for e in catalog
             if words & set(_words(f"{e.get('name')} {e.get('slug')}"))]
    if not catalog:
        return {"apps": [], "note": "This install has no Apps catalog. Search the MCP Registry next."}
    if not found:
        return {"apps": [], "note": "Nothing in the Apps catalog matches. Search the MCP Registry next."}
    return {"apps": found[:10]}


register_builder_tool(CATALOG_SCHEMA, find_in_catalog)


# ── 2. The official MCP Registry ───────────────────────────────────────────────────────────────────────────────────

REGISTRY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_mcp_registry",
        "description": (
            "Search the official MCP Registry for a provider's MCP server. Give the provider's name and, when you "
            "know it, its website's domain (stripe.com): a server counts as the provider's own only when its "
            "namespace is that domain. Only servers reachable over the internet are offered; one that must be "
            "installed by hand is named as such."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "provider": {"type": "string", "description": "The provider's name, e.g. Stripe"},
                "domain": {"type": "string", "description": "The provider's website domain, e.g. stripe.com"},
            },
            "required": ["provider"],
        },
    },
}


def provider_owns(namespace_name: str, provider: str, domain: str | None) -> bool:
    """Whether a Registry server (`com.stripe/mcp`) is the provider's own: its reverse-DNS namespace is the
    provider's domain (or a subdomain of it); with no domain given, its organisation is the provider's name.
    A GitHub namespace (`io.github.<org>`) is the provider's when the org is the provider's name."""
    namespace = (namespace_name or "").split("/", 1)[0].lower()
    labels = [x for x in namespace.split(".") if x]
    if len(labels) < 2:
        return False
    slug = _slug(provider)
    if labels[:2] == ["io", "github"]:
        return len(labels) >= 3 and _slug(labels[2]) == slug
    host = ".".join(reversed(labels))
    if domain:
        want = (urlsplit(domain if "//" in domain else f"//{domain}").hostname or domain).lower().removeprefix("www.")
        return host == want or host.endswith("." + want)
    return _slug(labels[1]) == slug


async def search_mcp_registry(args: dict, server_module: Any, events: list[dict]) -> dict:
    from core.safe_fetch import FetchRefused, fetch_bytes

    provider = str(args.get("provider") or "").strip()
    domain = str(args.get("domain") or "").strip() or None
    if not provider:
        return {"error": "Name the provider to look for."}
    url = f"{REGISTRY}?search={quote(provider)}&version=latest&limit={_REGISTRY_READ}"
    try:
        body = json.loads(await fetch_bytes(url))
    except (FetchRefused, ValueError) as exc:
        return {"servers": [], "note": f"The MCP Registry could not be read ({exc}). Search the web next."}
    servers = []
    for item in body.get("servers") or []:
        server = item.get("server") or item
        remotes = [r.get("url") for r in server.get("remotes") or [] if r.get("url")]
        servers.append({
            "name": server.get("name"),
            "description": (server.get("description") or "")[:200],
            "providers_own": provider_owns(server.get("name") or "", provider, domain),
            "remote_url": remotes[0] if remotes else None,
            "install_by_hand": not remotes,
            "website": server.get("websiteUrl"),
        })
    servers.sort(key=lambda s: (not s["providers_own"], s["install_by_hand"]))
    own = [s for s in servers if s["providers_own"]]
    note = ("The provider's own server is first." if own else
            f"None of these is {provider}'s own server (their namespace is not its domain); a third party's server "
            "is not the provider's, so say so if you offer one. Search the web for its API next.")
    return {"servers": servers[:_REGISTRY_KEEP], "note": note}


register_builder_tool(REGISTRY_SCHEMA, search_mcp_registry)


# ── The findings ────────────────────────────────────────────────────────────────────────────────────────────────────

FINDINGS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "report_findings",
        "description": (
            "End the research: one option per provider you looked at, each with how it would connect here "
            "(mcp: its own MCP server; api: a public API with docs; none: no public way in, partners only, or "
            "nothing found; unclear: you could not tell), what the person needs to use it, and the pages it rests "
            "on. The person sees this as a card. Call it once."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "What the person wants done, in a few words"},
                "options": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "provider": {"type": "string"},
                            "how": {"type": "string", "enum": list(_HOW)},
                            "detail": {"type": "string", "description": "One sentence: what it does here"},
                            "needs": {"type": "string", "description": "An account, a key, partner approval…"},
                            "sources": {"type": "array", "items": {"type": "string"}, "description": "URLs read"},
                        },
                        "required": ["provider", "how"],
                    },
                },
            },
            "required": ["options"],
        },
    },
}


async def report_findings(args: dict, server_module: Any, events: list[dict]) -> dict:
    options = []
    for raw in args.get("options") or []:
        if not isinstance(raw, dict) or not str(raw.get("provider") or "").strip():
            continue
        how = str(raw.get("how") or "unclear").lower()
        sources = [s for s in (raw.get("sources") or []) if isinstance(s, str) and s.startswith(("https://", "http://"))]
        options.append({"provider": str(raw["provider"]).strip()[:80], "how": how if how in _HOW else "unclear",
                        "detail": str(raw.get("detail") or "")[:300], "needs": str(raw.get("needs") or "")[:200],
                        "sources": sources[:5]})
    if not options:
        return {"error": "Give at least one option, with its provider and how it would connect."}
    findings = {"goal": str(args.get("goal") or "")[:200], "options": options}
    events.append({"type": "research_findings", **findings})
    return {"status": "reported", **findings}


register_builder_tool(FINDINGS_SCHEMA, report_findings)
