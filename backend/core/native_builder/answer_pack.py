"""The answer route's context, built by code from what the answer needs (SAI2-P4, F980; P4-PROMPT §3 and §7).

`step_answer` is an E1 step (no model) calling `answer_pack`. What it puts in front of the answering model depends
on `request.needs`, which classify set and code checked (F979):

* every pack: the workspace's name and its own description (or that it has none, so the reply never guesses the
  business), the asker's name and role, the reply's language and that it is written to them (F986), what the product
  can do, and what the last turn did; and each workflow the last turns built or the message names, by its parts in
  code's words, with the one rule for saying what it does (F1228);
* **help:** the product's help hits, found in code (`builder_help.search`; no quick call);
* **workspace:** the acts and changes this asker's role may ask for, and only those, plus the things the message
  names, with their ids.

Connect cards come from code here, for a provider the person named that the workspace can connect, once per
conversation unless asked again (SAI2's review), and never for what the product's own tools already do.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from core.product_tools.roles import Role

_log = logging.getLogger(__name__)

#: The product tool behind each button act (`builder_actions._offer`): its role is the act's role.
ACT_TOOLS = {"run_workflow": "run_orchestration", "run_schedule_now": "trigger_schedule",
             "schedule_on": "set_schedule_enabled", "schedule_off": "set_schedule_enabled",
             "answer_tool_call": "answer_tool_approval", "cancel_run": "cancel_run",
             "knowledge_create": "create_knowledge_base", "knowledge_add_file": "add_knowledge_file",
             "knowledge_add_pages": "add_knowledge_pages", "knowledge_add_note": "add_knowledge_note",
             "knowledge_retry_document": "retry_knowledge_document",
             "knowledge_rename": "update_knowledge_base", "knowledge_rebuild": "retry_knowledge_rebuild",
             "knowledge_delete_base": "delete_knowledge_base",
             "knowledge_delete_document": "delete_knowledge_document", "board_add_card": "add_board_card",
             "board_update_card": "update_board_card", "board_comment": "comment_on_card",
             "board_create": "create_board", "board_delete": "delete_board", "feature_on": "turn_on_feature",
             # F1260's resume and F1270's recordings (REACH1).
             "resume_run": "resume_run", "talk_to_agent": "chat_with_agent", "dead_job_retry": "retry_dead_job",
             "dead_job_discard": "discard_dead_job", "support_request": "file_support_request", "recording_approve_edit": "approve_recording_edit",
             "recording_discard_edit": "discard_recording_edit", "recording_rename": "rename_recording",
             "recording_delete": "delete_recording"}
#: An act whose card answers through the run's own door, not a registry tool (`builder_actions`, answer_run).
_ACT_ROLES = {"answer_run": Role.APPROVER}
_TARGET_WORDS = {"knowledge_base": "knowledge base", "approval": "tool call that waits"}

_NOT_RECORDED = "(not recorded"
#: Classify's `language` (an ISO 639-1 code, F979) by name: the reply's language is a fact code gives the answer (F986).
LANGUAGES = {"en": "English", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian", "pt": "Portuguese",
             "nl": "Dutch", "hi": "Hindi", "bn": "Bengali", "ta": "Tamil", "te": "Telugu", "mr": "Marathi",
             "ur": "Urdu", "ar": "Arabic", "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "ru": "Russian",
             "tr": "Turkish", "pl": "Polish", "id": "Indonesian", "vi": "Vietnamese", "th": "Thai"}


def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def _role_of(tool: str | None) -> Role:
    from core.product_tools import areas, registry

    if not tool:
        return Role.BUILDER
    areas.ensure_loaded()
    found = registry.get(tool)
    return found.min_role if found is not None else Role.BUILDER


def may_ask(role: Role) -> list[str]:
    """What this role may ask Synapse AI to do, in words: the button acts, then the changes."""
    from core.builder_actions import ACTIONS
    from core.native_builder import change_ops as C

    lines = []
    for name, said in ACTIONS.items():
        minimum = _ACT_ROLES.get(name) or _role_of(ACT_TOOLS.get(name))
        if role.can_act_as(minimum):
            words = said.split(" (`")[0].strip()
            lines.append(words[:1].upper() + words[1:])
    for op in C.OPS.values():
        if op.internal or op.kind not in ("write", "delete", "create") or not role.can_act_as(_role_of(op.tool)):
            continue
        target = _TARGET_WORDS.get(op.target, op.target.replace("_", " "))
        lines.append(f"{op.verb} {_article(target)} {target}")
    if role.can_act_as(Role.BUILDER):
        lines.append("Build something new: a workflow, an agent, a schedule, a tool")
    return list(dict.fromkeys(lines))


def _may_line(role: Role) -> str:
    """What this person may ask for, in one line, on every answer (SAI2's review, 10-27): a "could I…" ends in the
    offer when their role allows it, and never in one it doesn't."""
    if role.can_act_as(Role.BUILDER):
        return "They may ask you to build or change things here: offer it when they want something done."
    if role.can_act_as(Role.APPROVER):
        return ("They may answer what waits for them and use its cards; building and changing need the builder "
                "role, so say who can.")
    return ("They may read and ask about this workspace, not build or change it; say someone with the builder role "
            "can.")


def _reply_line(language: str | None) -> str:
    """How the reply is written: in the message's language, named when classify read it (lang-03 came back in English
    after two Spanish turns), and to the person, never about them (it opened "The user asked…")."""
    name = LANGUAGES.get(str(language or "").split("-")[0].strip().lower())
    where = f"Reply in {name}, the language this message is written in" if name else \
        "Reply in the language this message is written in"
    return f'{where}. Write to the person who asked, as "you"; never call them "the user".'


def describe_workspace(name: str, profile: str) -> str:
    """The workspace in one line: its name and its own description; empty when neither is recorded."""
    profile = "" if not profile or profile.strip().startswith(_NOT_RECORDED) else profile.strip()
    if name and profile:
        return f"{name}: {profile}"
    return name or profile


def _hit_lines(hits: list[dict]) -> list[str]:
    out = []
    for hit in hits[:5]:
        out.append(f"- {hit.get('title')}: {hit.get('summary') or ''}".rstrip(": "))
        out.extend(f"    {step}" for step in hit.get("steps") or [])
        if hit.get("where") or hit.get("screen"):
            out.append(f"    Where: {hit.get('where') or hit.get('screen')}")
    return out


#: How an answer may say what a workflow does (F1228): only by its parts, as code says them.
WORKFLOW_RULE = ("What these workflows do, in code's words from their parts. Say what one does only from these lines; "
                 "never that it can do, did or will do more than they say, never what a run of it will find or give, "
                 "and never that it does exactly what was asked. Say what is missing or off when it bears on the "
                 "question.")


#: How the answer is given a failed run (F1378): what code read of it, said in this order.
FAILURE_LEAD = "The run that failed, as code read it. Say the failing step and its error first, then what fixes it:"


def failure_said(block: dict | None) -> str:
    """A failed run's block (`triage.latest`) in the pack's words; empty when there is none."""
    if not block:
        return ""
    step = block.get("step") or {}
    lines = [FAILURE_LEAD, f'- workflow "{block.get("workflow")}", run {block.get("run_id")}, failed at '
                           f'{block.get("failed_at") or "an unrecorded time"}',
             f'- step "{step.get("name") or step.get("id")}" ({step.get("type") or "a"} step)'
             + (f', agent "{step["agent"]}"' if step.get("agent") else ""),
             f"- error: {block.get('error') or '(none recorded)'}",
             f"- what it means: {block.get('means')}",
             "- what fixes it, each a change you may offer: " + ", then ".join(block.get("fixes") or [])]
    lines.append("- once fixed it can resume from that step" if block.get("resumes")
                 else f"- it cannot resume: {block.get('resume_refused') or 'it would start again'}")
    return "\n".join(lines)


def build_pack(*, routed: dict, workspace_name: str, profile: str, asker_name: str, asker_role: Role, last: str,
               capabilities: str, help_hits: list[dict], named: list[dict], connect: str = "",
               workflows: list[str] | None = None, failure: str = "", live_fact: str = "",
               may_build: bool = True) -> str:
    """The pack's text, section by section."""
    needs = routed.get("needs") or "none"
    described = describe_workspace(workspace_name, "" if profile.strip().startswith(_NOT_RECORDED) else profile)
    has_profile = bool(profile.strip()) and not profile.strip().startswith(_NOT_RECORDED)
    parts = []
    unrecorded = ("It has no description recorded, so say nothing about what the business does or what industry "
                  "it is in.")
    if has_profile:
        parts.append(f"The workspace: {described}.")
    elif workspace_name:
        parts.append(f"The workspace: {workspace_name}. {unrecorded}")
    else:
        parts.append(f"The workspace: {unrecorded[3:]}".replace("The workspace: has", "This workspace has"))
    who = f"{asker_name}, " if asker_name else ""
    parts.append(f"Who is asking: {who}{_article(asker_role.label)} {asker_role.label} in this workspace. "
                 + _may_line(asker_role))
    parts.append(_reply_line(routed.get("language")))
    if last:
        parts.append(f"What the last turn did: {last}")
    if workflows:
        parts.append(WORKFLOW_RULE + "\n\n" + "\n\n".join(workflows))
    if connect:
        parts.append(connect)
    parts.append(capabilities.strip())
    if live_fact:
        # The fact the talk step offers an agent for (F1382); building one is a builder's, so a viewer is told who.
        said = f"What they asked for is outside this workspace and changes over time: {live_fact}."
        parts.append(said if may_build else f"{said} Building an agent that gets it is for an owner or a builder of "
                                            "this workspace: say they can ask for one.")
    if needs == "help":
        parts.append("What this product's help says about the question:\n" + (
            "\n".join(_hit_lines(help_hits)) if help_hits else
            "The help says nothing about this: say you are not sure, and do not invent a screen."))
    if needs == "workspace":
        asks = may_ask(asker_role)
        if asks:
            parts.append(f"What a {asker_role.label} may ask you to do here (nothing else):\n"
                         + "\n".join(f"- {a}" for a in asks))
        else:
            parts.append(f"A {asker_role.label} may read and ask about this workspace here, and nothing more: no "
                         "card, no change and no build is theirs to ask for.")
        if failure:
            parts.append(failure)
        if named:
            parts.append("What the message names here:\n" + "\n".join(
                f'- {n["kind"]} "{n["name"]}" ({n["id"]})' + (f" — {n['detail']}" if n.get("detail") else "")
                for n in named))
    return "\n\n".join(p for p in parts if p)


# ── the step ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _said_in(name: str, text: str) -> bool:
    from core.native_builder.target import _words

    want = _words(name)
    return bool(want) and f" {want} " in f" {_words(text)} "


async def _named_things(message: str) -> list[dict]:
    from core.native_builder.change_tools import candidates
    from core.native_builder.classify import _inventory, _named
    from core.native_builder.target import _words

    named = _named(_words(message), await _inventory())
    out = []
    for kind, names in named.items():
        for item in await candidates(kind) or []:
            if item["name"] in names:
                out.append({"kind": kind, **item})
    return out[:12]


async def _connect_card(routed: dict, message: str, run_id: str, session_id: str | None,
                        server_module: Any, events: list[dict]) -> str:
    """At most one Connect card, for a provider the person named that this workspace can connect, unless one was
    shown earlier in this conversation and they did not ask for it again."""
    from core.builder_connect import _read_options, prepare_connection
    from core.boards import assistant
    from core.native_builder.read_turn import previous_turns
    from core.native_builder.target import _words

    named = [p for p in routed.get("providers") or [] if _said_in(p, message)]
    if not named:
        return ""
    options = await _read_options()
    asked = any(w in _words(message).split() for w in ("connect", "link", "reconnect", "hook", "sign", "add"))
    shown = set()
    for previous in await previous_turns(session_id, run_id):
        shown |= {str(c.get("what") or "") for c in (await assistant.turn(previous)).get("connects") or []}
    for provider in named:
        want = _words(provider)
        app = next((e for e in options["catalog"] if want in (_words(str(e.get("slug") or "")),
                                                              _words(str(e.get("name") or "")))), None)
        key = next((p for p in options["providers"] if want in (_words(p), _words(options["providers"][p]))), None)
        if app is None and key is None:
            # A chat platform this host connects (F1313): its Connect card, put up before a word is written, so its
            # token goes in the card's box and is never asked for in the reply (prod-10: "I'll need: 1. Your
            # Telegram bot token").
            said = await _channel_card(provider, want, options, shown, asked, server_module, events)
            if said:
                return said
            continue
        what = str(app.get("slug") if app else key).lower()
        if what in shown and not asked:
            return (f"A Connect card for {provider} was shown earlier in this conversation: say it is above, and "
                    "do not put up another.")
        args = ({"kind": "app", "catalog_slug": app.get("slug"), "title": f"Connect {app.get('name') or provider}"}
                if app else {"kind": "provider_key", "provider": key, "title": f"Add a {provider} key"})
        done = await prepare_connection(args, server_module, events)
        if done.get("status") == "card_shown":
            return (f"A Connect card for {provider} is up above: say so, and that nothing connects until they press "
                    "Connect there. Put up no other card this turn.")
    return ""


def _channel_platform(want: str, options: dict) -> str | None:
    """The chat platform a provider's name is, as this host connects them (cloud's `channels`, else
    `core.messaging.fields`), by its id or its name; None for anything else."""
    from core.messaging import fields
    from core.native_builder.target import _words

    offered = options.get("channels") if options.get("channels") is not None else fields.PLATFORMS
    return next((p for p, entry in offered.items()
                 if want in (_words(p), _words(str(entry.get("label") or "")))), None)


async def _channel_card(provider: str, want: str, options: dict, shown: set, asked: bool, server_module: Any,
                        events: list[dict]) -> str:
    """A chat platform's Connect card for the answer, or what is said in its place; "" when `provider` is none."""
    from core.builder_connect import prepare_connection

    platform = _channel_platform(want, options)
    if platform is None:
        return ""
    if platform in shown and not asked:
        return (f"A Connect card for {provider} was shown earlier in this conversation: say it is above, and that its "
                "token is typed there, never in this chat; do not put up another.")
    label = provider[:1].upper() + provider[1:]
    done = await prepare_connection({"kind": "channel", "platform": platform, "name": label,
                                     "title": f"Connect {label}"}, server_module, events)
    if done.get("status") == "card_shown":
        return (f"A Connect card for {provider} is up above, where its token is typed: say so, and that nothing "
                "connects until they press Connect there. Say where to get the token if they ask (its own app or "
                "developer site), never ask for it here. Put up no other card this turn.")
    # Who may not connect one, or a platform this host does not: what to say instead, in code's words.
    return str(done.get("error") or "")


async def _workflows_said(turn: dict, named: list[dict], server_module: Any) -> list[str]:
    """Each workflow the last turns built or changed and each the message names, in code's words (F1228)."""
    from core.native_builder.parts_words import workflow_words

    ids = [str(i) for i in turn.get("built") or [] if i]
    ids += [str(n["id"]) for n in named if n.get("kind") == "workflow" and n.get("id")]
    out = []
    for orch_id in list(dict.fromkeys(ids))[:4]:
        try:
            if said := await workflow_words(orch_id, server_module):
                out.append(said)
        except Exception as exc:  # noqa: BLE001 — a workflow it cannot say is left out, and the rule still holds
            _log.warning("answer pack: workflow %s not said: %s", orch_id, exc)
    return out


async def answer_pack(args: dict, server_module: Any, events: list[dict]) -> dict:
    """The answering step's context, built in code from `request.needs`."""
    from core import builder_help
    from core.builder_tools import _current_run
    from core.native_builder.change_tools import _caller
    from core.orchestration.engine import live_run

    run_id = _current_run.get() or ""
    run = live_run(run_id) if run_id else None
    state = run.shared_state if run is not None else {}
    routed = state.get("request") if isinstance(state.get("request"), dict) else {}
    routed = {"needs": "workspace", **routed}
    needs = routed["needs"]
    message = str(state.get("user_message") or "")
    turn = state.get("turn") if isinstance(state.get("turn"), dict) else {}
    connect = ""
    try:
        connect = await _connect_card(routed, message, run_id, getattr(run, "session_id", None), server_module,
                                      events)
    except Exception as exc:  # noqa: BLE001 — the answer goes on without the card, which it never promises
        _log.warning("answer pack: no connect card: %s", exc)
    named = await _named_things(message)
    from core.native_builder.classify import asks_why_it_failed

    failed = ""
    if needs == "workspace" and asks_why_it_failed(message):
        # Why something failed is answered from the failed run as code reads it (F1378), never only if asked for.
        from core.orchestration.triage import latest

        try:
            failed = failure_said(await latest())
        except Exception as exc:  # noqa: BLE001 — the answer goes on reading for itself
            _log.warning("answer pack: the failed run not read: %s", exc)
    pack = build_pack(
        routed=routed, workspace_name=str(state.get("workspace_name") or ""),
        profile=str(state.get("org_profile") or ""), asker_name=str(state.get("asker_name") or ""),
        asker_role=_caller().role, last=str(turn.get("last") or ""),
        capabilities=builder_help.capabilities(examples=True),
        help_hits=builder_help.search(message) if needs == "help" else [],
        named=named if needs == "workspace" else [], connect=connect,
        workflows=await _workflows_said(turn, named, server_module), failure=failed,
        live_fact=str(routed.get("live_fact") or "").strip() if routed.get("overridden_by") == "live_fact" else "",
        may_build=state.get("may_change") is not False)
    return {"needs": needs, "pack": pack}


# ── an answer never asks for a yes (F1243) ────────────────────────────────────────────────────────────────────────

#: A sentence that asks the person for a yes: only the build route's plan card takes one, so an answer that asks for one
#: has nothing behind it (TRI1's turn 3: "Just say **approve**", and the "yes proceed" after it started a new build).
_ASKS_A_YES = re.compile(
    r"(?i)\b(?:say|reply|type|respond(?:\s+with)?|tell\s+me)\s+[\"“'*`]*\s*"
    r"(?:approve[sd]?|yes|go\s+ahead|build\s+it|confirm|ok(?:ay)?|proceed)\b"
    r"|\*\*\s*approve\s*\*\*")
#: Where a yes goes instead, said once after an answer that asked for one. Fixed words: the same answer, the same text.
NO_YES_HERE = ("Nothing here is waiting for your yes: to have it built or changed, ask for it, and a plan comes up for "
               "you to check and approve.")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def no_yes_offered(text: str) -> str:
    """`text` without any whole sentence that asks for a yes, and then where a yes goes (F1243). An answer about
    approvals that asks for nothing ("a Human step stops for your approval", "press “Approve” on the card") is kept
    as it is."""
    cut = False
    lines = []
    for line in str(text or "").split("\n"):
        kept = [x for x in _SENTENCE_END.split(line) if not _ASKS_A_YES.search(x)]
        cut |= len(kept) < len(_SENTENCE_END.split(line))
        if kept or not line.strip():
            lines.append(" ".join(kept))
    if not cut:
        return text
    return "\n".join(lines).rstrip() + "\n\n" + NO_YES_HERE


# ── an answer never claims a change this turn did not make (F1323) ────────────────────────────────────────────────

#: What a reply says in place of a change it claimed and this turn never made. Fixed words, as NO_YES_HERE.
NOTHING_CHANGED_HERE = ("Nothing was changed in this reply: to have something changed, ask for it, and a change comes "
                        "up for you to check and approve.")
_CHANGED = (r"(?:added|created|updated|changed|enabled|disabled|turned\s+(?:it\s+|them\s+)?(?:on|off)|"
            r"switched\s+(?:it\s+|them\s+)?(?:on|off)|deleted|removed|renamed|set\s+(?:it\s+|them\s+)?up|connected|"
            r"scheduled|built|made|saved|fixed|applied|moved|paused|resumed|activated|deactivated|installed|"
            r"registered|configured|edited|replaced|started|stopped|approved|sent)")
#: A sentence that claims a change was made: a "Done" or "All set" announcement; "I've turned on…" (the speaker did
#: it); a state said as just changed ("is now enabled", "has now been added", "successfully connected"). Never a status
#: ("is enabled"), history ("was turned off on Monday") or what the speaker can do or did not ("I can", "I haven't").
_CLAIMS_A_CHANGE = re.compile(
    r"(?i)^\W*(?:all\s+(?:done|set)|done|finished)\s*[.!,:\u2014\u2013-]"
    r"|^\W*(?:that['\u2019]s|that\s+is|it['\u2019]s|it\s+is)\s+(?:all\s+)?done\b"
    r"|\b(?:i|we)(?:['\u2019]ve|\s+have)?\s+(?:(?:now|just|already|successfully|also|gone\s+ahead\s+and)\s+)*"
    rf"{_CHANGED}\b"
    r"|(?:\b(?:is|are|has|have)|['\u2019](?:s|re))\s+now\s+(?:been\s+)?"
    rf"(?:{_CHANGED}|on|off|live|active|running|in\s+place)\b"
    rf"|\bsuccessfully\s+{_CHANGED}\b")
#: A sentence that sets a condition or a time ("Once it's enabled…", "If I added…") claims nothing.
_CONDITION = re.compile(r"(?i)^\W*(?:if|when|once|after|before|until|unless)\b")


def _claims_a_change(sentence: str) -> bool:
    text = sentence.strip()
    return bool(text) and not _CONDITION.match(text) and bool(_CLAIMS_A_CHANGE.search(text))


def no_change_claimed(text: str, writes: Any = None) -> str:
    """`text` without any whole sentence that claims a change was made, and then code's line, when this turn wrote
    nothing (`writes`: the run's own record, `run_writes`; F1323). No answer step can change anything, so a claim
    there has nothing behind it: prod-13's "Done. The schedule is now enabled and all tools have been added…" after no
    op applied. A turn that wrote keeps its words."""
    if writes:
        return text
    cut = False
    lines = []
    for line in str(text or "").split("\n"):
        parts = _SENTENCE_END.split(line)
        kept = [x for x in parts if not _claims_a_change(x)]
        cut |= len(kept) < len(parts)
        if kept or not line.strip():
            lines.append(" ".join(kept))
    if not cut:
        return text
    rest = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return f"{rest}\n\n{NOTHING_CHANGED_HERE}" if rest else NOTHING_CHANGED_HERE


# ── a live fact's offer is always made (F1382) ────────────────────────────────────────────────────────────────────

def offer_line(fact: str) -> str:
    """Code's own offer, said when the reply to a live fact did not ask: fixed words but the fact, as NO_YES_HERE."""
    return (f"I can build an agent that gets {fact} for you: run it now, and on a schedule if you like, sent by email "
            "or a chat channel. Want it?")


def offer_kept(text: str, offered: Any) -> str:
    """`text`, ending with the offer when this turn kept one (`classify.OFFERED_KEY`) and the reply does not end
    asking (F1382): the next turn's yes takes it, so a yes always has an offer behind it."""
    fact = str(offered.get("fact") or "").strip() if isinstance(offered, dict) else ""
    if not fact or str(text or "").rstrip().endswith("?"):
        return text
    return f"{str(text or '').rstrip()}\n\n{offer_line(fact)}".strip()


def checked_answer(said: str, writes: Any, offered: Any) -> str:
    """The answer as said: no change claimed that this turn did not make (F1323); no yes asked for that nothing can
    take (F1243), unless this turn kept a live fact's offer, which the next turn's yes takes; and that offer made
    (F1382)."""
    text = str(said or "")
    kept = isinstance(offered, dict) and bool(str(offered.get("fact") or "").strip())
    return offer_kept(no_change_claimed(text if kept else no_yes_offered(text), writes), offered)


async def check_answer(args: dict, server_module: Any, events: list[dict]) -> dict:
    """After every answer (talk, help, workspace, the planner's plain reply): the run's answer with no yes asked for
    that nothing can take (F1243), no change claimed that this turn did not make (F1323), and a live fact's offer made
    (F1382)."""
    from core.native_builder.change_tools import _run
    from core.native_builder.classify import OFFERED_KEY
    from core.orchestration.run_writes import WRITES_KEY

    _, run = _run()
    state = run.shared_state if run is not None else {}
    said = state.get("summary") or ""
    return {"object": "answer_checked", "text": checked_answer(said, state.get(WRITES_KEY), state.get(OFFERED_KEY))}


CHECK_SCHEMA = {"type": "function", "function": {
    "name": "check_answer",
    "description": "Synapse AI's own step: the answer, with no yes asked for that nothing can take. No arguments.",
    "parameters": {"type": "object", "properties": {}},
}}


SCHEMA = {"type": "function", "function": {
    "name": "answer_pack",
    "description": "Synapse AI's own step: what the answer needs to know, built in code. Takes no arguments.",
    "parameters": {"type": "object", "properties": {}},
}}


def _register() -> None:
    from core.builder_tools import register_builder_tool

    register_builder_tool(SCHEMA, answer_pack)
    register_builder_tool(CHECK_SCHEMA, check_answer)


_register()
