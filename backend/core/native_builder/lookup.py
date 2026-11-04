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
from core.builder_registry import register_builder_tool

#: Providers one look-up reads for, and a research's whole run (SAI2's probe: unbounded, six calls of five new names
#: read 30 searches and 30 Registry pages); the rest are the research's. A failed source is tried once more.
MAX_PROVIDERS, MAX_LOOKS, MAX_TRIES = 5, 10, 2
#: A page is a developer page when it says at least this many of these (one of them "api").
_API_WORDS = frozenset({"api", "apis", "endpoint", "endpoints", "authentication", "authenticate", "oauth", "rest",
                        "graphql", "sdk", "webhook", "webhooks", "reference", "developer", "developers", "token"})
_DOMAIN = re.compile(r"\b((?:[a-z0-9-]+\.)+[a-z]{2,})\b")
#: Endings of file names, not of a provider's site: notes.md, app.py and node.js are not providers.
_FILE_ENDINGS = frozenset({"md", "py", "js", "ts", "tsx", "jsx", "sh", "rb", "rs", "go", "txt", "csv", "pdf", "png",
                           "jpg", "jpeg", "gif", "doc", "docx", "xls", "xlsx", "json", "yaml", "yml", "html", "css",
                           "zip", "exe", "log", "sql", "env", "ini", "cfg", "toml"})

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
    # The route's own look-up locks what it finds: its step sets `by_route` (a step's set arguments only; the schema
    # offers it to no model). A look-up the Researcher asks for, of providers it named itself, is its own to report or
    # leave out (SAI2, 10-24; its review, N1).
    locked = args.get("by_route") is True
    unnamed: list[str] = []
    from_the_run = not any(args.get(k) for k in ("providers", "needs", "need"))
    if from_the_run:
        args = _from_the_run()
        args["providers"], unnamed = await _named_only(args["providers"])
    need = str(args.get("need") or "").strip()
    asks = await _asks(args, need)
    record, run = research._record()
    looked = record.setdefault(research.LOOKED_KEY, {})
    found, left, unmatched = [], [], list(unnamed)
    for ask in asks[:MAX_PROVIDERS]:
        provider, said = ask.get("provider"), {k: ask[k] for k in ("provider", "tool", "does") if ask.get(k)}
        if not provider:
            left.append({**said, "note": "No provider is named for this yet: the research finds who provides it."})
            continue
        if not ask.get("named"):
            # SAI2's probe: "Close the Jira ticket" is not the Close app. A name found in the words is the research's.
            left.append({**said, "note": f"{provider}, in the Apps catalogue, matches words of this request; whether "
                                         "it is the one meant is for the research."})
            continue
        key = research._slug(provider)
        got = looked.get(key)
        if got is None or (got.get("failed") and got.get("tries", 1) < MAX_TRIES):
            if record.get("looks", 0) >= MAX_LOOKS:
                left.append({**said, "note": "This research has looked up enough providers in code: the research "
                                             "reads this one."})
                continue
            record["looks"] = record.get("looks", 0) + 1
            got = {**await _look(provider, ask.get("domain"), server_module), "tries": (got or {}).get("tries", 0) + 1,
                   "locked": bool((got or {}).get("locked"))}
            looked[key] = got
        if locked and not got.get("locked"):
            got["locked"] = True
        await research._keep(run)
        if got.get("found"):
            if got["found"] not in found:
                found.append(got["found"])
        else:
            left.append(said | got["left"])
            if not got.get("matched") and not got.get("failed") and not ask.get("domain"):
                unmatched.append(provider)
    # `unmatched`: not named, or named, looked up in full, and nothing of it found (F997's guard: the classify can name
    # a category, "hotel booking"). Never in the Researcher's prompt, which reads only `left`.
    if from_the_run and not found:
        # The gate (SAI2, 10-27): with nothing found, the Researcher starts as it did before P5, with no lead text in
        # its prompt. The leads stay in the run's record, for the fallback card (`finish_findings`).
        left = []
    return {"findings": {"goal": need[:200], "options": found}, "found": found, "left": left, "unmatched": unmatched}


async def _named_only(providers: list[str]) -> tuple[list[str], list[str]]:
    """The request's providers a person named, and the rest (the gate, SAI2 10-27). P5's measure: the classify wrote
    categories as providers, all in lower case ("train operator", "our usual dental supplier"), and real names with a
    capital (Uber, Stripe). Named: a capital letter, an Apps catalogue name, or a domain. A category written with a
    capital is looked up and matches nothing: one look in code, no lead, no tokens."""
    from core.builder_connect import _read_options

    catalog = (await _read_options()).get("catalog") or []
    named, rest = [], []
    for p in providers:
        is_named = (any(c.isupper() for c in p) or research.catalog_entry(p, catalog) is not None
                    or bool(_domains_in(p)))
        (named if is_named else rest).append(p)
    return named, rest


def _from_the_run() -> dict:
    """The research route's step sets no arguments: the run's own request (P4's typed `Request`, when the front door
    wrote one) and message. Read here, not from a template, so a run with no `request` (a paused run on the old
    definition, a channel turn) falls back to the message instead of failing on a missing reference (SAI2, 10-24)."""
    from core.native_builder.build_plan import _obj, _state

    state = _state()
    request = _obj(state.get("request"))
    providers = request.get("providers") if isinstance(request, dict) else None
    return {"providers": [p for p in providers or [] if isinstance(p, str)],
            "need": str(state.get("user_message") or "")}


async def _asks(args: dict, need: str) -> list[dict]:
    """What to look up. `named` marks a provider the person named: the request's providers (P4's
    `Request.providers`), a need's own provider (SAI2-P3's), or a domain written out. Only a named provider can be a
    verdict of code's. An Apps catalogue name found among a need's or the message's words is not named: Close,
    Square and Linear are words too (SAI2's probe). With neither providers nor needs (a paused run on the old
    definition, a channel turn), the message is read for both."""
    from core.builder_connect import _read_options

    catalog = (await _read_options()).get("catalog") or []
    asks: list[dict] = []
    for p in args.get("providers") or []:
        if isinstance(p, str) and p.strip():
            asks.append({"provider": p.strip()[:80], "named": True})
    for n in args.get("needs") or []:
        if not isinstance(n, dict):
            continue
        ask = {k: str(n[k])[:200] for k in ("tool", "does") if n.get(k)}
        provider = str(n.get("provider") or "").strip()
        if provider:
            asks.append({"provider": provider[:80], **ask, "named": True})
            continue
        matches = _catalogue_words(f"{n.get('tool') or ''} {n.get('does') or ''}".replace("_", " "), catalog)
        asks.append({"provider": matches[0], **ask, "named": False} if len(matches) == 1 else ask)
    if not asks:
        asks = [{"provider": name, "named": False} for name in _catalogue_words(need, catalog)] + _domains_in(need)
    out, seen = [], set()
    for ask in asks:
        key = research._slug(ask.get("provider") or "") or f"need:{ask.get('tool')}:{ask.get('does')}"
        if key not in seen:
            seen.add(key)
            out.append(ask)
        elif ask.get("named"):
            # A name both written out and found in the words is named.
            next(o for o in out if research._slug(o.get("provider") or "") == key)["named"] = True
    return out


def _catalogue_words(text: str, catalog: list[dict]) -> list[str]:
    """The Apps catalogue names that appear, word for word, in `text`."""
    joined = f" {' '.join(research._words(text))} "
    names = []
    for e in catalog:
        words = research._words(str(e.get("name") or ""))
        if words and f" {' '.join(words)} " in joined:
            names.append(str(e.get("name")))
    return names


def _domains_in(message: str) -> list[dict]:
    """The providers a message names by their domain (calendly.com is Calendly): named, by the person."""
    named = []
    for domain in _DOMAIN.findall(message.lower()):
        if domain.rsplit(".", 1)[-1] in _FILE_ENDINGS or not research._common_ending(domain):
            continue
        name = research._registrable_name(domain)
        if name:
            named.append({"provider": name[:1].upper() + name[1:], "domain": research._registrable_domain(domain),
                          "named": True})
    return named


def _an(name: str) -> str:
    return f"{'An' if name[:1].lower() in 'aeiou' else 'A'} {name} account"


def _verified(server: dict) -> bool:
    """A Registry namespace the Registry verified by DNS before it published (`com.stripe`); a GitHub namespace
    (`io.github.<user>`) is anyone's who holds that GitHub name (SAI2's probe)."""
    labels = [x for x in str(server.get("name") or "").split("/", 1)[0].lower().split(".") if x]
    return len(labels) >= 2 and labels[:2] != ["io", "github"]


def _reachable(url: str | None) -> bool:
    """A remote a person can connect: https, a public host by name, and no `{…}` left to fill (SAI2's probe:
    `http://10.0.0.1:9/{tenant}/mcp` had counted)."""
    import ipaddress
    from urllib.parse import urlsplit

    if not url or "{" in url or "}" in url:
        return False
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")  # `localhost.` is localhost (SAI2's review, N2)
    if parts.scheme != "https" or "." not in host or host.endswith((".local", ".internal", ".localhost", ".lan")):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return False


async def _look(provider: str, domain: str | None, server_module: Any) -> dict:
    """One named provider: `{"found": Finding}`, or `{"left": {"provider", "note", "lead"?, "page"?}, "failed"?,
    "matched"?}`. `matched`: something of the provider's own was seen (a Registry server, a page on its own site)."""
    from core.builder_connect import _read_options

    entry = research.catalog_entry(provider, (await _read_options()).get("catalog") or [])
    if entry is not None:
        url = str(entry.get("url") or "")
        own = [url] if url.startswith("https://") and research._providers_own_page(provider,
                                                                                  research._host(url)) else []
        name = str(entry.get("name") or provider)
        return {"found": _finding(
            provider, "catalog_app", f"{name} is in this workspace's Apps catalogue: it connects in a click.",
            _an(name), own, sorted({research._registrable_domain(research._host(u)) for u in own}),
            app={"slug": str(entry.get("slug") or ""), "name": name})}

    notes, anchors, failed, matched = [], set(), False, False
    servers, error = await research.registry_servers(provider, domain)
    if servers is None:
        notes.append(f"The MCP Registry could not be read ({error}).")
        failed = True
    else:
        own = [s for s in servers if s["providers_own"] and _verified(s)]
        server = next((s for s in own if _reachable(s.get("remote_url"))), None)
        if server is not None:
            page = server.get("website") or server.get("repository") or server["remote_url"]
            domains = research._server_domains(server)
            await research._saw_own_server(provider, page, domains)
            return {"found": _finding(
                provider, "mcp", f"{provider}'s own MCP server is in the MCP Registry ({server['name']}), at "
                                 f"{server['remote_url']}.",
                _an(provider), [page], sorted(domains))}
        for s in own:
            anchors |= research._server_domains(s)
        matched = bool(own)
        unverified = [s for s in servers if s["providers_own"] and not _verified(s)]
        notes.append(f"{provider}'s own server is in the MCP Registry but can't be reached from here." if own else
                     f"A GitHub account named for {provider} publishes {unverified[0]['name']} in the MCP Registry; "
                     f"the Registry doesn't check that it is {provider}'s." if unverified else
                     f"The MCP Registry has no server of {provider}'s own.")

    record, run = research._record()
    if record.get("web_down"):
        notes.append("The web can't be read here right now.")
        return {"left": {"provider": provider, "note": " ".join(notes)}, "failed": True, "matched": matched}
    web = await _developer_page(provider, anchors, server_module)
    notes.append(web["said"])
    if web.get("down"):
        record["web_down"] = True
        await research._keep(run)
    left: dict = {"provider": provider, "note": " ".join(notes)}
    if web.get("page"):
        # The page read, lead or not: what the research's verdict rests on ("orders are placed by phone"). Its words
        # stay in the research's record, never in the step's output or its history (SAI2-P3): read_web_page hands
        # them to the Researcher again without reading the page twice, marked as the page's own words.
        left["page"] = web["page"]
        record.setdefault(research.PAGES_KEY, {})[web["page"]] = web["text"]
        await research._keep(run)
    if web.get("lead") is not None:
        left["lead"] = web["lead"]
    return {"left": left, "failed": failed or bool(web.get("failed")), "matched": matched or bool(web.get("page"))}


async def _developer_page(provider: str, anchors: set[str], server_module: Any) -> dict:
    """One search for the provider's developer page, and the first result on its own domain, read: `{said, page?,
    text?, lead?, failed?, down?}`. The lead is there only when an own page with an API on it was read."""
    from urllib.parse import quote_plus

    query = quote_plus(f"{provider} API documentation")
    page = await research._scrape(server_module, {"url": research.SEARCH_URL + query, "include_links": True,
                                                  "remove_overlays": True})
    if page.get("error"):
        research._searched("failed")
        return {"said": f"The web could not be searched ({page['error'][:120]}).", "failed": True,
                "down": bool(page.get("down"))}
    results = research.result_links(page)
    if not results:
        refused = research.search_refused(page)
        research._searched("blocked" if refused else "empty")
        return {"said": "The web search was refused as automated." if refused else "The web search gave no results.",
                "failed": refused}
    research._searched("results")
    url = next((r["url"] for r in results if r["url"].startswith("https://")
                and research._providers_own_page(provider, research._host(r["url"]), anchors or None)), None)
    if url is None:
        return {"said": f"No result of the web search is on {provider}'s own site."}
    read = await research._scrape(server_module, {"url": url, "remove_overlays": True})
    if read.get("error"):
        return {"said": f"{provider}'s page {url} could not be read.", "failed": True, "down": bool(read.get("down"))}
    text = str(read.get("markdown") or "")
    await research._read_host(url)
    extract = research.page_extract(text, research._words(provider))
    words = set(research._words(text))
    if "api" not in words or len(words & _API_WORDS) < 2:
        return {"said": f"{provider}'s own page {url} says nothing of an API.", "page": url, "text": extract}
    own_domains = sorted(anchors | {research._registrable_domain(research._host(url))})
    return {"said": f"{provider}'s developer page was read: {url}.", "page": url, "text": extract,
            "lead": _finding(provider, "api", f"{provider} has developer docs at {url}; whether its API is open to "
                                              "this workspace is for the research to read.", "", [url], own_domains)}


def _finding(provider: str, how: str, detail: str, needs: str, sources: list[str], own_domains: list[str],
             app: dict | None = None) -> dict:
    """P3b's typed finding (P3B-HANDOFF.md)."""
    out = {"provider": provider, "how": how, "detail": detail[:300], "needs": needs[:200], "sources": sources[:5],
           "own_domains": own_domains}
    return out | ({"app": app} if app else {})


register_builder_tool(SCHEMA, look_up_providers)


# ── The research route's end: one card, and the reply in code's words ────────────────────────────────────────────

FINISH_SCHEMA = {"type": "function", "function": {
    "name": "finish_findings",
    "description": "(code) The research's findings card and its reply: the Researcher's report as it was, or the "
                   "look-up's when no model reported.",
    "parameters": {"type": "object", "properties": {}}}}


async def finish_findings(args: dict, server_module: Any, events: list[dict]) -> dict:
    """The research route's last word (SAI2-P5). A Researcher that reported has had its card put up by
    `report_findings`: that report passes through as it is, never reported twice (a second report would read a
    cleared record and call every verdict unclear). Otherwise (everything was found by code, or the Researcher ended
    without reporting) the look-up's verdicts are the card, with each provider left over `unclear` and why. Either way
    the reply is written here, from the findings (`said`): the person is never left without an answer (F958's
    class)."""
    from core.native_builder.build_plan import _obj, _state

    state = _state()
    request = _obj(state.get("request"))
    # F997 (SAI2, cnt-02): the person named no provider. Only a run whose front door wrote a request knows that. A
    # "provider" the look-up matched to nothing (a category the classify wrote, "hotel booking") is not one (SAI2's b).
    looked = _obj(state.get("lookup")) or {}
    nothing = {research._slug(p) for p in looked.get("unmatched") or [] if isinstance(p, str)}
    unnamed = isinstance(request, dict) and all(
        research._slug(p) in nothing for p in request.get("providers") or [] if isinstance(p, str))
    reported = _obj(state.get("findings"))
    if isinstance(reported, dict) and reported.get("status") == "reported":
        return {**reported, "said": said(reported, unnamed)}
    goal = str(((looked.get("findings") or {}).get("goal")) or state.get("user_message") or "")[:200]
    options = []
    for left in looked.get("left") or []:
        if not isinstance(left, dict) or not left.get("provider"):
            continue
        lead = left.get("lead") or {}
        options.append({"provider": left["provider"], "how": "unclear",
                        "detail": str(lead.get("detail") or left.get("note") or "")[:300],
                        "sources": [left["page"]] if left.get("page") else []})
    record = research._evidence()
    listed = {research._slug(o["provider"]) for o in options}
    for v in (record.get(research.LOOKED_KEY) or {}).values():
        # The Researcher's own look-up, never reported: what it found is said as code found it, and what it left is
        # said unclear with why (SAI2's review, N3a). The route's own finals are the look-up's `found`; its leads are
        # here too when it found nothing and so handed none on (the gate).
        if not isinstance(v, dict) or (v.get("locked") and v.get("found")):
            continue
        if v.get("found"):
            options.append({"provider": v["found"]["provider"], "how": "unclear"})
        elif (v.get("left") or {}).get("provider") and research._slug(v["left"]["provider"]) not in listed:
            left = v["left"]
            options.append({"provider": left["provider"], "how": "unclear",
                            "detail": str((left.get("lead") or {}).get("detail") or left.get("note") or "")[:300],
                            "sources": [left["page"]] if left.get("page") else []})
    if not options and not looked.get("found"):
        findings = {"status": "nothing", "goal": goal, "options": []}
        return {**findings, "said": said(findings, unnamed)}
    findings = await research.report_findings({"goal": goal, "options": options}, server_module, events)
    if findings.get("error"):
        findings = {"status": "nothing", "goal": goal, "options": []}
    return {**findings, "said": said(findings, unnamed)}


#: What code saw, never "can't": it has not checked every tool here against the need (F998).
SEEN_NOTHING = "I don't see anything in this workspace that does this today."


def said(findings: dict, unnamed: bool = False) -> str:
    """The research's reply, in code's words, from the findings: one line per provider and what comes next. Nothing
    in it asks for a key or says anything was booked. A request that named no provider opens with what code saw in
    the workspace (F997)."""
    options = [o for o in findings.get("options") or [] if isinstance(o, dict)]
    if not options:
        return ((SEEN_NOTHING + " " if unnamed else "") + "I couldn't find who provides this from what I could read. "
                "Tell me the service you use, or one you'd like to, and I'll look it up.")
    lines = []
    for o in options:
        name, how, detail = f"**{o.get('provider')}**", o.get("how"), str(o.get("detail") or "").strip()
        head = {"catalog_app": f"{name} is in your Apps catalogue: it connects with a click.",
                "mcp": f"{name} has its own MCP server.",
                "api": f"{name} has a public API{' whose calls I read' if o.get('api') else ''}.",
                "none": f"{name} has no public way in for this.",
                "partner": f"{name} is for approved partners only"
                           f"{' (' + ', '.join(o['partner_words']) + ')' if o.get('partner_words') else ''}."
                }.get(how, f"{name}: I couldn't confirm a way in.")
        needs = str(o.get("needs") or "").strip().rstrip(".")
        lines.append(" ".join(x for x in (head, detail, f"It needs: {needs}." if needs else "") if x))
    usable = any(o.get("how") in ("catalog_app", "mcp", "api") for o in options)
    nxt = ("Say \"build it\" and I'll draft a plan for you to approve; anything that books, pays or sends asks you "
           "first." if usable else
           "Nothing here can do it for you directly. Say \"build it\" and I'll plan a workflow that finds the options "
           "and leaves the booking or ordering to you.")
    return (SEEN_NOTHING + "\n\n" if unnamed else "") + "\n".join(f"- {line}" for line in lines) + "\n\n" + nxt


register_builder_tool(FINISH_SCHEMA, finish_findings)
