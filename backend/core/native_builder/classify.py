"""The quick model types the request; code overrides it with facts (SAI2-P4, F979; DESIGN.md decision 4).

`step_classify` is a quick tool step whose one forced call, `classify_request`, writes a `Request`: four kinds
(answer, change, build, research), the providers named, a second request (`also`), the language, and what an answer
needs. The handler then corrects that reading in code (`override`) from what code knows — what exists, the role,
the findings the last turn ended on, what this workspace can connect — and returns a `Routed`: the entry the turn
goes to, and the rule that moved it, so the router measure can tell the model's word from code's.

Nothing here is a prompt rule: each override is a fact about this workspace or this person.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from core.native_builder.objects import Request, Routed
from core.native_builder.review import is_yes
from core.native_builder.target import _words

_log = logging.getLogger(__name__)

#: The four kinds, where each goes first, and what each is: the legend's routes (`/api/builder/legend`).
KINDS = {
    "answer": ("step_answer", "Telling or talking: what exists here and what has happened, how the product works and "
                              "how to call its API, ideas, opinions and small talk. Read only what the answer needs."),
    "change": ("step_change_context", "Something that exists ends up different: run, pause, turn on or off, rename, "
                                      "move, delete, or edit an agent, a schedule, a tool or a skill."),
    "build": ("step_plan_room", "Something new is made — a workflow, an agent, a team, a tool — or a workflow's "
                                "steps are edited."),
    "research": ("step_research", "An outside company's service is needed to book, buy, pay, ship or order, or who "
                                  "could do it is asked."),
}


# ── what a message says, in words code reads ──────────────────────────────────────────────────────────────────────

#: A button act, said as a request: at the start, or after a please / can you.
_ACT = re.compile(
    r"(?:^|\b(?:please|pls|can you|could you|would you|now|then|and|just)\s+)"
    r"(?:turn(?: \w+){0,2} (?:on|off)|switch(?: \w+){0,2} (?:on|off)|enable|disable|pause|unpause|resume|run|start|"
    r"stop|cancel|kick off|"
    r"trigger|delete|remove|get rid of)\b")
#: A change to something that exists.
_CHANGE = re.compile(
    r"\b(?:change|edit|update|set|move|rename|reschedule|switch|make|give|attach|detach|turn)\b")
#: A change verb that is never talk, named thing or not ("rename it to Reception").
_CHANGE_ONLY = re.compile(r"\b(?:change|edit|update|rename|reschedule|move|delete|remove|detach)\b")
#: Words that ask for something new.
_NEW = re.compile(r"\b(?:new|another|create|build me|make me|from scratch|set up a|a fresh)\b")
#: Words that edit what is open.
_EDIT = re.compile(r"\b(?:add|change|edit|update|remove|modify|fix|rename|replace|insert|also|instead|make it)\b")
#: Asking to use an outside service: what a lookup answers, never a bare "can't".
_USE = re.compile(
    r"\b(?:integrate|sync|use|work with|works with|support|supports|post to|send to|send|book|order|buy|pay|ship|"
    r"read from|pull from|push to|can you|can it|can synapse|can we|does it|is there a way|possible)\b")
#: Asking to connect a named service: the answer route's Connect card, or how to connect it — never research (P4's
#: router replay: self-hosted has no catalogue, so "found nowhere" sent every connect request away).
_CONNECT = re.compile(r"\b(?:connect|reconnect|hook up|hook into|link|sign in|plug in|how do i|how to|set up)\b")
#: A question about how or whether (P4's router replay): an answer, whatever the model labelled it.
_QUESTION = re.compile(r"^(?:how do i|how can i|how to|how does|where do i|where can i|can i|can a|can an|"
                       r"is it possible|is there a way)\b")
#: A weaker start ("when", "is", "what"…) is a question only when the message ends asking ("?"): "When a plan is
#: accepted, send…" is a trigger, not a question (the combined-suite check, 10-27).
_QUESTION_IF_ASKED = re.compile(r"^(?:is|are|was|were|did|does|do i|what|which|why|when|who|how many|how much|has|"
                                r"have)\b")
#: Connecting a named service, said as an instruction; and what makes it an automation instead.
_CONNECT_DO = re.compile(r"\b(?:connect|reconnect|hook up|hook into|link|plug in|sign in to)\b")
_AUTOMATE = re.compile(r"\b(?:every|each|whenever|when|daily|weekly|automat\w*|workflow|build|create|schedule)\b")
#: Build-shaped words, after findings (F766).
_BUILD = re.compile(r"\b(?:build|make|create|set it up|set up|go ahead|do it|proceed)\b")

#: Workspace nouns and run questions: an answer that names one reads the workspace (SAI2's review: err towards it).
#: Words that ask for something to be done ("can you", "I want", "build"): a musing has none of them (F987).
_ASKS_FOR = re.compile(r"\b(?:build|make|create|set (?:it |this |that )?up|go ahead|do it|proceed|can you|could you|"
                       r"would you|will you|please|i want|i need|i d like|we want|we need|let s|lets)\b")
#: A command's first word ("send … every Monday?", "Every Friday, email me …?"): a request, whatever its punctuation.
_COMMAND_FIRST = re.compile(r"^(?:send|email|text|post|notify|remind|fetch|pull|check|watch|summari[sz]e|add|run|"
                            r"schedule|track|collect|every|each|whenever|daily|weekly|monthly|on|at)\b")
_WORKSPACE = re.compile(
    r"\b(?:agents?|workflows?|orchestrations?|runs?|ran|schedules?|scheduled|tools?|skills?|channels?|"
    r"boards?|cards?|knowledge|bases?|credits?|usage|spend|spent|cost|plan|keys?|models?|variables?|widgets?|phone|"
    r"failed|failing|failure|errors?|broke|broken|stopped|stuck|waiting|today|yesterday|this week|last week|status|"
    r"history|logs?|happened|did .+ do|is .+ (?:on|off|running)|why did|why didn t|why is|why isn t|why does|"
    r"why doesn t|why was|why wasn t)\b")
#: How the product works, its API and MCP: the help.
_HELP = re.compile(
    r"\b(?:how do i|how can i|how to|how does|api|apis|mcp|sdk|docs|documentation|endpoint|python|curl|javascript|"
    r"typescript|webhooks?|can synapse|does synapse|can it|does it support|"
    r"supports?|feature|features|pricing|price|plans?|tutorial|guide|where do i|where is the)\b")

_KINDS = {"schedule": ("schedule", "schedules"), "agent": ("agent", "agents"),
          "workflow": ("workflow", "workflows", "orchestration")}
_STOP = frozenset("the a an my our this that for to of and on off in at with every each daily nightly".split())


def _named(text: str, inventory: dict[str, list[str]]) -> dict[str, list[str]]:
    """The existing things `text` names: a whole name, or a distinctive word of one beside its kind's word."""
    found: dict[str, list[str]] = {}
    padded = f" {text} "
    for kind, names in inventory.items():
        for name in names:
            plain = _words(name)
            if not plain:
                continue
            whole = f" {plain} " in padded
            near = any(f" {w} " in padded for w in _KINDS.get(kind, ())) and any(
                f" {word} " in padded for word in plain.split() if len(word) >= 4 and word not in _STOP)
            if whole or near:
                found.setdefault(kind, []).append(name)
    return found


def _found(provider: str, connectable: set[str]) -> bool:
    want = _words(provider)
    return bool(want) and any(want == c or want in c or c in want for c in (_words(x) for x in connectable) if c)


def _findings(value: Any) -> bool:
    return isinstance(value, dict) and bool(value.get("options"))


# ── the overrides ─────────────────────────────────────────────────────────────────────────────────────────────────

def override(request: Request, *, message: str, state: dict, inventory: dict[str, list[str]],
             connectable: set[str]) -> Routed:
    """`request` corrected from what code knows, with the entry it goes to."""
    text = _words(message)
    if state.get("export_source"):
        # Pasted export code is a new build, whatever it names or says (SAI2's review 1).
        routed = Routed(**{**request.model_dump(), "kind": "build", "needs": "workspace"}, route="step_plan_room",
                        overridden_by=None if request.kind == "build" else "export", model_kind=request.kind,
                        model_needs=request.needs)
        return routed if _may("build", state) else routed.model_copy(update={"route": "step_not_yours"})
    kind, by = request.kind, None
    named = _named(text, inventory)
    things = {k: v for k, v in named.items() if v}

    # An act on something that exists is a change; P2 decides card or review (F755/act).
    if _ACT.search(text) and things and kind != "change":
        kind, by = "change", "act_on_existing"
    # A change verb on a named schedule or agent is a change, never a build ("change the schedule to 09:00").
    elif kind == "build" and not _NEW.search(text) and _CHANGE.search(text) and (
            things.get("schedule") or things.get("agent")
            or any(w in text.split() for k in ("schedule", "agent") if inventory.get(k) for w in _KINDS[k])):
        kind, by = "change", "named_change"
    # Findings, then a yes or a build: the build plans from them (F766).
    elif _findings(state.get("prior_findings")) and kind != "build" and (
            kind == "research" or is_yes(message) or (_BUILD.search(text) and kind != "answer")):
        kind, by = "build", "after_findings"
    # A question about how or whether is an answer (P4's router replay: Nova Pro labelled how-tos as changes).
    elif kind in ("change", "build") and not _ACT.search(text) and (
            _QUESTION.match(text) or (_QUESTION_IF_ASKED.match(text) and message.rstrip().endswith("?"))):
        kind, by = "answer", "question"
    # Thinking aloud ("I wonder if there's a way … maybe a reminder system?") asks for nothing: talk, not a plan (F987).
    elif kind == "build" and message.rstrip().endswith("?") and not _ASKS_FOR.search(text) and not \
            _COMMAND_FIRST.match(text):
        kind, by = "answer", "musing"
    # Connecting a named service is the answer route's Connect card, unless an automation through it is asked for.
    elif kind in ("change", "build") and request.providers and _CONNECT_DO.search(text) and not _AUTOMATE.search(
            text) and any(_words(p) and _words(p) in text for p in request.providers):
        kind, by = "answer", "connect_named"
    # An outside service found nowhere here is looked up, never answered "can't" (BWA1 #8).
    elif kind == "answer" and request.providers and _USE.search(text) and not _CONNECT.search(text) and not all(
            _found(p, connectable) for p in request.providers):
        kind, by = "research", "provider_unknown"

    needs = _needs(kind, request.needs, text, things)
    route = _entry(kind, text, state)
    if route == "step_answer" and kind == "build":
        needs = "workspace"  # two workflows match: the answer asks which, from the list
    if route not in ("step_answer", "step_research") and not _may(kind, state):
        route = "step_not_yours"
    return Routed(**{**request.model_dump(), "kind": kind, "needs": needs}, route=route, overridden_by=by,
                  model_kind=request.kind, model_needs=request.needs)


def _needs(kind: str, said: str | None, text: str, things: dict) -> str:
    """What the answer reads. `none` only when the model did not ask for more and code finds nothing to read."""
    if kind != "answer":
        return "workspace"
    if things or _WORKSPACE.search(text) or _ACT.search(text) or _CHANGE_ONLY.search(text):
        return "workspace"  # an act or a change with nothing named is the model's to place, but never talk
    if said in ("workspace", "help"):
        return said
    if _HELP.search(text):
        return "help"
    return "none"


def _entry(kind: str, text: str, state: dict) -> str:
    if kind == "answer":
        return "step_answer"
    if kind == "research":
        return "step_research"
    if kind == "change":
        return "step_change_context"
    # A build: a new one, or the workflow it names or has open.
    source = state.get("current_orchestration_source") or ""
    if state.get("export_source"):
        return "step_plan_room"
    if source == "ambiguous":
        return "step_answer"
    if source in ("named", "picked") and not _NEW.search(text):
        return "step_fetch_existing"
    if source == "viewing" and _EDIT.search(text) and not _NEW.search(text):
        return "step_fetch_existing"
    return "step_plan_room"


def _may(kind: str, state: dict) -> bool:
    """The role door, as `step_may_change` keeps it: a builder builds and changes; an approver reaches change."""
    if state.get("may_change") is not False:
        return True
    return kind == "change" and state.get("may_act") is not False


def intent_of(routed: Routed) -> str:
    """The legacy `state.intent` the role door and the build route read."""
    if routed.kind == "build":
        return "update" if routed.route == "step_fetch_existing" else "create"
    return {"change": "manage"}.get(routed.kind, routed.kind)


# ── the step ──────────────────────────────────────────────────────────────────────────────────────────────────────

async def _inventory() -> dict[str, list[str]]:
    from core.native_builder.change_tools import candidates

    out: dict[str, list[str]] = {}
    for kind in ("schedule", "agent", "workflow"):
        try:
            out[kind] = [str(i["name"]) for i in await candidates(kind) or []]
        except Exception as exc:  # noqa: BLE001 — a store that cannot list it: nothing of that kind is named
            _log.warning("classify: %ss not listed: %s", kind, exc)
            out[kind] = []
    return out


async def _connectable() -> set[str]:
    """What this workspace can connect: its Apps catalogue, the model providers, its own tool servers."""
    from core.builder_connect import _read_options
    from core.store.resources import load_mcp_servers

    options = await _read_options()
    names = {str(e.get(k) or "") for e in options["catalog"] for k in ("slug", "name")}
    names |= {str(p) for p in options["providers"]} | {str(v) for v in options["providers"].values()}
    try:
        names |= {str(s.get("name") or "") for s in await load_mcp_servers()}
    except Exception as exc:  # noqa: BLE001
        _log.warning("classify: tool servers not listed: %s", exc)
    return {n.lower() for n in names if n}


def also_notice(also: str) -> dict:
    """The line that says the second request is kept, not lost (DESIGN.md catalogue 12)."""
    return {"type": "builder_notice", "reason": "also", "final": False,
            "message": f"You also asked: {also}. I'll do this first; say yes when you want me to do that next."}


async def classify_request(args: dict, server_module: Any, events: list[dict]) -> dict:
    """The quick model's `Request`, corrected in code (`override`), as the `Routed` the front door switches on."""
    from core.builder_tools import _current_run
    from core.native_builder import turn_facts
    from core.native_builder.change_tools import _caller
    from core.orchestration.engine import live_run

    run_id = _current_run.get() or ""
    run = live_run(run_id) if run_id else None
    state = run.shared_state if run is not None else {}
    try:
        request = Request.model_validate(args or {})
    except Exception:  # noqa: BLE001 — a call that is not a Request: an answer, read by code alone
        request = Request(kind="answer")
    routed = override(request, message=str(state.get("user_message") or ""), state=state,
                      inventory=await _inventory(), connectable=await _connectable())
    if run is not None:
        # The role door (`step_may_change`) and the build route read the legacy intent.
        run.shared_state["intent"] = intent_of(routed)
    if routed.also and routed.also.strip():
        await turn_facts.keep(run_id, _caller().user_id, also=routed.also.strip())
        events.append(also_notice(routed.also.strip()))
    return routed.model_dump()


SCHEMA = {"type": "function", "function": {
    "name": "classify_request",
    "description": "Say what the person's message asks for, so the right route answers it.",
    "parameters": {k: v for k, v in Request.model_json_schema().items() if k in ("type", "properties", "required")},
}}


def _register() -> None:
    from core.builder_tools import register_builder_tool

    register_builder_tool(SCHEMA, classify_request)


_register()
