"""The assistant's action card: it shows what it would do, and the person's click does it (ASSIST1, D291, F683).

Naveen: the assistant does what a person can do in the app, through the gates the app already has, and never by
itself. `prepare_action` is a builder tool of the card kind (`builder_access`): it changes nothing.

1. **What was asked is resolved to exact ids in code**: a workflow by its name (two that match are offered back
   as a choice, never guessed between), a run that still runs or still waits, a schedule, a waiting tool call.
2. **The person's role is checked now**, so nobody is shown a button they cannot press.
3. **Each button carries a confirmation token** (`product_tools.confirm`) bound to that person, that product tool
   and those exact arguments, minted through `registry.invoke` with the assistant's `every_act` policy: the act's
   own handler does not run.
4. **The click** posts the button to the host's act route (`routes/assistant.py`; cloud's `/assistant/act`), which
   builds the person from the request and calls the same product tool with the token. The role is checked again
   there, a token works once, and it does nothing for other arguments.

A run's question is not a token: the card holds the question, and its answer goes to the run's own answer door, as
on the run page, where the approver role is checked.
"""
from __future__ import annotations

import re
import uuid
from typing import Any

from core.builder_tools import register_builder_tool
from core.product_tools.caller import Caller
from core.product_tools.errors import ToolRefusal
from core.product_tools.roles import Role

ACTIONS: dict[str, str] = {
    "run_workflow": "start a run of a workflow (`workflow`: its name; `message`: what it should work on)",
    "run_schedule_now": "run a schedule once now (`schedule`: its name)",
    "schedule_on": "turn a schedule on (`schedule`)",
    "schedule_off": "turn a schedule off (`schedule`)",
    "answer_run": "answer the question a waiting run asks, or approve it (`run_id`, from topic `waiting`)",
    "answer_tool_call": "allow or refuse a tool call an agent stopped on (`approval_id`, from topic `waiting`)",
    "cancel_run": "stop a run that is still going (`run_id`)",
    "resume_run": "run a failed or stopped run on from the step it stopped at, once what failed is fixed (`run_id`)",
    "knowledge_create": "make a knowledge base (`name`; `text`: what it holds; `folder`: a Vault folder, or leave it "
                        "out for knowledge/<name>)",
    "knowledge_add_file": "add a file from the Vault to a knowledge base (`knowledge_base`, `path`: its Vault path)",
    "knowledge_add_pages": "add public web pages to a knowledge base (`knowledge_base`, `urls`, `kind`: page, sitemap "
                           "or site)",
    "knowledge_add_note": "add a note to a knowledge base (`knowledge_base`, `title`, `text`)",
    "knowledge_retry_document": "index a failed document again (`doc_id`)",
    "knowledge_rename": "rename a knowledge base or say what it holds (`knowledge_base`; `name`, `text`)",
    "knowledge_rebuild": "start a knowledge base's failed or paused rebuild again (`knowledge_base`)",
    "knowledge_delete_base": "delete a knowledge base; its Vault folder stays (`knowledge_base`)",
    "knowledge_delete_document": "delete a document from its knowledge base (`doc_id`)",
    "board_add_card": "add a card to a board (`board`, `title`, `text`)",
    "board_update_card": "close a card as done or failed, or reopen it (`card_id`, `status`, `result`)",
    "board_comment": "comment on a card (`card_id`, `text`)",
    "board_create": "make a shared board (`name`, `text`: what it is for)",
    "board_delete": "delete a shared board (`board`)",
    "feature_on": "turn on Boards or Knowledge for this workspace (`feature`: boards or knowledge), when one is off",
    "eval_rubric": "score a workflow's runs by a rubric (`workflow`; `name`, `criteria`: [{name, weight, guidance}], "
                   "`floor`; or `rubric`: one that is here, to attach)",
    "eval_cases": "add test cases to a workflow's golden set (`workflow`; `cases`: [{name, input, notes}] as the person "
                  "gave them, or `count` and `steer` to have cases proposed; `golden_set` to add to one)",
    "eval_run": "run a workflow's golden set, each case a run scored by its rubric (`workflow`, `golden_set`)",
    "eval_cancel": "stop a golden set's run after the case in flight (`workflow`, `golden_set`)",
    # Recordings (F1270): a kept fix (topic `waiting` lists them), a new name, or the recording gone.
    "recording_approve_edit": "use a kept fix in its recording (`recording`, `edit_id`: from topic `waiting`)",
    "recording_discard_edit": "drop a kept fix, leaving the recording as it is (`recording`, `edit_id`)",
    "recording_rename": "rename a recording (`recording`, `name`)",
    "recording_delete": "delete a recording and its pictures (`recording`)",
    "widget_create": "make a website widget (`name`; `agent`: the agent that answers; `sites`: the sites it may sit on, "
                     "as https://example.com; `visitor_tools`: tools a visitor may use, only those the person named; "
                     "`greeting`, `title`, `accent`, `theme`, `position`, `launcher_label`, `font`, `header_style`, "
                     "`launcher`, `daily_cap`: messages a day)",
    "widget_update": "change a website widget (`widget`; any of `agent`, `sites`, `visitor_tools`, `greeting`, `title`, "
                     "`accent`, `theme`, `position`, `launcher_label`, `font`, `header_style`, `launcher`, `daily_cap`, "
                     "`on`); what it does not name is kept",
    "widget_delete": "delete a website widget (`widget`)",
    "widget_rotate_key": "give a website widget a new key, when its old one is abused: the old snippet stops working "
                         "(`widget`)",
    "widget_logo": "set a website widget's logo from an image in the Vault (`widget`, `path`)",
    "widget_logo_remove": "take a website widget's logo off (`widget`)",
}

SCHEMA = {
    "type": "function",
    "function": {
        "name": "prepare_action",
        "description": (
            "Put up a card the person presses to do something in the workspace. Nothing happens until they press "
            "it, so use this for any action they ask for instead of saying it is done. Actions: "
            + "; ".join(f"`{k}` — {v}" for k, v in ACTIONS.items()) + ". "
            "After it, tell them the card is above and what pressing it does. If it answers with an error, say "
            "that and do not claim anything was done."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(ACTIONS)},
                "workflow": {"type": "string", "description": "run_workflow: the workflow's name."},
                "message": {"type": "string", "description": "run_workflow: what the run should work on."},
                "schedule": {"type": "string", "description": "The schedule's name."},
                "run_id": {"type": "string"},
                "approval_id": {"type": "string", "description": "answer_tool_call: from topic `waiting`."},
                "question_id": {"type": "string", "description": "answer_tool_call: from topic `waiting`."},
                "knowledge_base": {"type": "string", "description": "The knowledge base's name."},
                "urls": {"type": "array", "items": {"type": "string"}},
                "kind": {"type": "string", "enum": ["page", "sitemap", "site"]},
                "doc_id": {"type": "string"},
                "folder": {"type": "string", "description": "knowledge_create: a Vault folder; omit for knowledge/<name>."},
                "path": {"type": "string", "description": "knowledge_add_file: the file's Vault path."},
                "board": {"type": "string", "description": "The board's name."},
                "card_id": {"type": "string", "description": "From topic `boards` with the board's name."},
                "status": {"type": "string", "enum": ["open", "done", "failed"]},
                "result": {"type": "string", "description": "board_update_card: what came of it."},
                "feature": {"type": "string", "enum": ["boards", "knowledge"]},
                "criteria": {"type": "array", "items": {"type": "object"},
                             "description": "eval_rubric: [{name, weight, guidance}]: what a good result is."},
                "floor": {"type": "number", "description": "eval_rubric: a score below it is a regression."},
                "rubric": {"type": "string", "description": "eval_rubric: a rubric here to attach, by name."},
                "cases": {"type": "array", "items": {"type": "object"},
                          "description": "eval_cases: [{name, input, notes}], as the person gave them."},
                "count": {"type": "integer", "description": "eval_cases: how many to propose, 1 to 10."},
                "steer": {"type": "string", "description": "eval_cases: what proposed cases should cover."},
                "golden_set": {"type": "string", "description": "The golden set's name; omit for the workflow's one."},
                "recording": {"type": "string", "description": "The recording's name or id."},
                "edit_id": {"type": "string", "description": "A kept fix's id, from topic `waiting`."},
                "widget": {"type": "string", "description": "The website widget's name."},
                "agent": {"type": "string", "description": "widget_create, widget_update: the agent's name."},
                "sites": {"type": "array", "items": {"type": "string"},
                          "description": "widget_*: every site it may sit on, as https://example.com."},
                "visitor_tools": {"type": "array", "items": {"type": "string"},
                                  "description": "widget_*: the tools a visitor may use; leave it out unless named."},
                "greeting": {"type": "string"},
                "accent": {"type": "string", "description": "A colour, as #4f46e5."},
                "theme": {"type": "string", "enum": ["auto", "light", "dark"]},
                "position": {"type": "string", "enum": ["right", "left"]},
                "launcher_label": {"type": "string"},
                "font": {"type": "string", "enum": ["system", "sans", "serif", "rounded", "mono"]},
                "header_style": {"type": "string", "enum": ["accent", "neutral"]},
                "launcher": {"type": "string", "enum": ["bubble", "logo"]},
                "daily_cap": {"type": "integer", "description": "widget_*: the most messages a day."},
                "on": {"type": "boolean", "description": "widget_update: on or off."},
                "name": {"type": "string"},
                "title": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["action"],
        },
    },
}


#: What turning each feature on does, on its card.
_TURNS_ON = {"boards": "Every workflow's agents get a board of their own, and you can make shared ones.",
             "knowledge": "Knowledge bases become searchable by your agents. Indexing them uses your embedding model."}


class _NotOffered(Exception):
    """Why no card: said to the model, which says it to the person. A "which one?" carries its options with their
    ids, and the field they answer, so the turn keeps them for the next turn's pick (F977)."""

    def __init__(self, why: str, *, field: str = "", options: list[dict] | None = None) -> None:
        super().__init__(why)
        self.field, self.options = field, options or []


class _FeatureOff(_NotOffered):
    """The action needs a feature that is off: what is said names where it is turned on, and its switch is a card of
    its own for one who may press it (`feature_switch`, F1203)."""

    def __init__(self, feature: str) -> None:
        from core.features import off_sentence
        super().__init__(off_sentence(feature))
        self.feature = feature


async def feature_switch(off: _FeatureOff) -> tuple[str, dict | None]:
    """What is said for a feature that is off, and its switch's card, or None for one who may not turn it on."""
    from core import features

    said = features.off_sentence(off.feature)
    try:
        switch = await _feature_card(off.feature)
    except _NotOffered:
        return f"{said} Ask an admin of this workspace to turn {features.label(off.feature)} on.", None
    return f"{said} A card is up to turn it on; ask again for this once it is on.", switch


def _caller() -> Caller:
    from core.builder_access import current_caller

    return current_caller() or Caller(role=Role.OWNER, via="assistant")


#: The id at the end of what a listing printed, `"name" (id)` or `name (id) — detail` (F760).
_LISTED_ID = re.compile(r"\(\s*([^()\s]+)\s*\)")


def listed(items: list[dict], wanted: str) -> dict | None:
    """The item whose id `wanted` carries in brackets, as a listing printed it (F760, QA1c on prod: Nova Pro sent the
    listing's own `"Inbox triage" (orch_…)` back and was told no workflow had that name). A bracket that is no item's
    id is part of a name ("Invoice chaser (EU)"), and is left to the name."""
    for found in reversed(_LISTED_ID.findall(wanted or "")):
        match = [i for i in items if i.get("id") == found]
        if len(match) == 1:
            return match[0]
    return None


def _pick(items: list[dict], wanted: str, what: str, list_hint: str) -> dict:
    needle = (wanted or "").strip().lower()
    if not needle:
        raise _NotOffered(f"Say which {what}. {list_hint}")
    if (by_listing := listed(items, wanted)) is not None:
        return by_listing
    exact = [i for i in items if str(i.get("name") or "").lower() == needle or i.get("id") == wanted]
    if len(exact) == 1:
        return exact[0]
    near = exact or [i for i in items if needle in str(i.get("name") or "").lower()]
    if len(near) == 1:
        return near[0]
    if not near:
        raise _NotOffered(f"No {what} is called {wanted!r}. {list_hint}")
    # Each with its id: two of the same name told apart by name alone sent the name back, and was refused again (F760).
    names = ", ".join(f'"{i.get("name")}" ({i.get("id")})' for i in near[:8])
    raise _NotOffered(f"More than one {what} matches {wanted!r}: {names}. Ask which one they mean, then call again "
                      f"with that one's id as `{what}`.", field=what,
                      options=[{"id": i.get("id"), "name": i.get("name")} for i in near[:8]])


async def _workflow(args: dict) -> dict:
    from core.builder_tools import _is_visible
    from core.routes.orchestrations import load_orchestrations

    flows = [o for o in await load_orchestrations() if _is_visible(o) and o.get("is_active") is not False]
    return _pick(flows, str(args.get("workflow") or ""), "workflow", "Topic `workflows` lists them.")


async def _schedule(args: dict) -> dict:
    from core.store import schedules as store

    return _pick(await store.load(), str(args.get("schedule") or ""), "schedule", "Topic `schedules` lists them.")


async def _run(args: dict) -> tuple[dict, str]:
    from core.orchestration.run_view import read_run, run_orchestration_id
    from core.routes.orchestrations import load_orchestrations

    run_id = str(args.get("run_id") or "").strip()
    if not run_id:
        raise _NotOffered("Say which run: its run_id (topic `runs` or `waiting` lists them).")
    orch_id = await run_orchestration_id(run_id)  # None for the builder's own and for a deleted workflow's
    view = await read_run(run_id) if orch_id else None
    if view is None:
        raise _NotOffered(f"There is no run {run_id!r} in this workspace's workflows.")
    names = {o.get("id"): o.get("name") for o in await load_orchestrations()}
    return view, names.get(orch_id) or orch_id


def _same_words(a: str, b: str) -> bool:
    """Whether two texts say the same, whatever their case, spacing or closing stop."""
    def norm(t: str) -> str:
        return " ".join(t.split()).casefold().rstrip(".!?:;")
    return bool(a) and norm(a) == norm(b)


def _own_title(title: str, text: str) -> str:
    """A note's or a card's title, as written; when it only repeats the text (a model's answer, SAI2's rendering
    row), one in code: the text's first sentence, cut at a word within 60 characters. A short one is kept as written
    (the card then says it once)."""
    if not _same_words(title, text) or len(title) <= 60:
        return title
    first = " ".join(re.split(r"(?<=[.!?])\s|\n", text.strip(), maxsplit=1)[0].split())
    if len(first) <= 60:
        return first
    cut = first[:60].rsplit(" ", 1)[0].rstrip(",;:")
    return f"{cut}…"


def _card(title: str, *, detail: str = "", danger: bool = False, buttons: list[dict] | None = None,
          question: dict | None = None, rows: list[dict] | None = None) -> dict:
    """`rows`: what it changes, field by field, as it stands and as it will be (`native_builder.rows`, F1262)."""
    return {"type": "action_needed", "id": f"act_{uuid.uuid4().hex[:10]}", "title": title, "detail": detail,
            "danger": danger, "buttons": buttons or [], **({"question": question} if question else {}),
            **({"rows": rows} if rows else {})}


async def _button(label: str, tool: str, arguments: dict) -> dict:
    """A button whose token confirms exactly this call, for this person: minted, never done."""
    from core.product_tools import areas, registry

    areas.ensure_loaded()
    try:
        preview = await registry.invoke(tool, _caller(), arguments, confirm_policy="every_act")
    except ToolRefusal as exc:
        raise _NotOffered(str(exc)) from None
    return {"label": label, "tool": tool, "arguments": arguments, "token": preview["confirm_token"],
            "expires_in_seconds": preview["expires_in_seconds"]}


async def _offer(action: str, args: dict) -> dict:
    if action == "run_workflow":
        flow = await _workflow(args)
        message = str(args.get("message") or "").strip()
        call = {"orchestration_id": flow["id"], "message": message, "wait_seconds": 0}
        return _card(f'Run "{flow.get("name")}"', detail=message and f"Working on: {message}",
                     buttons=[await _button("Run it", "run_orchestration", call)])

    if action == "run_schedule_now":
        schedule = await _schedule(args)
        return _card(f'Run "{schedule.get("name")}" now',
                     detail="Once, now. Its next scheduled time does not move.",
                     buttons=[await _button("Run now", "trigger_schedule", {"schedule_id": schedule["id"]})])

    if action in ("schedule_on", "schedule_off"):
        schedule, on = await _schedule(args), action == "schedule_on"
        if bool(schedule.get("enabled")) == on:
            raise _NotOffered(f'"{schedule.get("name")}" is already {"on" if on else "off"}.')
        return _card(f'Turn {"on" if on else "off"} "{schedule.get("name")}"',
                     detail="" if on else "It stops starting runs until it is turned on again.",
                     buttons=[await _button("Turn on" if on else "Turn off", "set_schedule_enabled",
                                            {"schedule_id": schedule["id"], "enabled": on})])

    if action == "cancel_run":
        view, name = await _run(args)
        if view.get("status") not in ("running", "paused", "queued"):
            raise _NotOffered(f'That run of "{name}" has already {view.get("status")}: there is nothing to stop.')
        return _card(f'Stop the run of "{name}"', danger=True,
                     detail="It stops where it is. What it already did stays done.",
                     buttons=[await _button("Stop it", "cancel_run", {"run_id": view["run_id"]})])

    if action == "resume_run":
        # The run page's Replay (F1260): its refusals said before the card, a nested run's, one still going.
        from core.product_tools import runs

        view, name = await _run(args)
        said = await _resolved(runs._preview_resume(_caller(), {"run_id": view["run_id"]}))
        return _card(f'Resume the run of "{name}"', detail=f'From the step "{said["from_step"]}". {said["note"]}',
                     buttons=[await _button("Resume it", "resume_run", {"run_id": view["run_id"]})])

    if action == "answer_run":
        view, name = await _run(args)
        asked = view.get("human_input_required")
        if view.get("status") != "paused" or not asked:
            raise _NotOffered(f'That run of "{name}" is not waiting for an answer (it is {view.get("status")}).')
        if not _caller().role.can_act_as(Role.APPROVER):
            raise _NotOffered(f"Answering a run needs the approver role; you are a {_caller().role.label}.")
        return _card(f'"{name}" is asking', detail=str(asked.get("prompt") or ""),
                     question={"run_id": view["run_id"], **asked})

    if action == "answer_tool_call":
        from core.product_tools import approvals

        waiting = [i for i in (await approvals._list_waiting(_caller(), {"kind": "tool_approval", "limit": 100}))
                   ["items"]]
        wanted = [i for i in waiting
                  if (args.get("approval_id") and i.get("approval_id") == args["approval_id"])
                  or (args.get("question_id") and i.get("question_id") == args["question_id"])]
        if not wanted and not (args.get("approval_id") or args.get("question_id")) and len(waiting) == 1:
            wanted = waiting
        if len(wanted) != 1:
            raise _NotOffered("That tool call is not waiting any more." if args.get("approval_id")
                              else "Say which tool call: its approval_id from topic `waiting`.")
        item = wanted[0]
        where = {k: item[k] for k in ("session_id", "schedule_id", "approval_id", "question_id") if item.get(k)}
        return _card(str(item.get("title") or f'Allow "{item.get("tool_name")}"?'),
                     detail=str(item.get("detail") or ""),
                     buttons=[await _button("Allow", "answer_tool_approval", {**where, "approve": True,
                                                                              "wait_seconds": 0}),
                              await _button("Refuse", "answer_tool_approval", {**where, "approve": False,
                                                                               "wait_seconds": 0})])

    if action == "feature_on":
        return await _feature_card(str(args.get("feature") or "").strip().lower())
    if action.startswith("knowledge_"):
        from core import features
        if not await features.is_on("knowledge"):
            raise _FeatureOff("knowledge")
        return await _knowledge(action, args)
    if action.startswith("eval_"):
        return await _evals(action, args)
    if action.startswith("board_"):
        from core import features
        if not await features.is_on("boards"):
            raise _FeatureOff("boards")
        return await _board(action, args)
    if action.startswith("recording_"):
        return await _recording(action, args)
    if action.startswith("widget_"):
        return await _widget(action, args)

    raise _NotOffered(f"action must be one of {', '.join(ACTIONS)}.")


async def _feature_card(feature: str) -> dict:
    """Its switch, as a card: only for a person who may turn it on (the Settings screen's admin)."""
    from core import features

    if feature not in features.FEATURES:
        raise _NotOffered(f"feature is one of {', '.join(features.FEATURES)}.")
    name = features.label(feature)
    if await features.is_on(feature):
        raise _NotOffered(f"{name} is already on.")
    return _card(f"Turn on {name}", detail=f"{_TURNS_ON[feature]} Its switch is in {features.where(feature)}.",
                 buttons=[await _button(f"Turn on {name}", "turn_on_feature", {"feature": feature})])


async def _resolved(lookup):
    """A product tool's own lookup, its refusal said as why there is no card."""
    try:
        return await lookup
    except ToolRefusal as exc:
        raise _NotOffered(str(exc)) from None


async def _knowledge(action: str, args: dict) -> dict:
    from core.product_tools import knowledge

    if action == "knowledge_create":
        # The tool's own preview checks it (name free, folder allowed and no other base's) before the card (F1200).
        name = str(args.get("name") or "").strip()
        if not name:
            raise _NotOffered("Say what the knowledge base is called.")
        call = {"name": name, "description": str(args.get("text") or "").strip(),
                **({"folder": str(args["folder"]).strip()} if str(args.get("folder") or "").strip() else {})}
        button = await _button("Make it", "create_knowledge_base", call)
        folder = call.get("folder") or knowledge._folder_for(name)
        return _card(f'Make a knowledge base "{name}"',
                     detail=f"On the Vault folder {folder}. Files already there are indexed with your embedding model.",
                     buttons=[button])
    if action == "knowledge_add_file":
        kb = await _resolved(knowledge._base(str(args.get("knowledge_base") or "")))
        path = str(args.get("path") or "").strip().lstrip("/")
        if not path:
            raise _NotOffered("Say which file: its path in the Vault.")
        button = await _button("Add", "add_knowledge_file", {"knowledge_base": kb.id, "path": path})
        return _card(f'Add "{path.rsplit("/", 1)[-1]}" to "{kb.name}"',
                     detail=f"{path}. Indexing it uses your embedding model.", buttons=[button])
    if action in ("knowledge_rename", "knowledge_rebuild", "knowledge_delete_base"):
        # Each tool's own checks come first: a name taken, nothing to change, what uses it (F1218).
        kb = await _resolved(knowledge._base(str(args.get("knowledge_base") or "")))
        if action == "knowledge_rename":
            call = {"knowledge_base": kb.id,
                    **({"name": str(args["name"]).strip()} if str(args.get("name") or "").strip() else {}),
                    **({"description": str(args["text"]).strip()} if str(args.get("text") or "").strip() else {})}
            await _resolved(knowledge._changed(kb, call))
            what = f' to "{call["name"]}"' if call.get("name") else ""
            return _card(f'Change "{kb.name}"{what}', detail=call.get("description", ""),
                         buttons=[await _button("Save", "update_knowledge_base", call)])
        if action == "knowledge_rebuild":
            return _card(f'Rebuild "{kb.name}"', detail="Its index is built again beside the one answering now, "
                                                        "with its embedding model; nothing happens when it is current.",
                         buttons=[await _button("Rebuild", "retry_knowledge_rebuild", {"knowledge_base": kb.id})])
        from core.knowledge import bases

        uses = await bases.used_by(kb.id)
        users = [f'the workflow "{w["name"]}"' for w in uses["workflows"]] + [f'the agent "{a["name"]}"'
                                                                             for a in uses["agents"]]
        detail = f"Its index and documents go; {kb.folder} and its files stay in the Vault. This cannot be undone."
        if users:
            detail += " Used by " + ", ".join(dict.fromkeys(users)) + ": they stop finding anything in it."
        return _card(f'Delete the knowledge base "{kb.name}"', danger=True, detail=detail,
                     buttons=[await _button("Delete it", "delete_knowledge_base", {"knowledge_base": kb.id})])
    if action in ("knowledge_add_pages", "knowledge_add_note"):
        kb = await _resolved(knowledge._base(str(args.get("knowledge_base") or "")))
        if action == "knowledge_add_pages":
            urls = [str(u) for u in (args.get("urls") or []) if str(u).strip()]
            if not urls:
                raise _NotOffered("Say which pages: their URLs.")
            kind = args.get("kind") if args.get("kind") in ("page", "sitemap", "site") else "page"
            title = {"page": f'Add {len(urls)} page{"s" if len(urls) != 1 else ""} to "{kb.name}"',
                     "sitemap": f'Add the pages in {urls[0]}\'s sitemap to "{kb.name}"',
                     "site": f'Crawl {urls[0]} into "{kb.name}"'}[kind]
            return _card(title, detail="\n".join(urls[:10]) + "\nFetching and indexing them uses your embedding model.",
                         buttons=[await _button("Add", "add_knowledge_pages",
                                                {"knowledge_base": kb.id, "urls": urls, "kind": kind})])
        text = str(args.get("text") or "").strip()
        if not text:
            raise _NotOffered("Say what the note says.")
        title = _own_title(str(args.get("title") or "").strip(), text)
        return _card(f'Add a note to "{kb.name}"',
                     detail=(f"{title}: " if title and not _same_words(title, text) else "") + text[:300],
                     buttons=[await _button("Add note", "add_knowledge_note",
                                            {"knowledge_base": kb.id, "title": title or None, "text": text})])
    row = await _resolved(knowledge._document(str(args.get("doc_id") or "")))
    name = getattr(row, "path", None) or getattr(row, "title", None) or row.id
    if action == "knowledge_retry_document":
        return _card(f'Index "{name}" again', buttons=[await _button("Retry", "retry_knowledge_document",
                                                                      {"doc_id": row.id})])
    if action == "knowledge_delete_document":
        return _card(f'Delete "{name}" from its knowledge base', danger=True,
                     detail="Its Vault file goes too. This cannot be undone.",
                     buttons=[await _button("Delete it", "delete_knowledge_document", {"doc_id": row.id})])
    raise _NotOffered(f"action must be one of {', '.join(ACTIONS)}.")


async def _eval_workflow(wanted: str) -> dict:
    """The workflow an eval is about. Evals score a workflow's runs, so an agent named instead is said, with the
    workflows that use it to pick from (Naveen, 10-30); with none, a one-step workflow running it is offered."""
    from core.product_tools import evals as E

    try:
        return await E._workflow(wanted)
    except ToolRefusal as missing:
        said = await agent_eval_words(wanted)
        raise _NotOffered(said or str(missing)) from None


async def agent_eval_words(wanted: str) -> str | None:
    """What to say when an eval names an agent, not a workflow; None when no agent is called that."""
    from core.builder_tools import _is_visible
    from core.routes.agents import load_user_agents
    from core.routes.orchestrations import load_orchestrations

    wanted = str(wanted or "").strip().casefold()
    agent = next((a for a in await load_user_agents() if not a.get("system")
                  and wanted in (str(a.get("id") or "").casefold(), str(a.get("name") or "").casefold())), None)
    if agent is None:
        return None
    using = [o.get("name") for o in await load_orchestrations() if _is_visible(o)
             and any(s.get("agent_id") == agent["id"] for s in o.get("steps") or [])]
    head = f'Evals score a workflow\'s runs, not an agent on its own: "{agent.get("name")}" is an agent.'
    if using:
        return head + " Workflows that use it: " + ", ".join(f'"{n}"' for n in using[:8]) + ". Ask which one."
    return head + (" No workflow uses it yet: offer to make a one-step workflow that runs it (the op create), and "
                   "its eval goes on that one.")


async def _evals(action: str, args: dict) -> dict:
    """Evals' acts (F1255), each a card whose button is the Evals screen's own tool, previewed there."""
    from core.product_tools import evals as E

    flow = await _eval_workflow(str(args.get("workflow") or ""))
    name = flow.get("name") or flow["id"]
    if action == "eval_rubric":
        if str(args.get("rubric") or "").strip() and not args.get("criteria"):
            rubric = await _resolved(E._rubric(str(args["rubric"])))
            call = {"workflow": flow["id"], "rubric": rubric["id"]}
            return _card(f'Score "{name}" with the rubric "{rubric.get("name")}"',
                         detail=E._criteria_words(rubric) + ". Each run that finishes is judged: one more model call "
                                                           "per run.",
                         buttons=[await _button("Attach it", "attach_rubric", call)])
        call = {"name": str(args.get("name") or "").strip() or f"{name} quality",
                "criteria": list(args.get("criteria") or []), "workflow": flow["id"],
                **({"floor": args["floor"]} if args.get("floor") is not None else {})}
        preview = await _resolved(E._preview_save_rubric(_caller(), call))
        replaces = ""
        if flow.get("rubric_id"):
            old = next((r for r in await E.service.list_rubrics() if r.get("id") == flow["rubric_id"]), None)
            replaces = f' It replaces "{(old or {}).get("name") or flow["rubric_id"]}": the score trend starts again.'
        return _card(f'Score "{name}" with the rubric "{call["name"]}"',
                     detail=f'{preview["criteria"]}. Each run that finishes is judged: one more model call per run.'
                            + replaces, buttons=[await _button("Save and attach", "save_rubric", call)])
    if action == "eval_cases":
        cases = [c for c in args.get("cases") or [] if isinstance(c, dict)]
        if not cases:
            # Proposed now, a model call, and shown in full: only the press saves them (Naveen, 10-30).
            got = await _resolved(E._propose_cases(_caller(), {"workflow": flow["id"],
                                                               "count": int(args.get("count") or 5),
                                                               "steer": str(args.get("steer") or "")}))
            cases = [{k: c.get(k) or "" for k in ("name", "input", "notes")} for c in got["cases"]]
        call = {"workflow": flow["id"], "cases": cases,
                **({"golden_set": str(args["golden_set"])} if str(args.get("golden_set") or "").strip() else {})}
        preview = await _resolved(E._preview_save_cases(_caller(), call))
        return _card(f'Add {len(cases)} test case{"s" if len(cases) != 1 else ""} to "{name}"\'s golden set',
                     detail="\n".join(preview["cases"]), buttons=[await _button("Save these", "save_eval_cases",
                                                                               call)])
    call = {"workflow": flow["id"],
            **({"golden_set": str(args["golden_set"])} if str(args.get("golden_set") or "").strip() else {})}
    if action == "eval_run":
        preview = await _resolved(E._preview_run_set(_caller(), call))
        return _card(f'Run the eval of "{name}"', detail=f'{preview["runs"]}: {preview["costs"]}.',
                     buttons=[await _button("Run it", "start_eval_suite", call)])
    suite = await _resolved(E._set(call))
    return _card(f'Stop the eval of "{name}"', danger=True,
                 detail=f'"{suite.get("name")}" stops after the case running now; the runs already done stay.',
                 buttons=[await _button("Stop it", "cancel_eval_suite", call)])


async def _recording(action: str, args: dict) -> dict:
    """A recording's acts (F1270), each a card whose button is the product tool the Recordings screen's own code is
    behind; a plan without Browser is said in its host's words first."""
    from core.browser import recordings
    from core.builder_tools import _browser_refused
    from core.product_tools import browser

    if (refused := await _browser_refused("recording")) is not None:
        raise _NotOffered(refused["error"])
    docs = [{"id": d["id"], "name": d.get("name") or d["id"]} for d in await recordings.all_recordings()]
    doc = _pick(docs, str(args.get("recording") or ""), "recording", "Topic `recordings` lists them.")
    name = doc["name"]
    if action == "recording_rename":
        call = {"recording_id": doc["id"], "name": str(args.get("name") or "").strip()}
        said = await _resolved(browser._preview_rename(_caller(), call))
        return _card(f'Rename the recording "{name}" to "{said["to"]}"',
                     buttons=[await _button("Rename", "rename_recording", call)])
    if action == "recording_delete":
        said = await _resolved(browser._preview_delete(_caller(), {"recording_id": doc["id"]}))
        users = [f'the workflow "{w["name"]}"' for w in said["used_by"]["workflows"]] + [
            f'the agent "{a["name"]}"' for a in said["used_by"]["agents"]]
        detail = said["note"] + (" Used by " + ", ".join(dict.fromkeys(users)) + "." if users else "")
        return _card(f'Delete the recording "{name}"', danger=True, detail=detail,
                     buttons=[await _button("Delete it", "delete_recording", {"recording_id": doc["id"]})])
    call = {"recording_id": doc["id"], "edit_id": str(args.get("edit_id") or "").strip()}
    approve = action == "recording_approve_edit"
    said = await _resolved((browser._preview_approve if approve else browser._preview_discard)(_caller(), call))
    where = f'step {said["from_step"]}' + (" and every step after it" if said["rest"] else "")
    detail = "\n".join([f"Now, from {where}:", *(f"- {line}" for line in said["before"]),
                        "The fix:", *(f"- {line}" for line in said["after"]), said["note"]])
    if approve:
        return _card(f'Use the kept fix in "{name}"', detail=detail,
                     buttons=[await _button("Use the fix", "approve_recording_edit", call)])
    return _card(f'Drop the kept fix in "{name}"', detail=detail,
                 buttons=[await _button("Drop it", "discard_recording_edit", call)])


async def _board(action: str, args: dict) -> dict:
    from core.product_tools import boards
    from core.store import boards as store

    await _resolved(boards._on())
    if action in ("board_add_card", "board_delete"):
        board = await _resolved(boards._board_named(str(args.get("board") or "")))
        if action == "board_add_card":
            title = str(args.get("title") or "").strip()
            if not title:
                raise _NotOffered("Say what the card is: its title.")
            text = str(args.get("text") or "").strip()
            title = _own_title(title, text)
            return _card(f'Add "{title}" to "{board["name"]}"',
                         detail="" if _same_words(title, text) else text[:300],
                         buttons=[await _button("Add card", "add_board_card",
                                                {"board": board["id"], "title": title, "body": text})])
        return _card(f'Delete the board "{board["name"]}"', danger=True,
                     detail="Its cards and their history go with it. This cannot be undone.",
                     buttons=[await _button("Delete it", "delete_board", {"board": board["id"]})])
    if action == "board_create":
        name = str(args.get("name") or "").strip()
        if not name:
            raise _NotOffered("Say what the board is called.")
        # A shared board of that name is there already (F1214): a build gives its workflow that one; none is made twice.
        there = next((b for b in await store.list_boards("shared") if b.name.casefold() == name.casefold()), None)
        if there is not None:
            raise _NotOffered(f'There is a shared board called "{there.name}" already: nothing new is made. A '
                              "workflow is given it to post to on the board's access list.")
        text = str(args.get("text") or "").strip()
        return _card(f'Make a shared board "{name}"', detail=text[:300],
                     buttons=[await _button("Make it", "create_board", {"name": name, "description": text})])
    card = await store.get_card(str(args.get("card_id") or ""))
    if card is None:
        raise _NotOffered("There is no such card. Topic `boards` with the board's name lists its cards.")
    if action == "board_comment":
        text = str(args.get("text") or "").strip()
        if not text:
            raise _NotOffered("Say what the comment says.")
        return _card(f'Comment on "{card.title}"', detail=text[:300],
                     buttons=[await _button("Comment", "comment_on_card", {"card_id": card.id, "text": text})])
    if action == "board_update_card":
        status = args.get("status") or "done"
        if status not in ("open", "done", "failed"):
            raise _NotOffered("status is open, done or failed.")
        result = str(args.get("result") or "").strip()
        verb = {"open": "Reopen", "done": "Close as done", "failed": "Close as failed"}[status]
        call = {"card_id": card.id, "status": status, **({"result": result} if result else {})}
        return _card(f'{verb}: "{card.title}"', detail=result[:300],
                     buttons=[await _button(verb, "update_board_card", call)])
    raise _NotOffered(f"action must be one of {', '.join(ACTIONS)}.")


# ── website widgets (F1262) ──────────────────────────────────────────────────────────────────────────────────────────

#: A card's words → the widget's own field.
_WIDGET_ARGS = {"agent": "agent_id", "sites": "allowed_origins", "visitor_tools": "visitor_tools",
                "greeting": "greeting", "title": "title", "accent": "accent", "theme": "theme",
                "position": "position", "launcher_label": "launcher_label", "font": "font",
                "header_style": "header_style", "launcher": "launcher", "daily_cap": "daily_message_cap",
                "on": "enabled"}

#: What each setting is called on the card, in the screen's order.
_WIDGET_LABELS = {"name": "Name", "agent_id": "Answers as", "allowed_origins": "Sites", "enabled": "On",
                  "visitor_tools": "Visitors may use", "daily_message_cap": "Messages a day", "greeting": "Greeting",
                  "title": "Title", "launcher_label": "Button label", "accent": "Accent", "theme": "Theme",
                  "position": "Position", "font": "Font", "header_style": "Header", "launcher": "Launcher"}

#: What a new widget's card always says, set or not.
_WIDGET_SAID = ("name", "agent_id", "allowed_origins", "enabled", "visitor_tools", "daily_message_cap")

#: A visitor tool that acts, in the card's words (`tool_policy.decide`'s categories; read_only is the one that doesn't).
_ACTS_AS = {"destructive": "deletes or overwrites", "open_world": "reaches outside this workspace",
            "changes": "changes things", "undeclared": "does not say what it does", "tool": "has its own rule"}


async def _widget_agent(wanted: str) -> dict:
    """An agent of the person's that a widget may answer as: never the builder's own (the screen's rule)."""
    from core.store.resources import load_agents

    agents = [a for a in await load_agents() if not a.get("system") and a.get("type") != "builder"]
    return _pick(agents, wanted, "agent", "Topic `agents` lists them.")


def _widget_changes(args: dict) -> dict:
    """The settings a card's arguments name, as the widget's own fields."""
    out = {field: args[arg] for arg, field in _WIDGET_ARGS.items() if arg in args and args[arg] is not None}
    if isinstance(out.get("allowed_origins"), list):
        from core.chat_widgets import origins

        bad = [s for s in out["allowed_origins"] if not origins.normalise_pattern(str(s))]
        if bad:
            raise _NotOffered(f"{', '.join(map(str, bad))} is not a site a widget can sit on: write it as "
                              "https://example.com (or https://*.example.com for its subdomains).")
        out["allowed_origins"] = origins.clean_list(out["allowed_origins"])
    return out


def _widget_view(widget: dict, agents: dict[str, str], tools: list[dict] | None = None) -> dict:
    """A widget's settings as its card says them."""
    from core.chat_widgets import service

    seen = service.look(widget)
    tools = tools if tools is not None else [{"name": n} for n in widget.get("visitor_tools") or []]
    return {**{k: seen.get(k) for k in _WIDGET_LABELS},
            "agent_id": agents.get(str(widget.get("agent_id") or ""), widget.get("agent_id") or None),
            "allowed_origins": list(widget.get("allowed_origins") or []) or None,
            "visitor_tools": [t["name"] for t in tools],
            "daily_message_cap": f"{seen.get('daily_message_cap')}"}


def _visitor_lines(tools: list[dict]) -> list[str]:
    """Each visitor tool that acts, on a line of its own (TRI1, F1262)."""
    return [f"A visitor's turn can use {t['name']}, which {_ACTS_AS.get(t['category'], 'acts')}."
            for t in tools if t.get("category") != "read_only"]


async def _widget(action: str, args: dict) -> dict:
    from core.chat_widgets import service
    from core.native_builder import rows as R
    from core.product_tools import widgets as W

    host = W.host()
    agents = {a["id"]: a.get("name") or a["id"] for a in await _all_agents()}

    async def reached(agent_id: str, names: list[str]) -> list[dict]:
        try:
            return await service.reachable_visitor_tools(agent_id, names, await host.choices(agent_id))
        except service.WidgetRefused as exc:
            raise _NotOffered(exc.detail) from None

    if action == "widget_create":
        name = str(args.get("name") or "").strip()
        if not name:
            raise _NotOffered("Say what the widget is called.")
        widgets = await host.load()
        if any(str(w.get("name") or "").casefold() == name.casefold() for w in widgets):
            raise _NotOffered(f'There is a widget called "{name}" already: change that one, or say another name.')
        if not str(args.get("agent") or "").strip():
            raise _NotOffered("Say which agent answers on it.")
        agent = await _widget_agent(str(args["agent"]))
        settings = {**_widget_changes(args), "name": name, "agent_id": agent["id"]}
        # None unless the person named them: never the agent's whole list (TRI1, F1262).
        tools = await reached(agent["id"], list(settings.get("visitor_tools") or []))
        settings["visitor_tools"] = [t["name"] for t in tools]
        try:
            service.body(settings)
        except service.WidgetRefused as exc:
            raise _NotOffered(exc.detail) from None
        # What every widget has, and the look the person named; the rest is the screen's default.
        shown = {k: v for k, v in _WIDGET_LABELS.items()
                 if k in _WIDGET_SAID or k in settings}
        rows = R.changed(None, _widget_view(settings, agents, tools), shown)
        if not settings.get("allowed_origins"):
            rows.append({"label": "Sites", "before": None, "after": "none yet",
                         "note": "It shows on no site until one is added."})
        return _card(f'Make a website widget "{name}"', rows=rows,
                     detail="\n".join(_visitor_lines(tools) + ["Its snippet is here once it is made."]),
                     buttons=[await _button("Make it", "create_widget", settings)])

    widget = _pick(await host.load(), str(args.get("widget") or ""), "widget", "Topic `widgets` lists them.")
    name = widget.get("name") or widget["id"]
    if action == "widget_update":
        changes = _widget_changes(args)
        if "agent_id" in changes:
            changes["agent_id"] = (await _widget_agent(str(changes["agent_id"])))["id"]
        if not changes:
            raise _NotOffered("Say what to change on it.")
        agent_id = str(changes.get("agent_id") or widget.get("agent_id") or "")
        tools: list[dict] | None = None
        if "visitor_tools" in changes or "agent_id" in changes:
            # A new agent keeps the list only if its visitor turn reaches every tool on it.
            tools = await reached(agent_id, list(changes.get("visitor_tools", widget.get("visitor_tools")) or []))
            changes["visitor_tools"] = [t["name"] for t in tools]
        after = {**widget, **changes}
        try:
            service.body({k: after.get(k) for k in service.FIELDS if after.get(k) is not None})
        except service.WidgetRefused as exc:
            raise _NotOffered(exc.detail) from None
        labels = {k: v for k, v in _WIDGET_LABELS.items() if k in changes}
        rows = R.changed(_widget_view(widget, agents), _widget_view(after, agents, tools), labels)
        if not rows:
            raise _NotOffered(f'"{name}" is already set that way.')
        return _card(f'Change the website widget "{name}"', rows=rows,
                     detail="\n".join(_visitor_lines(tools or [])),
                     buttons=[await _button("Save", "update_widget", {"widget_id": widget["id"], **changes})])
    if action == "widget_delete":
        sites = ", ".join(widget.get("allowed_origins") or []) or "no site yet"
        return _card(f'Delete the website widget "{name}"', danger=True,
                     detail=f"Every page carrying its snippet stops showing it ({sites}). This cannot be undone.",
                     buttons=[await _button("Delete it", "delete_widget", {"widget_id": widget["id"]})])
    if action == "widget_rotate_key":
        return _card(f'Give "{name}" a new key', danger=True,
                     detail="Its old snippet stops working at once, on every page that carries it. The new snippet "
                            "is here once it is done, to put on the site in its place.",
                     buttons=[await _button("New key", "rotate_widget_key", {"widget_id": widget["id"]})])
    if action == "widget_logo":
        path = str(args.get("path") or "").strip().lstrip("/")
        if not path:
            raise _NotOffered("Say which image: its path in the Vault.")
        return _card(f'Set "{name}"\'s logo', detail=f"{path}. A PNG, JPEG, GIF or WebP under 128 KB, drawn at 28 "
                                                     "pixels.",
                     rows=[{"label": "Logo", "before": "set" if widget.get("logo") else None,
                            "after": path.rsplit("/", 1)[-1]}],
                     buttons=[await _button("Set it", "set_widget_logo", {"widget_id": widget["id"], "path": path})])
    if action == "widget_logo_remove":
        if not widget.get("logo"):
            raise _NotOffered(f'"{name}" has no logo.')
        return _card(f'Take "{name}"\'s logo off', rows=[{"label": "Logo", "before": "set", "after": None}],
                     buttons=[await _button("Take it off", "set_widget_logo",
                                            {"widget_id": widget["id"], "remove": True})])
    raise _NotOffered(f"action must be one of {', '.join(ACTIONS)}.")


async def _all_agents() -> list[dict]:
    from core.store.resources import load_agents

    return await load_agents()


async def prepare_action(args: dict, server_module: Any, events: list[dict]) -> dict:
    action = str(args.get("action") or "").strip()
    try:
        card = await _offer(action, args)
    except _FeatureOff as off:
        # Never a quiet refusal (F1203): what is off and where; and its switch as a card, for one who may press it.
        said, switch = await feature_switch(off)
        if switch is None:
            return {"error": said}
        events.append(switch)
        return {"error": said, "card": switch["title"]}
    except _NotOffered as why:
        if why.options:
            from core.builder_tools import _current_run
            from core.native_builder import turn_facts

            await turn_facts.keep(_current_run.get(), _caller().user_id, picks=[{
                "action": action, "field": why.field, "options": why.options,
                "arguments": {k: v for k, v in args.items() if k not in ("action", why.field)}}])
        return {"error": str(why)}
    events.append(card)
    pressed = " or ".join(f'"{b["label"]}"' for b in card["buttons"]) or "answer on it"
    return {"card": card["title"],
            "say": f"A card is up: {card['title']}. Nothing has been done yet; press {pressed} to do it."}


register_builder_tool(SCHEMA, prepare_action)
