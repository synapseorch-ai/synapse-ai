"""How-to answers from the product's own help, and what the product cannot do, said honestly (BWA1, D274).

BWA1's Nova Pro baseline answered "How do I connect Slack?" with a screen that does not exist ("Settings →
Integrations") and told people it could build an iPhone app, place a phone call, print forms and guarantee a top
Google ranking. A model asked how a product works answers from what products usually look like.

- `search_help(question)` searches the "?" panel's own words (each product's `lib/help.ts`, generated into
  `native_builder/help_topics.json`, or a host's own index through `set_help_index`) and returns the topics that
  answer it, with the screen each lives on;
- `product_capabilities()` says what this product does and does not do, with the nearest thing that works for
  what it cannot do, so "I can't, but here is what I can" comes from the product, not a guess.

`search_help` also searches the docs site's pages (F1257, D340 A): `native_builder/docs_topics.json`, a snapshot made
by `scripts/docs_index.py` at each docs publish. The "?" panel's topics come first, the screen's own words; the docs
pages after them, for the depth the panel leaves out. A host reads only the pages for its edition (`set_docs_edition`:
the self-hosted product never cites a cloud-only page), or its own index (`set_docs_index`).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core import feature_census
from core.builder_registry import register_builder_tool

_HELP_FILE = Path(__file__).parent / "native_builder" / "help_topics.json"
_DOCS_FILE = Path(__file__).parent / "native_builder" / "docs_topics.json"
_index: dict[str, dict] | None = None
_capabilities: str | None = None
#: The docs pages, read once on the first search (half a megabyte: never on import).
_docs: list[dict] | None = None
#: Which edition's pages a search may cite: this product's, and those for both.
_edition = "self_hosted"
DOCS_SITE = "https://docs.synapseorch.com"
#: How many docs pages a search returns, after the panel's topics.
DOCS_TOP = 3

_STOP = frozenset("""
    a an and are as at be by can do does for from how i in into is it its me my of on or our so the their them then
    there this to up use using we what when where which who why will with you your want need get make set please
""".split())


def set_help_index(topics: dict[str, dict] | None) -> None:
    """A host's own help (cloud's `lib/help.ts`), in the same shape: {key: {title, summary, steps, guide?, where?}}."""
    global _index
    _index = topics


def set_capabilities(text: str | None) -> None:
    """A host's own statement of what it does and does not do."""
    global _capabilities
    _capabilities = text


def set_docs_index(pages: list[dict] | None) -> None:
    """A host's own docs pages, in `docs_topics.json`'s shape; None goes back to the shipped snapshot."""
    global _docs
    _docs = pages


def set_docs_edition(edition: str) -> None:
    """The edition a host is (`self_hosted` or `cloud`): its searches cite its own pages and those for both."""
    global _edition
    _edition = edition


def _docs_pages() -> list[dict]:
    global _docs
    if _docs is None:
        _docs = json.loads(_DOCS_FILE.read_text(encoding="utf-8")).get("pages") or []
    return _docs


def _topics() -> dict[str, dict]:
    global _index
    if _index is None:
        _index = json.loads(_HELP_FILE.read_text())
    return _index


def _stem(word: str) -> str:
    """Plurals and simple endings folded: schedules → schedule, policies → policy, boxes → box, connecting →
    connect, runs → run."""
    if len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("es") and word[:-2].endswith(("s", "x", "z", "ch", "sh")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    for end in ("ing", "ed"):
        if word.endswith(end) and len(word) - len(end) >= 4:
            return word[: -len(end)]
    return word


def _words(text: str) -> list[str]:
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _STOP and len(w) > 1]


#: The words people use for what the help calls by another name. A search box's synonyms, nothing more.
_ALSO = {
    "every": "schedule", "daily": "schedule", "weekly": "schedule", "weekday": "schedule", "hourly": "schedule",
    "cron": "schedule", "timetable": "schedule", "approval": "human", "approve": "human", "website": "widget",
    "embed": "widget", "site": "widget", "bot": "channel", "slack": "channel", "telegram": "channel",
    "spend": "cost", "spending": "cost", "budget": "cost", "credit": "cost", "cap": "cost",
}


def search(question: str, top: int = 5) -> list[dict]:
    """The help topics that best answer `question`, best first; empty when none shares a word with it."""
    asked = set(_words(question))
    asked |= {_ALSO[w] for w in asked if w in _ALSO}
    if not asked:
        return []
    scored = []
    for key, topic in _topics().items():
        title = set(_words(topic.get("title", "")) + _words(key.replace(".", " ").replace("_", " ")))
        summary = set(_words(topic.get("summary", "")))
        body = set(_words(" ".join(topic.get("steps") or []) + " " + str(topic.get("guide") or "")))
        # Distinct words, not occurrences: a long topic must not win on its length.
        score = 5 * len(asked & title) + 2 * len(asked & summary) + len(asked & body)
        if score:
            scored.append((score, key, topic))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [{"topic": key, "title": t.get("title"), "summary": t.get("summary"), "steps": t.get("steps") or [],
             "where": (t.get("where") or {}).get("href"), "screen": (t.get("where") or {}).get("label")}
            for _, key, t in scored[:top]]


def search_docs(question: str, top: int = DOCS_TOP) -> list[dict]:
    """The docs pages for this edition that best answer `question`, best first, each at its best section. A guide
    outranks a generated API reference page on the same words; empty when none shares a word with it. A feature the
    question names in its own words ("evaluate … with a rubric" is Evals, by the census row's names) brings the
    feature's own name along, so its page is found though the question never says it."""
    from core.native_builder.features import named_in, words_of
    asked = set(_words(question))
    asked |= {_ALSO[w] for w in asked if w in _ALSO}
    asked |= {w for screen in named_in(question) for name in words_of(screen) for w in _words(name)}
    if not asked:
        return []
    scored = []
    for page in _docs_pages():
        if page.get("edition") not in ("both", _edition):
            continue
        title = set(_words(page.get("title", "")))
        about = set(_words(page.get("description", "")))
        best, at = 0, {"heading": "", "anchor": "", "excerpt": ""}
        for section in page.get("sections") or []:
            here = 3 * len(asked & set(_words(section.get("heading", "")))) + len(
                asked & set(_words(section.get("excerpt", ""))))
            if here > best:
                best, at = here, section
        score = 5 * len(asked & title) + 2 * len(asked & about) + best
        if page.get("kind") == "reference":
            score //= 2
        if score:
            scored.append((score, page.get("slug", ""), page, at))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [{"title": p.get("title"), "section": at.get("heading") or None,
             "url": f"{DOCS_SITE}{slug}" + (f"#{at['anchor']}" if at.get("anchor") else ""),
             "excerpt": at.get("excerpt") or p.get("description") or ""}
            for _, slug, p, at in scored[:top]]


HELP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_help",
        "description": (
            "Search this product's own help (what the '?' panel on each screen says) for how to do something here: "
            "connect a channel, set a schedule, invite someone, add a key, put an agent on a website, set a cap. "
            "Answer a how-to question from what it returns, naming the screen it lives on and its steps in its own "
            "words. `docs` adds the docs site's pages on it, with their links, for more than the panel says. If "
            "nothing it returns answers the question, say you are not sure rather than describing a screen it did "
            "not name."
        ),
        "parameters": {
            "type": "object",
            "properties": {"question": {"type": "string", "description": "The how-to question, in the person's words."}},
            "required": ["question"],
        },
    },
}


async def search_help(args: dict, server_module: Any, events: list[dict]) -> dict:
    question = args.get("question") or ""
    found, docs = search(question), search_docs(question)
    out: dict[str, Any] = {"topics": found, **({"docs": docs} if docs else {})}
    if not found:
        out["note"] = ("The '?' panel says nothing about this; answer only from the docs pages below, with their "
                       "links, and do not invent a screen." if docs else
                       "The help says nothing about this. Say you are not sure; do not invent a screen.")
    return out


#: What the self-hosted product does and does not do. A host replaces it (`set_capabilities`). Made from the feature
#: census (F1256): every screen's reach or its "not from here yet", and the product's general limits. Never kept by
#: hand: it left out Evals, Boards and Skills.
SELF_HOSTED_CAPABILITIES = feature_census.capabilities_text()

CAPABILITIES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "product_capabilities",
        "description": (
            "What this product does and does not do, with the nearest thing that works for what it cannot do. "
            "Read it before you say yes to anything unusual, and before you say no: when the answer is no, say so "
            "plainly and offer the nearest thing that works."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}


def capabilities(examples: bool = False) -> str:
    """Who Synapse AI is (F1382), then what this host's product does and does not do: every route reads this. The
    answering steps also read what the product delivers, with an example of each (`examples`); the planner never."""
    delivers = f"{feature_census.WHAT_IT_DELIVERS.strip()}\n\n" if examples else ""
    return f"{feature_census.WHO_YOU_ARE.strip()}\n\n{delivers}{(_capabilities or SELF_HOSTED_CAPABILITIES).strip()}"


async def product_capabilities(args: dict, server_module: Any, events: list[dict]) -> dict:
    return {"capabilities": capabilities()}


register_builder_tool(HELP_SCHEMA, search_help)
register_builder_tool(CAPABILITIES_SCHEMA, product_capabilities)
