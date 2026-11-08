"""Research the one planner calls while it plans (D340 D2; Naveen 10-30: "researcher is also everything", 10-31: the
plan as restated).

Research ran only after a plan was checked, and only to find who provides a tool nothing here had: every other fact
a plan rests on — the site a person named, an API's auth, how the product itself does a thing — was guessed. Now the
planner can finish with `research_questions`: up to four questions, each tagged

* **product** — how this product does something: code answers from its own help (`builder_help.search_help`);
* **public** — a fact on the web: code reads a URL the question names through the guarded read
  (`named_sites.read_named`), and what code can't settle goes to the researcher with the exact questions, inside
  the plan's web budget (`builder_research.web_call`: 6 calls a question, 24 a plan);
* **business** — what only the person knows: asked in one form, the same `ask_person` round the planner's own
  questions take (F1253), with any public question the researcher couldn't settle.

The answers go back into the planner's next pass (`state.plan_answers`), typed, with their sources: the planner
never reads a raw page. Each research round is one of the planner's passes, under its cap. The provider lookup after
the plan check stays as it was.
"""
from __future__ import annotations

from typing import Any

from core.builder_registry import register_builder_tool
from core.log import get_logger

_log = get_logger(__name__)

#: The questions one research round asks, at most.
QUESTIONS = 4
KINDS = ("product", "public", "business")
#: What a code answer from the help says, at most.
HELP_CHARS = 700

RESEARCH = {"type": "function", "function": {
    "name": "research_questions",
    "description": (
        "Finish to get a fact the plan rests on that you don't have: up to 4 questions. `product`: how this product "
        "does something (its help answers). `public`: a fact on the web — a site's pages, an API's auth or limits, "
        "who provides a service (give the URL when there is one). `business`: what only the person knows — their "
        "process, their customers, a value of theirs (they are asked in one form). Each with `why` the plan needs it. "
        "The answers come back on your next pass. Never research what the pack or read_workspace already tells you."),
    "parameters": {"type": "object", "properties": {"questions": {"type": "array", "maxItems": QUESTIONS, "items": {
        "type": "object", "properties": {
            "kind": {"type": "string", "enum": list(KINDS)},
            "ask": {"type": "string", "description": "The question, as one sentence"},
            "why": {"type": "string", "description": "What in the plan rests on it"},
            "url": {"type": "string", "description": "A web address the question is about, if any"},
        }, "required": ["kind", "ask"]}}}, "required": ["questions"]},
}}


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _state() -> dict:
    from core.native_builder.change_tools import _run

    _, run = _run()
    return run.shared_state if run is not None else {}


async def research(args: dict, server_module: Any, events: list[dict]) -> dict:
    """The planner's `research_questions` finish: its questions, checked — never more than four, each a kind and an ask."""
    from core.secret_scrub import asks_for_a_secret

    raw = [q for q in args.get("questions") or [] if isinstance(q, dict)]
    if not raw:
        return {"error": "research_questions needs at least one question, each with its kind and ask."}
    if len(raw) > QUESTIONS:
        return {"error": f"Ask at most {QUESTIONS} questions in one research round: the ones the plan rests on most."}
    questions = []
    for q in raw:
        kind, ask = q.get("kind"), _text(q.get("ask"))
        if kind not in KINDS or not ask:
            return {"error": f"Each question needs a kind ({', '.join(KINDS)}) and the ask itself."}
        if asks_for_a_secret(ask):
            return {"error": "A key, a token or a password is never researched or asked for: connections come "
                             "through a card."}
        questions.append({"kind": kind, "ask": ask, "why": _text(q.get("why")), "url": _text(q.get("url"))})
    return {"finish": "research_questions", "questions": questions}


async def _from_help(ask: str) -> dict | None:
    """A product question answered from the product's own help, or None when it has nothing on it."""
    from core.builder_help import search_help

    try:
        found = await search_help({"question": ask}, None, [])
    except Exception:  # noqa: BLE001 — the help unread is a question left, never a run that fails
        _log.warning("research: the help could not be searched", exc_info=True)
        return None
    hits = [h for h in (found or {}).get("topics") or [] if isinstance(h, dict)]
    if not hits:
        return None
    lines = []
    for hit in hits[:3]:
        steps = " ".join(_text(x) for x in hit.get("steps") or [])
        body = " ".join(x for x in (_text(hit.get("summary")), steps) if x)
        title = _text(hit.get("title"))
        lines.append(f"{title}: {body}" if title else body)
    answer = " · ".join(x for x in lines if x)[:HELP_CHARS]
    sources = [s for s in (_text(h.get("screen") or h.get("where") or h.get("title")) for h in hits[:3]) if s]
    return {"answer": answer, "sources": sources, "sure": True} if answer else None


async def _from_page(question: dict) -> dict | None:
    """A public question about a page: what the guarded read found there, typed, or None when nothing was read."""
    from core.native_builder import named_sites

    text = " ".join(x for x in (question.get("url"), question["ask"]) if x)
    if not named_sites.named(text):
        return None
    state = _state()
    kept = state.get(named_sites.PAGES_KEY)
    already = {**((state.get("plan_answers") or {}).get("pages") or {}), **(kept if isinstance(kept, dict) else {})}
    pages = await named_sites.read_named(text, already=named_sites.settled(already))
    if pages:
        # Read once a conversation, as gather's are (D340 D2).
        state[named_sites.PAGES_KEY] = {**already, **pages}
    reached = [p for p in pages.values() if isinstance(p, dict) and p.get("reached")]
    if not reached:
        why = next((p.get("why_not") for p in pages.values() if isinstance(p, dict) and p.get("why_not")), "")
        return {"answer": f"The page could not be read: {why}" if why else "The page could not be read.",
                "sources": list(pages), "sure": False, "pages": pages}
    facts = []
    for page in reached:
        kind = {"site": "a site of many pages", "page": "a single page",
                "needs_browser": "a page that needs a browser to read (scripted)"}.get(page.get("kind") or "", "")
        bits = [f"{page.get('final_url') or page.get('url')}: “{page.get('title') or ''}”",
                f"it says “{page.get('what')}”" if page.get("what") else "", kind,
                f"API spec: {page['api_spec']}" if page.get("api_spec") else ""]
        facts.append(", ".join(b for b in bits if b))
    return {"answer": "; ".join(facts), "sources": [p.get("final_url") or p.get("url") for p in reached],
            "sure": True, "pages": pages}


def _next(left: list, business: list) -> str:
    return "researcher" if left else ("person" if business else "plan")


async def answer_research(args: dict, server_module: Any, events: list[dict]) -> dict:
    """Code's answers to the planner's research questions, before any model (step `step_research_answer`)."""
    state = _state()
    questions = list(((state.get("plan_draft") or {}).get("questions")) or [])
    before = state.get("plan_answers") if isinstance(state.get("plan_answers"), dict) else {}
    answers = list(before.get("answers") or [])
    pages = dict(before.get("pages") or {})
    left, business = [], []
    for q in questions:
        if q.get("kind") == "business":
            business.append(q)
            continue
        found = await (_from_help(q["ask"]) if q.get("kind") == "product" else _from_page(q))
        if found is not None:
            pages.update(found.pop("pages", {}) or {})
            answers.append({"ask": q["ask"], "kind": q["kind"], "by": "code", **found})
            if found.get("sure") or q.get("kind") == "product":
                continue
        if q.get("kind") == "product":
            answers.append({"ask": q["ask"], "kind": "product", "by": "code", "sure": False, "sources": [],
                            "answer": "The product's help says nothing on this: it may not be done here."})
            continue
        left.append(q)
    return {"object": "research_answers", "answers": answers, "pages": pages, "left": left,
            "business": business, "next": _next(left, business)}


async def settle_research(args: dict, server_module: Any, events: list[dict]) -> dict:
    """The researcher's answers kept beside code's; a public question it couldn't settle goes to the person, with the
    business questions (step `step_research_settle`)."""
    state = _state()
    before = state.get("plan_answers") if isinstance(state.get("plan_answers"), dict) else {}
    found = state.get("research_found") if isinstance(state.get("research_found"), dict) else {}
    answered = {_text(a.get("ask")).casefold(): a for a in found.get("answers") or [] if isinstance(a, dict)}
    answers, still = list(before.get("answers") or []), []
    for q in before.get("left") or []:
        got = answered.get(_text(q.get("ask")).casefold())
        if got and _text(got.get("answer")) and got.get("sure") is not False:
            answers.append({"ask": q["ask"], "kind": q.get("kind", "public"), "by": "research",
                            "answer": _text(got.get("answer")), "sources": list(got.get("sources") or []),
                            "sure": True})
        else:
            still.append(q)
    if found.get("options"):
        answers.append({"ask": "who provides it", "kind": "public", "by": "research", "sure": True,
                        "answer": "; ".join(f"{o.get('provider')}: {o.get('how')}" for o in found["options"]
                                            if isinstance(o, dict)), "sources": []})
    business = list(before.get("business") or []) + still
    return {**before, "object": "research_answers", "answers": answers, "left": [], "business": business,
            "next": _next([], business)}


async def ask_research(args: dict, server_module: Any, events: list[dict]) -> dict:
    """The questions only the person can answer, as the planner's own one-form round (`ask_person`, F1253): what
    comes back is an `ask_person` finish, so the plan check asks it and keeps the answers as it keeps any (F1226)."""
    from core.native_builder.change_tools import ask_person

    state = _state()
    asked = list(((state.get("plan_answers") or {}).get("business")) or [])
    if not asked:
        return {"finish": "cannot", "why": "There was nothing to ask.", "nearest": "Say again what you want."}
    form = [{"question": q["ask"], "about": "what"} for q in asked]
    draft = await ask_person({"questions": form}, server_module, events)
    if "error" in draft:
        # A question the round refuses (asked already, too many): the planner goes on with what it has.
        return {"finish": "cannot", "why": draft["error"], "nearest": "Answer it in your next message."}
    return draft


for _schema, _handler in (
    (RESEARCH, research),
    ({"type": "function", "function": {"name": "answer_research", "description": (
        "Synapse AI's own step: code's answers to the planner's research questions. No arguments."),
        "parameters": {"type": "object", "properties": {}}}}, answer_research),
    ({"type": "function", "function": {"name": "settle_research", "description": (
        "Synapse AI's own step: the researcher's answers kept beside code's. No arguments."),
        "parameters": {"type": "object", "properties": {}}}}, settle_research),
    ({"type": "function", "function": {"name": "ask_research", "description": (
        "Synapse AI's own step: what only the person knows, asked in one form. No arguments."),
        "parameters": {"type": "object", "properties": {}}}}, ask_research),
):
    register_builder_tool(_schema, _handler)
