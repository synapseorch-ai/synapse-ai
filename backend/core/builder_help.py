"""How-to answers from the product's own help, and what the product cannot do, said honestly (BWA1, D274).

BWA1's Nova Pro baseline answered "How do I connect Slack?" with a screen that does not exist ("Settings →
Integrations") and told people it could build an iPhone app, place a phone call, print forms and guarantee a top
Google ranking. A model asked how a product works answers from what products usually look like.

- `search_help(question)` searches the "?" panel's own words (each product's `lib/help.ts`, generated into
  `native_builder/help_topics.json`, or a host's own index through `set_help_index`) and returns the topics that
  answer it, with the screen each lives on;
- `product_capabilities()` says what this product does and does not do, with the nearest thing that works for
  what it cannot do, so "I can't, but here is what I can" comes from the product, not a guess.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.builder_tools import register_builder_tool

_HELP_FILE = Path(__file__).parent / "native_builder" / "help_topics.json"
_index: dict[str, dict] | None = None
_capabilities: str | None = None

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


HELP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_help",
        "description": (
            "Search this product's own help (what the '?' panel on each screen says) for how to do something here: "
            "connect a channel, set a schedule, invite someone, add a key, put an agent on a website, set a cap. "
            "Answer a how-to question from what it returns, naming the screen it lives on and its steps in its own "
            "words. If nothing it returns answers the question, say you are not sure rather than describing a "
            "screen it did not name."
        ),
        "parameters": {
            "type": "object",
            "properties": {"question": {"type": "string", "description": "The how-to question, in the person's words."}},
            "required": ["question"],
        },
    },
}


async def search_help(args: dict, server_module: Any, events: list[dict]) -> dict:
    found = search(args.get("question") or "")
    if not found:
        return {"topics": [], "note": "The help says nothing about this. Say you are not sure; do not invent a screen."}
    return {"topics": found}


#: What the self-hosted product does and does not do. A host replaces it (`set_capabilities`).
SELF_HOSTED_CAPABILITIES = """\
What this product does:
- Workflows: steps in order or in parallel — agents, single model calls, routing, loops, a person's answer or
  approval (in the app, by email or on a chat channel), tools, data extraction, knowledge retrieval — run by hand,
  on a schedule, from a webhook or the API.
- Agents: a model, a system prompt and the tools it may use (built-in tools, custom HTTP or Python tools, apps
  through MCP servers), with skills, memory and knowledge bases.
- Chat channels (Telegram, Discord, Slack, Teams, WhatsApp), a website widget, and a phone line that answers
  incoming calls on a Twilio number.
- Approvals: any step or tool call can wait for a person before it acts.

What it does not do, and the nearest thing that does:
- It does not build native mobile or desktop apps. Nearest: an agent on the website widget or a chat channel, or a
  workflow behind an API.
- It does not place a phone call on request. Its phone line answers incoming calls. Nearest: a message on a chat
  channel or an email.
- It does not drive printers or other devices, or reach into someone else's computer. Nearest: prepare the document
  and send it (email, chat) for the person to print. A self-hosted install can read the folders on this machine that
  Settings allows.
- It cannot guarantee what others decide (search rankings, prices, approvals). Nearest: draft, research, watch and
  report.
- It never takes card numbers or passwords in chat, and does not store card data. Payments and bookings go through
  the provider's own checkout or a payment link; a service connects through its sign-in or a key on a secure screen.
- It cannot use a service that offers no API or app it can connect to (some booking systems are partner-only).
  Nearest: a workflow that researches the options and asks the person to book.
- Runs are not free: they cost what the models they use charge. Nearest: caps per run, smaller models for simple
  steps.
"""

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


async def product_capabilities(args: dict, server_module: Any, events: list[dict]) -> dict:
    return {"capabilities": _capabilities or SELF_HOSTED_CAPABILITIES}


register_builder_tool(HELP_SCHEMA, search_help)
register_builder_tool(CAPABILITIES_SCHEMA, product_capabilities)
