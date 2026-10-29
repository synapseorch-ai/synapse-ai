"""Boards for people, as product tools (ASSIST1, D291, F684).

What a person does on the Boards screens, through the person-facing service both products' routes use
(`core/boards/service.py`; BOARD1b): read the boards and one board's cards, add a card, close or reopen one, comment,
make a shared board, delete one. A person's write is a person's (`service.person`, no claim, the version just read),
never an agent's board tool (`core/boards/tools.py`, BOARD2's). Reading is anyone's; writing a builder's, as on the
screens. A team's run boards are never a person's and never appear.

What only a host knows — whether its workspace has Boards on, its directory of names, the person's name for a card's
history, the caps — comes from its `BoardHost` (`set_board_host`); this product's own by default.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from core.product_tools import page
from core.product_tools.errors import ToolRefusal
from core.product_tools.registry import ProductTool
from core.product_tools.roles import Role


@dataclass(frozen=True)
class BoardHost:
    enabled: Callable[[], Awaitable[bool]]
    directory: Callable[[], Awaitable[Any]]
    #: The person's name for a card's history, from the caller (`Caller.user_id`; None: the install's one person).
    person_name: Callable[[Any], Awaitable[str]]
    #: (max shared boards, max open cards per board), 0 for no cap.
    caps: Callable[[], Awaitable[tuple[int, int]]]


async def _oss_enabled() -> bool:
    from core.boards.config import boards_enabled
    return boards_enabled()


async def _oss_directory():
    from core.routes.boards import _directory
    return await _directory()


async def _oss_person(caller: Any) -> str:
    from core.personal_details import load_personal_details

    details = await load_personal_details() or {}
    name = " ".join(p for p in (str(details.get("first_name") or "").strip(),
                                str(details.get("last_name") or "").strip()) if p)
    return name or "You"


async def _oss_caps() -> tuple[int, int]:
    from core.boards.config import board_settings

    caps = board_settings()
    return caps.max_shared, caps.max_open_cards


_HOST = BoardHost(enabled=_oss_enabled, directory=_oss_directory, person_name=_oss_person, caps=_oss_caps)
_host = _HOST


def set_board_host(host: BoardHost | None) -> None:
    global _host
    _host = host or _HOST


async def _on() -> None:
    if not await _host.enabled():
        from core.features import off_sentence
        raise ToolRefusal(off_sentence("boards"))  # what is off and where it is turned on (F1203)


def _refusing(exc: Exception) -> ToolRefusal:
    return ToolRefusal(str(exc) or type(exc).__name__)


async def _board_named(wanted: str) -> dict:
    """A board a person can open, by its id or its name (exact first, then a part of it; two that match are asked
    about, never guessed between)."""
    from core.boards import service

    listed = (await service.boards_list(await _host.directory(), max_shared=0))["boards"]
    needle = (wanted or "").strip().lower()
    if not needle:
        raise ToolRefusal("Say which board.")
    exact = [b for b in listed if b.get("id") == wanted or str(b.get("name") or "").lower() == needle]
    near = exact or [b for b in listed if needle in str(b.get("name") or "").lower()]
    if len(near) == 1:
        return near[0]
    if not near:
        raise ToolRefusal(f"No board is called {wanted!r}. list_boards names them.")
    raise ToolRefusal("More than one board matches: " + ", ".join(f'"{b.get("name")}"' for b in near[:8])
                      + ". Ask which one they mean.")


async def _person(caller: Any):
    from core.boards import service
    return service.person(await _host.person_name(caller))


# ── reads ────────────────────────────────────────────────────────────────────────

async def _list(caller: Any, args: dict) -> dict:
    from core.boards import service

    await _on()
    listed = await service.boards_list(await _host.directory(), max_shared=0)
    items = [{"board": b.get("name"), "id": b.get("id"), "kind": b.get("scope"), "open": b.get("open"),
              "being_worked_on": b.get("claimed")} for b in listed["boards"]]
    return page.page(items, args)


async def _read(caller: Any, args: dict) -> dict:
    from core.boards import service
    from core.store import boards as store

    await _on()
    board = await _board_named(str(args.get("board") or ""))
    try:
        view = await service.board_view(await store.get_board(board["id"]), await _host.directory())
    except store.BoardError as exc:
        raise _refusing(exc) from None

    def card(c: dict) -> dict:
        out = {k: c.get(k) for k in ("id", "title", "status", "outcome", "kind", "result", "updated_at") if c.get(k)}
        holder = c.get("holder_label") or c.get("holder_agent")
        return out | ({"held_by": holder} if holder else {}) | ({"added_by_a_person": True} if c.get("by_person") else {})

    return {"board": board.get("name"), "id": board["id"],
            "open": [card(c) for c in view.get("cards") or []][:50],
            "recently_closed": [card(c) for c in view.get("done") or []][:20]}


# ── a person's writes ────────────────────────────────────────────────────────────

async def _add_card(caller: Any, args: dict) -> dict:
    from core.boards import service
    from core.store import boards as store

    await _on()
    board = await _board_named(str(args.get("board") or ""))
    _, max_open = await _host.caps()
    try:
        return await service.add_card(board["id"], title=str(args.get("title") or ""), body=str(args.get("body") or ""),
                                      by=await _person(caller), max_open=max_open)
    except store.BoardError as exc:
        raise _refusing(exc) from None


async def _update_card(caller: Any, args: dict) -> dict:
    from core.boards import service
    from core.store import boards as store

    await _on()
    card = await store.get_card(str(args.get("card_id") or ""))
    if card is None:
        raise ToolRefusal("There is no such card.")
    try:
        return await service.update_card(card.id, version=card.version, by=await _person(caller),
                                         title=args.get("title"), body=args.get("body"), status=args.get("status"),
                                         result=args.get("result"))
    except store.BoardError as exc:
        raise _refusing(exc) from None


async def _comment(caller: Any, args: dict) -> dict:
    from core.boards import service
    from core.store import boards as store

    await _on()
    try:
        return await service.comment(str(args.get("card_id") or ""), str(args.get("text") or ""),
                                     by=await _person(caller))
    except store.BoardError as exc:
        raise _refusing(exc) from None


async def _create(caller: Any, args: dict) -> dict:
    from core.boards import service
    from core.store import boards as store

    await _on()
    max_shared, _ = await _host.caps()
    try:
        return await service.create_shared(str(args.get("name") or ""), description=str(args.get("description") or ""),
                                           by=await _person(caller), max_shared=max_shared)
    except store.BoardError as exc:
        raise _refusing(exc) from None


async def _give_access(caller: Any, args: dict) -> dict:
    """One entry on a shared board's access list, the rest kept: what the Boards screen's access editor saves, through
    the same service (F1214, CAP3), so the same rules hold — a shared board, a principal of this workspace, `add` or
    `work`, a principal's cap of shared boards — and the same role, a builder's write (cloud's route asks the same).
    A build gives its workflow the board the person asked for this way: its agents' cards go there."""
    from core.boards import service
    from core.store import boards as store

    await _on()
    board = await _board_named(str(args.get("board") or ""))
    if board.get("scope") != "shared":
        raise ToolRefusal("Only a shared board has an access list: a workflow's or a schedule's own board is its own.")
    kind, principal = str(args.get("kind") or ""), str(args.get("id") or "")
    listed = await store.list_access(board["id"])
    was = next((a.access for a in listed if (a.principal_kind, a.principal_id) == (kind, principal)), None)
    kept = [{"kind": a.principal_kind, "id": a.principal_id, "access": a.access}
            for a in listed if (a.principal_kind, a.principal_id) != (kind, principal)]
    try:
        saved = await service.set_access(board["id"], [*kept, {"kind": kind, "id": principal,
                                                                "access": str(args.get("access") or "")}],
                                         directory=await _host.directory(), by=await _person(caller))
    except store.BoardError as exc:
        raise _refusing(exc) from None
    return {"board": board.get("name"), "id": board["id"], "access": saved, "was": was}


async def _delete(caller: Any, args: dict) -> dict:
    from core.boards import service
    from core.store import boards as store

    await _on()
    board = await _board_named(str(args.get("board") or ""))
    try:
        await service.delete(board["id"], await _host.directory())
    except store.BoardError as exc:
        raise _refusing(exc) from None
    return {"deleted": board.get("name")}


async def _preview_delete(caller: Any, args: dict) -> dict:
    """What deleting the board removes, or why it can't go, before the confirmation (MCP2's token)."""
    await _on()
    board = await _board_named(str(args.get("board") or ""))
    if board.get("scope") != "shared" and not board.get("owner_gone"):
        noun = "workflow" if board.get("scope") == "workflow" else "schedule"
        raise ToolRefusal(f"This board belongs to its {noun}; close the cards you no longer want instead.")
    return {"deletes": f'the board "{board.get("name")}"', "with_it": "its cards, their comments and history",
            "open_cards": board.get("open") or 0}


_BOARD = {"board": {"type": "string", "description": "The board's name (or its id)."}}
_CARD = {"card_id": {"type": "string", "description": "The card's id, from read_board."}}


def entries() -> list[ProductTool]:
    act = {"kind": "act", "area": "boards", "min_role": Role.BUILDER, "scope": "build"}
    return [
        ProductTool(name="list_boards", description="The boards a person can open, with how many cards are open on each.",
                    input_schema={"type": "object", "properties": {}}, handler=_list, kind="read", area="boards",
                    paginated=True),
        ProductTool(name="read_board", description="One board's open cards (who holds each) and the latest closed ones.",
                    input_schema={"type": "object", "properties": dict(_BOARD), "required": ["board"]},
                    handler=_read, kind="read", area="boards"),
        ProductTool(name="add_board_card", description="Add a card to a board, as the person.",
                    input_schema={"type": "object", "properties": {
                        **_BOARD, "title": {"type": "string"}, "body": {"type": "string"}}, "required": ["board", "title"]},
                    handler=_add_card, audit_action="board.card.added",
                    target=lambda a, r: ("board_card", str(((r or {}).get("card") or {}).get("id") or "")), **act),
        ProductTool(name="update_board_card",
                    description="Close a card (done or failed, with its result), reopen it, or change its title or text.",
                    input_schema={"type": "object", "properties": {
                        **_CARD, "status": {"type": "string", "enum": ["open", "done", "failed"]},
                        "result": {"type": "string"}, "title": {"type": "string"}, "body": {"type": "string"}},
                        "required": ["card_id"]},
                    handler=_update_card, audit_action="board.card.updated",
                    target=lambda a, r: ("board_card", str(a.get("card_id"))), **act),
        ProductTool(name="comment_on_card", description="Comment on a card, as the person.",
                    input_schema={"type": "object", "properties": {**_CARD, "text": {"type": "string"}},
                                  "required": ["card_id", "text"]},
                    handler=_comment, audit_action="board.card.commented",
                    target=lambda a, r: ("board_card", str(a.get("card_id"))), **act),
        ProductTool(name="create_board", description="Make a shared board.",
                    input_schema={"type": "object", "properties": {
                        "name": {"type": "string"}, "description": {"type": "string"}}, "required": ["name"]},
                    handler=_create, audit_action="board.created",
                    target=lambda a, r: ("board", str((r or {}).get("id") or "")), **act),
        ProductTool(name="give_board_access",
                    description="Let an agent, a workflow or a schedule add cards to a shared board ('add') or work "
                                "its cards ('work'), as the board's access list on the Boards screen does.",
                    input_schema={"type": "object", "properties": {
                        **_BOARD, "kind": {"type": "string", "enum": ["agent", "workflow", "schedule"]},
                        "id": {"type": "string"}, "access": {"type": "string", "enum": ["add", "work"]}},
                        "required": ["board", "kind", "id", "access"]},
                    handler=_give_access, audit_action="board.access.given",
                    target=lambda a, r: ("board", str((r or {}).get("id") or "")), **act),
        ProductTool(name="delete_board", description="Delete a shared board with its cards and history.",
                    input_schema={"type": "object", "properties": dict(_BOARD), "required": ["board"]},
                    handler=_delete, irreversible=True, preview=_preview_delete, audit_action="board.deleted",
                    target=lambda a, r: ("board", str(a.get("board"))), **act),
    ]
