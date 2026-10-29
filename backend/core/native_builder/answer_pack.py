"""The answer route's context, built by code from what the answer needs (SAI2-P4, F980; P4-PROMPT §3 and §7).

`step_answer` is an E1 step (no model) calling `answer_pack`. What it puts in front of the answering model depends
on `request.needs`, which classify set and code checked (F979):

* every pack: the workspace's name and its own description (or that it has none, so the reply never guesses the
  business), the asker's name and role, the reply's language and that it is written to them (F986), what the product
  can do, and what the last turn did;
* **help:** the product's help hits, found in code (`builder_help.search`; no quick call);
* **workspace:** the acts and changes this asker's role may ask for, and only those, plus the things the message
  names, with their ids.

Connect cards come from code here, for a provider the person named that the workspace can connect, once per
conversation unless asked again (SAI2's review), and never for what the product's own tools already do.
"""
from __future__ import annotations

import logging
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
             "knowledge_delete_document": "delete_knowledge_document", "board_add_card": "add_board_card",
             "board_update_card": "update_board_card", "board_comment": "comment_on_card",
             "board_create": "create_board", "board_delete": "delete_board"}
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


def build_pack(*, routed: dict, workspace_name: str, profile: str, asker_name: str, asker_role: Role, last: str,
               capabilities: str, help_hits: list[dict], named: list[dict], connect: str = "") -> str:
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
    if connect:
        parts.append(connect)
    parts.append(capabilities.strip())
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
    pack = build_pack(
        routed=routed, workspace_name=str(state.get("workspace_name") or ""),
        profile=str(state.get("org_profile") or ""), asker_name=str(state.get("asker_name") or ""),
        asker_role=_caller().role, last=str(turn.get("last") or ""),
        capabilities=builder_help._capabilities or builder_help.SELF_HOSTED_CAPABILITIES,
        help_hits=builder_help.search(message) if needs == "help" else [],
        named=await _named_things(message) if needs == "workspace" else [], connect=connect)
    return {"needs": needs, "pack": pack}


SCHEMA = {"type": "function", "function": {
    "name": "answer_pack",
    "description": "Synapse AI's own step: what the answer needs to know, built in code. Takes no arguments.",
    "parameters": {"type": "object", "properties": {}},
}}


def _register() -> None:
    from core.builder_tools import register_builder_tool

    register_builder_tool(SCHEMA, answer_pack)


_register()
