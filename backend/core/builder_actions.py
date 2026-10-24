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
    "knowledge_add_pages": "add public web pages to a knowledge base (`knowledge_base`, `urls`, `kind`: page, sitemap "
                           "or site)",
    "knowledge_add_note": "add a note to a knowledge base (`knowledge_base`, `title`, `text`)",
    "knowledge_retry_document": "index a failed document again (`doc_id`)",
    "knowledge_delete_document": "delete a document from its knowledge base (`doc_id`)",
    "board_add_card": "add a card to a board (`board`, `title`, `text`)",
    "board_update_card": "close a card as done or failed, or reopen it (`card_id`, `status`, `result`)",
    "board_comment": "comment on a card (`card_id`, `text`)",
    "board_create": "make a shared board (`name`, `text`: what it is for)",
    "board_delete": "delete a shared board (`board`)",
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
                "board": {"type": "string", "description": "The board's name."},
                "card_id": {"type": "string", "description": "From topic `boards` with the board's name."},
                "status": {"type": "string", "enum": ["open", "done", "failed"]},
                "result": {"type": "string", "description": "board_update_card: what came of it."},
                "name": {"type": "string"},
                "title": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["action"],
        },
    },
}


class _NotOffered(Exception):
    """Why no card: said to the model, which says it to the person. A "which one?" carries its options with their
    ids, and the field they answer, so the turn keeps them for the next turn's pick (F977)."""

    def __init__(self, why: str, *, field: str = "", options: list[dict] | None = None) -> None:
        super().__init__(why)
        self.field, self.options = field, options or []


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
          question: dict | None = None) -> dict:
    return {"type": "action_needed", "id": f"act_{uuid.uuid4().hex[:10]}", "title": title, "detail": detail,
            "danger": danger, "buttons": buttons or [], **({"question": question} if question else {})}


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

    if action.startswith("knowledge_"):
        return await _knowledge(action, args)
    if action.startswith("board_"):
        return await _board(action, args)

    raise _NotOffered(f"action must be one of {', '.join(ACTIONS)}.")


async def _resolved(lookup):
    """A product tool's own lookup, its refusal said as why there is no card."""
    try:
        return await lookup
    except ToolRefusal as exc:
        raise _NotOffered(str(exc)) from None


async def _knowledge(action: str, args: dict) -> dict:
    from core.product_tools import knowledge

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


async def prepare_action(args: dict, server_module: Any, events: list[dict]) -> dict:
    action = str(args.get("action") or "").strip()
    try:
        card = await _offer(action, args)
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
