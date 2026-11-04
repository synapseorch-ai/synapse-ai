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

from core.native_builder.objects import Request, RequestAtPlan, Routed
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
#: A how-to, as the message opens (F1342): about the product, it reads the help whatever product nouns it names.
_HOW_TO = re.compile(r"^(?:how do i|how can i|how to|where do i|where can i|where is the)\b")
#: What makes a how-to about this workspace's own state, not the product: what it spent, what failed or is broken.
_OWN_STATE = re.compile(
    r"\b(?:spend|spent|spending|costs?|usage|credits?|bills?|billing|failed|failing|failure|errors?|broke|broken|"
    r"stopped|stuck|waiting|status|history|logs?|happened|today|yesterday|this week|last week|why|fix|fixing|repair|"
    r"not working|working again|work again|doesn t work|does not work|wrong|isn t running|not running)\b")
#: How the product works, its API and MCP: the help.
_HELP = re.compile(
    r"\b(?:how do i|how can i|how to|how does|api|apis|mcp|sdk|docs|documentation|endpoint|python|curl|javascript|"
    r"typescript|webhooks?|can synapse|does synapse|can it|does it support|"
    r"supports?|feature|features|pricing|price|plans?|tutorial|guide|where do i|where is the)\b")

#: A question about a failure that has happened (F1378): "why did my run fail", "why is it failing", "what went wrong".
#: Never a change that only names one ("this keeps failing, add a retry").
_CAUSE = re.compile(
    r"\bwhy (?:did|didn t|was|wasn t|were|has|hasn t|have|had|is|isn t|are)\b(?:\s+\w+){0,6}?\s+"
    r"(?:fail|failed|failing|break|broke|broken|stop|stopped|error|errored|crash|crashed|stuck|"
    r"not work\w*|not run\w*)\b"
    r"|\bwhat went wrong\b")
#: The fix half's words when the message gave none of its own beside the question.
FIX_IT = "fix what made it fail"


def asks_why_it_failed(text: str) -> bool:
    """Whether a message asks why something of this workspace failed (F1378): the answer says it first."""
    return bool(_CAUSE.search(_words(text)))


def _fix_half(message: str, said: str | None) -> str:
    """The rest of a message whose first ask is why something failed: the model's own second ask unless that is the
    question itself, else what follows the question, else `FIX_IT`."""
    if said and said.strip() and not asks_why_it_failed(said):
        return said.strip()
    rest = message.split("?", 1)[1].strip(" .!") if "?" in message else ""
    return rest or FIX_IT


#: A question about Synapse AI itself (F1382): answered by the talk step, with things it could build here.
_ABOUT_YOU = re.compile(r"\b(?:what|how) (?:can|could) (?:you|synapse ai) (?:do|help)\b|\bwhat (?:do|are) you\b|"
                        r"\bwho are you\b|\bwhat can i (?:ask|use) you for\b")
#: Words about this workspace's own things or state (F1382): a fact that changes over time here is read, never offered.
#: `_WORKSPACE` without its time words, which an outside fact says too ("the rate today").
_OURS = re.compile(
    r"\b(?:agents?|workflows?|orchestrations?|runs?|ran|schedules?|scheduled|tools?|skills?|channels?|boards?|cards?|"
    r"knowledge|bases?|credits?|usage|spend|spent|spending|bills?|billing|keys?|variables?|widgets?|failed|failing|"
    r"failure|errors?|broke|broken|stuck|waiting|logs?|history|workspace|synapse)\b")
#: What a turn that offered to set up the workflow for a live fact keeps (F1382), `{fact, ask}`, for a builder only; and
#: the next turn's mark that its yes took the offer, which goes to the build route through the role door.
OFFERED_KEY = "offered_build"
OFFER_TAKEN_KEY = "offer_taken"
#: How a route that leaves the talk step's work to code reads: nothing else moved it.
_MAY_TALK = (None, "question", "musing")

_KINDS = {"schedule": ("schedule", "schedules"), "agent": ("agent", "agents"),
          "workflow": ("workflow", "workflows", "orchestration"), "recording": ("recording", "recordings")}
_STOP = frozenset("the a an my our this that for to of and on off in at with every each daily nightly".split())
#: The state key holding a name that is both a recording and a workflow, which the change route asks about (F1231).
BOTH_KEY = "recording_or_workflow"


def _recording_named(text: str, things: dict[str, list[str]], inventory: dict[str, list[str]]) -> str | None:
    """`recording` when the message is about a recording it names, `both` when that name is a workflow's too and it
    says neither word, else None (F1231). A recording is replayed by a workflow's Browser step, so running,
    scheduling or repeating one is a build."""
    if not things.get("recording"):
        return None
    words = set(text.split())
    said_recording, said_workflow = bool(words & set(_KINDS["recording"])), bool(words & set(_KINDS["workflow"]))
    if said_workflow and not said_recording:
        return None
    workflows = {_words(w) for w in inventory.get("workflow") or []}
    if not said_recording and any(_words(r) in workflows for r in things["recording"]):
        return "both"
    return "recording"


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
    if request.feature == "evals" and kind in ("change", "build"):
        # Evals' own ops, on the change route, never a workflow built to imitate them (F1255; the field is typed).
        kind, by = "change", "evals"
    named = _named(text, inventory)
    things = {k: v for k, v in named.items() if v}

    recording = _recording_named(text, things, inventory)
    asked = _QUESTION.match(text) or (_QUESTION_IF_ASKED.match(text) and message.rstrip().endswith("?"))
    also = request.also
    # Why something failed is said before it is fixed (F1378, prod-07): the answer reads the failed run, and the fix
    # waits for after it. The quick model's order of the two asks never decides it.
    if state.get(OFFER_TAKEN_KEY):
        # A yes to the offer to set up the workflow for a live fact (F1382): its ask is this turn's message.
        kind, by = "build", "offer_taken"
    elif kind in ("change", "build") and by is None and _CAUSE.search(text):
        kind, by, also = "answer", "cause_first", _fix_half(message, request.also)
    # A request to run, schedule or repeat a recording builds the workflow whose Browser step replays it; a name that
    # is a workflow's too is asked about first, on the change route (F1231). An edit of the recording is the app's.
    elif recording and kind in ("change", "build") and not asked and not _CHANGE_ONLY.search(text):
        kind, by = ("change", "recording_or_workflow") if recording == "both" else ("build", "recording_named")
    # An act on something that exists is a change; P2 decides card or review (F755/act).
    elif _ACT.search(text) and things and kind != "change":
        kind, by = "change", "act_on_existing"
    # A change verb on a named schedule or agent is a change, never a build ("change the schedule to 09:00").
    elif kind == "build" and not _NEW.search(text) and _CHANGE.search(text) and (
            things.get("schedule") or things.get("agent")
            or any(w in text.split() for k in ("schedule", "agent") if inventory.get(k) for w in _KINDS[k])):
        kind, by = "change", "named_change"
    # Findings, then a yes or a build: the build plans from them (F766).
    elif _findings(state.get("prior_findings")) and kind != "build" and by != "evals" and (
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
    if kind == "answer" and by in _MAY_TALK and not things and not request.providers and _ABOUT_YOU.search(text):
        # What it can do for them is the talk step's, from the workspace's description (F1382), never the help's list.
        needs, by = "none", "about_you"
    elif kind == "answer" and by in _MAY_TALK and offers(request, text, things):
        # A fact from outside that changes: the talk step offers the workflow that gets it, never a read here (F1382).
        needs, by = "none", "live_fact"
    # What a recording's request names is the recording: not the conversation's last build (F1229's "it"), nor a
    # workflow found by the recording's own name.
    source = state.get("current_orchestration_source")
    not_it = source == "built" or source in ("named", "picked") and _words(
        state.get("current_orchestration_name") or "") in {_words(r) for r in things.get("recording") or []}
    route = _entry(kind, text, {**state, "current_orchestration_source": ""} if by == "recording_named" and not_it
                   else state)
    if route == "step_answer" and kind == "build":
        needs = "workspace"  # two workflows match: the answer asks which, from the list
    if route not in ("step_answer", "step_research") and not _may(kind, state):
        route = "step_not_yours"
    return Routed(**{**request.model_dump(), "kind": kind, "needs": needs, "also": also}, route=route,
                  overridden_by=by, model_kind=request.kind, model_needs=request.needs)


def offers(request: Request, text: str, things: dict) -> bool:
    """Whether the live fact the model typed is one to offer a workflow for (F1382): never on what the message names
    here, a how-to, an act, or this workspace's own state ("how many runs failed today?")."""
    return bool((request.live_fact or "").strip()) and not things and not _ACT.search(text) and not \
        _HOW_TO.match(text) and not _OURS.search(text)


def offer_ask(fact: str) -> str:
    """The build a yes to the offer asks for (F1382), as the person would ask it."""
    return f"Build a workflow that gets {fact} each time it runs, so I can run it now and put it on a schedule."


def _needs(kind: str, said: str | None, text: str, things: dict) -> str:
    """What the answer reads. `none` only when the model did not ask for more and code finds nothing to read."""
    if kind != "answer":
        return "workspace"
    if things or _ACT.search(text):
        return "workspace"  # a thing it names, or an act said to it: the workspace
    if _HOW_TO.search(text):
        # A how-to (F1342): about the product it is the help's, whatever nouns or change it names ("move my
        # workflows"); about what this workspace spent, or what of theirs failed or is broken, it reads the workspace.
        return "workspace" if _OWN_STATE.search(text) else "help"
    if _WORKSPACE.search(text) or _CHANGE_ONLY.search(text):
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
    # The conversation's own last build too (F1229): more detail, or a change, is about it unless they ask for a new one.
    if source in ("named", "picked", "built") and not _NEW.search(text):
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
    for kind in ("schedule", "agent", "workflow", "recording"):
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
    try:
        request = Request.model_validate(args or {})
    except Exception:  # noqa: BLE001 — a call that is not a Request: an answer, read by code alone
        request = Request(kind="answer")
    return await _classified(request, events)


async def classify_at_plan(args: dict, server_module: Any, events: list[dict]) -> dict:
    """The same, while a plan waits at its review (`step_classify_at_plan`): `continues` is required. A call that
    still leaves it out, or says null, is read as unsure: the plan is not carried and the notice says it waits."""
    try:
        request: Request = RequestAtPlan.model_validate(args or {})
    except Exception:  # noqa: BLE001 — not said as asked: its kind still counts, its continues does not
        try:
            request = Request.model_validate({**(args or {}), "continues": None})
        except Exception:  # noqa: BLE001
            request = Request(kind="answer")
    return await _classified(request, events)


async def _classified(request: Request, events: list[dict]) -> dict:
    from core.builder_tools import _current_run
    from core.native_builder import turn_facts
    from core.native_builder.change_tools import _caller
    from core.orchestration.engine import live_run

    run_id = _current_run.get() or ""
    run = live_run(run_id) if run_id else None
    state = run.shared_state if run is not None else {}
    message = str(state.get("user_message") or "")
    inventory = await _inventory()
    routed = override(request, message=message, state=state, inventory=inventory, connectable=await _connectable())
    from core.native_builder import change_tools as CT

    waiting = CT.staged_plan(state) if run is not None and routed.kind in ("build", "change") else None
    if waiting and request.continues is True and routed.route != "step_not_yours" and _may("build", state):
        # A plan waits at its review and this message continues the request (F1241, F1242): it is that plan's
        # reply, through the review the card's Change something takes; never the change route, which cannot make a
        # workflow nobody saved. Only on the model's own yes: a wrong carry would change a waiting plan.
        routed = routed.model_copy(update={"kind": "build", "route": CT.REVISE_ROUTE, "overridden_by": "waiting_plan"})
        events.append({"type": "builder_notice", "reason": "plan_change", "final": False,
                       "message": CT.TAKEN_AS_A_CHANGE})
    elif waiting and request.continues is None:
        events.append({"type": "builder_notice", "reason": "plan_waiting", "final": False,
                       "message": CT.STILL_WAITING})
    if run is not None:
        # The role door (`step_may_change`) and the build route read the legacy intent.
        run.shared_state["intent"] = intent_of(routed)
        if routed.overridden_by == "live_fact" and _may("build", state):
            # The offer the talk step makes, kept for the next turn's yes (F1382); a viewer's turn keeps none.
            fact = (routed.live_fact or "").strip()
            run.shared_state[OFFERED_KEY] = {"fact": fact, "ask": offer_ask(fact)}
        if routed.overridden_by == "recording_or_workflow":
            # The change route asks which, once (F1231).
            named = _named(_words(message), inventory).get("recording") or []
            workflows = {_words(w) for w in inventory.get("workflow") or []}
            run.shared_state[BOTH_KEY] = next((r for r in named if _words(r) in workflows), "")
        # The previous request's questions, and a plan waiting at its review, are this run's only if this message
        # continues it (F1226, F1241).
        CT.adopt_carry(run.shared_state, request.continues)
    if routed.also and routed.also.strip():
        await turn_facts.keep(run_id, _caller().user_id, also=routed.also.strip())
        events.append(also_notice(routed.also.strip()))
    return routed.model_dump()


SCHEMA = {"type": "function", "function": {
    "name": "classify_request",
    "description": "Say what the person's message asks for, so the right route answers it.",
    "parameters": {k: v for k, v in Request.model_json_schema().items() if k in ("type", "properties", "required")},
}}

#: While a plan waits at its review (`read_turn`'s `plan_waiting`, the door `step_classify_door`): the same call with
#: `continues` required, a boolean (F1241's follow-up). Optional everywhere else, as before.
SCHEMA_AT_PLAN = {"type": "function", "function": {
    "name": "classify_at_plan",
    "description": "Say what the person's message asks for while a plan waits at its review, and whether it changes "
                   "that plan, so the right route answers it.",
    "parameters": {k: v for k, v in RequestAtPlan.model_json_schema().items()
                   if k in ("type", "properties", "required")},
}}


def _register() -> None:
    from core.builder_tools import register_builder_tool

    register_builder_tool(SCHEMA, classify_request)
    register_builder_tool(SCHEMA_AT_PLAN, classify_at_plan)


_register()
