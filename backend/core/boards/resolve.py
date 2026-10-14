"""Which boards an agent's turn has, decided in code from where it runs (C2, C8; BOARD1a, D280).

* **Its own board:** a workflow step has that workflow's board (a scheduled workflow's too; a nested run's is its
  own workflow's), and a schedule that runs an agent directly has that schedule's board. Plain chat has none.
  The agent's Boards section can turn its own board off (`boards.own`, on by default).
* **Plus up to three shared boards** whose access lists name the agent, or the workflow or schedule it runs in,
  each at the stronger access listed (`add`, or `work`: read, claim and update). The oldest three; a fourth is
  logged, never silently taken.
* **Nothing, and no query**, while Boards are off, on a visitor's turn (C4), for a caller with its own tool list
  (the AI builder), and in a nested turn: a helper (S1) or a delegate's member (TEAM1a decides members).

The caller passes the scope (`board_scope`): no workflow or schedule id reaches the engine otherwise.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from core.boards.config import board_settings
from core.store import boards as store

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BoardScope:
    """Where a turn runs, as its caller knows it: a workflow step or a schedule's firing.

    `holder` is the turn's claim key: a workflow step's `run_id/step_id`, a schedule firing's run id. A claim is
    the turn's (Naveen at BOARD1a's plan): it is released when the turn ends, so a Parallel's branches claim
    different cards and a later step claims again.
    """

    kind: str  # workflow | schedule
    id: str
    holder: str

    @classmethod
    def of(cls, value: "BoardScope | dict | None") -> "BoardScope | None":
        if value is None or isinstance(value, BoardScope):
            return value
        kind, scope_id = str(value.get("kind") or ""), str(value.get("id") or "")
        if kind not in ("workflow", "schedule") or not scope_id:
            return None
        return cls(kind=kind, id=scope_id, holder=str(value.get("holder") or ""))


@dataclass
class TurnBoard:
    """A board as one turn has it. `board_id` is None for an own board nothing has written yet: it is made at
    its first write, so reading a workflow's empty board makes nothing."""

    board_id: str | None
    scope: str
    scope_id: str
    name: str
    access: str  # add | work
    own: bool

    @property
    def label(self) -> str:
        if self.own:
            return f"this {self.scope}'s board"
        return f"the shared board '{self.name}'"


@dataclass
class TurnBoards:
    boards: list[TurnBoard]
    holder: store.Holder
    actor: store.Actor
    run_id: str | None
    max_open: int
    #: Whether this turn holds a claim it should renew, and when it last did.
    holding: bool = False
    renewed_at: float = field(default_factory=time.monotonic)

    def work(self) -> list[TurnBoard]:
        return [b for b in self.boards if b.access == "work"]

    def add_only(self) -> list[TurnBoard]:
        return [b for b in self.boards if b.access == "add"]

    def for_board_id(self, board_id: str) -> TurnBoard | None:
        return next((b for b in self.boards if b.board_id and b.board_id == board_id), None)

    def pick(self, name: str | None) -> TurnBoard:
        """The board a tool call names: a shared board's name or id, or `this` for its own board. Unnamed, the
        own board, else the only one."""
        wanted = str(name or "").strip()
        if not wanted or wanted.lower() in ("this", "own", "mine"):
            own = next((b for b in self.boards if b.own), None)
            if own is not None:
                return own
            if len(self.boards) == 1:
                return self.boards[0]
            raise store.BadValue("Name the board: " + ", ".join(f"'{b.name}'" for b in self.boards) + ".")
        folded = wanted.casefold().strip("'\"")
        for b in self.boards:
            if (b.board_id and b.board_id == wanted) or (not b.own and b.name.casefold() == folded):
                return b
        names = ", ".join("'this'" if b.own else f"'{b.name}'" for b in self.boards)
        raise store.BadValue(f"This agent has no board called '{wanted}'. Its boards: {names}.")

    async def refresh(self) -> None:
        """Find an own board made since this turn began: by a Parallel's other branch, say, whose first add made
        it. One query per board still missing, none once found."""
        for board in self.boards:
            if board.own and board.board_id is None:
                found = await store.find_scoped_board(board.scope, board.scope_id)
                if found is not None:
                    board.board_id = found.id

    async def ensure(self, board: TurnBoard) -> str:
        """The board's id, making an own board at its first write."""
        if board.board_id is None:
            made = await store.get_or_create_scoped_board(board.scope, board.scope_id, by=self.actor)
            board.board_id = made.id
        return board.board_id


def _holder(scope: BoardScope | None, *, agent_id: str, agent_name: str, run_id: str | None,
            session_id: str) -> store.Holder:
    name = agent_name or agent_id
    if scope is not None and scope.kind == "workflow":
        key = scope.holder or f"{run_id or session_id}/step"
        step = key.rsplit("/", 1)[-1]
        return store.Holder(agent=agent_id, key=key, label=f"{name} (workflow run {run_id}, step {step})")
    if scope is not None and scope.kind == "schedule":
        key = scope.holder or str(run_id or session_id)
        return store.Holder(agent=agent_id, key=key, label=f"{name} (a scheduled run)")
    return store.Holder(agent=agent_id, key=f"session:{session_id}", label=f"{name} (a chat)")


async def turn_boards(*, agent: dict[str, Any], agent_id: str, source: str, run_id: str | None,
                      session_id: str, board_scope: "BoardScope | dict | None", settings: dict[str, Any],
                      tools_override: Any, persist_turn: bool, visitor: Any) -> TurnBoards | None:
    """The boards this turn has, or None. The switch is asked first, so while Boards are off nothing here
    touches the store. A store failure is a turn without boards and a warning, never a failed turn."""
    knobs = board_settings(settings)
    if not knobs.enabled:
        return None
    if visitor is not None or tools_override is not None or not persist_turn:
        return None
    scope = BoardScope.of(board_scope)
    own_on = ((agent.get("boards") or {}).get("own", True) is not False)
    try:
        boards: list[TurnBoard] = []
        if scope is not None and own_on:
            found = await store.find_scoped_board(scope.kind, scope.id)
            boards.append(TurnBoard(board_id=found.id if found else None, scope=scope.kind, scope_id=scope.id,
                                    name="", access="work", own=True))
        principals = [("agent", agent_id)]
        if scope is not None:
            principals.append((scope.kind, scope.id))
        shared = await store.shared_boards_for(principals)
    except Exception as exc:  # noqa: BLE001 — a board never fails a turn
        logger.warning("boards for agent %s not read; this turn has none: %s", agent_id, exc)
        return None
    if len(shared) > store.SHARED_PER_PRINCIPAL:
        logger.warning("agent %s is named on %d shared boards; it has the oldest %d: %s", agent_id, len(shared),
                       store.SHARED_PER_PRINCIPAL,
                       ", ".join(b.name for b, _ in shared[store.SHARED_PER_PRINCIPAL:]))
    for board, access in shared[:store.SHARED_PER_PRINCIPAL]:
        boards.append(TurnBoard(board_id=board.id, scope="shared", scope_id="", name=board.name, access=access,
                                own=False))
    if not boards:
        return None
    name = str(agent.get("name") or agent_id)
    return TurnBoards(
        boards=boards,
        holder=_holder(scope, agent_id=agent_id, agent_name=name, run_id=run_id, session_id=session_id),
        actor=store.Actor(agent=agent_id, agent_name=name, run=run_id or "", session=session_id, source=source),
        run_id=run_id,
        max_open=knobs.max_open_cards,
    )


async def release(holder_key: str, *, settings: dict[str, Any] | None, agent: str | None = None,
                  prefix: bool = False, why: str = "") -> int:
    """Release a finished turn's (or, with `prefix`, a finished run's) claims, while Boards are on. Never raises:
    a claim it misses expires on its own. A visitor's turn holds none, so it asks nothing."""
    from core import visitor

    if not holder_key or visitor.current() is not None or not board_settings(settings).enabled:
        return 0
    try:
        return await store.release_holder(holder_key, agent=agent, prefix=prefix, why=why)
    except Exception as exc:  # noqa: BLE001 — the lease covers a release that did not happen
        logger.warning("board claims of %s not released (they expire on their own): %s", holder_key, exc)
        return 0
