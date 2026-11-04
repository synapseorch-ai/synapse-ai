"""Synapse AI's change route, in code (SAI2-P2, D308; F793, F794, F796, F797).

One model step writes language and judgement once, into a typed `Change`; code does every fact (DESIGN.md,
"Principle"). The route, as workflow steps:

1. `read_change_context` (E1): what the conversation's board card holds — the last change applied (for "undo that")
   and a which-one question waiting (its pick list).
2. The change planner (an agent, E2) finishes on exactly one of:
   * `submit_change`: ops by id or by name (`change_ops.OPS`);
   * `ask_person`: a question only the person can answer (which one, what time, what value);
   * `cannot`: the request is outside what the product does, and the nearest thing that does (P0's can't-do exit).
   Each checks what the model wrote and answers an error for the model to fix (E2 retries on `{"error": …}`).
3. `check_change` (E1): for each op, the target (by id, else by name; two that match become a pick list and a
   question), the person's role for that op (the registry tool's own `min_role`, read now), a no-op from live state
   (`change_ops.differs`), what uses a delete (F690 / F467). Acts become cards through `prepare_action`'s code (F755:
   by the op's kind). Writes become exact calls, rendered into the review by code. Its `outcome` routes the rest.
4. The review (`step_manage_review`) and F754's yes gate, unchanged.
5. `approve_change` (E1) records the person's yes to exactly those calls (F778); `apply_change` (E1) makes each
   through `registry.invoke(confirm_policy="approved_review")`, re-reads the target and says plainly what did not take
   effect, and keeps each op's before-image on the board card for undo.

The model never sees a token, an approval or another model's prose: the review it would act on is the code's.
"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from core import feature_census
from core.builder_registry import register_builder_tool
from core.log import get_logger
from core import python_edits as E
from core.native_builder import change_ops as C, one_writer
from core.product_tools.roles import Role
from core.secret_scrub import SECRET_NAMED, asks_for_a_secret

_log = get_logger(__name__)

#: The board card's stage while a change waits for its person, and once it is done.
WAITING, CHANGED, SET_ASIDE = "waiting", "changed", "set_aside"

#: What the person is told when the change planner ended in words, not with a finish (F958). Neutral: code knows only
#: that no change came of it, not why (a request outside the ops, or a model that answered in words anyway).
FALLBACK_CANNOT = "I couldn't turn that into something I can make or change here. Say it another way, or do it on its own page."

#: A change whose new value holds a key (or the door's mark where one was taken out of the person's message).
KEY_REFUSAL = ("Keys never go in a change: a key there would be kept with it. A tool's key goes on its Connect card "
               "once the tool is made; any other on its own settings screen, where it is stored encrypted. If it was "
               "real, rotate it at the provider.")


def _holds_a_key(fields: dict) -> bool:
    from core.native_builder.export import CREDENTIAL_MARK
    from core.secret_scrub import holds_credential

    text = json.dumps(fields, default=str, ensure_ascii=False)
    return holds_credential(text) or CREDENTIAL_MARK in text


def _login_in(fields: dict) -> str | None:
    """A tool's address in an op's fields that holds a login, said as one (F1312b × F1343): before the key check, which
    would read its password as a key and say only that; never with the address, and nothing of it kept."""
    from core.builder_tools import LOGIN_REFUSAL, holds_a_login

    return LOGIN_REFUSAL if any(holds_a_login(v) for k, v in fields.items() if str(k).endswith("url")) else None


async def _carries_a_key(fields: dict, says: str = "") -> bool:
    """An op that carries a key in its fields or its `says`, by shape or as a value the workspace holds (F1309: prod-02's
    "Make the HTTP tool … Authenticates with bearer token …" put a token pasted in chat into a made tool's header)."""
    from core.secret_scrub import held_in

    if _holds_a_key({**fields, "says": says}):
        return True
    return bool(await held_in(json.dumps({**fields, "says": says}, default=str, ensure_ascii=False)))


#: The label of the planner's own words for a change, under code's line (F1308): the review's and the panel card's.
DESCRIBED_AS = "Described as"

#: The review's lead-in is the definition's (`step_manage_review`, F754's words); these are what code adds.
DELETE_NOTE = ("It can come back only as a new copy: a new id, without its run history or an agent's memory; a "
               "schedule comes back paused, and a tool's key is entered again on a card.")


# ── the planner's three ways to finish ────────────────────────────────────────────────────────────────────────────

def _op_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": list(C.PLANNER_OPS)},
            "target": {"type": "object", "properties": {
                "kind": {"type": "string", "enum": list(C.TARGET_KINDS)},
                "id": {"type": "string", "description": "Its id, from what you read. Leave out when you only have "
                                                        "its name."},
                "name": {"type": "string", "description": "Its name as the person said it, or the new one's name "
                                                          "for create and create_schedule."},
            }, "required": ["kind"]},
            "fields": {"type": "object", "description": "What the op sets, by the op's own field names."},
            "says": {"type": "string", "description": "This op in the person's words, one short line."},
        },
        "required": ["op", "target"],
    }


SUBMIT = {"type": "function", "function": {
    "name": "submit_change",
    "description": (
        "Finish with the change to make: one op per thing to do. Nothing happens yet: code checks each op, puts up "
        "a card for each button act, and shows the person the rest to approve. Fields per op: rename_* {name}; "
        "update_workflow {description, max_total_turns, timeout_minutes, max_run_credits, max_run_usd}; "
        "set_schedule {cron_expression | interval_value + interval_unit, prompt, description, missed_run_policy: "
        "run_immediately | skip}; create_schedule {runs: {kind: workflow | agent, id}, prompt, cron_expression | "
        "interval_value + interval_unit, missed_run_policy}; retarget_schedule {runs: {kind: workflow | agent, id}}; "
        "set_agent_model {model}; set_agent_tools / set_agent_skills {add: [...], remove: [...]}; "
        "set_agent_instructions {system_prompt}; set_agent_settings {description, memory, knowledge: [base names], "
        "boards_own, planning, skip_default_tools, helpers, max_turns, turn_limit_mode: ask | continue | stop, "
        "reasoning_effort: off | low | medium | high, max_output_tokens}; set_agent_team (a team agent) {add / "
        "remove: [agent names], mode: one_at_a_time | team, at_once, task_budget, spend_limit_usd | "
        "clear_spend_limit}; set_agent_browser (a browser agent) {sites, sessions: [sites], recordings: [names], "
        "action_budget, mode: read_only | co_drive (letting it type and send: on its own)}; set_agent_code (a code "
        "agent) {repos, db_configs: [names]}; edit_agent_memory {content} and delete_agent_memory, each naming the "
        "item by its id from read_workspace agent_memory; set_tool_policy {destructive | open_world | undeclared: allow | "
        "confirm | deny | inherit, tools: {name: decision}}; update_skill {description, body}; run_workflow "
        "{message}; knowledge_create {text, folder}; knowledge_add_file {path}; knowledge_rename {name, text}; knowledge_add_pages {urls}; knowledge_add_note {title, text}; board_add_card {title, text}; "
        "board_update_card {status, result}; board_comment {text}; board_create {text}; board_rename {name}; "
        "board_describe {text}; board_access {who, access: add | work | none}; board_card_text {title, text}; recording_approve_edit and "
        "recording_discard_edit {edit_id}; recording_rename {name}; settings_set (target {kind: settings, id: "
        "workspace}) {<setting's name>: <new value>, …}, only the settings topic `settings` lists; talk_to_agent "
        "{message, needs: [each tool or knowledge base its answer needs, by name]}; dead_job_retry and dead_job_discard (target {kind: dead_job, id}); support_request (target {kind: "
        "support, name: its subject}) {text, category, severity, run_id}; eval_rubric {name, "
        "criteria: [{name, weight, guidance}], floor} or {rubric}; eval_cases {cases: [{name, input, notes}]} or "
        "{count, steer}; eval_run and eval_cancel {golden_set}; knowledge_edit_document {find: the passage as it "
        "stands, replace: its new text} or {text: a short note's whole new text}; knowledge_settings {language, "
        "visitor_access (opening it to visitors, on its own), budget_monthly_usd | clear_budget}; widget_create {agent, sites, visitor_tools, greeting, title, accent, theme, "
        "position, launcher_label, font, header_style, launcher, daily_cap}; widget_update {the same, on}; widget_logo "
        "{path}; channel_connect {platform, agent}; channel_agents {agents, agent}; edit_steps, create "
        "and use_recording {what}; create (an HTTP tool whose address you know: target {kind: tool, name: its "
        "snake_case name}) {description, url with {{ placeholders }} for what it reads, method: GET | POST | PUT | "
        "PATCH | DELETE, headers, body, input_schema, group, key: {header, scheme} where its key goes, never the key; "
        "or from: {curl: n}, a curl in the context's curls, or from: {spec, operation}, an operation of a spec you "
        "read with read_api_spec, and code fills its fields, your url and body marking only what varies with "
        "{{ }}}, before the ops that give it to an agent; update_tool {url, method, description} or {from: {curl: n}} "
        "(an HTTP tool's address, method or what it does, in place: never its headers or its key); update_python_tool "
        "{edits: [{find, replace}] or code, test_args: [{…}], needs_network, description} (a Python tool's code, "
        "its fix tested with no network before the person sees it). Deletes and the other acts take none. For \"undo that\", send "
        "undo_of with no ops."
    ),
    "parameters": {"type": "object", "properties": {
        "ops": {"type": "array", "items": _op_schema()},
        "asked": {"type": "array", "description": (
            "Always, with ops: every part of what the person asked, one entry per thing they want, in their words, with the numbers of "
            "the ops that do it (1 is the first; one op may serve several parts, a part may take several ops, an op "
            "another needs first goes under the part it serves), or why_not: why this change does not do it (another "
            "route builds it, a card does it, it can't be done here). One part and one op for a simple change."),
            "items": {"type": "object", "properties": {
                "part": {"type": "string"},
                "ops": {"type": "array", "items": {"type": "integer"}},
                "why_not": {"type": "string"},
            }, "required": ["part"]}},
        "undo_of": {"type": "string", "description": "The key of the change to undo, from the context you were "
                                                     "given."},
    }, "required": ["asked"]},
}}

_SUBJECT = ("The thing the question is about, in a few words: the knowledge base 'competitor_pricing', the Monday "
            "schedule. A question already asked about it is not asked again.")
_OPTIONS = "The choices, when there are a few: here only, never also listed in the question."
ASK = {"type": "function", "function": {
    "name": "ask_person",
    "description": ("Finish with what only the person can answer: which one they mean, what time, what value, who, or "
                    "how much. Several at once go in `questions`, one form they answer in one go, each with your "
                    "best guess filled in for them; one alone may be `question`. Never ask what reading the "
                    "workspace or a lookup would tell you, and never for a key or how to connect a service: those "
                    "come through a Connect card."),
    "parameters": {"type": "object", "properties": {
        "question": {"type": "string", "description": "One question; with `questions`, the line above them."},
        "about": {"type": "string", "enum": ["which", "when", "what", "who", "how_much"]},
        "subject": {"type": "string", "description": _SUBJECT},
        "options": {"type": "array", "items": {"type": "string"}, "description": _OPTIONS},
        "questions": {"type": "array", "maxItems": 6, "description": (
            "Every question at once, as one form: each a field the person fills in, your guess already in it."),
            "items": {"type": "object", "properties": {
                "question": {"type": "string"},
                "kind": {"type": "string", "enum": ["text", "address", "email", "number", "date", "yes_no",
                                                    "choice"]},
                "guess": {"type": "string", "description": (
                    "Your best guess as the answer itself, filled in for them to keep or change: a value (an address, "
                    "a day and time, a number, one of the options), never a description of what it could be. Leave "
                    "it out when you have no such value.")},
                "options": {"type": "array", "items": {"type": "string"}, "description": _OPTIONS},
                "about": {"type": "string", "enum": ["which", "when", "what", "who", "how_much"]},
                "subject": {"type": "string", "description": _SUBJECT},
            }, "required": ["question"]}},
    }},
}}

CANNOT = {"type": "function", "function": {
    "name": "cannot",
    "description": ("Finish when the request is outside what this product does, or no op fits it: why, in one "
                    "sentence, and the nearest thing that does work here, from product_capabilities."),
    "parameters": {"type": "object", "properties": {
        "why": {"type": "string"},
        "nearest": {"type": "string"},
        "screen": {"type": "string", "description": (
            "The screen where it is done, by its id from not_here or product_capabilities (`screen` `evals`): the "
            "person gets its link. Name it whenever one of those rows fits; leave it out only when none does.")},
    }, "required": ["why", "nearest"]},
}}

SAY = {"type": "function", "function": {
    "name": "say",
    "description": ("Finish when the ask needs nothing made or changed: a question answered from what you read, or a "
                    "plain reply. `text` is the reply itself, in a few sentences. Never an offer to build or change "
                    "something: for that, submit a plan or a change."),
    "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
}}

FINISH_TOOLS = ("submit_change", "ask_person", "cannot")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


#: A line of a list a question carries in its own words: "1. …", "2) …", "- …", "• …".
_LISTED = re.compile(r"^\s*(?:\d{1,2}[.)]|[-*•])\s+(.+?)\s*$")
#: How much of a choice two must share before one cut short ("Reb…") is the other.
_SAME_FROM = 12


def _choice_key(option: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", option).casefold() if c.isalnum())


def asked_options(question: str, options: list[str]) -> tuple[str, list[str]]:
    """The question without a list of its own, and its choices once each (F1220).

    The Asked card is the question in Markdown with the choices after it as a numbered list. A planner that also
    numbered them in its question showed two lists Markdown runs together — 1–3, then the same choices again as 4–5
    — so a list in the question is lifted into the choices, and a choice that repeats another (case, punctuation and a
    cut-short end aside) is kept once: as first said, or in full where the first was cut short."""
    lines = (question or "").splitlines()
    listed = [m.group(1) for line in lines if (m := _LISTED.match(line))]
    rest = "\n".join(line for line in lines if not _LISTED.match(line)).strip()
    if len(listed) >= 2 and rest:
        question = rest
    else:
        listed = []  # one listed line, or a question that is only its list: the words stay as they were
    kept: list[str] = []
    for option in [*listed, *(o.strip() for o in options if isinstance(o, str))]:
        key = _choice_key(option.rstrip(".…"))
        if not key:
            continue
        same = next((n for n, k in enumerate(kept) if (a := _choice_key(k.rstrip(".…"))) == key
                     or (min(len(a), len(key)) >= _SAME_FROM and (a.startswith(key) or key.startswith(a)))), None)
        if same is None:
            kept.append(option)
        elif len(key) > len(_choice_key(kept[same].rstrip(".…"))):
            kept[same] = option  # the one cut short gives way to its whole words; the same words keep the first
    return (question or "").strip(), kept


def asked_text(question: str, options: list[str]) -> str:
    """What the Asked card shows: the question, then its choices numbered once (F1220)."""
    question, options = asked_options(question, options)
    return question + ("\n" + "\n".join(f"{n}. {o}" for n, o in enumerate(options, 1)) if options else "")


async def submit_change(args: dict, server_module: Any, events: list[dict]) -> dict:
    from core.native_builder.objects import Change

    ops = C.as_planned(args.get("ops") if isinstance(args.get("ops"), list) else [])
    undo_of = _text(args.get("undo_of"))
    if not ops and not undo_of:
        return {"error": "Send at least one op, or undo_of for \"undo that\"."}
    if ops and undo_of:
        return {"error": "Send undo_of alone (its ops are built from the change it undoes), or ops alone; then the "
                         "rest as another change."}
    found: list[str] = []
    _, run = _run()
    kinds = run.shared_state.setdefault(_KINDS_KEY, {}) if run is not None else {}
    for n, op in enumerate(ops, 1):
        if not isinstance(op, dict):
            found.append(f"op {n} is not an object")
            continue
        fields = op.get("fields") if isinstance(op.get("fields"), dict) else {}
        if op.get("op") in ("create_tool", "update_tool") and isinstance(fields.get("from"), dict):
            # What the person pasted or the conversation read, filled in by code (F1312a): never retyped.
            fields, why = await _from_source(fields, run.shared_state if run is not None else {},
                                             update=op.get("op") == "update_tool")
            if why:
                found.append(f"op {n}: {why}")
                continue
            op["fields"] = fields
        lines = C.issues(str(op.get("op") or ""), op.get("target") or {}, fields)
        found += [f"op {n}: {line}" for line in lines]
        if op.get("op") == "update_python_tool" and not lines:
            # Its fix tested with no network before anything is shown (F1325): a first failure is the planner's to fix.
            fields, why = await _code_tested(op.get("target") or {}, fields, run.shared_state if run else {},
                                             send_back=True)
            if why:
                found.append(f"op {n}: {why}")
            else:
                op["fields"] = fields
        if op.get("op") in ("create", "create_tool") and (switched := _kind_switch(kinds, op.get("target") or {})):
            found.append(f"op {n}: {switched}")
    found += [f"op {n}: {SAYS_ASKS}" for n, op in enumerate(ops, 1)
              if isinstance(op, dict) and asks_for_a_secret(_text(op.get("says")), strict=False)]
    if not found and run is not None and (nudge := _curl_unnamed(ops, run.shared_state)):
        found.append(nudge)
    if not found and run is not None and (nudge := _curl_literal(ops, run.shared_state)):
        found.append(nudge)
    asked = args.get("asked") if isinstance(args.get("asked"), list) else []
    if not found and ops and asked and (gaps := _unaccounted(ops, asked)) and _send_back(run):
        found += gaps
    if found:
        return {"error": "Fix these and call submit_change again: " + "; ".join(found)}
    parts = [p for p in asked if isinstance(p, dict) and _text(p.get("part"))]
    change = Change.model_validate({"ops": ops, "asked": (parts or None) if ops else None, "undo_of": undo_of or None})
    return {"finish": "submit_change", **change.model_dump(exclude_none=True)}


#: How often a change whose parts don't account for the ask is sent back in a run (F1364): twice, never a loop; after
#: that it goes on as written, and what it says it isn't doing is said.
ASKED_SEND_BACKS = 2
_ASKED_SENT = "_asked_sent_back"


def _unaccounted(ops: list, asked: list) -> list[str]:
    """What `asked` leaves unsaid (F1364): a part with neither ops nor why_not, an op no part names, an op number that
    is no op. One op may serve several parts and one part several ops. A change sent without it is taken as it was
    (one op is one part; code can't see a part nobody wrote down, so the list is the planner's to give)."""
    out, named = [], set()
    for part in asked:
        if not isinstance(part, dict) or not _text(part.get("part")):
            out.append("each entry of asked needs its part, in the person's words")
            continue
        numbers = [n for n in part.get("ops") or [] if isinstance(n, int)]
        if not numbers and not _text(part.get("why_not")):
            out.append(f"part {_quoted(_text(part['part']))}: no op does it and no why_not says why")
        if bad := [n for n in numbers if not 1 <= n <= len(ops)]:
            out.append(f"part {_quoted(_text(part['part']))}: op {bad[0]} is not one of the ops")
        named.update(numbers)
    out += [f"op {n}: no part of the ask names it; put it under the part it serves" for n in range(1, len(ops) + 1)
            if n not in named]
    return out


def _send_back(run: Any) -> bool:
    """Whether this run may send its change back for its parts once more (`ASKED_SEND_BACKS`); counts the one sent."""
    if run is None:
        return True
    sent = int(run.shared_state.get(_ASKED_SENT) or 0)
    if sent >= ASKED_SEND_BACKS:
        return False
    run.shared_state[_ASKED_SENT] = sent + 1
    return True


def not_done(asked: Any) -> str:
    """The parts of the ask a change says it isn't doing, and why, as the review and the answer say them (F1364)."""
    lines = [f"{_text(p.get('part'))}: {_text(p.get('why_not'))}" for p in asked or []
             if isinstance(p, dict) and _text(p.get("why_not")) and not p.get("ops")]
    return ("Not done by this change: " + "; ".join(lines) + ".") if lines else ""


#: The kind each new thing of this step's change was first submitted as, by name (F1242), engine-private.
_KINDS_KEY = "_change_kinds"


#: Asked once a run (Naveen 11-04): a curl the person pasted, which a new or changed HTTP tool did not name.
_CURL_NUDGED = "_curl_nudged"
CURL_UNNAMED = "a curl is in the context: name it with from {curl: n}"


def _curl_unnamed(ops: list, state: dict) -> str | None:
    """A create_tool or update_tool with no `from` while the person pasted a curl this turn (F1312b, Naveen's ruling):
    sent back once, so code reads the curl rather than the planner retyping it; never twice, never a loop."""
    from core.native_builder.build_plan import curls

    if state.get(_CURL_NUDGED):
        return None
    if not any(isinstance(o, dict) and o.get("op") in ("create_tool", "update_tool") and
               not (o.get("fields") or {}).get("from") for o in ops):
        return None
    found = [c for c in curls(state) if not c.get("problem")]
    if not found:
        return None
    state[_CURL_NUDGED] = True
    listed = "; ".join(f"curl {c['n']}: {c['method']} {c['url']}" for c in found)
    return f"{CURL_UNNAMED} ({listed}), and mark only what varies with {{{{ }}}}; or send it again as it is"


_CURL_LITERAL = "_curl_literal_nudged"


def _curl_literal(ops: list, state: dict) -> str | None:
    """A tool made or changed `from` a curl whose address keeps one call's value as written (F1377: fix-01's tool
    fetched the curl's one id every time): sent back once, so a value that changes from call to call becomes an input;
    sent again as it is, it is taken, so an address that is the same for every call still is."""
    from core import http_facts
    from core.native_builder.build_plan import curls

    if state.get(_CURL_LITERAL):
        return None
    found = {c["n"]: c for c in curls(state) if not c.get("problem")}
    for op in ops:
        fields = (op.get("fields") or {}) if isinstance(op, dict) else {}
        source = fields.get("from") if isinstance(fields.get("from"), dict) else {}
        if op.get("op") not in ("create_tool", "update_tool") or source.get("curl") not in found:
            continue
        n = source["curl"]
        url = _text(fields.get("url")) or found[n]["url"]
        if "{{" in url or not (value := http_facts.one_calls_value(url)):
            continue
        state[_CURL_LITERAL] = True
        return (f"curl {n}'s address keeps one call's value as written ({value} in {url}): give its url with {{{{ }}}} "
                "where a value changes from call to call, or send it again as it is when that address is the same for every call")
    return None


#: The Python tools whose fix has failed its test once in this run (F1325): the next failure is said, not sent back.
_CODE_TRIES = "_code_tries"


async def _code_tested(target: dict, fields: dict, state: dict, *, send_back: bool) -> tuple[dict, str | None]:
    """A Python tool's fix with its test before the yes (F1325): run on each sample with **no network** (Naveen: no
    internet before the yes). Its fields with `_test` (`passed`, `failed`, or `button` where the network is needed
    or can't be off), or why to send it back: a passage not in its code, or a first failure when `send_back`."""
    from core.execution import ExecUnsupported, Network
    from core.store.resources import load_tools

    name = _text(target.get("name") or target.get("id"))
    live = next((t for t in await load_tools() if t.get("name") == name), None)
    if live is None or live.get("tool_type") != "python":
        return fields, None  # the check says what it is
    if fields.get("code") is None:
        _, why = E.apply_edits(str(live.get("code") or ""), fields.get("edits") or [])
        if why:
            return fields, why
    new = C.expected(C.OPS["update_python_tool"], fields, live)["code"]
    samples = list(fields.get("test_args") or [])
    if fields.get("needs_network"):
        return {**fields, "_test": {"button": "it needs the internet to test"}}, None
    try:
        results = await E.run_samples(new, live.get("globals") or [], samples, Network.NONE)
        # What it printed reaches the review and the run's state: never a secret (F1381).
        results = await E.masked_results(results, live.get("globals") or [])
    except ExecUnsupported:
        return {**fields, "_test": {"button": "this install can't run code with no network"}}, None
    if results and all(r["ok"] for r in results):
        return {**fields, "_test": {"passed": results}}, None
    if E.needed_network(results):
        return {**fields, "_test": {"button": "it needs the internet to test"}}, None
    failed = next(r for r in results if not r["ok"])
    line = E.last_line(failed.get("error") or "")
    tries = state.setdefault(_CODE_TRIES, {}) if isinstance(state, dict) else {}
    if send_back and not tries.get(name):
        tries[name] = 1
        return fields, (f"the fix to {_quoted(name)} fails its test with {json.dumps(failed['args'])}: {line}. Fix "
                        "it and call submit_change again")
    return {**fields, "_test": {"failed": {"args": failed["args"], "error": line}}}, None


async def _from_source(fields: dict, state: dict, *, update: bool = False) -> tuple[dict, str | None]:
    """A new HTTP tool's fields from where its `from` says (F1312a): the n-th curl the person pasted this turn
    (`build_plan.curls`), or an operation of a spec this conversation read (`own_api.SPECS_KEY`). The source's method,
    URL, headers, body, inputs and where its key goes are the tool's; the planner's own url and body may only mark
    what varies, in the curl's shape. The fields, or why not, in words the planner can act on."""
    from core import http_facts

    source = fields["from"]
    if "curl" in source:
        from core.native_builder.build_plan import curls

        found, n = curls(state), source.get("curl")
        if not isinstance(n, int) or not 1 <= n <= len(found):
            held = f"{len(found)} curl{'' if len(found) == 1 else 's'}" if found else "no curl"
            return fields, f"`from` names curl {n}, and this turn holds {held}" + (
                "" if found else ": give the tool's fields from the person's words instead")
        facts = found[n - 1]
        if facts.get("problem"):
            return fields, f"curl {n} can't be a tool: {facts['problem']}"
        if fields.get("url") and (why := http_facts.same_shape(fields["url"], facts["url"])):
            return fields, f"its url is the curl's ({facts['url']}) with {{{{ }}}} only where a value varies: {why}"
        if fields.get("body") and facts.get("body") and (why := http_facts.same_body(fields["body"], facts["body"])):
            return fields, f"curl {n}: {why}"
        out = {**fields, "url": fields.get("url") or facts["url"], "method": facts["method"]}
        if update:
            return out, None  # a change to a tool: its address and method; its headers and key stay (F1312b)
        for name in ("headers", "key"):
            if facts.get(name):
                out[name] = facts[name]
        if facts.get("body") is not None:
            out["body"] = fields.get("body") or facts["body"]
        return out, None
    from core.native_builder import own_api

    read = state.get(own_api.SPECS_KEY) if isinstance(state.get(own_api.SPECS_KEY), dict) else {}
    spec = read.get(str(source.get("spec") or "").strip().lower())
    if not spec:
        named = ", ".join(str(v.get("name")) for v in read.values() if isinstance(v, dict)) or "none"
        return fields, (f"`from` names the spec {_quoted(source.get('spec'))}, and the specs this conversation read are "
                        f"{named}: read it with read_api_spec first")
    got = await own_api.read_spec(spec)
    if got.get("problem"):
        return fields, f"{spec['name']}'s spec can't be read: {got['problem']}"
    operation = str(source.get("operation") or "").strip()
    tool = next((t for t in got.get("tools") or [] if t.get("name") == operation), None)
    if tool is None:
        names = ", ".join(t["name"] for t in (got.get("tools") or [])[:20])
        return fields, f"{spec['name']}'s spec has no operation {_quoted(operation)}: its operations are {names}"
    out = {**fields, "url": tool["url"], "method": tool.get("method") or "GET",
           "description": fields.get("description") or tool.get("description") or ""}
    if update:
        return out, None
    if isinstance(tool.get("inputSchema"), dict):
        out["input_schema"] = tool["inputSchema"]
    if tool.get("body"):
        out["body"] = tool["body"]
    if (auth := got.get("auth")) and auth.get("in") == "header" and auth.get("name"):
        out["key"] = {"header": auth["name"], "scheme": auth.get("scheme") or ""}
    return out, None


def _kind_switch(kinds: dict, target: dict) -> str | None:
    """A new workflow sent again as something else is refused (F1242: prod's was refused as a workflow, sent again as
    an agent, and one agent was made of a two-agent workflow). A new workflow is the build's; any other kind may be
    corrected. Records the first kind each name was submitted as."""
    name, kind = _text(target.get("name")).lower(), target.get("kind")
    if not name or not kind:
        return None
    first = kinds.setdefault(name, kind)
    if first in C.BUILDS and kind != first:
        return (f"{_quoted(target.get('name'))} was a new {first} in your last call, and a new {first} is a build, "
                f"made by the build route: send it as create with target.kind {first}, never as a {kind}")
    return None


#: A question that asks the person for a secret (chg-09: a key asked in chat was typed there and kept): a key, a
#: token, a password, a PIN or a code, or a login, as something of theirs or a service's. Not a count of tokens, a
#: password reset or a licence to buy (SAI2's review, 17).
_ASKS_SECRET = SECRET_NAMED  # one detector for "asks for" and "holds" a secret (F1093, with F1092)
#: … or for how a service is reached (res-05: "Apps catalog, MCP URL or HTTP tool?"), which the lookup finds out. A
#: "which one" question may still name an HTTP tool the workspace has.
_ASKS_WAY_IN = re.compile(
    r"(?i)\bhow\b[^?]{0,80}?\b(?:connect(?:ed|ing)?|integrat(?:e|ed|ing)|hook(?:ed)?\s+(?:it\s+)?up|"
    r"authenticat\w*)\b[^?]{0,40}?\b(?:to|with|into|via)\b|\b(?:mcp(?:\s+(?:server|url))?|apps?\s+catalog\w*|"
    r"via\s+(?:its|their|the)\s+api|api\s+or\s+(?:a\s+)?webhook)\b|\bhttp\s+tool\b")
CANNOT_FOR_A_KEY = ("A key, a token or a password is never why something can't be done, and never asked for: a "
                    "service's tool is planned, and its key comes through a Connect card after the yes.")
SAYS_ASKS = "its says asks for a key, a token or a password; a key comes through a Connect card, never the chat"
SECRET_ASKED = ("Never ask the person for a key, a token or a password: a connection comes through a Connect card, "
                "never through the chat. Ask only what the person alone knows.")
WAY_IN_ASKED = ("Never ask the person how to connect a service: who provides a tool, and how it is reached, is "
                "looked up, not asked. Ask only what the person alone knows.")
ABOUT = ("which", "when", "what", "who", "how_much")
#: How many questions one request asks, across its runs, before it goes on with what it has (F1226).
QUESTIONS = 5
ASKED_ENOUGH = ("You have asked the person {n} questions already: go on with what you know. What you pick is on the "
                "plan's card for them to check.")
#: The most questions one form asks (F1253): a form is one round of the request's cap.
FORM_MAX = 6
#: A form question's kind, as the field it is (`humanFields.ts`).
KIND_TYPE = {"text": "string", "address": "string", "email": "email", "number": "number", "date": "date",
             "yes_no": "boolean", "choice": "choice"}
#: The form's line when the planner gives none.
FORM_LEAD = "A few things before I plan it:"
#: The steps that put a planner's question to the person.
_ASKING_STEPS = ("step_human_clarify", "step_change_clarify")
#: The questions this conversation's request has asked, across its runs (F1226): `[{about, subject, question, answer,
#: carried}]`. A run starts with the ones its conversation asked since the last thing it saved (`carried_questions`).
ASKED_KEY = "request_questions"
#: How many questions the request showed the person in its earlier runs (F1226): the cap counts the request.
ROUNDS_KEY = "request_rounds"
ASKED_ALREADY = ("You asked the person this already: \"{question}\" Their answer: {answer}. Don't ask it again: go on "
                 "with that answer.")
#: An answer to our own question that says the person doesn't know (F1310, prod-07). A short closed list, read only
#: to stop asking: it never acts, never fills a value, and reads nothing but answers to questions we put.
_DONT_KNOW = re.compile(r"\b(?:not sure|unsure|not certain|don't know|do not know|no idea|don't have|do not have|"
                        r"can you (?:check|find|look))\b", re.I)
#: What the planner proposes instead, once the person has said they don't know (F1310: the change route's exit).
PROPOSE = ("Propose a change they can approve with submit_change: for a step that fails, edit_steps on its workflow "
           "(what the step does when its tool fails: stop and say why, or carry on without it); its card says plainly "
           "what it does not fix (a tool's address is changed on the Tools screen).")
DONT_KNOW_SUBJECT = ("The person said they don't know this: \"{answer}\" Don't ask what they can't answer: go on without "
                     "it, or propose a change they can approve.")
DONT_KNOW_ALL = "The person said they don't know, {n} times: don't ask what they can't answer. " + PROPOSE
#: That a change request's words went back once for a card (F1310); the run's own, never a public read's.
WORDS_SENT_KEY = "_change_words_sent"
WORDS_INSTEAD = ("The person said they don't know, or has been asked all this request asks: don't end a change in "
                 "words. " + PROPOSE + " Send your words again as they are to keep them.")


def dont_knows(state: dict) -> list[dict]:
    """The request's questions whose answer says the person doesn't know (F1310); [] for none."""
    return [a for a in _with_answers(state)
            if _DONT_KNOW.search(str(a.get("answer") or "").replace("\u2019", "'").replace("\u2018", "'"))]


#: Words a subject shares with any other, never what it is about (F1310's match of one subject to another).
_GENERIC = frozenset({"tool", "tools", "step", "steps", "workflow", "agent", "the", "what", "which", "this", "that",
                      "should", "does", "use", "uses", "call", "calls", "run", "runs"})


def _named(entry_or_text: Any) -> set[str]:
    """What a question is about, by its subject's own words (its question's where it names none), generic ones aside."""
    if isinstance(entry_or_text, dict):
        entry_or_text = entry_or_text.get("subject") or entry_or_text.get("question")
    return {w for w in _question_words(str(entry_or_text or "").replace("_", " ")) if w not in _GENERIC}


def _unknown_refusal(state: dict, about: str, subject: str, question: str) -> str | None:
    """Why a question is not put, after the person said they don't know: one such answer stops questions about the
    thing it named (a word of their subjects in common: the price_feed endpoint, its supplier, its URL), and nothing
    else; two (by their words, a form's one reply counting once) stop them all."""
    unknown = dont_knows(state)
    answers = list(dict.fromkeys(str(a.get("answer")) for a in unknown))
    if len(answers) >= 2:
        return DONT_KNOW_ALL.format(n=len(answers))
    asked = _named(subject or question)
    same = next((a for a in unknown if asked & _named(a)), None)
    return DONT_KNOW_SUBJECT.format(answer=same.get("answer")) if same else None


def words_sent_back(state: dict, run: Any = None) -> str | None:
    """A change request that ends in words after the person said they don't know, or at the request's cap, goes back
    once to propose the card (F1310); None when its words stand."""
    if state.get("intent") != "manage" or state.get(WORDS_SENT_KEY):
        return None
    if not dont_knows(state) and questions_asked(state, run) < QUESTIONS:
        return None
    state[WORDS_SENT_KEY] = True
    return WORDS_INSTEAD


def _question_words(text: Any) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKC", str(text or "")).casefold())
            if len(w) > 2}


def _same_question(earlier: dict, about: str, subject: str, question: str) -> bool:
    """Whether `earlier` asked this: the same kind of question about the same thing, or, where either names no
    subject, nearly the same words (a fallback: a rephrasing about one subject is caught by its subject)."""
    if subject and earlier.get("subject"):
        return earlier.get("about") == about and _question_words(earlier["subject"]) == _question_words(subject)
    a, b = _question_words(earlier.get("question")), _question_words(question)
    return bool(a and b) and len(a & b) / len(a | b) >= 0.8


def reply_of(state: dict) -> tuple[str | None, dict]:
    """The person's answer to what a run last asked (a build's clarify or a change's), if it has one: its words, and a
    form's answers by field (F1253; `{}` for a reply typed in the chat box or a channel)."""
    for key in ("human_response", "change_clarify"):
        value = state.get(key)
        value = json.loads(value) if isinstance(value, str) and value.startswith("{") else value
        said = value.get("answer") if isinstance(value, dict) else value
        form = value.get("form") if isinstance(value, dict) and isinstance(value.get("form"), dict) else {}
        if isinstance(said, str) and said.strip() or form:
            return (said.strip() if isinstance(said, str) and said.strip() else None), form
    return None, {}


def _answer_of(state: dict) -> str | None:
    """The person's answer to the question a run last put (a build's clarify or a change's), if it has one."""
    return reply_of(state)[0]


def form_answer(value: Any) -> str:
    """A form field's answer as words: yes or no for a tick box, "(left empty)" for nothing."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value).strip() if value is not None and str(value).strip() else "(left empty)"


def _with_answers(state: dict) -> list[dict]:
    """The run's questions, those it last put given the answer the run holds for them: a form's each by its field,
    or all of them the reply typed instead (F1253); one question alone, as before it."""
    asked = [dict(a) for a in state.get(ASKED_KEY) or [] if isinstance(a, dict)]
    said, form = reply_of(state)
    if not (said or form):
        return asked
    # The form this run put last: its fields still unanswered, at the end. A carried one is an earlier run's.
    last = []
    for a in reversed(asked):
        if a.get("answer") or not a.get("field") or a.get("carried"):
            break
        last.append(a)
    for a in last or ([asked[-1]] if asked and not asked[-1].get("answer") else []):
        if a.get("field") in form:
            a["answer"] = form_answer(form[a["field"]])
        elif said:
            a["answer"] = said
    return asked


#: Where the route stages what the previous run's request would carry, until classify says whether this message
#: continues it (`adopt_carry`).
CARRY_KEY = "request_carry"


#: A plan left waiting at its review (F1241, TRI1's class 1): the panel sets it aside when the person types something
#: other than its yes, and the next turn started from the chat alone. It is staged with the carry under this key and,
#: when classify says the message continues the request, changed through its own review, as the card's Change
#: something is. Not adopted, it is parked on the run (`PARKED_PLAN_KEY`) for the turn after.
WAITING_PLAN = "waiting_plan"
PARKED_PLAN_KEY = "plan_parked"
PLAN_REVIEW_STEP = "step_human_review"
#: Where the run goes with it: the review's answer, as if typed at the card.
REVISE_ROUTE = "step_review_research"
TAKEN_AS_A_CHANGE = "I've taken that as a change to the plan above."
STILL_WAITING = "Your plan above is still waiting. If this was meant as a change to it, say so."


def _plan_keys() -> tuple[str, ...]:
    """What of a run is its plan: the draft, the check's card, the findings, the workflow it changes, the agents'
    instructions, and which workflow is open."""
    from core.native_builder.build_plan import CHECKED_KEY, DRAFT_KEY, EXISTING_KEY, FINDINGS_KEY, PROMPTS_KEY

    return (DRAFT_KEY, CHECKED_KEY, FINDINGS_KEY, EXISTING_KEY, PROMPTS_KEY, "intent", "current_orchestration_id",
            "current_orchestration_name", "current_orchestration_source")


def staged_plan(state: dict) -> dict | None:
    """The waiting plan the route staged for this run, if there is one (`{"run_id", "state"}`)."""
    staged = state.get(CARRY_KEY)
    plan = staged.get(WAITING_PLAN) if isinstance(staged, dict) else None
    return plan if isinstance(plan, dict) and plan.get("state") else None


def waiting_plan_title(state: dict) -> str:
    """The title of the plan waiting at its review for this run (`staged_plan`), "" when none waits: the classify
    names it and asks plainly whether this message changes it (F1241's follow-up)."""
    from core.native_builder.build_plan import CHECKED_KEY, DRAFT_KEY, _obj

    plan = staged_plan(state)
    if not plan:
        return ""
    held = plan["state"]
    checked = _obj(held.get(CHECKED_KEY))
    for raw in ((checked or {}).get("plan") if isinstance(checked, dict) else None, _obj(held.get(DRAFT_KEY))):
        if isinstance(raw, dict) and str(raw.get("title") or "").strip():
            return str(raw["title"]).strip()
    return "the plan above"


def adopt_carry(state: dict, continues: bool | None) -> bool:
    """The staged carry becomes the run's when classify said this message continues the previous request; anything
    else (false, unsure, not said) starts afresh: one question asked again costs less than another request's carried
    (8a's review of the FF14 check, 10-29: "second ask" was carried as the first's). Returns whether it carried.

    A waiting plan in it (F1241) comes back whole, with this message as the person's reply to it and the card it
    was (`PLAN_BEFORE_KEY`, which "Changed from the last plan" reads); not adopted, it is parked."""
    from core.native_builder.build_plan import CHECKED_KEY, PLAN_BEFORE_KEY, checked_in

    staged = state.pop(CARRY_KEY, None)
    plan = staged.pop(WAITING_PLAN, None) if isinstance(staged, dict) else None
    if continues is not True or not isinstance(staged, dict) or not (staged or plan):
        if isinstance(plan, dict) and plan.get("state"):
            state[PARKED_PLAN_KEY] = plan
        return False
    state.update(staged)
    if isinstance(plan, dict) and plan.get("state"):
        state.update(plan["state"])
        card = checked_in({CHECKED_KEY: plan["state"].get(CHECKED_KEY)}) or {}
        state[PLAN_BEFORE_KEY] = {"plan": card.get("plan"), "draft": card.get("draft"), "run_id": plan.get("run_id")}
        state["review_feedback"] = {"feedback": str(state.get("user_message") or "")}
    return True


# ── the request's questions: one seam for every question put to the person (F1226) ──────────────────────────────
# `ask_person` uses these, and so does a question code asks itself (F1210's dates): each is counted and never repeated.
def asked_before(state: dict, about: str, subject: str, question: str) -> dict | None:
    """The question this request already asked that this one repeats, with its answer, or None."""
    return next((a for a in _with_answers(state) if _same_question(a, about, subject, question)), None)


def questions_asked(state: dict, run: Any = None) -> int:
    """How many questions this request has put to the person: the rounds of its earlier runs, then this run's own.
    A round is any question shown, the planner's or code's own (F1210's dates)."""
    return int(state.get(ROUNDS_KEY) or 0) + sum(
        1 for h in (getattr(run, "step_history", None) or []) if h.get("step_id") in _ASKING_STEPS)


def note_question(state: dict, about: str, subject: str, question: str, field: str = "") -> None:
    """Record a question put to the person, so no later run of the request asks it again; `field` is its field in a
    form (F1253), which its answer comes back by."""
    state[ASKED_KEY] = [*_with_answers(state), {"about": about, "subject": subject, "question": question,
                                                 **({"field": field} if field else {})}]


def form_text(lead: str, fields: list[dict]) -> str:
    """A form's words (F1253): its line, then each field numbered, with its choices or the guess filled in. What the
    run page, a channel and the chat box answer; the panel draws the fields themselves."""
    def line(f: dict) -> str:
        options, value, label = f.get("options") or [], f.get("value"), str(f.get("label") or f.get("name") or "")
        if options:
            label += " (" + (", ".join(options[:-1]) + " or " + options[-1] if len(options) > 1 else options[0]) + ")"
        elif value and str(value) not in label:
            label += f" (my guess: {value})"
        return label
    return "\n".join([lead.strip(), *(f"{n}. {line(f)}" for n, f in enumerate(fields, 1))]).strip()


def carry_request(previous: dict | None, history: list[dict] | None = None, run_id: str = "") -> dict:
    """What a new run of a conversation starts with from its request so far (F1226), as initial state, from the
    newest run (`previous`, its state; `history`, its step history) unless that run saved something: a saved build,
    change or card ends the request, and the next one asks afresh. Stage 1's brief 6 asked "has the knowledge base been
    created?" in three runs, five times each.

    * `ASKED_KEY`: the questions `ask_person` put, with their answers, never asked again;
    * `ROUNDS_KEY`: how many questions the request has shown so far, the cap's count;
    * the build check's own `asked` (`CHECKED_KEY`), each entry whole: the planner reads every question with its
      answer, and a question code asked itself is known by its entry (F1210's dates, by `means`).

    * `WAITING_PLAN` (F1241): the plan, when that run stopped waiting at its review (or parked one it did not take
      up), so a message that continues the request changes it rather than starting over; and when its save stopped
      after the yes having made only its tools (F1375): the request is not done, and "Ask again" has a plan to show.

    The route stages this as `CARRY_KEY`; it is the run's only when classify says the new message continues the
    request (`adopt_carry`). `run_id` is that run's."""
    from core.native_builder.build_plan import CHECKED_KEY, checked_in
    from core.orchestration.run_writes import WRITES_KEY

    if not isinstance(previous, dict) or not previous:
        return {}
    stopped = _stopped_with_only_its_tools(previous)
    if previous.get(WRITES_KEY) and not stopped:
        return {}
    shown = sum(1 for h in history or [] if isinstance(h, dict) and h.get("step_id") in _ASKING_STEPS)
    out: dict[str, Any] = {ASKED_KEY: [{**a, "carried": True} for a in _with_answers(previous)],
                           ROUNDS_KEY: int(previous.get(ROUNDS_KEY) or 0) + shown}
    if asked := (checked_in(previous) or {}).get("asked"):
        out[CHECKED_KEY] = {"asked": [dict(a) for a in asked if isinstance(a, dict)]}
    last = next((h for h in reversed(history or []) if isinstance(h, dict) and h.get("step_id")), {})
    if stopped or (last.get("step_id") == PLAN_REVIEW_STEP and (checked_in(previous) or {}).get("outcome") == "review"):
        out[WAITING_PLAN] = {"run_id": run_id, "state": {k: previous[k] for k in _plan_keys() if k in previous}}
    elif isinstance(parked := previous.get(PARKED_PLAN_KEY), dict) and parked.get("state"):
        out[WAITING_PLAN] = parked
    return out


def _stopped_with_only_its_tools(previous: dict) -> bool:
    """Whether the run's save stopped after the yes having written nothing but the tools it made (F1375): the tools'
    pass (`tools_made`) or the save (`build_saved`), each the step's output, under the run's own approval, so no
    plan without a yes is ever carried as one waiting (BLD1's read)."""
    from core.native_builder.build_plan import _obj
    from core.native_builder.review import approved_review
    from core.orchestration.run_writes import WRITES_KEY

    if not any((_obj(previous.get(k)) or {}).get("outcome") == "stopped" for k in ("tools_made", "build_saved")):
        return False
    if approved_review(previous) is None:
        return False
    return all(isinstance(w, dict) and w.get("kind") == "tool" for w in previous.get(WRITES_KEY) or [])


async def ask_person(args: dict, server_module: Any, events: list[dict]) -> dict:
    items = [q for q in args.get("questions") or [] if isinstance(q, dict) and _text(q.get("question"))]
    if items:
        return _ask_form(_text(args.get("question")) or FORM_LEAD, items[:FORM_MAX])
    question = _text(args.get("question"))
    if not question:
        return {"error": "ask_person needs the question."}
    question, options = asked_options(question, list(args.get("options") or []))
    options = options[:8]
    asked = " / ".join([question, *options])
    if _ASKS_SECRET.search(asked):
        return {"error": SECRET_ASKED}
    way_in = _ASKS_WAY_IN.search(asked)
    if way_in and not (args.get("about") == "which" and way_in.group(0).lower().startswith("http tool")):
        return {"error": WAY_IN_ASKED}
    _, run = _run()
    about = args.get("about") if args.get("about") in ABOUT else "what"
    subject = _text(args.get("subject"))
    state = run.shared_state if run is not None else {}
    # After a don't-know, its words before the asked-twice guard's: what to do instead (F1310).
    if (unknown := _unknown_refusal(state, about, subject, question)) is not None:
        return {"error": unknown}
    if (same := asked_before(state, about, subject, question)) is not None:
        return {"error": ASKED_ALREADY.format(question=same.get("question") or question,
                                              answer=same.get("answer") or "none yet")}
    # The request's count, not the run's: its questions in earlier runs, then this run's own (F1226).
    if (asked := questions_asked(state, run)) >= QUESTIONS:
        return {"error": ASKED_ENOUGH.format(n=asked)}
    if run is not None:
        note_question(state, about, subject, question)
    return {"finish": "ask_person", "question": _with_address_hint(question, [question, subject]), "about": about,
            "options": options}


def _ask_form(lead: str, items: list[dict]) -> dict:
    """Several questions as one form (F1253): one round of the request's cap, each question checked and noted as
    `ask_person`'s one is, and kept with its field, which its answer comes back by."""
    asked: list[tuple[str, str, str, list[str], dict]] = []
    for n, item in enumerate(items, 1):
        question, options = asked_options(_text(item.get("question")), list(item.get("options") or []))
        options = options[:8]
        words = " / ".join([lead, question, *options])
        if _ASKS_SECRET.search(words):
            return {"error": SECRET_ASKED}
        way_in = _ASKS_WAY_IN.search(words)
        if way_in and not (item.get("about") == "which" and way_in.group(0).lower().startswith("http tool")):
            return {"error": WAY_IN_ASKED}
        kind = "choice" if options else item.get("kind") if item.get("kind") in KIND_TYPE else "text"
        field = {"name": f"q{n}", "type": KIND_TYPE[kind], "label": question,
                 **({"options": options} if options else {}),
                 **({"value": guess} if (guess := _text(item.get("guess"))) and kind != "yes_no" else {})}
        asked.append((item.get("about") if item.get("about") in ABOUT else "what", _text(item.get("subject")),
                      question, options, field))
    _, run = _run()
    state = run.shared_state if run is not None else {}
    for about, subject, question, _, _ in asked:
        if (unknown := _unknown_refusal(state, about, subject, question)) is not None:
            return {"error": unknown}
        if (same := asked_before(state, about, subject, question)) is not None:
            return {"error": ASKED_ALREADY.format(question=same.get("question") or question,
                                                  answer=same.get("answer") or "none yet")}
    if (n := questions_asked(state, run)) >= QUESTIONS:
        return {"error": ASKED_ENOUGH.format(n=n)}
    if run is not None:
        for about, subject, question, _, field in asked:
            note_question(state, about, subject, question, field=field["name"])
    return {"finish": "ask_person", "question": _with_address_hint(lead, [w for a in asked for w in a[1:3]]),
            "form": [field for *_, field in asked]}


#: Code's own line under a question for a tool's address (Naveen 11-04): what else the person may give.
ADDRESS_HINT = "You can also paste a curl, its OpenAPI spec, or a link to its docs."
#: A question for where a tool calls: a URL or an endpoint, or an address said with what calls it (a ship-from address,
#: an email address, is a person's and gets no line).
_ASKS_URL = re.compile(r"(?i)\b(?:url|urls|endpoint|endpoints)\b")
_ASKS_ADDRESS = re.compile(r"(?i)\baddress\b")
_A_TOOLS = re.compile(r"(?i)\b(?:tool|api|service|webhook|server)\b")


def _with_address_hint(question: str, said: list[str]) -> str:
    """`question`, and under it ADDRESS_HINT when it, or what it is about, asks where a tool calls."""
    words = " ".join(w for w in said if w)
    if ADDRESS_HINT in question or not (_ASKS_URL.search(words) or
                                        (_ASKS_ADDRESS.search(words) and _A_TOOLS.search(words))):
        return question
    return f"{question}\n\n{ADDRESS_HINT}"


async def cannot(args: dict, server_module: Any, events: list[dict]) -> dict:
    why, nearest = _text(args.get("why")), _text(args.get("nearest"))
    if not why or not nearest:
        return {"error": "cannot needs both why and nearest: the nearest thing this product does instead."}
    if asks_for_a_secret(f"{why}\n{nearest}"):
        # Said to the person as it is: a key is never a reason to stop, nor something to ask for (F1093).
        return {"error": CANNOT_FOR_A_KEY}
    _, run = _run()
    if run is not None and (back := words_sent_back(run.shared_state, run)):
        return {"error": back}
    screen = _text(args.get("screen"))
    if not screen:
        return {"finish": "cannot", "why": why, "nearest": nearest}
    ids = [s.id for s in feature_census.current()]
    if screen not in ids:
        return {"error": f"cannot's screen {screen!r} is no screen here: name one of {', '.join(ids)}, or leave it out "
                         "when none fits."}
    return {"finish": "cannot", "why": why, "nearest": nearest, "screen": screen}


def cannot_said(draft: dict) -> str:
    """A `cannot` in words: why, the nearest thing that works, and the link to the screen it names, made from the
    host's census (F1265). A finish that names no screen gets no link: never one guessed from its words."""
    said = f"{str(draft.get('why') or '').rstrip('.')}. {draft.get('nearest') or ''}".strip()
    screen = next((s for s in feature_census.current() if s.id == draft.get("screen")), None)
    return f"{said} Open {feature_census.link(screen)}." if screen else said


async def say(args: dict, server_module: Any, events: list[dict]) -> dict:
    """The one planner's finish when the ask needs nothing made or changed (D340 D0): the reply. It is said through
    the answer route's own check (`answer_pack.check_answer`), so no yes is offered that nothing can take (F1243)."""
    text = _text(args.get("text"))
    if not text:
        return {"error": "say needs the reply itself as text."}
    if asks_for_a_secret(text):
        return {"error": CANNOT_FOR_A_KEY}  # a reply never asks for a key either (F1093)
    _, run = _run()
    if run is not None and (back := words_sent_back(run.shared_state, run)):
        return {"error": back}  # a change the person can't answer for ends in a card, once asked (F1310)
    return {"finish": "say", "text": text}


# ── what exists ───────────────────────────────────────────────────────────────────────────────────────────────────

async def candidates(kind: str) -> list[dict] | None:
    """Everything of `kind` a change may name, as {id, name, detail}; None for a kind an act's own lookup resolves
    (a run, a board, a knowledge base…)."""
    if kind == "workflow":
        from core.builder_tools import _is_visible
        from core.store.resources import load_orchestrations

        return [{"id": o["id"], "name": o.get("name") or o["id"], "detail": o.get("description") or ""}
                for o in await load_orchestrations() if _is_visible(o) and o.get("is_active") is not False]
    if kind == "agent":
        from core.store.resources import load_agents

        return [{"id": a["id"], "name": a.get("name") or a["id"], "detail": a.get("description") or ""}
                for a in await load_agents() if not a.get("system")]
    if kind == "schedule":
        from core.store import schedules

        return [{"id": s["id"], "name": s.get("name") or s["id"],
                 "detail": s.get("cron_expression") or ""} for s in await schedules.load()]
    if kind == "skill":
        from core import skills

        # The library's only: a change is made there (`update_skill`), and a library skill shadows a disk copy of the
        # same name, which would otherwise be a second, identical option (the branch review's finding 6).
        return [{"id": s["name"], "name": s["name"], "detail": s.get("description") or ""}
                for s in await skills.list_skills() if s.get("source") == "library"]
    if kind == "tool":
        from core.store.resources import load_tools

        return [{"id": t["name"], "name": t["name"], "detail": t.get("description") or ""}
                for t in await load_tools() if t.get("name")]
    if kind == "document":
        return await _documents()
    if kind == "knowledge_base":
        return await _bases()
    if kind == "memory":
        return await _memory_items()
    if kind == "recording":
        from core.browser import recordings

        return [{"id": d["id"], "name": d.get("name") or d["id"], "detail": ", ".join(d.get("sites") or [])}
                for d in await recordings.all_recordings()]
    return None


def resolve(items: list[dict], target: dict) -> list[dict]:
    """The items `target` names: by id, else one exact name, else every name it is part of (F760's rule, as the
    act cards' `_pick`)."""
    from core.builder_actions import listed

    wanted_id, wanted = _text(target.get("id")), _text(target.get("name")) or _text(target.get("id"))
    if wanted_id:
        by_id = [i for i in items if i["id"] == wanted_id]
        if by_id:
            return by_id
    for text in (wanted_id, wanted):  # what a pick list printed, `"name" (id)`, picks by its id (F760)
        if (by_listing := listed(items, text)) is not None:
            return [by_listing]
    needle = wanted.lower()
    if not needle:
        return []
    exact = [i for i in items if i["name"].lower() == needle]
    if exact:
        return exact
    partial = [i for i in items if needle in i["name"].lower()]
    # "the Front desk agent": a name said with its own kind after it (F959, Nova Pro). The name without it, exactly,
    # sits beside what the whole name is part of: "Support agent" is "Support" or "Support agent for billing", a
    # question, never the partial one alone (SAI2's review, 5). Only the target's own kind's word is set aside.
    bare = next((needle[: -len(word)].strip() for word in _KIND_WORDS.get(target.get("kind"), ())
                 if needle.endswith(" " + word)), "")
    if not bare:
        return partial
    named = [i for i in items if i["name"].lower() == bare and i not in partial]
    return (named + partial) or [i for i in items if bare in i["name"].lower()]


#: The words a person puts after a name to say what kind of thing it is: "the Front desk agent".
_KIND_WORDS = {"agent": ("agent",), "workflow": ("workflow", "orchestration"), "schedule": ("schedule",),
               "tool": ("tool",), "skill": ("skill",), "recording": ("recording",)}


async def read(kind: str, target_id: str) -> dict | None:
    """The target as it is now, for the no-op check, the before-image and the effect check."""
    if kind == "workflow":
        from core.builder_tools import _visible_orchestration

        return await _visible_orchestration(target_id)
    if kind == "agent":
        from core.store.resources import get_agent

        agent = await get_agent(target_id)
        return None if agent is None or agent.get("system") else agent
    if kind == "schedule":
        from core.store import schedules

        found = await schedules.get(target_id)
        # One saved before the policy was asked reads as the screen shows it, skip (F1267): the no-op check, the
        # before-image an undo puts back and the effect check all need its value.
        return None if found is None else {"missed_run_policy": "skip", **found}
    if kind == "skill":
        from core import skills

        # The library's own (F1266): a disk skill of the same name is not what a change or a delete is about.
        return await skills.get_library_skill(target_id)
    if kind == "tool":
        from core.store.resources import load_tools

        return next((t for t in await load_tools() if t.get("name") == target_id), None)
    if kind == "settings":
        # The workspace's one set of settings, whatever it was called (F1264): what Synapse AI may set, as it reads.
        from core.product_tools import settings

        return await settings.current()
    if kind == "document":
        return await _document_now(target_id)
    if kind == "knowledge_base":
        return await _base_now(target_id)
    if kind == "memory":
        return await _memory_now(target_id)
    return None


async def _bases() -> list[dict] | None:
    """The workspace's knowledge bases, by name, for a change that names one (F1276); none while Knowledge is off."""
    from core.knowledge import bases
    from core.knowledge.config import knowledge_enabled

    if not knowledge_enabled():
        return []
    return [{"id": kb.id, "name": kb.name, "detail": kb.description or ""} for kb in await bases.list_bases()]


async def _base_now(kb_id: str) -> dict | None:
    """A base's plain settings as a change reads them (F1276)."""
    from core.knowledge import bases
    from core.knowledge.config import knowledge_enabled

    kb = await bases.get_base(kb_id) if knowledge_enabled() else None
    if kb is None:
        return None
    return {"id": kb.id, "name": kb.name, "language": kb.language, "visitor_access": kb.visitor_access,
            "budget_monthly_usd": kb.budget_monthly_usd}


def _usd(value: Any) -> str:
    return "no budget" if value is None else f"${float(value):,.2f} a month"


async def _settings_line(target: dict, live: dict, want: dict) -> str:
    """The review's lines for a base's settings (F1276): each one before → after, what opening it to visitors means,
    and what a new language's index costs by the Knowledge screen's own estimate."""
    name = _quoted(target["name"])
    lines = [f"Change the settings of the knowledge base {name}:"]
    if "language" in want:
        lines.append(f"  language: {live.get('language')} → {want['language']}")
        try:
            from core.knowledge import bases, costs

            est = await costs.preview(await bases.require_base(target["id"]), {"language": want["language"]})
            lines.append(f"    It is indexed again with the new language, beside the index in use, which keeps "
                         f"answering until the new one is ready: about ${est.total_est_usd:,.2f} for its {est.docs} "
                         "documents, by the Knowledge screen's estimate.")
        except Exception as exc:  # noqa: BLE001 — the change is still shown; its price is said to be unknown
            _log.warning("knowledge_settings: no estimate for %s: %s", target.get("id"), exc)
            lines.append("    It is indexed again with the new language, beside the index in use; its cost could not "
                         "be estimated here, so see it on the Knowledge screen first.")
    if "visitor_access" in want:
        lines.append(f"  visitors may search it: {'on' if live.get('visitor_access') else 'off'} → "
                     f"{'on' if want['visitor_access'] else 'off'}")
        if want["visitor_access"]:
            lines.append(f"    Visitors of this workspace's widgets and channels, where its knowledge tools are "
                         f"ticked, will be able to search {name}: everything in it.")
    if "budget_monthly_usd" in want:
        lines.append(f"  monthly budget: {_usd(live.get('budget_monthly_usd'))} → {_usd(want['budget_monthly_usd'])}")
    return "\n".join(lines)


# ── an agent's memory and settings (F1259) ────────────────────────────────────────────────────────────────────────

async def _memory_items() -> list[dict]:
    """Every agent's memory items, each by `<agent id>/<item>` and named "<agent>: <item>", as a change names one."""
    from core.product_tools.agent_memory import items
    from core.store.resources import load_agents

    out: list[dict] = []
    for agent in await load_agents():
        if not agent.get("system"):
            out += [{"id": i["id"], "name": f"{agent.get('name') or agent['id']}: {i['name']}", "detail": i["summary"]}
                    for i in await items(agent)]
    return out


async def _memory_now(item_id: str) -> dict | None:
    """One memory item, whole, as the Memory screen opens it."""
    from core import agent_memory
    from core.store.resources import get_agent

    agent_id, _, name = item_id.partition("/")
    agent = await get_agent(agent_id) if agent_id and name else None
    if agent is None or agent.get("system"):
        return None
    content = agent_memory.read_file(agent_id, "items", name)
    return None if content is None else {"id": item_id, "name": name, "agent": agent.get("name") or agent_id,
                                         "content": content}


#: Which kind of agent has each section's settings, as the agent editor shows that section only on it.
#: Each section's agent type, as the agent keeps it, and the section in the person's words.
_SECTION_KIND = {"set_agent_team": ("delegate", "team"), "set_agent_browser": ("browser", "browser"),
                 "set_agent_code": ("code", "code access")}
#: An agent's type in the person's words, where the kept one differs (a team is kept as `delegate`).
_KIND_WORD = {"delegate": "team"}

#: The settings that name other things, each as the agent keeps it (by id), and what one is called to the person.
_NAMED = {"knowledge": "knowledge base", "add": "agent", "remove": "agent", "repos": "repository",
          "db_configs": "database", "recordings": "recording"}


async def _rows(key: str, live: dict) -> list[dict]:
    """The things a setting may name, as {id, name}: what the agent editor's own pickers list."""
    if key == "knowledge":
        from core.knowledge import bases
        from core.knowledge.config import knowledge_enabled

        return [{"id": kb.id, "name": kb.name} for kb in await bases.list_bases()] if knowledge_enabled() else []
    if key in ("add", "remove"):
        here = await candidates("agent") or []
        # One taken out may be gone since: it is still the agent's to drop.
        return here + [{"id": i, "name": i} for i in live.get("delegate_agent_ids") or []
                       if i not in {r["id"] for r in here}]
    if key in ("repos", "db_configs"):
        from core.routes.db_configs import load_db_configs
        from core.routes.repos import load_repos

        rows = await (load_repos() if key == "repos" else load_db_configs())
        return [{"id": r["id"], "name": r.get("name") or r["id"]} for r in rows if r.get("id")]
    if key == "recordings":
        return await candidates("recording") or []
    if key == "sessions":
        from core import site_sessions
        from core.builder_tools import _acting_caller

        return [{"id": r["id"], "name": f"the {r.get('site')} session"} for r in await site_sessions.listed(
            _acting_caller()) if r.get("id")]
    return []


async def _agent_refs(op: C.Op, fields: dict, live: dict) -> str | None:
    """An agent op's names made the ids the agent keeps (F1259): its knowledge bases, members, repositories,
    databases and recordings by name, its sessions by site, as the editor's pickers and save take them; and each
    section only on the kind of agent that has it. The refusal, in the person's words, or None."""
    from core.browser import save_checks
    from core.browser.picks import PickRefused
    from core.builder_tools import _acting_caller

    name = _quoted(live.get("name") or live.get("id") or "")
    if op.name in _SECTION_KIND:
        kind, section = _SECTION_KIND[op.name]
        if (have := live.get("type") or "conversational") != kind:
            return (f"Only a {_KIND_WORD.get(kind, kind)} agent has {section} settings, and {name} is a "
                    f"{_KIND_WORD.get(have, have)} agent: a different kind of agent is a new one, made by a build.")
    for key, label in _NAMED.items():
        if not fields.get(key) or (key in ("add", "remove") and op.name != "set_agent_team"):
            continue
        rows = await _rows(key, live)
        known, by_name = {r["id"] for r in rows}, {r["name"].lower(): r["id"] for r in rows}
        got = [v if v in known else by_name.get(str(v).strip().lower()) for v in fields[key]]
        if missing := [v for v, i in zip(fields[key], got) if i is None]:
            listed = ", ".join(sorted(r["name"] for r in rows)[:10])
            return (f"No {label} is called {', '.join(_quoted(str(m)) for m in missing)}"
                    + (f": there are {listed}." if listed else "."))
        fields[key] = list(dict.fromkeys(got))
    if live.get("id") in (fields.get("add") or []):
        return f"{name} can't be a member of its own team."
    if fields.get("sessions"):
        try:
            fields["sessions"] = await save_checks.resolve_sessions(fields["sessions"], caller=_acting_caller())
        except PickRefused as refused:
            return str(refused)
    return None


async def _agent_problem(op: C.Op, live: dict, want: dict) -> str | None:
    """What the agent editor's save would refuse in the agent this op leaves (F1259): its own model's check of the
    whole agent, and the knowledge bases it answers from as the editor holds them."""
    from core import agent_knowledge
    from core.native_builder.agent_census import editor_problem

    if "knowledge" in want and (wrong := await agent_knowledge.refusal(want["knowledge"], live.get("knowledge"))):
        return wrong
    return editor_problem({**live, **want}, only=set(want))


def _setting_value(value: Any, names: dict[str, str]) -> str:
    if value is None or value == []:
        return "not set" if value is None else "none"
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, list):
        return ", ".join(names.get(str(v), str(v)) for v in value)
    return str(value)


async def _agent_lines(op: C.Op, target: dict, live: dict, want: dict) -> str:
    """The review's lines for an agent's settings (F1259): each one that changes, before → after, things it names by
    their names; and what letting a browser agent type and send means."""
    from core.models import AgentBrowser, AgentTeam

    names: dict[str, str] = {}
    for key in ("knowledge", "add", "repos", "db_configs", "recordings", "sessions"):
        try:
            names.update({r["id"]: r["name"] for r in await _rows(key, live)})
        except Exception as exc:  # noqa: BLE001 — an id the review can't name is shown as itself
            _log.warning("agent review: %s not named: %s", key, exc)
    shown = {"delegate_agent_ids": "members", "db_configs": "databases", "repos": "repositories",
             "system_prompt": "instructions", "skip_default_tools": "leave out the default tools",
             "tools": "tools", "max_turns": "turn limit", "turn_limit_mode": "at the turn limit",
             "max_output_tokens": "longest answer (tokens)", "reasoning_effort": "reasoning"}
    # A part the agent has never saved reads as the editor shows it: its defaults.
    parts = {"team": AgentTeam, "browser": AgentBrowser}
    lines = [f"{op.verb} the agent {_quoted(target['name'])}:"]
    for key, after in want.items():
        before = live.get(key)
        if key in parts:
            before = {**parts[key]().model_dump(), **(before or {})}
        if isinstance(after, dict):
            for sub, value in after.items():
                was = (before or {}).get(sub) if isinstance(before, dict) else None
                if not C._same(value, was):
                    lines.append(f"  {key} {sub.replace('_', ' ')}: {_setting_value(was, names)} → "
                                 f"{_setting_value(value, names)}")
        elif not C._same(after, before):
            lines.append(f"  {shown.get(key, key.replace('_', ' '))}: {_setting_value(before, names)} → "
                         f"{_setting_value(after, names)}")
    browser = want.get("browser") or {}
    if browser.get("mode") == "co_drive" and (live.get("browser") or {}).get("mode") != "co_drive":
        where = ", ".join(browser.get("sites") or []) or "any public site"
        lines.append(f"    {_quoted(target['name'])} may then type and send on {where}, with you watching.")
    return "\n".join(lines)


def _memory_line(op: C.Op, target: dict, live: dict | None, want: dict | None) -> str:
    """The review's line for one memory item (F1259): which agent's, and its text before and after."""
    agent, _, item = target["name"].rpartition(": ")
    what = f"the memory item {_quoted(item or target['name'])} of the agent {_quoted(agent)}"
    if op.kind == "delete":
        return f"Delete {what}, which reads {_quoted(_text((live or {}).get('content'))[:200])}"
    if op.internal:
        return f"Bring back {what}, as it was before it was deleted"
    return f"Change {what}, kept under its name —{_before_after(str((live or {}).get('content') or ''), want['content'])}"


#: The most documents a change can name, across the bases (F1258).
_DOCUMENTS = 500


def _document_name(row: Any) -> str:
    return (row.title or "").strip() or row.path.rsplit("/", 1)[-1]


async def _documents() -> list[dict] | None:
    """Every document of the workspace's active knowledge bases, by its title (or file name), for a change that names
    one (F1258); none while Knowledge is off."""
    from core.knowledge import bases, documents
    from core.knowledge.config import knowledge_enabled

    if not knowledge_enabled():
        return []
    out: list[dict] = []
    for kb in await bases.list_bases():
        if kb.status != "active":
            continue
        for row in await documents.list_for(kb.id, limit=_DOCUMENTS - len(out)):
            out.append({"id": row.id, "name": _document_name(row), "detail": f"in {_quoted(kb.name)}"})
        if len(out) >= _DOCUMENTS:
            break
    return out


async def _document_now(doc_id: str) -> dict | None:
    """A document as a change reads it: its text when it can be edited here, else why not (F1258)."""
    from core.knowledge import bases, documents, ingest
    from core.knowledge.config import knowledge_enabled

    if not knowledge_enabled():
        return None
    row = await documents.get(doc_id)
    if row is None or row.status == "deleting":
        return None
    kb = await bases.get_base(row.kb_id)
    text, why = await ingest.editable_text(row)
    return {"id": row.id, "name": _document_name(row), "knowledge_base": kb.name if kb else "", "text": text,
            "why_not": why}


def _edit_refusal(target: dict, fields: dict, live: dict) -> str | None:
    """Why a document's edit can't be made as proposed, in the person's words (F1258), or None: its text can't be
    edited here, or the passage it changes isn't in it exactly once, or it would leave the document empty."""
    name = _quoted(target["name"])
    if live.get("text") is None:
        return f"I can't change {name}: {live.get('why_not')}."
    find = fields.get("find")
    if find not in (None, ""):
        times = live["text"].count(find)
        if times == 0:
            return f"{name} has no passage that reads {_quoted(_text(find)[:120])}: which part should change?"
        if times > 1:
            return (f"{_quoted(_text(find)[:120])} is in {name} {times} times: which one should change? Quote a "
                    "little more of it.")
    if not (C.expected(C.OPS["knowledge_edit_document"], fields, live) or {}).get("text", "").strip():
        return f"That would leave {name} empty: to take it out of its knowledge base, delete it instead."
    return None


def _edit_line(target: dict, before: str, after: str, base: str) -> str:
    """The review's line for a document's edit (F1258): the section as it stands and as it will be, the rest kept."""
    where = f" in {_quoted(base)}" if base else ""
    return (f"Change {_quoted(target['name'])}{where}, kept under its own name and folder and indexed again —"
            f"{_before_after(before, after)}")


def _before_after(before: str, after: str) -> str:
    """A text's changed part as it stands and as it will be, marked, with a little of what is kept around it."""
    start = 0
    while start < min(len(before), len(after)) and before[start] == after[start]:
        start += 1
    end = 0
    while end < min(len(before), len(after)) - start and before[-1 - end] == after[-1 - end]:
        end += 1
    # Whole words: the marks go around "30 days", never inside "[3]0".
    while start > 0 and not before[start - 1].isspace():
        start -= 1
    while end > 0 and not before[len(before) - end].isspace():
        end -= 1

    def part(text: str) -> str:
        middle = text[start:len(text) - end]
        lead = ("…" if start > 60 else "") + text[max(0, start - 60):start]
        tail = text[len(text) - end:len(text) - end + 60] + ("…" if end > 60 else "")
        if len(middle) > 600:
            middle = middle[:597] + "…"
        return " ".join(f"{lead}[{middle}]{tail}".split())

    return f"\n  before: {part(before)}\n  after: {part(after)}"


# ── the check ─────────────────────────────────────────────────────────────────────────────────────────────────────

def _caller():
    from core.builder_actions import _caller as caller

    return caller()


def _run():
    from core.builder_tools import _current_run
    from core.orchestration.engine import live_run

    run_id = _current_run.get() or ""
    return run_id, (live_run(run_id) if run_id else None)


def _quoted(name: str) -> str:
    return f'"{name}"'


def _words(op: C.Op, target: dict, fields: dict, says: str, want: dict | None = None,
           live: dict | None = None) -> str:
    """One line of the review, in code's words: what this op does and every value its call sets (MCP2's review: the
    person approves what is done). Built from `want`, what the call leaves the target reading, the object the call
    itself is built from; a new schedule says what it starts by name. `says` is code's own note (an undo's), never
    the planner's gloss on a write."""
    kind = "workflow" if op.target == "workflow" else op.target.replace("_", " ")
    what = f"{op.verb} the {kind} {_quoted(target['name'])}"
    if op.name in ("rename_workflow", "rename_schedule", "rename_agent"):
        detail = f"to {_quoted(fields['name'])}"
    elif op.name == "create_tool":
        detail = _new_tool_detail(target["name"], fields)
    elif op.name == "update_tool":
        detail = _tool_change_detail(fields, want or {}, live)
    elif op.name == "update_python_tool":
        detail = _code_change_detail(fields, want or {}, live)
    elif op.internal:
        detail = "as it was before it was deleted"  # an undo's re-create, from the card's before-image
    elif op.kind == "delete":
        detail = ""
    else:
        shown = dict(want if want is not None else {k: v for k, v in fields.items() if k != "runs"})
        if op.name in ("create_schedule", "retarget_schedule"):
            runs = fields["runs"]
            shown = {"starts": _quoted(runs.get("name") or runs["id"]),
                     **{k: v for k, v in shown.items() if k not in ("target_type", "target_id")}}
        detail = ", ".join(f"{k}: {json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v}"
                           for k, v in shown.items())
    detail = "; ".join(p for p in (detail, says) if p)
    return f"{what} — {detail}" if detail else what


def _settings_rows(live: dict | None, want: dict) -> list[dict]:
    """A settings change's rows (F1264, REACH2's one shape): each setting by its label, as it reads now and as it
    will, in the words its screen uses ("automatic", "the default model")."""
    from core import settings_allow
    from core.native_builder import rows

    live = live or {}
    shown = {k: settings_allow.shown(k, live.get(k)) for k in want}
    after = {k: settings_allow.shown(k, v) for k, v in want.items()}
    return rows.changed(shown, after, {k: settings_allow.ALLOWED[k].label for k in want})


def _settings_words(changed: list[dict], says: str) -> str:
    """Its review line: what it is, then a line per setting, as the card's rows say it."""
    from core.native_builder import rows

    head = "Change the workspace's settings:" + (f" {says}" if says else "")
    return f"{head}\n{rows.as_text(changed)}"


def _review(ops: list[dict]) -> str:
    lines: list[str] = []
    for item in ops:
        if item["status"] != "ready":
            continue
        if item["kind"] == "delete":
            used = item.get("used_by") or []
            uses = ("used by " + ", ".join(used)) if used else "nothing uses it"
            lines.append(f"- ⚠️ DELETES: {item['line']} — {uses}. {DELETE_NOTE}")
        else:
            lines.append(f"- {_with_says(item['line'], item.get('says'))}")
    return "\n".join(lines)


def _with_says(line: str, says: Any) -> str:
    """An op's review line with the planner's own words for it under it, above the exact change (F1308): prod-04's
    "Change Friday closing time from 7pm to 6pm" over a before/after that closed every weekday at 6pm, approved because
    only the diff was shown. The line stays code's alone (MCP2's note 3): the gloss is on its own line, said as whose
    it is, and scrubbed like the rest of the card (the shapes, and the workspace's held secrets)."""
    from core.secret_scrub import scrub_credentials, scrub_secrets

    said = scrub_secrets(scrub_credentials(_text(says))[0])
    head, _, rest = str(line).partition("\n")
    if not said or said.casefold() in head.casefold():
        return str(line)
    return f"{head}\n  {DESCRIBED_AS}: {said}" + (f"\n{rest}" if rest else "")


def _said(ops: list[dict]) -> str:
    """What the person is told when nothing waits for approval: each card, no-op, refusal and op left out."""
    out = [item["say"] for item in ops if item["status"] in ("card", "noop", "refused", "left_out") and item.get("say")]
    return " ".join(out) or "There was nothing to change."


def _plain_words(text: Any) -> str:
    """Words as compared for "did the person say it": case, quotes and spacing aside."""
    # Letters, digits and marks: a Devanagari vowel sign is a mark, which `isalnum` would drop (SAI2's review).
    return " ".join("".join(c if unicodedata.category(c)[0] in "LNM" else " "
                            for c in str(text or "").casefold()).split())


def _persons_words(state: dict) -> str:
    """What the person has said in this conversation: this message, their own earlier turns (never the assistant's),
    and every answer to the planner's questions and reply to a review this run has heard (`HEARD_KEY`, kept by
    `_heard`: a pick's "the second one" no longer replaces the name they gave before it, SAI2's review)."""
    history = str(state.get("chat_history") or "")
    theirs = [m.group("said") for m in _TURN.finditer(history) if m.group("role") == "user"]
    return " ".join(_plain_words(w) for w in (state.get("user_message"), *theirs, *(state.get(HEARD_KEY) or [])))


#: One turn of `chat_history` (`role: content`, one per line, as the builder's route writes it).
_TURN = re.compile(r"^(?P<role>user|assistant|system): (?P<said>.*(?:\n(?!(?:user|assistant|system): ).*)*)", re.M)

#: Words that ask for a new name without saying it ("update its name to match").
_RENAME_WORDS = frozenset({"rename", "renamed", "name", "named", "call", "called", "title", "retitle"})

#: Where the run keeps what the person answered and replied, each once, in order.
HEARD_KEY = "change_heard"


def _heard(state: dict) -> None:
    """Keep this pass's answer to the planner and reply to the review with the run's earlier ones."""
    clarify, review = state.get("change_clarify"), state.get("manage_review")
    heard = list(state.get(HEARD_KEY) or [])
    for said in (clarify.get("answer") if isinstance(clarify, dict) else clarify,
                 review.get("feedback") if isinstance(review, dict) else review):
        if isinstance(said, str) and said.strip() and said not in heard:
            heard.append(said)
    state[HEARD_KEY] = heard


async def _used_by(op: C.Op, target_id: str) -> tuple[list[str], str | None]:
    """What uses a delete's target, as names, and the refusal when that stops the delete (F690 / F467)."""
    from core.orchestration import dependents

    if op.target == "agent":
        users = await dependents.what_uses_agent(target_id)
        agent = await read("agent", target_id) or {}
        return [f"{u.kind} {_quoted(u.name)}" for u in users], (
            dependents.agent_refusal(agent.get("name") or target_id, users) if users else None)
    if op.target == "workflow":
        runners = await dependents.what_runs(target_id)
        orch = await read("workflow", target_id) or {}
        return [f"{u.kind} {_quoted(u.name)}" for u in runners], (
            dependents.refusal(orch.get("name") or target_id, runners) if runners else None)
    if op.target == "tool":
        # Every step that calls it or holds its agent to it, and every agent that holds it (F1348): a step's use
        # refuses the delete; agents alone are named, never refused.
        users = await dependents.what_uses_tool(target_id)
        named = [f"workflow {_quoted(u.name)} (step {_quoted(u.via)})" if u.kind == "step"
                 else f"agent {_quoted(u.name)}" for u in users]
        return named, dependents.tool_refusal(target_id, users)
    if op.target == "skill":
        # Agents that select it keep its name and load nothing until it is back (F1266): said, never refused.
        from core.store.resources import load_agents

        holders = [a.get("name") or a["id"] for a in await load_agents()
                   if not a.get("system") and target_id in (a.get("skills") or [])]
        return [f"agent {_quoted(n)}" for n in holders], None
    return [], None


async def _group_name(group_id: str) -> str | None:
    """A tool group's name: a tool keeps its group's id, and `create_http_tool` takes the name (an undo's re-create)."""
    from core.routes.tools import _load_groups

    return next((g.get("name") for g in await _load_groups() if g["id"] == group_id), None)


async def _not_found(kind: str, target: dict) -> str:
    """Why nothing matches: there is none by that name, or (a skill) it is read from disk, where it is changed."""
    wanted = target.get("name") or target.get("id") or ""
    if kind == "skill":
        from core import skills

        if resolve([{"id": s["name"], "name": s["name"]} for s in skills.scan_disk_skills()], target):
            return f"The skill {_quoted(wanted)} is read from a folder on disk, so it is changed there, not here."
    return f"No {kind} is called {_quoted(wanted)}."


async def _skill_names(fields: dict) -> str | None:
    """An agent's skills, named as `attach_skills` keeps them (lower case, hyphens: "Customer FAQ" is
    `customer-faq`), in place; and the refusal when one to add is no skill here (said now, not after the yes)."""
    from core import skills

    for key in ("add", "remove"):
        if fields.get(key):
            fields[key] = list(dict.fromkeys(skills.normalize_name(n) for n in fields[key]))
    known = {s["name"] for s in await skills.list_skills()}
    missing = [n for n in fields.get("add") or [] if n not in known]
    return f"No skill is called {', '.join(_quoted(n) for n in missing)}." if missing else None


async def _tool_names(fields: dict, made: set[str], server_module: Any) -> str | None:
    """An agent is given only a tool that is here, or one this change makes (F1322: prod-13's four names nothing had
    made reached the review); the refusal names each other one, with the nearest that are here (`builder_guard`'s
    own check, the agent editor's)."""
    from core import builder_guard

    wanted = [n for n in fields.get("add") or [] if n not in made]
    gone = await builder_guard.missing(wanted, server_module) if wanted else []
    return " ".join(f"No tool is called {_quoted(n)}" + (f": the nearest here {'is' if len(near) == 1 else 'are'} "
                                                         f"{', '.join(_quoted(c) for c in near)}." if near else ".")
                    for n, near in gone) or None


async def _tool_change_refusal(name: str, fields: dict, live: dict | None) -> str | None:
    """Why this HTTP tool can't be changed as asked, before the review (F1312b): `update_http_tool`'s own refusal, in
    its own words (`http_tool_update_issues`, the check the tool itself runs)."""
    from core.builder_tools import http_tool_update_issues

    refused = await http_tool_update_issues(live, {"name": name, **C.expected(C.OPS["update_tool"], fields, live)})
    return f"I can't change the tool {_quoted(name)}: {refused['error']}" if refused else None


async def _new_tool_refusal(name: str, fields: dict) -> str | None:
    """Why this new HTTP tool can't be made as asked, before the review (F1322): code's own reading of it, then
    `create_http_tool`'s own refusal in its own words (`http_tool_issues`, the check the tool itself runs)."""
    from core.builder_tools import http_tool_issues

    found = C.http_tool_problems(fields)
    if not found and (refused := await http_tool_issues(C.http_tool_args(name, fields))):
        found = [refused["error"]]
    return f"I can't make the HTTP tool {_quoted(name)}: {'; '.join(found)}" if found else None


def _new_tool_detail(name: str, fields: dict) -> str:
    """A new HTTP tool's review line after "Make the tool …" (F1322): its method and URL first, then every other value
    its call sends as it sends it (its inputs, body, headers, group, what it does), and where its key goes, to which
    host only; the person approves what is made (MCP2's finding 2)."""
    args = C.http_tool_args(name, fields)
    parts = [f"an HTTP tool: {args['method']} {args['url']}"]
    source = fields.get("from") if isinstance(fields.get("from"), dict) else {}
    if "curl" in source:
        parts.append("from the curl you pasted")
    elif source.get("spec"):
        parts.append(f"from {source['spec']}'s {source.get('operation')}")
    # A key in its query is a workspace variable the person sets (F1312a), never a value in the tool or the chat.
    if variables := re.findall(r"\{\{\s*global\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", args["url"]):
        parts.append(f"it reads the workspace variable{'s' if len(variables) > 1 else ''} {', '.join(variables)}: set "
                     f"{'them' if len(variables) > 1 else 'it'} in Settings, never in the chat")
    parts.append(f"inputs: {args['input_schema_json']}")
    parts += [f"{label}: {args[arg]}" for label, arg in (("body", "body_template"), ("headers", "headers_json"),
                                                         ("group", "group")) if arg in args]
    parts.append(f"does: {args['description']}")
    if "base_lock" in args:
        key = fields["key"]
        scheme = f" ({key['scheme']})" if _text(key.get("scheme")) else ""
        parts.append(f"its key goes in its {key['header']} header{scheme}, only ever to {args['base_lock']}, given on "
                     "a card after you approve")
    return "; ".join(parts)


def _fenced(kind: str, text: str) -> str:
    """A block inside the review's list item: indented, so it stays the item's."""
    body = "\n".join(f"  {line}" for line in (text or "").splitlines())
    return f"\n  ```{kind}\n{body}\n  ```"


def _code_change_detail(fields: dict, want: dict, live: dict | None) -> str:
    """A Python tool's change, after "Change the tool …" (F1325): its code before and after as a diff, what each
    sample printed with no network (or why it waits on "Test this fix"), what it does in new words, and its code as it
    will be (every value its call sends: the person approves what is made)."""
    new = str(want.get("code") or "")
    parts = [f"its code:{_fenced('diff', E.diff(str((live or {}).get('code') or ''), new))}"]
    tested = E.is_tested(fields) or {}
    if tested.get("passed"):
        parts.append("; ".join(E.said(tested["passed"])))
    elif tested.get("button"):
        parts.append(f"not tested yet: {tested['button']}; press Test this fix after reading it, then Go ahead")
    if "description" in want:
        parts.append(f"does: {want['description']}")
    parts.append(f"its code as it will be:{_fenced('python', new)}")
    return "\n  ".join(parts)


async def _code_change_refusal(name: str, fields: dict, live: dict | None) -> str | None:
    """Why this Python tool's code can't be changed as asked, before the review (F1325): `update_python_tool`'s own
    refusal, in its own words (`python_code_issues`: create_python_tool's draft and the host's plan gate among it)."""
    from core.builder_tools import python_code_issues

    if live and live.get("tool_type") == "python" and fields.get("code") is None:
        if why := E.apply_edits(str(live.get("code") or ""), fields.get("edits") or [])[1]:
            return f"I can't change the tool {_quoted(name)}: {why}"
    args = {"name": name, **(C.expected(C.OPS["update_python_tool"], fields, live)
                             if live and live.get("tool_type") == "python" else {"code": ""})}
    refused = await python_code_issues(live, args)
    return f"I can't change the tool {_quoted(name)}: {refused['error']}" if refused else None


async def _test_this_fix(run_id: str, n: int, name: str) -> dict:
    """The card that tests a fix on the person's press, with the internet (F1325): its button runs the change's own
    fix (`test_code_change`, by the run and its place), never code given to it."""
    from core.builder_actions import _button, _card

    return _card(f"Test this fix to {_quoted(name)}", detail=(
        "It can't be tested here with no network. Pressing this runs the fix above on its sample inputs, with the "
        "internet, as the Tools screen's Test does; nothing is changed until you press Go ahead after it passes."),
        buttons=[await _button("Test this fix", "test_code_change", {"run_id": run_id, "op": n})])


def _tool_change_detail(fields: dict, want: dict, live: dict | None) -> str:
    """An HTTP tool's change, after "Change the tool …" (F1312b): its address before and after (the call's own values,
    so the person approves what is sent), the inputs a new address adds, and what it does in new words."""
    from core import http_facts

    live = live or {}
    new = f"{want.get('method') or live.get('method') or 'GET'} {want.get('url') or live.get('url') or ''}"
    old = f"{live.get('method') or 'GET'} {live.get('url')}" if live.get("url") else ""
    parts = [f"an HTTP tool: {old} → {new}" if old and old != new else f"an HTTP tool: {new}"]
    if "curl" in (fields.get("from") if isinstance(fields.get("from"), dict) else {}):
        parts.append("from the curl you pasted; its key stays on its card, and its headers as they are")
    if added := http_facts.new_inputs(live.get("inputSchema"), want.get("url") or live.get("url") or "")[1]:
        parts.append(f"now asks for {', '.join(added)}")
    if "description" in want:
        parts.append(f"does: {want['description']}")
    return "; ".join(parts)


def _keep(op: C.Op, record: dict | None, want: dict | None) -> dict | None:
    """The before-image an undo needs, never a secret's value: the fields the op touches, or for a delete the
    record with its credentials, memory and bookkeeping left out (F797)."""
    from core.secret_scrub import holds_credential

    if record is None:
        return None
    if want is not None:
        kept = {k: record.get(k) for k in want}
        if op.target == "schedule" and any(k in C._SCHEDULE_TIME for k in want):
            # The clock its time is on, though a time change does not set it: the card says the new time there (F1010).
            kept.setdefault("timezone", record.get("timezone"))
        if op.name == "update_python_tool":
            kept["code"] = record.get("code")
        if op.name == "update_tool":
            # The inputs it had, so its undo takes out only the ones this change added (F1312b).
            kept["inputSchema"] = record.get("inputSchema")
    else:
        kept = {k: v for k, v in record.items()
                if k not in ("memory", "created_at", "updated_at", "last_run_at", "next_run_at", "headers")}
    clean: dict[str, Any] = {}
    for key, value in kept.items():
        if holds_credential(json.dumps(value, default=str)):
            clean[key] = None
            clean.setdefault("_not_kept", []).append(key)
        else:
            clean[key] = value
    return clean


async def _runs_found(chosen: dict | None, n: int, item: dict, picks: list[dict]) -> bool:
    """A schedule's `runs` made the one workflow or agent it names, by id, as its target is (a new schedule's, and a
    retarget's, F1267); False when it is refused or picked between, with the item saying so."""
    runs = dict(item["fields"]["runs"])
    if _answers(chosen, n, "runs", runs["kind"], runs):
        runs["id"] = chosen["id"]
    found = resolve(await candidates(runs["kind"]) or [], runs)
    if len(found) != 1:
        item.update(status="refused" if not found else "pick",
                    say=f"No {runs['kind']} is called {_quoted(runs.get('name') or runs.get('id'))}." if not found
                    else "")
        if found:
            picks.append({"op": n, "field": "runs", "kind": runs["kind"],
                          "wanted": runs.get("name") or runs.get("id"), "options": found[:8]})
        return False
    item["fields"]["runs"] = {"kind": runs["kind"], "id": found[0]["id"], "name": found[0]["name"]}
    return True


async def check_change(args: dict, server_module: Any, events: list[dict]) -> dict:
    """Every fact about the planner's change, decided here; see the module's docstring. `draft` is the planner's
    finish (E2's object); the person and their role are the run's, read now, never an argument. What its parts say it
    isn't doing, and why, is said with whatever it does (F1364), on the review or as the answer."""
    out = await _check_change(args, server_module, events)
    draft = args.get("draft")
    if isinstance(draft, str):
        try:
            draft = json.loads(draft)
        except ValueError:
            draft = None
    left = not_done((draft or {}).get("asked")) if isinstance(draft, dict) else ""
    if left and out.get("outcome") in ("review", "said", "create", "steps"):
        out["say"] = " ".join(x for x in (out.get("say") or "", left) if x)
    return out


async def _check_change(args: dict, server_module: Any, events: list[dict]) -> dict:
    from core.builder_actions import _FeatureOff, _NotOffered, _offer, _pressed, feature_switch
    from core.product_tools import areas, registry
    from core.product_tools.approved import digest

    draft = args.get("draft")
    if isinstance(draft, str):
        try:
            draft = json.loads(draft)
        except ValueError:
            draft = None
    draft = draft if isinstance(draft, dict) else {}
    finish = draft.get("finish")
    base = {"object": "change", "ops": [], "picks": [], "calls": [], "review": "", "say": "", "question": ""}

    if (which := _recording_or_workflow(base)) is not None:
        return which
    if finish == "ask_person" and isinstance(draft.get("form"), list) and draft["form"]:
        return {**base, "outcome": "ask", "question": form_text(draft.get("question") or FORM_LEAD, draft["form"]),
                "form": draft["form"]}
    if finish == "ask_person":
        return {**base, "outcome": "ask", "question": asked_text(draft.get("question") or "",
                                                                 list(draft.get("options") or []))}
    if finish == "cannot" and draft.get("fallback"):
        # The planner answered in words, not with a finish (F958: a channel's agent, which no op re-points). Code's
        # words, never the model's, which said "Done, it's moved" of a change nobody made.
        return {**base, "outcome": "said", "say": FALLBACK_CANNOT}
    if finish == "cannot":
        return {**base, "outcome": "said", "say": cannot_said(draft)}
    if finish != "submit_change":
        return {**base, "outcome": "said", "say": "I could not work out what to change. Say it again in other words."}

    ops = draft.get("ops") or []
    notes: list[str] = []
    if draft.get("undo_of"):
        from core.native_builder.change_undo import inverse_ops

        ops, refusal, notes = await inverse_ops(str(draft["undo_of"]))
        if refusal:
            return {**base, "outcome": "said", "say": refusal}
    ops = C.as_planned(ops)

    handoffs = [o for o in ops if C.OPS.get(o.get("op", ""), C.OPS["create"]).kind == "handoff"]
    goes_on_its_own = (len(ops) == 1 and ops[0].get("op") == "create") or (handoffs and len(handoffs) == len(ops)) or any(
        o.get("op") == "create" and (o.get("target") or {}).get("kind") in C.BUILDS for o in ops)
    if goes_on_its_own:
        # A new thing, a build or a hand-over leaves before the check below, which refuses a key op by op: one that
        # carries a key is refused here, with nothing of it kept (F1309: prod-02's made tool took a pasted token).
        for o in ops:
            if login := _login_in(dict(o.get("fields") or {})):
                return {**base, "outcome": "said", "say": login}
            if await _carries_a_key(dict(o.get("fields") or {}), o.get("says") or ""):
                return {**base, "outcome": "said", "say": KEY_REFUSAL}
    if len(handoffs) > 1 and len(handoffs) == len(ops):
        # Each goes on as its own route for the rest of the run, so only one can be taken: say them, rather than
        # take the first and drop the rest (the branch review's finding 14).
        asked = "; ".join(f"{C.OPS[o['op']].verb.lower()} {_quoted((o.get('target') or {}).get('name') or '')}"
                          for o in handoffs)
        return {**base, "outcome": "said", "ops": handoffs,
                "say": f"Those are {len(handoffs)} separate requests ({asked}). Ask me for them one at a time, and "
                       "I'll take each in turn."}
    builds = [o for o in ops if o.get("op") == "create" and (o.get("target") or {}).get("kind") in C.BUILDS]
    if builds and len(ops) > 1:
        # The build takes the rest of the run, so the others would be dropped: say them as separate requests.
        return {**base, "outcome": "said", "ops": ops,
                "say": f"Making the workflow {_quoted((builds[0].get('target') or {}).get('name') or '')} is a build "
                       "of its own. Ask me for it on its own, then for the rest, and I'll take each in turn."}
    if builds:
        return _to_a_new_workflow(base, ops)
    if len(ops) == 1 and ops[0].get("op") == "create":
        return await _a_new_thing(base, ops[0])
    if handoffs and len(handoffs) == len(ops):
        return await _handed_over(base, handoffs, events)

    caller = _caller()
    areas.ensure_loaded()
    _, run = _run()
    state = run.shared_state if run is not None else {}
    _heard(state)
    words = _persons_words(state)
    chosen = await _chosen(state)
    checked: list[dict] = []
    picks: list[dict] = []
    for n, raw in enumerate(ops):
        op = C.OPS[raw["op"]]
        target = dict(raw.get("target") or {})
        fields = dict(raw.get("fields") or {})
        item = {"op": op.name, "kind": op.kind, "target": target, "fields": fields, "says": raw.get("says") or ""}
        checked.append(item)
        if login := _login_in(fields):
            item.update(fields={}, says="", status="refused", say=login)
            continue
        if await _carries_a_key(fields, item["says"]):
            # Nothing keeps it: not the review, its event, the card or this answer (the branch review's finding 7);
            # its says too, and a value the workspace holds (F1309).
            item.update(fields={}, says="", status="refused", say=KEY_REFUSAL)
            continue
        if op.kind == "handoff":
            item.update(status="refused", say=f"{op.verb} {target.get('name') or ''} is its own request: ask me "
                                              "for it on its own once this is done.".replace("  ", " "))
            continue
        if op.kind == "create":
            if op.name == "create_schedule" and not await _runs_found(chosen, n, item, picks):
                continue
            # Whatever is made needs a name nothing has: an undo's re-create checks nothing took it since (F797).
            name = _text(target.get("name")) or _text(fields.get("name"))
            if any(i["name"].lower() == name.lower() for i in await candidates(op.target) or []):
                article = "An" if op.target[0] in "aeiou" else "A"
                item.update(status="refused", say=f"{article} {op.noun} is already called {_quoted(name)}: "
                                                  + ("say another name." if not op.internal else
                                                     "so I can't bring the old one back under its name."))
                continue
            target["name"] = name
        elif op.name in C.MAKES_TARGET:
            # What it makes is named, not found: its card's own check says when the name is taken (F1200).
            target["name"] = _text(target.get("name")) or _text(fields.get("name"))
        else:
            if _answers(chosen, n, "target", op.target, target):
                target["id"] = chosen["id"]
            items = await candidates(op.target)
            if items is not None:
                found = resolve(items, target)
                if not found:
                    said = None
                    if op.name.startswith("eval_"):
                        # Evals score a workflow's runs: an agent named is said, with the workflows using it (F1255).
                        from core.builder_actions import agent_eval_words

                        said = await agent_eval_words(target.get("name") or target.get("id") or "")
                    if op.name == "update_python_tool":
                        said = (f"No Python tool is called {_quoted(target.get('name') or target.get('id'))}: a new "
                                "one is made with create.")
                    item.update(status="refused", say=said or await _not_found(op.target, target))
                    continue
                if len(found) > 1:
                    item["status"] = "pick"
                    picks.append({"op": n, "field": "target", "kind": op.target,
                                  "wanted": target.get("name") or target.get("id"), "options": found[:8]})
                    continue
                target.update(id=found[0]["id"], name=found[0]["name"])
            else:
                target.setdefault("name", target.get("id") or "")
            if op.name == "retarget_schedule" and not await _runs_found(chosen, n, item, picks):
                continue
        item["status"] = "resolved"

    if picks:
        question = _pick_question(picks)
        out = {**base, "outcome": "ask", "ops": checked, "picks": picks, "question": question}
        # The pick list waits on the person's card, where a later turn reads "the second one" from (P4's read_turn).
        await _card(out, stage=WAITING, title=question.split("\n", 1)[0])
        return out

    # One chat writes a workflow at a time (TABS2, F1063): an op on a workflow another Synapse AI chat holds is left
    # out with why, and the rest of the change is still reviewed.
    if any(i.get("status") == "resolved" and one_writer.guarded(i["op"]) for i in checked):
        holds = await one_writer.holds_now()
        for item in checked:
            if item.get("status") == "resolved" and one_writer.guarded(item["op"]) and (
                    held := one_writer.asked(holds, item["target"].get("id") or "", events)):
                item.update(status="refused", say=held)

    # Each target is read once, and an op is checked against what the ops before it leave: two on one field (an add
    # and a remove of tools, two policy rules) each build on the last, rather than the second undoing the first
    # (the branch review's finding 3).
    leaves: dict[tuple[str, str], dict | None] = {}
    #: The HTTP tools this change makes, by name: an agent may be given one (F1322).
    made: set[str] = set()
    for item in checked:
        if item.get("status") != "resolved":
            continue
        op, target, fields = C.OPS[item["op"]], item["target"], item["fields"]
        if op.is_act:
            try:
                card = await _offer(op.name, C.act_arguments(op, target["id"] if target.get("id") else
                                                             target.get("name", ""), fields))
            except _FeatureOff as off:
                # What is off and where, with its switch as a card for one who may press it (F1203).
                said, switch = await feature_switch(off)
                if switch is not None:
                    events.append(switch)
                item.update(status="noop", say=said)
                continue
            except _NotOffered as why:
                item.update(status="noop", say=str(why))
                continue
            events.append(card)
            pressed = _pressed(card)
            item.update(status="card", say=f"A card is up: {card['title']}. Press {pressed} to do it.")
            continue
        tool = registry.get(op.tool)
        if tool is None or not caller.role.can_act_as(tool.min_role):
            need = tool.min_role.label if tool is not None else Role.BUILDER.label
            item.update(status="refused", say=f"{op.verb} the {op.noun} {_quoted(target['name'])} needs the {need} "
                                              f"role; you are a {caller.role.label} in this workspace, so I can't.")
            continue
        here = (op.target, target.get("id") or "")
        if op.kind == "create":
            live = None
        elif here in leaves:
            live = leaves[here]
        else:
            live = await read(op.target, target["id"])
        if op.kind != "create" and live is None:
            item.update(status="refused", say=f"The {op.noun} {_quoted(target['name'])} is not there any more.")
            continue
        if op.name in ("rename_workflow", "rename_schedule", "rename_agent"):
            new_name = _text(fields.get("name"))
            if not draft.get("undo_of") and (not _plain_words(new_name)
                                             or f" {_plain_words(new_name)} " not in f" {words} "):
                # A new name is the person's words or it is not proposed (F960: "move it to 3am" came back renamed
                # "Every night at 3"). An undo's rename back is code's, from the card, so it is not asked for again.
                # Asked for without its name ("update its name to match"): asked, not dropped (SAI2's review, 2).
                item.update(status="ask_name" if _RENAME_WORDS & set(words.split()) else "left_out",
                            say=f"I left out renaming the {op.noun} {_quoted(target['name'])} to "
                                f"{_quoted(new_name)}: tell me the new name if you want it.")
                continue
            if any(i["id"] != target["id"] and i["name"].lower() == new_name.lower()
                   for i in await candidates(op.target) or []):
                article = "An" if op.target[0] in "aeiou" else "A"
                item.update(status="refused", say=f"{article} {op.noun} is already called {_quoted(new_name)}: "
                                                  "say another name.")
                continue
        if op.name == "set_agent_model":
            from core.native_builder import models_menu as MM

            if not _text(fields.get("model")):
                # What the agent needs, or the person's wish, made a model id by code (F966): the planner never
                # writes one. Its reason goes on the review beside the model it replaces.
                needs, wish, menu = fields.get("needs") or "simple", _text(fields.get("wish")), await MM.menu()
                has_tools = bool(live.get("tools"))
                got = (MM.resolve_wish(wish, needs, has_tools=has_tools, menu=menu) if wish
                       else MM.pick(needs, has_tools=has_tools, menu=menu, default=MM.workspace_model()))
                if got is None:
                    item.update(status="refused", say="I can't read which models this workspace can use right now: "
                                                      "name the model you want.")
                    continue
                if isinstance(got, MM.NoModel):
                    item.update(status="refused", say=got.issue)
                    continue
                if isinstance(got, MM.Ask):
                    item.update(status="ask_model", question=got.question)
                    continue
                fields["model"] = got.id
                item["why"] = f"was {live.get('model') or 'the workspace default'}: {got.reason}"
            # One check at every door that sets an agent's model (F1250): one whose own id is what was said
            # ("nova-pro" for `synapse.nova-pro`, F959) is that choice, and the review shows its full id, so the exact
            # model is what the person approves.
            refused, model = await MM.model_refusal(_text(fields.get("model")), has_tools=bool(live.get("tools")),
                                                    default_ok=False)
            if refused:
                item.update(status="refused", say=refused)
                continue
            fields["model"] = model
        if op.name == "settings_set":
            # The product tool's own checks, before the review rather than after the yes: a model here is one the
            # workspace can run (F1250), the host's list narrows (cloud's WRITABLE).
            from core.product_tools import settings

            if found := await settings.issues(fields):
                item.update(status="refused", say=" ".join(found))
                continue
        if op.name == "set_agent_skills":
            refusal = await _skill_names(fields)
            if refusal:
                item.update(status="refused", say=refusal)
                continue
        if op.name in C.AGENT_SECTIONS and (said := await _agent_refs(op, fields, live)):
            item.update(status="refused", say=said)
            continue
        if op.target == "memory" and op.kind != "delete":
            from core.agent_memory import MAX_MEMORY_CHARS

            if len(_text(fields.get("content"))) > MAX_MEMORY_CHARS:
                item.update(status="refused", say=f"A memory item holds at most {MAX_MEMORY_CHARS} characters: "
                                                  "say it shorter, or split it into two.")
                continue
        if (op.name in ("set_schedule", "create_schedule") and fields.get("cron_expression")
                and not fields.get("timezone") and not (live or {}).get("timezone")):
            # A time the person says is on the schedule's own clock, or, when it has none, on theirs (F948).
            if tz := ((_run()[1].shared_state if _run()[1] else {}) or {}).get("person_timezone"):
                fields["timezone"] = tz
        if op.name == "knowledge_edit_document" and (said := _edit_refusal(target, fields, live)):
            item.update(status="refused", say=said)
            continue
        if op.name == "create_tool" and (said := await _new_tool_refusal(target["name"], fields)):
            item.update(status="refused", say=said)
            continue
        if op.name == "update_python_tool":
            if said := await _code_change_refusal(target["name"], fields, live):
                item.update(status="refused", say=said)
                continue
            if E.is_tested(fields) is None and fields.get("test_args"):
                fields, _ = await _code_tested(target, fields, {}, send_back=False)
                item["fields"] = fields
            if failed := (E.is_tested(fields) or {}).get("failed"):
                item.update(status="refused", say=(
                    f"The fix I wrote for {_quoted(target['name'])} fails its test with "
                    f"{json.dumps(failed['args'], ensure_ascii=False)}: {failed['error']}. Nothing was changed."))
                continue
        if op.name == "update_tool" and (said := await _tool_change_refusal(target["name"], fields, live)):
            item.update(status="refused", say=said)
            continue
        if op.name == "set_agent_tools" and (said := await _tool_names(fields, made, server_module)):
            item.update(status="refused", say=said)
            continue
        want = C.expected(op, fields, live)
        if (op.name in C.AGENT_SECTIONS or op.name == "set_agent_fields") and \
                (said := await _agent_problem(op, live, want)):
            item.update(status="refused", say=said)
            continue
        if op.kind == "delete":
            used, refusal = await _used_by(op, target["id"])
            if refusal:
                item.update(status="refused", say=refusal)
                continue
            item["used_by"] = used
            if op.target == "tool" and live.get("group"):
                live = {**live, "group_name": await _group_name(live["group"])}
        elif op.kind != "create" and not C.differs(live, want):
            item.update(status="noop", say=f"The {op.noun} {_quoted(target['name'])} is already that way: "
                                           "nothing to change.")
            continue
        tool_name, call_args = C.call(op, target.get("id", ""), fields, live, name=target.get("name", ""))
        if op.name == "settings_set":
            # One shape wherever a yes is asked (REACH2's rows): the review's lines and the card's rows say the same.
            item["rows"] = _settings_rows(live, want)
            line = _settings_words(item["rows"], item["says"] if draft.get("undo_of") else "")
        else:
            line = _words(op, target, fields, item["says"] if draft.get("undo_of") else "", want, live)
        if op.name == "knowledge_settings":
            line = await _settings_line(target, live, want)
        if op.name == "knowledge_edit_document":
            line = _edit_line(target, live["text"], want["text"], live.get("knowledge_base") or "")
        if op.name in C.AGENT_SECTIONS or op.name == "set_agent_fields":
            line = await _agent_lines(op, target, live, want)
        if op.target == "memory":
            line = _memory_line(op, target, live, want)
        if op.name == "create_tool":
            made.add(target["name"])
        if op.name == "update_tool" and (used := (await _used_by(C.OPS["delete_tool"], target["id"]))[0]):
            line = f"{line}; used by {', '.join(used)}"
        if op.name == "update_python_tool" and (E.is_tested(fields) or {}).get("button"):
            # Tested on the person's press, with the internet, after they read it (Naveen 11-04): the yes waits on it.
            events.append(await _test_this_fix(_run()[0], checked.index(item), target["name"]))
        item.update(status="ready", call={"tool": tool_name, "args": call_args}, expected=want,
                    before=_keep(op, live, want), line=f"{line} ({item['why']})" if item.get("why") else line)
        if op.kind != "create":
            leaves[here] = None if want is None else {**live, **want}

    ready = [i for i in checked if i["status"] == "ready"]
    if len(ready) > 1:
        # Opening a base to visitors is a change of its own, never on one card with another (8a, 10-31).
        for item in ready:
            if item["op"] == "knowledge_settings" and (item.get("expected") or {}).get("visitor_access") is True:
                item.update(status="refused", say=f"Opening the knowledge base {_quoted(item['target']['name'])} to "
                                                  "visitors is a change of its own: ask for it by itself.")
        # Letting a browser agent type and send is a change of its own, never on one card with another (Naveen 10-31).
        for item in ready:
            if ((item.get("expected") or {}).get("browser") or {}).get("mode") == "co_drive" and \
                    ((item.get("before") or {}).get("browser") or {}).get("mode") != "co_drive":
                item.update(status="refused", say=f"Letting the agent {_quoted(item['target']['name'])} type and send "
                                                  "is a change of its own: ask for it by itself.")
        ready = [i for i in checked if i["status"] == "ready"]
    asked = next((i for i in checked if i["status"] == "ask_model"), None)
    if asked is not None:
        # Which model a wish means is the person's to say (F966): never a guess.
        return {**base, "outcome": "ask", "ops": checked, "question": asked["question"]}
    unnamed = [i for i in checked if i["status"] == "ask_name"]
    if unnamed or (not ready and checked and all(i["status"] == "left_out" for i in checked)):
        # A rename they asked for without its name, or only a rename and no new name in their words: asked, rather
        # than said and dropped (Naveen, 10-23).
        first = (unnamed or checked)[0]
        return {**base, "outcome": "ask", "ops": checked,
                "question": f"What should the {C.OPS[first['op']].target} {_quoted(first['target']['name'])} be "
                            "called?"}
    # What can't be undone is said once: on the review when there is one, else as the answer.
    said = " ".join([*([] if ready else notes), *([_said(checked)] if len(ready) < len(checked) or not ready else [])])
    if not ready:
        return {**base, "outcome": "said", "ops": checked, "say": said}
    calls = [[i["call"]["tool"], i["call"]["args"]] for i in ready]
    review = _review(checked) + "".join(f"\n- Not undone: {note}" for note in notes)
    out = {**base, "outcome": "review", "ops": checked, "calls": calls, "review": review, "say": said,
           **({"undo_of": draft["undo_of"]} if draft.get("undo_of") else {})}
    out["digest"] = digest(reviewed_of(out))
    await _card(out, stage=WAITING)
    return out


async def _chosen(state: dict) -> dict | None:
    """The option the person chose from this conversation's waiting pick list, answered in code by the front door
    (SAI2-P4's read_turn: state.turn), re-checked here against that card's own options; None for anything else."""
    turn = state.get("turn")
    if not isinstance(turn, dict) or turn.get("then") != "change" or not isinstance(turn.get("picked"), dict):
        return None
    picked, key = turn["picked"], turn.get("previous_run_id")
    if not key:
        return None
    from core.boards import assistant

    try:
        card = await assistant.card(str(key))
    except Exception:  # noqa: BLE001 — no card to check it against: not taken
        return None
    person = _caller().user_id
    if card is None or (person and card.owner_id != person):
        return None
    for pick in (((card.data or {}).get("change") or {}).get("picks")) or []:
        if (pick.get("op"), pick.get("field")) == (picked.get("op"), picked.get("field")) and any(
                option.get("id") == picked.get("id") for option in pick.get("options") or []):
            return {**picked, "wanted": pick.get("wanted")}
    return None


def _answers(chosen: dict | None, n: int, field: str, kind: str, target: dict) -> bool:
    """Whether the person's chosen option is this op's target: the same field and kind, no id of its own yet, and the
    name the list asked about (by its place only when the planner gave no name, so a reordered draft never takes it)."""
    if chosen is None or chosen.get("field") != field or chosen.get("kind") != kind or target.get("id"):
        return False
    name = _text(target.get("name"))
    return name.lower() == str(chosen.get("wanted") or "").lower() if name else n == chosen.get("op")


def reviewed_of(checked: dict) -> dict:
    """The part of the check a person approves: the ops as shown and the exact calls (what F778's digest covers)."""
    return {"ops": checked.get("ops") or [], "calls": checked.get("calls") or []}


def _pick_question(picks: list[dict]) -> str:
    parts = []
    for pick in picks:
        options = "\n".join(f"{n}. {_quoted(o['name'])} ({o['id']})" + (f" — {o['detail']}" if o.get("detail") else "")
                            for n, o in enumerate(pick["options"], 1))
        parts.append(f"More than one {pick['kind']} matches {_quoted(pick['wanted'])}. Which one do you mean?\n"
                     f"{options}")
    return "\n\n".join(parts)


#: What one new thing is called on the review, and the registry tool that makes it (the maker step's own four).
_NEW = {"agent": ("agent", "create_agent"), "skill": ("skill", "create_skill"), "tool": ("HTTP tool", "create_http_tool"),
        "openapi": ("OpenAPI import", "import_openapi"), "python_tool": ("Python tool", "create_python_tool")}


async def _a_new_thing(base: dict, raw: dict) -> dict:
    """One new agent, skill, HTTP tool, Python tool (F1198) or OpenAPI import (Naveen, 10-23: today's create path made none of them, 0/6 in
    the hand-over probe): shown on the review in code's words and, after the yes, made by the maker step, whose four
    tools hold their own checks (F489, F769's host rule, TAG1, the Connect card). P3's build route replaces it."""
    from core.product_tools import areas, registry

    target, what = dict(raw.get("target") or {}), _text((raw.get("fields") or {}).get("what"))
    kind, name = target.get("kind"), _text(target.get("name"))
    said, tool_name = _NEW[kind]
    item = {"op": "create", "kind": "create", "target": {"kind": kind, "name": name}, "fields": {"what": what},
            "says": raw.get("says") or ""}
    areas.ensure_loaded()
    tool, caller = registry.get(tool_name), _caller()
    if tool is None or not caller.role.can_act_as(tool.min_role):
        need = tool.min_role.label if tool is not None else Role.BUILDER.label
        return {**base, "outcome": "said", "ops": [item],
                "say": f"Making a new {said} needs the {need} role; you are a {caller.role.label} in this workspace, "
                       "so I can't."}
    from core import skills

    same = (lambda a, b: skills.normalize_name(a) == skills.normalize_name(b)) if kind == "skill" else (
        lambda a, b: a.lower() == b.lower())
    # A Python tool's name is a tool's name: one of either kind holds it.
    if kind in ("agent", "skill", "tool", "python_tool") and any(
            same(i["name"], name) for i in await candidates("tool" if kind == "python_tool" else kind) or []):
        article = "An" if said[0] in "aeiou" else "A"
        return {**base, "outcome": "said", "ops": [item],
                "say": f"{article} {said} is already called {_quoted(name)}: say another name."}
    item.update(status="ready", line=f"Make the {said} {_quoted(name)}" + (f" — {what}" if what else ""))
    out = {**base, "outcome": "create", "ops": [item], "review": f"- {_with_says(item['line'], item['says'])}"}
    await _card(out, stage=WAITING)
    return out


#: A recording's request said where none is (F1231): recording is a person's, in the app.
NO_RECORDING = ("No recording is called {name}. A person records one in the app (Recordings › Record a task); then "
                "ask me again.")


def _to_a_new_workflow(base: dict, ops: list[dict], **facts: Any) -> dict:
    """The run goes on as a new build: its plan room makes the workflow from the person's message, one whose Browser
    step replays the recording (F1231) or the one a change asked to create (F1242); nothing open or last built
    is it."""
    _routed("create", current_orchestration_id="", current_orchestration_name="", current_orchestration_source="")
    return {**base, "outcome": "steps", "ops": ops, **facts}


def _recording_or_workflow(base: dict) -> dict | None:
    """A name that is both a recording and a workflow (`classify.BOTH_KEY`, set by the router): asked once, through the
    request's seam (F1226), unless answered; the recording answered goes to a new build, the workflow goes on as
    this change. None when there is nothing to ask, or the workflow was meant."""
    from core.native_builder.classify import BOTH_KEY

    _, run = _run()
    state = run.shared_state if run is not None else {}
    name = _text(state.get(BOTH_KEY))
    if not name:
        return None
    question = f"{_quoted(name)} is both a recording and a workflow. Which do you mean?"
    options = [f"The recording {_quoted(name)}", f"The workflow {_quoted(name)}"]
    subject = f"recording or workflow {name}"
    earlier = asked_before(state, "which", subject, question)
    if earlier is None:
        if questions_asked(state, run) >= QUESTIONS:
            state[BOTH_KEY] = ""
            return None  # asked enough: the change goes on as the planner wrote it
        note_question(state, "which", subject, question)
        return {**base, "outcome": "ask", "question": asked_text(question, options)}
    state[BOTH_KEY] = ""
    said = _plain_words(earlier.get("answer") or "").split()
    if "recording" in said and "workflow" not in said or said == ["1"]:
        return _to_a_new_workflow(base, [], recording={"name": name})
    return None


async def _handed_over(base: dict, handoffs: list[dict], events: list[dict]) -> dict:
    """`edit_steps` goes to the build route's update path with the workflow on it (Naveen's split, 10-22), and
    `use_recording` to a new build (F1231). Same turn: the person is not asked again. (One new thing is
    `_a_new_thing`'s: the review, then the maker.)"""
    first = handoffs[0]
    if first["op"] == "use_recording":
        found = resolve(await candidates("recording") or [], first.get("target") or {})
        if len(found) > 1:
            pick = {"op": 0, "field": "target", "kind": "recording", "wanted": first["target"].get("name"),
                    "options": found[:8]}
            return {**base, "outcome": "ask", "ops": handoffs, "picks": [pick], "question": _pick_question([pick])}
        if not found:
            return {**base, "outcome": "said", "ops": handoffs, "say": NO_RECORDING.format(
                name=_quoted(first["target"].get("name") or first["target"].get("id") or ""))}
        return _to_a_new_workflow(base, handoffs, recording={"id": found[0]["id"], "name": found[0]["name"]})
    if first["op"] == "edit_steps":
        found = resolve(await candidates("workflow") or [], first.get("target") or {})
        if len(found) > 1:
            pick = {"op": 0, "field": "target", "kind": "workflow", "wanted": first["target"].get("name"),
                    "options": found[:8]}
            return {**base, "outcome": "ask", "ops": handoffs, "picks": [pick], "question": _pick_question([pick])}
        if not found:
            return {**base, "outcome": "said",
                    "say": f"No workflow is called {_quoted(first['target'].get('name') or first['target'].get('id'))}."}
        if held := one_writer.asked(await one_writer.holds_now(), found[0]["id"], events):
            return {**base, "outcome": "said", "ops": handoffs, "say": held}  # one writer at a time (F1063)
        _routed("update", current_orchestration_id=found[0]["id"], current_orchestration_name=found[0]["name"])
        return {**base, "outcome": "steps", "ops": handoffs, "workflow": {"id": found[0]["id"],
                                                                          "name": found[0]["name"]}}
    return {**base, "outcome": "said", "ops": handoffs, "say": "I could not work out what to change. Say it again in "
                                                               "other words."}


def _routed(intent: str, **facts: str) -> None:
    """The run goes on as the route it is handed to: `intent` is what the door (`step_may_change`, which the hand-over
    passes through again: a builder's route) and the build route's update path (SAI2-P3: its pack reads the open
    workflow) read."""
    _, run = _run()
    if run is not None:
        run.shared_state.update(intent=intent, **facts)


async def _card(checked: dict, *, stage: str, data: dict | None = None, title: str = "") -> None:
    """Keep this change on Synapse AI's board, as its person's card (F776): a failure to write it never stops the
    change itself."""
    from core.boards import assistant

    run_id, _ = _run()
    if not run_id:
        return
    caller = _caller()
    title = title or "; ".join(i.get("line") or i["op"] for i in checked.get("ops") or [] if i.get("status") == "ready")
    try:
        await assistant.put(run_id, title=(title or "A change")[:200], stage=stage, owner_id=caller.user_id,
                            body=checked.get("review") or "", data=data or {"change": checked}, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 — the board is the record, not the change
        _log.warning("change card for run %s not written: %s", run_id, exc)


for _schema, _handler in ((SUBMIT, submit_change), (ASK, ask_person), (CANNOT, cannot), (SAY, say)):
    register_builder_tool(_schema, _handler)

CHECK = {"type": "function", "function": {
    "name": "check_change",
    "description": "Synapse AI's change check (code): targets, roles, no-ops, cards, the review.",
    "parameters": {"type": "object", "properties": {"draft": {"type": "object"}}, "required": ["draft"]},
}}
register_builder_tool(CHECK, check_change)



# ── what a turn ends by saying (F1093) ────────────────────────────────────────────────────────────────────────────

async def said_without_secret_asks(run: Any) -> None:
    """A builder turn's reply never asks for a key, a token or a password: a sentence of it that does is taken out,
    and code's line pointing to the Connect card and the Vault is said in its place (the manager's net, 10-27). The
    replies of the talk and answer routes are a model's own words, with no finish tool to refuse them; here, where
    every run ends (fleet included), before the panel reads `summary` and the conversation keeps it. A file the reply
    says is in the Vault is held to the Vault there too (F1211, `vault_claims`)."""
    from core.native_builder.identity import is_builder_run
    from core.orchestration.state import SharedState
    from core.secret_scrub import without_secret_asks

    if getattr(run, "status", "") != "completed":
        return
    said = run.shared_state.get("summary")
    if not isinstance(said, str):
        return
    from core.native_builder import vault_claims

    cleaned, was = without_secret_asks(said)
    # Which run it is, asked last: on a host it is a read, and every run that ends comes here.
    if not (was or vault_claims.claimed(cleaned)) or not await is_builder_run(run):
        return
    # A file it says is in the Vault is read there first (F1211): one that is not is said so, by code.
    cleaned, wrong = await vault_claims.checked(cleaned)
    if was or wrong:
        run.shared_state["summary"] = cleaned
        await SharedState(run).checkpoint()


from core.orchestration.cancel import on_end  # noqa: E402 — registered with the module, as its tools are

on_end(said_without_secret_asks)
