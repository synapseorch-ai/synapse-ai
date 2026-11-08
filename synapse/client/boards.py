"""Boards from the terminal: shared to-do lists people and agents work from (D294, BOARD1).

A key reaches Boards only through the product tools (`mcp.py`): there is no
v1 route. The CLI acts as a person does in the app: adding a card is giving
the agents a task, read first by the next turn on that board.
"""
from __future__ import annotations

import json

from synapse.client import errors
from synapse.client.commands import Tree, _handler, _quiet_flag, _when, confirmed_call
from synapse.client.http import ApiClient
from synapse.client.mcp import Mcp
from synapse.client.output import Output


def _value(raw):
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


@_handler
def boards_list(args, api: ApiClient, out: Output):
    mcp, boards, cursor = Mcp(api), [], None
    while True:
        page = _value(mcp.call("list_boards", {"cursor": cursor, "limit": 50}))
        boards += page.get("items") or []
        cursor = page.get("next_cursor")
        if not cursor:
            break
    if args.quiet:
        return out.ids(boards)
    if out.json_mode:
        return out.document(boards)
    out.table(boards, [("ID", lambda b: b.get("id")), ("BOARD", lambda b: b.get("board")),
                       ("KIND", lambda b: b.get("kind")), ("OPEN", lambda b: b.get("open")),
                       ("BEING WORKED ON", lambda b: b.get("being_worked_on"))],
              empty="No boards yet. Make one with: synapse boards create NAME")


def _card_columns():
    return [("CARD", lambda c: c.get("id")), ("TITLE", lambda c: c.get("title")),
            ("STATUS", lambda c: c.get("status")),
            ("HELD BY", lambda c: c.get("held_by") or ("a person" if c.get("added_by_a_person") else "")),
            ("UPDATED", lambda c: _when(c.get("updated_at")))]


@_handler
def boards_show(args, api: ApiClient, out: Output):
    board = _value(Mcp(api).call("read_board", {"board": args.board}))
    if out.json_mode:
        return out.document(board)
    out.out.print(f"[bold]{board.get('board')}[/bold]  [dim]{board.get('id')}[/dim]", markup=True, highlight=False)
    out.table(board.get("open") or [], _card_columns(), empty="Nothing open on it.")
    closed = board.get("recently_closed") or []
    if closed and args.all:
        out.text("")
        out.text("Recently closed")
        out.table(closed, [*_card_columns()[:2], ("RESULT", lambda c: c.get("result") or c.get("outcome"))],
                  empty="")
    elif closed:
        out.note(f"{len(closed)} closed recently; --all lists them.")


@_handler
def boards_create(args, api: ApiClient, out: Output):
    made = _value(Mcp(api).call("create_board", {"name": args.name, "description": args.description}))
    if out.json_mode:
        return out.document(made)
    board = made.get("board") if isinstance(made, dict) and isinstance(made.get("board"), dict) else made
    out.text(board.get("id") if isinstance(board, dict) else str(made))
    out.note(f"Made the board {args.name}. Give it a task with: synapse boards card add {args.name!r} TITLE")


@_handler
def boards_delete(args, api: ApiClient, out: Output):
    done = confirmed_call(Mcp(api), out, "delete_board", {"board": args.board}, args.yes,
                          f"Delete the board '{args.board}' with its cards and history")
    if done is None:
        return errors.OK
    if out.json_mode:
        return out.document(_value(done))
    out.text("Deleted.")


def _card_out(out: Output, card, verb: str) -> None:
    card = _value(card)
    if out.json_mode:
        return out.document(card)
    inner = card.get("card") if isinstance(card, dict) and isinstance(card.get("card"), dict) else card
    if isinstance(inner, dict) and inner.get("id"):
        out.text(inner["id"])
        out.note(f"{verb}: {inner.get('title')} ({inner.get('status')}).")
    else:
        out.text(str(card))


@_handler
def card_add(args, api: ApiClient, out: Output):
    _card_out(out, Mcp(api).call("add_board_card", {"board": args.board, "title": args.title, "body": args.body}),
              "Added")


@_handler
def card_update(args, api: ApiClient, out: Output):
    from synapse.client.errors import CliError

    change = {"status": args.status, "result": args.result, "title": args.title, "body": args.body}
    if not any(v is not None for v in change.values()):
        raise CliError("Say what to change: --status, --result, --title or --body.", errors.USAGE)
    _card_out(out, Mcp(api).call("update_board_card", {"card_id": args.card_id, **change}), "Updated")


@_handler
def card_comment(args, api: ApiClient, out: Output):
    done = _value(Mcp(api).call("comment_on_card", {"card_id": args.card_id, "text": args.text}))
    if out.json_mode:
        return out.document(done)
    out.text("Commented.")


def register(tree: Tree) -> None:
    add, group = tree.command, tree.group
    boards = group(tree.sub, "boards", "Boards: the to-do lists people and agents work from")
    _quiet_flag(add(boards, "list", boards_list, "List boards"))
    p = add(boards, "show", boards_show, "A board's open cards")
    p.add_argument("board", help="name or id")
    p.add_argument("--all", action="store_true", help="the recently closed ones too")
    p = add(boards, "create", boards_create, "Make a shared board")
    p.add_argument("name")
    p.add_argument("--description")
    p = add(boards, "delete", boards_delete, "Delete a board, its cards and their history")
    p.add_argument("board", help="name or id")
    p.add_argument("--yes", action="store_true", help="do not ask first")

    cards = group(boards, "card", "Add, change and comment on cards")
    p = add(cards, "add", card_add, "Give the board a task: the next turn on it reads it first")
    p.add_argument("board", help="name or id")
    p.add_argument("title")
    p.add_argument("--body", help="the details")
    p = add(cards, "update", card_update, "Close, reopen or edit a card")
    p.add_argument("card_id")
    p.add_argument("--status", choices=["open", "done"], help="done closes it; open reopens it")
    p.add_argument("--result", help="what came of it, when closing")
    p.add_argument("--title")
    p.add_argument("--body")
    p = add(cards, "comment", card_comment, "Comment on a card")
    p.add_argument("card_id")
    p.add_argument("text")
