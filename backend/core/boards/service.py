"""Boards for people: what a host's routes serve, in one place for both products (BOARD1b, D282; MEMORY 20 §5).

The store (`core.store.boards`) keeps the rules — compare-and-set, holds, the scrubber, caps, history and undo.
This module shapes what a person's screens read, and makes a person's writes as a person's: `Actor(person=…)`,
no claim holding them off (`holder=None`), and always the version the person read. The routes of each product
are thin around it (OSS `core/routes/boards.py`, cloud's `api/control/routes/boards.py`), so the two APIs keep
one shape.

What a host knows and the engine does not — the names of its agents, workflows and schedules, which schedule
runs what, a workflow's latest run — comes in as a `Directory` and a `current_run`, built by the host per request.

A team's run boards (`scope="run"`, TEAM1a) are never a person's: they are not listed, read or written here. Nor,
yet, is Synapse AI's own board (`scope="assistant"`, SAI2-P1, `core.boards.assistant`): P3 shows it, each person their
own cards.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.store import boards as store

#: What a person reads of a board at once: its live cards (the store's cap per read), and the latest closed ones.
LIVE_LIMIT = 200
DONE_LIMIT = 50
#: History kinds a person can take back (`store.undo`).
UNDOABLE = frozenset({"created", "edited", "closed", "reopened", "comment"})
#: Scopes a person's screens never list, read or write here.
_NOT_A_PERSONS = ("run", "assistant")
#: The principals an access list names, as a person says them.
PRINCIPAL_NOUN = {"agent": "agent", "workflow": "workflow", "schedule": "schedule"}


class WrongScope(store.BoardError):
    """The board asked for is not one a person can have here (a 409): a schedule that runs a workflow, or a
    workflow's board while its workflow is still there."""


@dataclass
class Directory:
    """The host's workspace, for names and checks: agents and workflows by id → name; schedules by id →
    {"name", "target_type", "target_id"}."""

    agents: dict[str, str] = field(default_factory=dict)
    workflows: dict[str, str] = field(default_factory=dict)
    schedules: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: The last names of owners that are gone, by (kind, id): a deleted workflow's board says whose it was (F872).
    gone: dict[tuple[str, str], str] = field(default_factory=dict)

    def name_of(self, kind: str, principal_id: str) -> str | None:
        if kind == "agent":
            return self.agents.get(principal_id)
        if kind == "workflow":
            return self.workflows.get(principal_id)
        if kind == "schedule":
            row = self.schedules.get(principal_id)
            return str(row.get("name") or principal_id) if row is not None else None
        return None


# ── what a person reads ─────────────────────────────────────────────────────────

def _board_out(board: store.Board, directory: Directory, counts: tuple[int, int] = (0, 0)) -> dict[str, Any]:
    out = board.to_dict()
    gone = False
    if board.scope != "shared":
        owner = directory.name_of(board.scope, board.scope_id)
        gone = owner is None
        # Never its owner's raw id (F872): the owner's last name, or what it was, in words.
        last = directory.gone.get((board.scope, board.scope_id)) if gone else None
        out["name"] = (owner if not gone else f"{last} (deleted {board.scope})" if last
                       else f"A deleted {board.scope}'s board")
    out.update(owner_gone=gone, open=counts[0], claimed=counts[1])
    return out


def _access_out(entry: store.Access, directory: Directory) -> dict[str, Any]:
    name = directory.name_of(entry.principal_kind, entry.principal_id)
    return {"kind": entry.principal_kind, "id": entry.principal_id, "access": entry.access,
            "name": name or entry.principal_id, "gone": name is None}


async def _readable(board_id: str) -> store.Board:
    board = await store.get_board(board_id)
    if board is None or board.scope in _NOT_A_PERSONS:
        raise store.NoSuchBoard(f"There is no board {board_id}.")
    return board


async def _readable_card(card_id: str) -> store.Card:
    card = await store.get_card(card_id)
    if card is None:
        raise store.NoSuchCard(f"There is no card {card_id}.")
    await _readable(card.board_id)
    return card


async def boards_list(directory: Directory, *, max_shared: int) -> dict[str, Any]:
    """Every board a person can open — shared, each workflow's and each schedule's — with its live counts, and
    how many shared boards the workspace has of its cap (0 is none)."""
    boards = [b for b in await store.list_boards() if b.scope not in _NOT_A_PERSONS]
    counts = await store.live_counts([b.id for b in boards])
    shared = sum(1 for b in boards if b.scope == "shared")
    return {"boards": [_board_out(b, directory, counts.get(b.id, (0, 0))) for b in boards],
            "shared_used": shared, "shared_limit": max_shared or None}


def _closed_newest_first(cards: list[store.Card]) -> list[store.Card]:
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(cards, key=lambda c: c.closed_at or c.updated_at or oldest, reverse=True)


async def board_view(board: store.Board, directory: Directory, *, current_run: dict[str, Any] | None = None,
                     only_this_run: bool = False, query: str | None = None) -> dict[str, Any]:
    """A board as its page shows it: the live cards (this run's first, then a person's, then tasks oldest first,
    then notes newest first), the latest closed ones, and on a search the archived ones that match too."""
    run_id = str((current_run or {}).get("run_id") or "") or None
    q = (query or "").strip() or None
    only = bool(only_this_run and run_id)
    live = await store.list_cards([board.id], statuses=("open", "claimed"), run_id=run_id, only_run=only,
                                  query=q, limit=LIVE_LIMIT)
    done = await store.list_cards([board.id], statuses=("done",), run_id=run_id, only_run=only, query=q,
                                  limit=LIVE_LIMIT)
    archived = (await store.list_cards([board.id], statuses=("archived",), query=q, limit=DONE_LIMIT)) if q else []
    counts = (await store.live_counts([board.id])).get(board.id, (0, 0))
    out: dict[str, Any] = {
        "board": _board_out(board, directory, counts),
        "current_run": current_run or None,
        "cards": [c.to_dict() for c in live],
        "done": [c.to_dict() for c in _closed_newest_first(done)[:DONE_LIMIT]],
        "archived": [c.to_dict() for c in archived],
        "access": [],
    }
    if board.scope == "shared":
        out["access"] = [_access_out(a, directory) for a in await store.list_access(board.id)]
    return out


async def open_board(board_id: str, directory: Directory, **view: Any) -> dict[str, Any]:
    return await board_view(await _readable(board_id), directory, **view)


def _scope_check(kind: str, scope_id: str, directory: Directory) -> None:
    if kind == "workflow":
        if scope_id not in directory.workflows:
            raise store.NoSuchBoard(f"There is no workflow {scope_id}.")
        return
    if kind == "schedule":
        row = directory.schedules.get(scope_id)
        if row is None:
            raise store.NoSuchBoard(f"There is no schedule {scope_id}.")
        if str(row.get("target_type") or "agent") != "agent":
            raise WrongScope("This schedule runs a workflow, so its cards are on that workflow's board.")
        return
    raise store.NoSuchBoard(f"There is no {kind} board.")


async def scoped_view(kind: str, scope_id: str, directory: Directory, **view: Any) -> dict[str, Any]:
    """A workflow's or a schedule's own board, read without making it: one nothing has written yet reads as an
    empty board with no id."""
    _scope_check(kind, scope_id, directory)
    board = await store.find_scoped_board(kind, scope_id)
    if board is not None:
        return await board_view(board, directory, **view)
    name = directory.name_of(kind, scope_id) or scope_id
    return {"board": {"id": None, "scope": kind, "scope_id": scope_id, "name": name, "description": "",
                      "created_by": {}, "created_at": None, "owner_gone": False, "open": 0, "claimed": 0},
            "current_run": view.get("current_run") or None, "cards": [], "done": [], "archived": [], "access": []}


async def card_view(card_id: str) -> dict[str, Any]:
    """A card with its comments and its history, newest first; each change says whether a person can take it
    back (the store decides when one is asked)."""
    card = await _readable_card(card_id)
    events = await store.history(card_id, limit=100)
    taken_back = {int((e.after or {}).get("undoes") or 0) for e in events if e.kind == "undo"}
    cutoff = datetime.now(timezone.utc) - store.HISTORY_FOR
    history = []
    for event in events:
        row = event.to_dict()
        row["undoable"] = (event.kind in UNDOABLE and event.seq not in taken_back and card.status != "archived"
                           and (event.created_at is None or event.created_at >= cutoff))
        history.append(row)
    return {"card": card.to_dict(), "comments": [c.to_dict() for c in await store.comments(card_id)],
            "history": history}


async def boards_for(kind: str, principal_id: str, directory: Directory, *,
                     runs_it: list[tuple[str, str]] | None = None) -> list[dict[str, Any]]:
    """The shared boards whose access lists name this agent, workflow or schedule, with its access on each, in
    the order its turns take them (the oldest three); `through` is empty for these.

    An agent's turn also gets the boards of the schedule or workflow it runs in (C8). With `runs_it` — the
    `(kind, id)` the host found running the agent — each such board follows, once, naming what it comes through
    (F583): its access the stronger of theirs, and in use when it is among the oldest three of some turn's."""
    if kind not in PRINCIPAL_NOUN:
        raise store.BadValue(f"Unknown kind {kind!r}: an agent, a workflow or a schedule.")
    rows = await store.shared_boards_for([(kind, principal_id)])
    out = [{**_board_out(board, directory), "access": access, "through": [],
            "in_use": i < store.SHARED_PER_PRINCIPAL} for i, (board, access) in enumerate(rows)]
    if kind != "agent" or not runs_it:
        return out
    named = {board.id for board, _ in rows}
    reached: dict[str, dict[str, Any]] = {}
    for run_kind, run_id in runs_it:
        # What one turn of the agent in this schedule or workflow gets: the two lists together, the oldest three.
        turn = await store.shared_boards_for([(kind, principal_id), (run_kind, run_id)])
        for i, (board, access) in enumerate(turn):
            if board.id in named:
                continue
            entry = reached.setdefault(board.id, {**_board_out(board, directory), "access": access,
                                                  "through": [], "in_use": False})
            entry["through"].append({"kind": run_kind, "id": run_id,
                                     "name": directory.name_of(run_kind, run_id) or run_id})
            if access == "work":
                entry["access"] = "work"
            entry["in_use"] = entry["in_use"] or i < store.SHARED_PER_PRINCIPAL
    return out + list(reached.values())


# ── what a person writes ───────────────────────────────────────────────────────

def person(name: str) -> store.Actor:
    """A person's write, as the history keeps it."""
    return store.Actor(person=(name or "").strip() or "Someone", source="person")


async def create_shared(name: str, *, description: str = "", by: store.Actor, max_shared: int) -> dict[str, Any]:
    board = await store.create_shared_board(name, description=description, by=by, max_shared=max_shared)
    return _board_out(board, Directory())


async def update_shared(board_id: str, *, name: str | None = None, description: str | None = None,
                        directory: Directory) -> dict[str, Any]:
    await _readable(board_id)
    return _board_out(await store.update_board(board_id, name=name, description=description), directory)


async def delete(board_id: str, directory: Directory) -> None:
    """A shared board, with its list, cards and history. A workflow's or a schedule's own board goes only once
    its workflow or schedule is gone: until then it is that workflow's, and a person closes its cards."""
    board = await _readable(board_id)
    if board.scope != "shared" and directory.name_of(board.scope, board.scope_id) is not None:
        noun = "workflow" if board.scope == "workflow" else "schedule"
        raise WrongScope(f"This board belongs to its {noun}; close the cards you no longer want instead.")
    await store.delete_board(board_id)


async def set_access(board_id: str, entries: list[dict[str, Any]], *, directory: Directory,
                     by: store.Actor) -> list[dict[str, Any]]:
    """Replace a shared board's list. A principal newly named must exist; one already named that has since been
    deleted may stay until a person removes it."""
    board = await _readable(board_id)
    if board.scope != "shared":
        raise store.BadValue("Only a shared board has an access list.")
    known = {(a.principal_kind, a.principal_id) for a in await store.list_access(board_id)}
    rows: list[tuple[str, str, str]] = []
    for entry in entries:
        kind, principal_id = str(entry.get("kind") or ""), str(entry.get("id") or "")
        if kind not in PRINCIPAL_NOUN:
            raise store.BadValue(f"Unknown kind {kind!r}: an agent, a workflow or a schedule.")
        if (kind, principal_id) not in known and directory.name_of(kind, principal_id) is None:
            raise store.BadValue(f"There is no {PRINCIPAL_NOUN[kind]} {principal_id}.")
        rows.append((kind, principal_id, str(entry.get("access") or "")))
    saved = await store.set_access(board_id, rows, by=by)
    return [_access_out(a, directory) for a in saved]


async def add_card(board_id: str, *, title: str, body: str = "", kind: str = "task", key: str | None = None,
                   by: store.Actor, max_open: int) -> dict[str, Any]:
    """A person's card: read first by the next turn on this board. An existing key returns that card."""
    await _readable(board_id)
    card, added = await store.add_card(board_id, title=title, body=body, kind=kind, key=key or None, by=by,
                                       max_open=max_open)
    return {"card": card.to_dict(), "added": added}


async def add_scoped_card(scope: str, scope_id: str, *, directory: Directory, **card: Any) -> dict[str, Any]:
    """A person's first card on a workflow's or a schedule's own board makes the board."""
    _scope_check(scope, scope_id, directory)
    board = await store.get_or_create_scoped_board(scope, scope_id, by=card.get("by"))
    return await add_card(board.id, **card)


async def update_card(card_id: str, *, version: int, by: store.Actor, title: str | None = None,
                      body: str | None = None, status: str | None = None, result: str | None = None) -> dict:
    """A person's change, against the version they read. No claim holds a person off; the version still does."""
    await _readable_card(card_id)
    card = await store.update_card(card_id, version=version, by=by, holder=None, title=title, body=body,
                                   status=status, result=result)
    return card.to_dict()


async def comment(card_id: str, text: str, *, by: store.Actor) -> dict[str, Any]:
    await _readable_card(card_id)
    return (await store.add_comment(card_id, text, by=by)).to_dict()


async def undo(card_id: str, seq: int, *, by: store.Actor) -> dict[str, Any]:
    """Take back one change from a card's history: a person's only (C4)."""
    if not by.person:
        raise store.BadValue("Only a person can take back a change.")
    await _readable_card(card_id)
    return (await store.undo(card_id, int(seq), by=by)).to_dict()
