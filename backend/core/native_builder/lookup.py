"""Code looks a provider up before any model reads the web (SAI2-P5, D300; DESIGN.md decision 6).

Research was web-first: the Researcher searched, read page heads and wrote prose that a quick model turned into the
findings card. It was the weakest class on every model (Nova Pro 1/12 at SAI2's re-baseline) and the dearest (about
150k tokens a persona). `look_up_providers` looks where the answer is known first, with no model:

1. this product's Apps catalogue (`catalog_entry`, by the provider's whole name): `catalog_app`, with the app;
2. the MCP Registry (`registry_servers`): the provider's own reachable server (`provider_owns`) is `mcp`, its domains
   by F951's anchors, recorded as the Researcher's own Registry search records it;
3. one web search for the provider's developer page, read only on the provider's own domain
   (`_providers_own_page`, with the Registry's anchors when there are any), through the product's scraper.

Code gives only the two verdicts it can stand behind: an app in the catalogue and the provider's own server. A
developer page is a lead (`how: "api"` with no spec, its page already read), never a verdict (Naveen, 10-24):
Booking.com, OpenTable and Uber have developer pages and are partners-only, so whether an API is open, and to whom,
is for the research to read. Code never writes `none`, `unclear` or an `api` spec, and says when a source could not
be read. The findings are P3b's typed `Finding` (P3B-HANDOFF.md), so `report_findings`, the card and the build
route's `_found` read them unchanged; `report_findings` puts the look-up's verdicts in front of the model's.

What was looked up is kept in the research's record for the run (`LOOKED_KEY`, apart from the backstop's `spent`),
so a second look-up reads nothing again and never spends the Researcher's budget. Code's own reading is bounded
instead: at most `MAX_PROVIDERS` providers, each with one Registry search, one web search and one page.
"""
from __future__ import annotations

import re
from typing import Any

# The research module, by its name: it imports builder_tools, which imports this module while it loads.
import core.builder_research as research
from core.builder_tools import register_builder_tool

#: Providers a look-up reads for; the rest are the research's.
MAX_PROVIDERS = 5
#: A page is a developer page when it says at least this many of these (one of them "api").
_API_WORDS = frozenset({"api", "apis", "endpoint", "endpoints", "authentication", "authenticate", "oauth", "rest",
                        "graphql", "sdk", "webhook", "webhooks", "reference", "developer", "developers", "token"})
_DOMAIN = re.compile(r"\b((?:[a-z0-9-]+\.)+[a-z]{2,})\b")

SCHEMA = {
    "type": "function",
    "function": {
        "name": "look_up_providers",
        "description": (
            "(code) Look providers up in this product's Apps catalogue, the MCP Registry and each provider's own "
            "developer page, with no model. Returns what was found (an app here, or the provider's own MCP server), "
            "and what is left for the research, with any developer page already read."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "providers": {"type": "array", "items": {"type": "string"},
                              "description": "Outside services the request names"},
                "needs": {"type": "array", "items": {"type": "object"},
                          "description": "The build's missing tools: {tool, does, provider?}"},
                "need": {"type": "string", "description": "What the person wants done"},
            },
        },
    },
}


async def look_up_providers(args: dict, server_module: Any, events: list[dict]) -> dict:
    need = str(args.get("need") or "").strip()
    asks = await _asks(args, need)
    record, run = research._record()
    looked = record.setdefault(research.LOOKED_KEY, {})
    found, left = [], []
    for ask in asks[:MAX_PROVIDERS]:
        provider = ask.get("provider")
        if not provider:
            left.append({**ask, "note": "No provider is named for this yet: the research finds who provides it."})
            continue
        key = research._slug(provider)
        if key not in looked:
            looked[key] = await _look(provider, ask.get("domain"), server_module)
            await research._keep(run)
        got = looked[key]
        if got.get("found"):
            if got["found"] not in found:
                found.append(got["found"])
        else:
            left.append({k: v for k, v in ask.items() if k != "domain"} | got["left"])
    return {"findings": {"goal": need[:200], "options": found}, "found": found, "left": left}


async def _asks(args: dict, need: str) -> list[dict]:
    """What to look up: the request's providers (P4's `Request.providers`), the build's needs (SAI2-P3's), or, when
    neither came (a paused run on the old definition, a channel turn), the catalogue's names and the domains the
    message itself names. A need that names no provider takes one only from a catalogue name in its words."""
    from core.builder_connect import _read_options

    catalog = (await _read_options()).get("catalog") or []
    asks: list[dict] = []
    for p in args.get("providers") or []:
        if isinstance(p, str) and p.strip():
            asks.append({"provider": p.strip()[:80]})
    for n in args.get("needs") or []:
        if not isinstance(n, dict):
            continue
        ask = {k: str(n[k])[:200] for k in ("tool", "does") if n.get(k)}
        provider = str(n.get("provider") or "").strip()
        if not provider:
            said = set(research._words(f"{n.get('tool') or ''} {n.get('does') or ''}".replace("_", " ")))
            entry = next((e for e in catalog if set(research._words(str(e.get("name") or ""))) <= said
                          and research._words(str(e.get("name") or ""))), None)
            provider = str(entry.get("name")) if entry else ""
        asks.append({"provider": provider[:80], **ask} if provider else ask)
    if not asks:
        asks = _named_in(need, catalog)
    out, seen = [], set()
    for ask in asks:
        key = research._slug(ask.get("provider") or "") or f"need:{ask.get('tool')}:{ask.get('does')}"
        if key not in seen:
            seen.add(key)
            out.append(ask)
    return out


def _named_in(message: str, catalog: list[dict]) -> list[dict]:
    """The providers a message names that code can tell without a model: an Apps catalogue name, in its words, and
    a domain written out (calendly.com is Calendly)."""
    words = research._words(message)
    joined = f" {' '.join(words)} "
    named = []
    for e in catalog:
        name = research._words(str(e.get("name") or ""))
        if name and f" {' '.join(name)} " in joined:
            named.append({"provider": str(e.get("name"))})
    for domain in _DOMAIN.findall(message.lower()):
        if not research._common_ending(domain):
            continue
        name = research._registrable_name(domain)
        if name:
            named.append({"provider": name[:1].upper() + name[1:], "domain": research._registrable_domain(domain)})
    return named


async def _look(provider: str, domain: str | None, server_module: Any) -> dict:
    """One provider: `{"found": Finding}`, or `{"left": {"provider", "note", "lead"?, "page"?, "page_text"?}}`."""
    from core.builder_connect import _read_options

    entry = research.catalog_entry(provider, (await _read_options()).get("catalog") or [])
    if entry is not None:
        url = str(entry.get("url") or "")
        own = [url] if url.startswith("https://") and research._providers_own_page(provider,
                                                                                  research._host(url)) else []
        name = str(entry.get("name") or provider)
        return {"found": _finding(
            provider, "catalog_app", f"{name} is in this workspace's Apps catalogue: it connects in a click.",
            f"A {name} account", own, sorted({research._registrable_domain(research._host(u)) for u in own}),
            app={"slug": str(entry.get("slug") or ""), "name": name})}

    notes, anchors = [], set()
    servers, error = await research.registry_servers(provider, domain)
    if servers is None:
        notes.append(f"The MCP Registry could not be read ({error}).")
    else:
        own = [s for s in servers if s["providers_own"]]
        reachable = [s for s in own if not s["install_by_hand"]]
        if reachable:
            server = reachable[0]
            page = server.get("website") or server.get("repository") or server["remote_url"]
            domains = research._server_domains(server)
            await research._saw_own_server(provider, page, domains)
            return {"found": _finding(
                provider, "mcp", f"{provider}'s own MCP server is in the MCP Registry ({server['name']}), at "
                                 f"{server['remote_url']}.",
                f"A {provider} account", [page], sorted(domains))}
        for server in own:
            anchors |= research._server_domains(server)
        notes.append(f"{provider}'s own server is in the MCP Registry but must be installed by hand."
                     if own else f"The MCP Registry has no server of {provider}'s own.")

    lead, page, text, said = await _developer_page(provider, anchors, server_module)
    notes.append(said)
    left: dict = {"provider": provider, "note": " ".join(notes)}
    if page:
        # The page read, lead or not: what the research's verdict rests on ("orders are placed by phone").
        left |= {"page": page, "page_text": text}
    if lead is not None:
        left["lead"] = lead
    return {"left": left}


async def _developer_page(provider: str, anchors: set[str], server_module: Any
                          ) -> tuple[dict | None, str, str, str]:
    """One search for the provider's developer page, and the first result on its own domain, read. `(lead, the page
    read, its extract, what was found)`; the lead is None when no own page with an API on it was read."""
    from urllib.parse import quote_plus

    query = quote_plus(f"{provider} API documentation")
    page = await research._scrape(server_module, {"url": research.SEARCH_URL + query, "include_links": True,
                                                  "remove_overlays": True})
    if page.get("error"):
        research._searched("failed")
        return None, "", "", f"The web could not be searched ({page['error'][:120]})."
    results = research.result_links(page)
    if not results:
        research._searched("blocked" if research.search_refused(page) else "empty")
        return None, "", "", ("The web search was refused as automated." if research.search_refused(page)
                              else "The web search gave no results.")
    research._searched("results")
    url = next((r["url"] for r in results if r["url"].startswith("https://")
                and research._providers_own_page(provider, research._host(r["url"]), anchors or None)), None)
    if url is None:
        return None, "", "", f"No result of the web search is on {provider}'s own site."
    read = await research._scrape(server_module, {"url": url, "remove_overlays": True})
    if read.get("error"):
        return None, "", "", f"{provider}'s page {url} could not be read."
    text = str(read.get("markdown") or "")
    await research._read_host(url)
    extract = research.page_extract(text, research._words(provider))
    said = set(research._words(text))
    if "api" not in said or len(said & _API_WORDS) < 2:
        return None, url, extract, f"{provider}'s own page {url} says nothing of an API."
    own_domains = sorted(anchors | {research._registrable_domain(research._host(url))})
    return (_finding(provider, "api", f"{provider} has developer docs at {url}; whether its API is open to this "
                                      "workspace is for the research to read.", "", [url], own_domains),
            url, extract, f"{provider}'s developer page was read: {url}.")


def _finding(provider: str, how: str, detail: str, needs: str, sources: list[str], own_domains: list[str],
             app: dict | None = None) -> dict:
    """P3b's typed finding (P3B-HANDOFF.md)."""
    out = {"provider": provider, "how": how, "detail": detail[:300], "needs": needs[:200], "sources": sources[:5],
           "own_domains": own_domains}
    return out | ({"app": app} if app else {})


register_builder_tool(SCHEMA, look_up_providers)
