"""Boards: cards of shared work that agents and people read, claim and resolve (BOARD1a, D280; MEMORY 20 §5).

A board belongs to a **workflow** (one, lasting across its runs, each card stamped with the run that added it),
a **schedule** (whose cards tell its next firing what to do), a **run** (internal: a team's task list, TEAM1a),
or is a named **shared** board whose access list says which agents, workflows and schedules may `add` cards and
which may `work` them (read, claim and update). Which boards an agent sees is decided in `core/boards/resolve.py`;
this module only keeps them.

A card is a **task** (work: claimed, then closed done or failed with a result) or a **note** (information for
others: never claimed, edited by the version it was read at, closed when it is no longer true). The guards are
here, in code, so a person's write gets them as an agent's does:

* **Compare-and-set.** An update names the `version` it read; a card changed since is refused (`StaleCard`).
  A claim never bumps the version, so claiming a card does not make the claimer's own read stale.
* **Leases.** A claim holds until `claim_expires_at`; past it the card reads as open and anyone may claim it, so a
  crashed run frees its cards with nothing to sweep. The holder renews; its turn's end releases.
* **History.** Every change is a `board_card_events` row with the changed fields before and after; comments only
  append; nothing here deletes a card, and a person can undo a change (`undo`).
* **Provenance and taint.** Every write is stamped with who made it (agent, run, conversation, person) and when,
  and a write by a turn that had read content nobody typed marks the card `tainted` (F279).
* **No secrets.** Text holding a credential (`core.secret_scrub`) is refused (`SecretInCard`). A secret the workspace
  holds is masked by its value instead (F1307), so the builder's own work card still writes, and the card's events
  are built from the masked text.
* **Caps.** Open cards per board and shared boards per org, from the caller (settings in OSS, the plan in cloud);
  a `run` board has none.

Every query is scoped to `get_tenant()`. Each write transaction issues its UPDATE (or INSERT) first: on SQLite a
transaction that has read cannot then take the write lock (`core/knowledge/bases.py`), so a card is read in its
own session, checked, and then written with a WHERE that holds the read still true.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, case, delete, func, or_, select, true, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from core.secret_scrub import holds_credential
from core.store.ids import new_id
from core.tenancy import get_tenant

logger = logging.getLogger(__name__)

SCOPES = ("shared", "workflow", "schedule", "run", "assistant")
#: Where a build or a change stands on Synapse AI's board (scope `assistant`, SAI2-P1): open while it is being
#: drafted, waits on its person or on a connection; closed once built, changed or set aside.
STAGES = ("drafting", "waiting", "needs_connection", "built", "changed", "set_aside")
CLOSED_STAGES = ("built", "changed", "set_aside")
PRINCIPALS = ("agent", "workflow", "schedule")
ACCESS = ("add", "work")
KINDS = ("task", "note")
STATUSES = ("open", "claimed", "done", "archived")
OUTCOMES = ("succeeded", "failed")
EVENT_KINDS = ("created", "edited", "claimed", "released", "closed", "reopened", "comment", "undo", "archived")

TITLE_MAX = 200
BODY_MAX = 4000
RESULT_MAX = 4000
COMMENT_MAX = 2000
#: A card's typed object (Synapse AI's Plan / Change), as JSON: far more than a plan card holds.
DATA_MAX = 64000
KEY_MAX = 200
NAME_MAX = 120
#: The most shared boards an agent, a workflow or a schedule is given (C8).
SHARED_PER_PRINCIPAL = 3
#: How long a claim holds unless its holder renews it.
LEASE = timedelta(minutes=10)
#: A closed card is archived after this (still readable, never in a digest).
ARCHIVE_AFTER = timedelta(days=90)
#: How long a change can be undone; the change rows go after it, comments and creations stay.
HISTORY_FOR = timedelta(days=30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite hands timestamps back naive; everything stored is UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return _aware(dt).isoformat() if dt is not None else None


def _parse(value: str | None) -> datetime | None:
    return _aware(datetime.fromisoformat(value)) if value else None


# ── refusals ────────────────────────────────────────────────────────────────────


class BoardError(Exception):
    """A refusal. Its text is the one sentence that says why, said to the model or the person as it is."""


class NoSuchBoard(BoardError):
    pass


class NoSuchCard(BoardError):
    pass


class BadValue(BoardError):
    pass


class SecretInCard(BoardError):
    def __init__(self) -> None:
        super().__init__("Not saved: the text looks like it holds a secret (an API key, a token or a private key). "
                         "Leave the secret out and say where it is kept instead.")


class BoardFull(BoardError):
    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"This board has {limit:,} open cards, its limit: close some cards first.")


class TooManyBoards(BoardError):
    pass


class StaleCard(BoardError):
    def __init__(self, card: "Card") -> None:
        self.card = card
        super().__init__(f"Card {card.id} changed since you read it (it is now version {card.version}). "
                         "Read it again, then make your change against that version.")


class CardHeld(BoardError):
    def __init__(self, card: "Card") -> None:
        self.card = card
        until = card.claim_expires_at.strftime("%H:%M UTC") if card.claim_expires_at else "?"
        super().__init__(f"Card {card.id} is held by {card.holder_label or card.holder_agent} until {until}. "
                         "Pick another card, or leave this one to its holder.")


class NotClaimable(BoardError):
    pass


class CardClosed(BoardError):
    pass


class NothingToUndo(BoardError):
    pass


# ── values ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Actor:
    """Who wrote: an agent in a turn (its id and name, the run, the conversation, the surface) or a person."""

    agent: str = ""
    agent_name: str = ""
    run: str = ""
    session: str = ""
    person: str = ""
    source: str = ""

    def to_json(self) -> dict[str, str]:
        return {k: v for k, v in asdict(self).items() if v}


@dataclass(frozen=True)
class Holder:
    """Who holds a claim: an agent in one turn of work. `key` is the turn's — a workflow step's `run/step`, a
    schedule firing's run id, a conversation's `session:<id>` — and `label` is how others are told who it is."""

    agent: str
    key: str
    label: str = ""


@dataclass(frozen=True)
class Board:
    id: str
    scope: str
    scope_id: str
    name: str
    description: str
    created_by: dict[str, Any]
    created_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "created_at": _iso(self.created_at)}


@dataclass(frozen=True)
class Access:
    board_id: str
    principal_kind: str
    principal_id: str
    access: str


@dataclass(frozen=True)
class Card:
    """A card as it reads now: a claim past its expiry reads as open, with no holder."""

    id: str
    board_id: str
    kind: str
    title: str
    body: str
    status: str
    outcome: str | None
    result: str
    key: str | None
    version: int
    holder_agent: str | None
    holder_key: str | None
    holder_label: str | None
    claim_expires_at: datetime | None
    created_by: dict[str, Any]
    by_person: bool
    run_id: str | None
    tainted: bool
    created_at: datetime | None
    updated_at: datetime | None
    closed_at: datetime | None
    #: Synapse AI's board only (SAI2-P1): its stage, its typed Plan / Change, and the person it belongs to.
    stage: str | None = None
    data: dict[str, Any] | None = None
    owner_id: str | None = None

    def held_by(self, holder: Holder | None) -> bool:
        return (holder is not None and self.status == "claimed"
                and self.holder_key == holder.key and self.holder_agent == holder.agent)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        for name in ("claim_expires_at", "created_at", "updated_at", "closed_at"):
            out[name] = _iso(out[name])
        return out


@dataclass(frozen=True)
class CardEvent:
    card_id: str
    seq: int
    board_id: str
    kind: str
    by: dict[str, Any]
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    comment: str
    tainted: bool
    created_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "created_at": _iso(self.created_at)}


@dataclass
class DigestRows:
    """The digest's cards for each board, in reading order, and which boards had more than were read."""

    cards: dict[str, list[Card]] = field(default_factory=dict)
    more: set[str] = field(default_factory=set)


def _board(row) -> Board:
    return Board(id=row.id, scope=row.scope, scope_id=row.scope_id or "", name=row.name or "",
                 description=row.description or "", created_by=dict(row.created_by or {}),
                 created_at=_aware(row.created_at))


def _card(row, now: datetime | None = None) -> Card:
    now = now or _now()
    status, holder_agent, holder_key, holder_label = row.status, row.holder_agent, row.holder_key, row.holder_label
    expires = _aware(row.claim_expires_at)
    if status == "claimed" and (expires is None or expires <= now):
        status, holder_agent, holder_key, holder_label, expires = "open", None, None, None, None
    elif status != "claimed":
        holder_agent = holder_key = holder_label = None
        expires = None
    return Card(
        id=row.id, board_id=row.board_id, kind=row.kind, title=row.title, body=row.body or "", status=status,
        outcome=row.outcome, result=row.result or "", key=row.key, version=int(row.version or 1),
        holder_agent=holder_agent, holder_key=holder_key, holder_label=holder_label, claim_expires_at=expires,
        created_by=dict(row.created_by or {}), by_person=bool(row.by_person), run_id=row.run_id,
        tainted=bool(row.tainted), created_at=_aware(row.created_at), updated_at=_aware(row.updated_at),
        closed_at=_aware(row.closed_at), stage=row.stage, data=row.data, owner_id=row.owner_id,
    )


def _event(row) -> CardEvent:
    return CardEvent(card_id=row.card_id, seq=int(row.seq), board_id=row.board_id, kind=row.kind,
                     by=dict(row.by or {}), before=row.before, after=row.after, comment=row.comment or "",
                     tainted=bool(row.tainted), created_at=_aware(row.created_at))


def _text(value: Any, what: str, limit: int, *, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise BadValue(f"A card needs a {what}.")
    if len(text) > limit:
        raise BadValue(f"The {what} is {len(text):,} characters, more than the {limit:,} a card keeps. "
                       "Shorten it, or put the detail in a file and name the file.")
    if holds_credential(text):
        raise SecretInCard()
    return text


async def _held_masked(*parts: Any) -> tuple[Any, ...]:
    """`parts` (a card's title, body, key, result, data, a comment) with every secret the workspace holds masked by its
    value (F1307): this step's matcher and the door's finder alike, so a card written in a run or outside one never
    keeps one. Masked, not refused: the builder's own work card must still write (a deliberate change from F1307's
    first refusal)."""
    from core.secret_scrub import CREDENTIAL_MARK, held_in, scrub_held

    joined = "\n".join(p if isinstance(p, str) else json.dumps(p, ensure_ascii=False, default=str)
                       for p in parts if p)
    found = await held_in(joined) if joined else []

    def mask(value: Any) -> Any:
        value = scrub_held(value)
        if isinstance(value, str):
            for word, _ in found:
                value = value.replace(word, CREDENTIAL_MARK)
            return value
        if isinstance(value, dict):
            return {k: mask(v) for k, v in value.items()}
        if isinstance(value, list):
            return [mask(v) for v in value]
        return value

    return tuple(mask(p) for p in parts)


def _stage(value: Any) -> str | None:
    if value is None:
        return None
    if value not in STAGES:
        raise BadValue(f"A card's stage is one of {', '.join(STAGES)}.")
    return value


def _data(value: Any) -> dict[str, Any] | None:
    """A card's typed object: a JSON object, held to the same rule as its text (no credential), and capped."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise BadValue("A card's data is a JSON object.")
    text = json.dumps(value, ensure_ascii=False)
    if len(text) > DATA_MAX:
        raise BadValue(f"The card's data is {len(text):,} characters, more than the {DATA_MAX:,} a card keeps.")
    if holds_credential(text):
        raise SecretInCard()
    return value


def _key(value: Any) -> str | None:
    key = str(value or "").strip()
    if not key:
        return None
    if len(key) > KEY_MAX:
        raise BadValue(f"A card's key is at most {KEY_MAX} characters.")
    if holds_credential(key):
        raise SecretInCard()
    return key


# ── boards ──────────────────────────────────────────────────────────────────────


async def get_board(board_id: str) -> Board | None:
    from core.store import session
    from core.store.models import BoardDB

    async with session() as s:
        row = (await s.execute(select(BoardDB).where(
            BoardDB.tenant_id == get_tenant(), BoardDB.id == board_id))).scalars().first()
    return _board(row) if row is not None else None


async def find_scoped_board(scope: str, scope_id: str) -> Board | None:
    """The board of a workflow, a schedule or a run, if one was made."""
    from core.store import session
    from core.store.models import BoardDB

    async with session() as s:
        row = (await s.execute(select(BoardDB).where(
            BoardDB.tenant_id == get_tenant(), BoardDB.scope == scope, BoardDB.scope_id == scope_id,
            BoardDB.name == ""))).scalars().first()
    return _board(row) if row is not None else None


async def get_or_create_scoped_board(scope: str, scope_id: str, *, by: Actor | None = None) -> Board:
    """A workflow's, a schedule's or a run's board, or the workspace's one Synapse AI board (`assistant`, no
    scope id), made at its first write. Two first writes make one board."""
    from core.store import session
    from core.store.models import BoardDB

    if not ((scope in ("workflow", "schedule", "run") and scope_id) or (scope == "assistant" and not scope_id)):
        raise BadValue(f"No board scope {scope!r} for {scope_id!r}.")
    found = await find_scoped_board(scope, scope_id)
    if found is not None:
        return found
    try:
        async with session() as s:
            s.add(BoardDB(tenant_id=get_tenant(), id=new_id("brd"), scope=scope, scope_id=scope_id, name="",
                          description="", created_by=(by or Actor()).to_json()))
    except IntegrityError:
        pass  # made meanwhile by another first write
    found = await find_scoped_board(scope, scope_id)
    if found is None:
        raise BoardError(f"The {scope}'s board could not be made.")
    return found


async def list_boards(scope: str | None = None) -> list[Board]:
    from core.store import session
    from core.store.models import BoardDB

    stmt = select(BoardDB).where(BoardDB.tenant_id == get_tenant())
    if scope:
        stmt = stmt.where(BoardDB.scope == scope)
    async with session() as s:
        rows = (await s.execute(stmt.order_by(BoardDB.created_at, BoardDB.id))).scalars().all()
    return [_board(r) for r in rows]


async def create_shared_board(name: str, *, description: str = "", by: Actor | None = None,
                              max_shared: int = 0) -> Board:
    """A named shared board. `max_shared` is the org's limit (0: none); private boards never count."""
    from core.store import session
    from core.store.models import BoardDB

    name = str(name or "").strip()
    if not name or len(name) > NAME_MAX:
        raise BadValue(f"A shared board needs a name of at most {NAME_MAX} characters.")
    description = _text(description, "description", BODY_MAX)
    if max_shared:
        async with session() as s:
            have = (await s.execute(select(func.count()).select_from(BoardDB).where(
                BoardDB.tenant_id == get_tenant(), BoardDB.scope == "shared"))).scalar_one()
        if have >= max_shared:
            raise TooManyBoards(f"This workspace has {have} shared boards, its limit. Delete one first.")
    board_id = new_id("brd")
    try:
        async with session() as s:
            s.add(BoardDB(tenant_id=get_tenant(), id=board_id, scope="shared", scope_id="", name=name,
                          description=description, created_by=(by or Actor()).to_json()))
    except IntegrityError:
        raise BadValue(f"A shared board called '{name}' already exists.") from None
    return await get_board(board_id)  # type: ignore[return-value]


async def update_board(board_id: str, *, name: str | None = None, description: str | None = None) -> Board:
    from core.store import session
    from core.store.models import BoardDB

    values: dict[str, Any] = {"updated_at": _now()}
    if name is not None:
        name = str(name).strip()
        if not name or len(name) > NAME_MAX:
            raise BadValue(f"A shared board needs a name of at most {NAME_MAX} characters.")
        values["name"] = name
    if description is not None:
        values["description"] = _text(description, "description", BODY_MAX)
    try:
        async with session() as s:
            result = await s.execute(update(BoardDB).where(
                BoardDB.tenant_id == get_tenant(), BoardDB.id == board_id, BoardDB.scope == "shared",
            ).values(**values).execution_options(synchronize_session=False))
    except IntegrityError:
        raise BadValue(f"A shared board called '{name}' already exists.") from None
    if not result.rowcount:
        raise NoSuchBoard(f"There is no shared board {board_id}.")
    return await get_board(board_id)  # type: ignore[return-value]


async def delete_board(board_id: str) -> bool:
    """A person's delete of a whole board: its access list, cards and history go with it. Agents never call it."""
    from core.store import session
    from core.store.models import BoardAccessDB, BoardCardDB, BoardCardEventDB, BoardDB

    tenant = get_tenant()
    async with session() as s:
        gone = await s.execute(delete(BoardDB).where(BoardDB.tenant_id == tenant, BoardDB.id == board_id))
        for model in (BoardAccessDB, BoardCardDB, BoardCardEventDB):
            await s.execute(delete(model).where(model.tenant_id == tenant, model.board_id == board_id))
    return bool(gone.rowcount)


async def list_access(board_id: str) -> list[Access]:
    from core.store import session
    from core.store.models import BoardAccessDB

    async with session() as s:
        rows = (await s.execute(select(BoardAccessDB).where(
            BoardAccessDB.tenant_id == get_tenant(), BoardAccessDB.board_id == board_id,
        ).order_by(BoardAccessDB.principal_kind, BoardAccessDB.principal_id))).scalars().all()
    return [Access(r.board_id, r.principal_kind, r.principal_id, r.access) for r in rows]


async def set_access(board_id: str, entries: list[tuple[str, str, str]], *, by: Actor | None = None) -> list[Access]:
    """Replace a shared board's access list with `entries` of (kind, id, access). A principal that would then
    have more than `SHARED_PER_PRINCIPAL` shared boards is refused, naming it."""
    from core.store import session
    from core.store.models import BoardAccessDB, BoardDB

    board = await get_board(board_id)
    if board is None or board.scope != "shared":
        raise NoSuchBoard(f"There is no shared board {board_id}.")
    wanted: dict[tuple[str, str], str] = {}
    for kind, principal, access in entries:
        principal = str(principal or "").strip()
        if kind not in PRINCIPALS or not principal or access not in ACCESS:
            raise BadValue(f"An access entry is an agent, workflow or schedule with 'add' or 'work': "
                           f"not {kind!r} {principal!r} {access!r}.")
        if wanted.get((kind, principal)) != "work":  # the stronger of two entries for one principal
            wanted[(kind, principal)] = access
    tenant = get_tenant()
    if wanted:
        async with session() as s:
            rows = (await s.execute(
                select(BoardAccessDB.principal_kind, BoardAccessDB.principal_id, func.count())
                .join(BoardDB, and_(BoardDB.tenant_id == BoardAccessDB.tenant_id,
                                    BoardDB.id == BoardAccessDB.board_id))
                .where(BoardAccessDB.tenant_id == tenant, BoardAccessDB.board_id != board_id,
                       BoardDB.scope == "shared")
                .group_by(BoardAccessDB.principal_kind, BoardAccessDB.principal_id))).all()
        others = {(k, p): n for k, p, n in rows}
        for (kind, principal) in wanted:
            if others.get((kind, principal), 0) >= SHARED_PER_PRINCIPAL:
                raise TooManyBoards(f"The {kind} '{principal}' already has {SHARED_PER_PRINCIPAL} shared boards, "
                                    "the most it can have. Take it off one of them first.")
    async with session() as s:
        await s.execute(delete(BoardAccessDB).where(
            BoardAccessDB.tenant_id == tenant, BoardAccessDB.board_id == board_id))
        for (kind, principal), access in wanted.items():
            s.add(BoardAccessDB(tenant_id=tenant, board_id=board_id, principal_kind=kind, principal_id=principal,
                                access=access, created_by=(by or Actor()).to_json()))
    return await list_access(board_id)


async def shared_boards_for(principals: list[tuple[str, str]]) -> list[tuple[Board, str]]:
    """The shared boards whose access lists name any of `principals`, oldest first, each with the stronger
    access any of them has. One query: what resolution asks at the start of a turn."""
    from core.store import session
    from core.store.models import BoardAccessDB, BoardDB

    wanted = [(k, p) for k, p in principals if k in PRINCIPALS and p]
    if not wanted:
        return []
    tenant = get_tenant()
    async with session() as s:
        rows = (await s.execute(
            select(BoardDB, BoardAccessDB.access)
            .join(BoardAccessDB, and_(BoardAccessDB.tenant_id == BoardDB.tenant_id,
                                      BoardAccessDB.board_id == BoardDB.id))
            .where(BoardDB.tenant_id == tenant, BoardDB.scope == "shared",
                   or_(*[and_(BoardAccessDB.principal_kind == k, BoardAccessDB.principal_id == p)
                         for k, p in wanted]))
            .order_by(BoardDB.created_at, BoardDB.id))).all()
    out: dict[str, tuple[Board, str]] = {}
    for row, access in rows:
        have = out.get(row.id)
        if have is None:
            out[row.id] = (_board(row), access)
        elif access == "work":
            out[row.id] = (have[0], "work")
    return list(out.values())


# ── reading cards ───────────────────────────────────────────────────────────────


def _live_now(model, now: datetime):
    """A claim still in force."""
    return and_(model.status == "claimed", model.claim_expires_at > now)


def _status_filter(model, statuses: tuple[str, ...], now: datetime):
    parts = []
    for status in statuses:
        if status == "open":
            parts.append(or_(model.status == "open",
                             and_(model.status == "claimed",
                                  or_(model.claim_expires_at.is_(None), model.claim_expires_at <= now))))
        elif status == "claimed":
            parts.append(_live_now(model, now))
        elif status in ("done", "archived"):
            parts.append(model.status == status)
    return or_(*parts) if parts else model.status.in_(("open", "claimed"))


def _reading_order(model, run_id: str | None) -> list:
    """This run's cards first, then a person's, then tasks oldest first, then notes newest first."""
    order = []
    if run_id:
        order.append(case((model.run_id == run_id, 0), else_=1))
    order += [
        case((model.by_person.is_(True), 0), else_=1),
        case((model.kind == "task", 0), else_=1),
        case((model.kind == "task", model.created_at)).asc(),
        case((model.kind == "note", model.created_at)).desc(),
        model.id,
    ]
    return order


async def get_card(card_id: str, *, now: datetime | None = None) -> Card | None:
    from core.store import session
    from core.store.models import BoardCardDB

    async with session() as s:
        row = (await s.execute(select(BoardCardDB).where(
            BoardCardDB.tenant_id == get_tenant(), BoardCardDB.id == card_id))).scalars().first()
    return _card(row, now) if row is not None else None


async def _card_by_key(board_id: str, key: str) -> Card | None:
    from core.store import session
    from core.store.models import BoardCardDB

    async with session() as s:
        row = (await s.execute(select(BoardCardDB).where(
            BoardCardDB.tenant_id == get_tenant(), BoardCardDB.board_id == board_id,
            BoardCardDB.key == key))).scalars().first()
    return _card(row) if row is not None else None


async def list_cards(board_ids: list[str], *, statuses: tuple[str, ...] = ("open", "claimed"),
                     kind: str | None = None, run_id: str | None = None, only_run: bool = False,
                     query: str | None = None, limit: int = 50, now: datetime | None = None) -> list[Card]:
    """Cards on `board_ids` in reading order (`run_id`'s first). `only_run` keeps that run's alone."""
    from core.store import session
    from core.store.models import BoardCardDB

    if not board_ids:
        return []
    now = now or _now()
    stmt = select(BoardCardDB).where(
        BoardCardDB.tenant_id == get_tenant(), BoardCardDB.board_id.in_(board_ids),
        _status_filter(BoardCardDB, statuses, now))
    if kind in KINDS:
        stmt = stmt.where(BoardCardDB.kind == kind)
    if only_run and run_id:
        stmt = stmt.where(BoardCardDB.run_id == run_id)
    if query:
        needle = str(query).strip().lower()
        stmt = stmt.where(or_(func.lower(BoardCardDB.title).contains(needle, autoescape=True),
                              func.lower(BoardCardDB.body).contains(needle, autoescape=True),
                              func.lower(BoardCardDB.key).contains(needle, autoescape=True)))
    stmt = stmt.order_by(*_reading_order(BoardCardDB, run_id)).limit(max(1, min(int(limit), 200)))
    async with session() as s:
        rows = (await s.execute(stmt)).scalars().all()
    return [_card(r, now) for r in rows]


async def digest_cards(board_ids: list[str], *, run_id: str | None, per_board: int,
                       now: datetime | None = None) -> DigestRows:
    """Each board's open cards in reading order, at most `per_board` each, in one query (a window per board).
    A board with more than that is named in `more`."""
    from core.store import session
    from core.store.models import BoardCardDB

    out = DigestRows()
    if not board_ids:
        return out
    now = now or _now()
    rank = func.row_number().over(partition_by=BoardCardDB.board_id,
                                  order_by=_reading_order(BoardCardDB, run_id)).label("pos")
    inner = (select(BoardCardDB, rank)
             .where(BoardCardDB.tenant_id == get_tenant(), BoardCardDB.board_id.in_(board_ids),
                    BoardCardDB.status.in_(("open", "claimed")))
             .subquery())
    row_of = aliased(BoardCardDB, inner)
    async with session() as s:
        rows = (await s.execute(select(row_of, inner.c.pos).where(inner.c.pos <= per_board + 1)
                                .order_by(inner.c.board_id, inner.c.pos))).all()
    for row, position in rows:
        if position > per_board:
            out.more.add(row.board_id)
            continue
        out.cards.setdefault(row.board_id, []).append(_card(row, now))
    return out


async def held_cards(holder: Holder, *, now: datetime | None = None) -> list[Card]:
    """The cards `holder` holds now, oldest first: what a turn near its end is told it still holds (F582). Asked
    only by a turn that has claimed something, on its last calls."""
    from core.store import session
    from core.store.models import BoardCardDB

    now = now or _now()
    async with session() as s:
        rows = (await s.execute(
            select(BoardCardDB).where(BoardCardDB.tenant_id == get_tenant(), BoardCardDB.status == "claimed",
                                      BoardCardDB.holder_key == holder.key, BoardCardDB.holder_agent == holder.agent)
            .order_by(BoardCardDB.created_at, BoardCardDB.id))).scalars().all()
    return [_card(row, now) for row in rows]


async def live_count(board_id: str) -> int:
    """Open and claimed cards on a board: what its cap counts."""
    from core.store import session
    from core.store.models import BoardCardDB

    async with session() as s:
        return int((await s.execute(select(func.count()).select_from(BoardCardDB).where(
            BoardCardDB.tenant_id == get_tenant(), BoardCardDB.board_id == board_id,
            BoardCardDB.status.in_(("open", "claimed"))))).scalar_one())


async def live_counts(board_ids: list[str], *, now: datetime | None = None) -> dict[str, tuple[int, int]]:
    """Each board's (open, claimed) cards in one query: the boards list's counts (BOARD1b). A claim that lapsed
    counts as open, as it reads. A board with none is left out."""
    from core.store import session
    from core.store.models import BoardCardDB

    if not board_ids:
        return {}
    now = now or _now()
    held = func.sum(case((_live_now(BoardCardDB, now), 1), else_=0))
    async with session() as s:
        rows = (await s.execute(
            select(BoardCardDB.board_id, func.count(), held)
            .where(BoardCardDB.tenant_id == get_tenant(), BoardCardDB.board_id.in_(board_ids),
                   BoardCardDB.status.in_(("open", "claimed")))
            .group_by(BoardCardDB.board_id))).all()
    return {board_id: (int(total) - int(claimed or 0), int(claimed or 0)) for board_id, total, claimed in rows}


async def comments(card_id: str, *, limit: int = 20) -> list[CardEvent]:
    """A card's comments, newest first, without any a person took back."""
    events = await history(card_id, limit=500)
    taken_back = {int((e.after or {}).get("undoes") or 0) for e in events if e.kind == "undo"}
    return [e for e in events if e.kind == "comment" and e.seq not in taken_back][:limit]


async def history(card_id: str, *, limit: int = 100) -> list[CardEvent]:
    """A card's history, newest first."""
    from core.store import session
    from core.store.models import BoardCardEventDB

    async with session() as s:
        rows = (await s.execute(select(BoardCardEventDB).where(
            BoardCardEventDB.tenant_id == get_tenant(), BoardCardEventDB.card_id == card_id,
        ).order_by(BoardCardEventDB.seq.desc()).limit(max(1, int(limit))))).scalars().all()
    return [_event(r) for r in rows]


async def board_history(board_id: str, *, since: datetime | None = None, limit: int = 200) -> list[CardEvent]:
    """Every card's history on a board, newest first (a board's own page)."""
    from core.store import session
    from core.store.models import BoardCardEventDB

    stmt = select(BoardCardEventDB).where(
        BoardCardEventDB.tenant_id == get_tenant(), BoardCardEventDB.board_id == board_id)
    if since is not None:
        stmt = stmt.where(BoardCardEventDB.created_at >= since)
    async with session() as s:
        rows = (await s.execute(stmt.order_by(BoardCardEventDB.created_at.desc(), BoardCardEventDB.seq.desc())
                                .limit(max(1, int(limit))))).scalars().all()
    return [_event(r) for r in rows]


# ── writing cards ───────────────────────────────────────────────────────────────


def _held_clause(model, holder: Holder | None, now: datetime):
    """The card is not held by anyone else right now. A person (no holder) is never held off."""
    if holder is None:
        return true()
    return or_(model.status != "claimed", model.claim_expires_at.is_(None), model.claim_expires_at <= now,
               and_(model.holder_key == holder.key, model.holder_agent == holder.agent))


async def _write(card_id: str, where: list, values: dict[str, Any],
                 event: dict[str, Any]) -> tuple[int, str] | None:
    """One card write and its history row, in one transaction: the UPDATE (bumping `event_seq`) first, then the
    row it numbers. The new `event_seq` and the card's board, or None when the WHERE no longer held."""
    from core.store import session
    from core.store.models import BoardCardDB, BoardCardEventDB

    tenant = get_tenant()
    async with session() as s:
        seq = (await s.execute(
            update(BoardCardDB)
            .where(BoardCardDB.tenant_id == tenant, BoardCardDB.id == card_id, *where)
            .values(event_seq=BoardCardDB.event_seq + 1, **values)
            .returning(BoardCardDB.event_seq, BoardCardDB.board_id)
            .execution_options(synchronize_session=False))).first()
        if seq is None:
            return None
        s.add(BoardCardEventDB(tenant_id=tenant, card_id=card_id, seq=int(seq[0]), board_id=seq[1], **event))
    return int(seq[0]), str(seq[1])


async def _explain(card_id: str, holder: Holder | None, version: int | None) -> BoardError:
    """Why a write's WHERE did not hold, read back."""
    card = await get_card(card_id)
    if card is None:
        return NoSuchCard(f"There is no card {card_id}.")
    if card.status == "archived":
        return CardClosed(f"Card {card_id} is archived. A person can bring it back from the board's history.")
    if version is not None and card.version != version:
        return StaleCard(card)
    if card.status == "claimed" and not card.held_by(holder):
        return CardHeld(card)
    return StaleCard(card)


async def add_card(board_id: str, *, title: str, body: str = "", kind: str = "task", key: str | None = None,
                   by: Actor, tainted: bool = False, max_open: int = 0, stage: str | None = None,
                   data: dict[str, Any] | None = None, owner_id: str | None = None) -> tuple[Card, bool]:
    """Add a card, or, when `key` is already on the board, return that card unchanged. Whether it was added.

    `max_open` is the board's cap on open cards (0: none; a `run` board has none). Two adds of one key at once
    make one card: the second's insert meets the first's key and returns it (first write wins).
    """
    from core.store import session
    from core.store.models import BoardCardDB, BoardCardEventDB

    if kind not in KINDS:
        raise BadValue("A card is a 'task' (work to do) or a 'note' (information for others).")
    title, body, key, data = await _held_masked(title, body, key, data)
    title = _text(title, "title", TITLE_MAX, required=True)
    body = _text(body, "body", BODY_MAX)
    key = _key(key)
    stage, data = _stage(stage), _data(data)
    board = await get_board(board_id)
    if board is None:
        raise NoSuchBoard(f"There is no board {board_id}.")
    if key:
        existing = await _card_by_key(board_id, key)
        if existing is not None:
            return existing, False
    if max_open and board.scope != "run" and await live_count(board_id) >= max_open:
        raise BoardFull(max_open)
    tenant, now = get_tenant(), _now()
    for attempt in (1, 2):
        card_id = new_id("card")
        try:
            async with session() as s:
                s.add(BoardCardDB(
                    tenant_id=tenant, id=card_id, board_id=board_id, kind=kind, title=title, body=body,
                    status="open", result="", key=key, version=1, created_by=by.to_json(),
                    by_person=bool(by.person), run_id=by.run or None, tainted=bool(tainted), event_seq=1,
                    created_at=now, updated_at=now, stage=stage, data=data, owner_id=owner_id or None))
                await s.flush()
                s.add(BoardCardEventDB(
                    tenant_id=tenant, card_id=card_id, seq=1, board_id=board_id, kind="created", by=by.to_json(),
                    after={"kind": kind, "title": title, "body": body, "key": key,
                           **({"stage": stage} if stage else {})}, tainted=bool(tainted),
                    created_at=now))
        except IntegrityError:
            if key:
                existing = await _card_by_key(board_id, key)
                if existing is not None:
                    return existing, False
            if attempt == 2:
                raise
            continue
        break
    card = await get_card(card_id)
    assert card is not None
    return card, True


async def update_card(card_id: str, *, version: int, by: Actor, holder: Holder | None, tainted: bool = False,
                      title: str | None = None, body: str | None = None, status: str | None = None,
                      result: str | None = None, stage: str | None = None,
                      data: dict[str, Any] | None = None, needs_result: bool = False) -> Card:
    """Change a card read at `version`: its title or body, its result, or its status — `done` (a task succeeded,
    or a note is no longer true), `failed` (a task, with the reason as `result`), or `open` again.

    Refused when the card changed since (`StaleCard`) or someone else holds it (`CardHeld`). Closing releases
    the holder's claim. `holder` None is a person, whom no claim holds off.
    """
    title, body, result, data = await _held_masked(title, body, result, data)
    card = await get_card(card_id)
    if card is None:
        raise NoSuchCard(f"There is no card {card_id}.")
    if card.status == "archived":
        raise CardClosed(f"Card {card_id} is archived. A person can bring it back from the board's history.")
    if int(version) != card.version:
        raise StaleCard(card)
    if card.status == "claimed" and holder is not None and not card.held_by(holder):
        raise CardHeld(card)

    now = _now()
    after: dict[str, Any] = {}
    if title is not None:
        after["title"] = _text(title, "title", TITLE_MAX, required=True)
    if body is not None:
        after["body"] = _text(body, "body", BODY_MAX)
    if result is not None:
        if card.kind == "note":
            raise BadValue("A note takes no result: change its body, or close it when it is no longer true.")
        after["result"] = _text(result, "result", RESULT_MAX)
    if stage is not None:
        after["stage"] = _stage(stage)
    # The typed object is written, never kept in the history: an undo restores what the history holds, and an old
    # copy of a plan must never come back in place of the live one (SAI2-P1).
    new_data = _data(data) if data is not None else None
    data_changed = new_data is not None and new_data != card.data
    closing = False
    if status is not None:
        if status == "done":
            if card.status == "done":
                raise CardClosed(f"Card {card_id} is already done.")
            # An agent's door (F862): its description asks for the result, and a task closed without one read Done
            # with nothing in it. A person's Done keeps it optional.
            if needs_result and card.kind == "task" and not str(after.get("result") or card.result or "").strip():
                raise BadValue("A task closes with its result: put what was found or done in `result` "
                               "(a comment is not its result).")
            after.update(status="done", outcome="succeeded" if card.kind == "task" else None, closed_at=_iso(now))
            closing = True
        elif status == "failed":
            if card.kind == "note":
                raise BadValue("A note cannot fail: close it ('done') when it is no longer true.")
            if card.status == "done":
                raise CardClosed(f"Card {card_id} is already done.")
            if not (after.get("result") or card.result):
                raise BadValue("Say why it failed: put the reason in `result`.")
            after.update(status="done", outcome="failed", closed_at=_iso(now))
            closing = True
        elif status == "open":
            if card.status in ("open", "claimed"):
                raise BadValue(f"Card {card_id} is already open.")
            after.update(status="open", outcome=None, closed_at=None)
        else:
            raise BadValue("A card's status is set to 'done', 'failed' or 'open'.")
    # Only what changes is written and kept; a status always changes (an unchanged one is refused above).
    after = {k: v for k, v in after.items()
             if k == "status" or getattr(card, k) != (_parse(v) if k == "closed_at" else v)}
    if not after and not data_changed:
        raise BadValue("Nothing to change: name a new title, body, result or status.")
    before = {k: (_iso(card.closed_at) if k == "closed_at" else getattr(card, k)) for k in after}
    if before.get("status") == "claimed":
        before["status"] = "open"  # an undo reopens; it never gives a claim back

    values: dict[str, Any] = {k: (_parse(v) if k == "closed_at" else v) for k, v in after.items()}
    if data_changed:
        values["data"] = new_data
    values.update(version=card.version + 1, updated_at=now)
    if tainted:
        values["tainted"] = True
    if closing or after.get("status") == "open":
        values.update(holder_agent=None, holder_key=None, holder_label=None, claim_expires_at=None)
    from core.store.models import BoardCardDB

    seq = await _write(card_id, [BoardCardDB.version == card.version, BoardCardDB.status != "archived",
                                 _held_clause(BoardCardDB, holder, now)], values, {
        "kind": "closed" if closing else ("reopened" if after.get("status") == "open" else "edited"),
        "by": by.to_json(), "before": before, "after": after, "tainted": bool(tainted), "created_at": now})
    if seq is None:
        raise await _explain(card_id, holder, card.version)
    fresh = await get_card(card_id)
    assert fresh is not None
    return fresh


async def claim(card_id: str, holder: Holder, *, by: Actor, lease: timedelta = LEASE) -> tuple[Card, bool]:
    """Claim a task until now + `lease`, atomically: one UPDATE that holds only while the card is open, its
    claim has expired, or it is already this holder's (then it is renewed). Whether it was newly claimed."""
    from core.store.models import BoardCardDB

    card = await get_card(card_id)
    if card is None:
        raise NoSuchCard(f"There is no card {card_id}.")
    if card.kind == "note":
        raise NotClaimable(f"Card {card_id} is a note: it has nothing to claim. Read it, comment on it, or "
                           "update it with the version you read.")
    if card.status in ("done", "archived"):
        raise CardClosed(f"Card {card_id} is {card.status}: there is nothing to claim. Reopen it first if it "
                         "needs more work.")
    now = _now()
    until = now + lease
    if card.held_by(holder):
        await renew_holder(holder, lease=lease, card_id=card_id)
        fresh = await get_card(card_id)
        return fresh or card, False
    seq = await _write(card_id, [
        BoardCardDB.kind == "task", BoardCardDB.status.in_(("open", "claimed")),
        or_(BoardCardDB.status == "open", BoardCardDB.claim_expires_at.is_(None),
            BoardCardDB.claim_expires_at <= now,
            and_(BoardCardDB.holder_key == holder.key, BoardCardDB.holder_agent == holder.agent)),
    ], {"status": "claimed", "holder_agent": holder.agent, "holder_key": holder.key,
        "holder_label": holder.label or holder.agent, "claim_expires_at": until, "updated_at": now}, {
        "kind": "claimed", "by": by.to_json(), "after": {"holder": holder.label or holder.agent,
                                                          "until": _iso(until)}, "created_at": now})
    if seq is None:
        raise await _explain(card_id, holder, None)
    fresh = await get_card(card_id)
    assert fresh is not None
    return fresh, True


async def release(card_id: str, holder: Holder, *, by: Actor, why: str = "") -> Card:
    """Give a claim back: the card reads as open again."""
    from core.store.models import BoardCardDB

    now = _now()
    seq = await _write(card_id, [BoardCardDB.status == "claimed", BoardCardDB.holder_key == holder.key,
                                 BoardCardDB.holder_agent == holder.agent],
                       {"status": "open", "holder_agent": None, "holder_key": None, "holder_label": None,
                        "claim_expires_at": None, "updated_at": now},
                       {"kind": "released", "by": by.to_json(), "comment": why, "created_at": now})
    if seq is None:
        card = await get_card(card_id)
        if card is None:
            raise NoSuchCard(f"There is no card {card_id}.")
        raise BadValue(f"You do not hold card {card_id}.")
    fresh = await get_card(card_id)
    assert fresh is not None
    return fresh


async def renew_holder(holder: Holder, *, lease: timedelta = LEASE, card_id: str | None = None) -> int:
    """Extend every claim this holder still has in force (or just `card_id`'s) to now + `lease`. Not history:
    one UPDATE, how many it renewed."""
    from core.store import session
    from core.store.models import BoardCardDB

    now = _now()
    stmt = update(BoardCardDB).where(
        BoardCardDB.tenant_id == get_tenant(), BoardCardDB.holder_key == holder.key,
        BoardCardDB.holder_agent == holder.agent, _live_now(BoardCardDB, now))
    if card_id:
        stmt = stmt.where(BoardCardDB.id == card_id)
    async with session() as s:
        result = await s.execute(stmt.values(claim_expires_at=now + lease)
                                 .execution_options(synchronize_session=False))
    return int(result.rowcount or 0)


async def release_holder(holder_key: str, *, agent: str | None = None, prefix: bool = False,
                         why: str = "") -> int:
    """Release every claim held under `holder_key` (or, with `prefix`, under any key starting `holder_key/`: a
    run's steps), each with a history row. What a turn's or a run's end calls; how many it released."""
    from core.store import session
    from core.store.models import BoardCardDB, BoardCardEventDB

    if not holder_key:
        return 0
    tenant, now = get_tenant(), _now()
    match = (BoardCardDB.holder_key.startswith(f"{holder_key}/", autoescape=True) if prefix
             else BoardCardDB.holder_key == holder_key)
    where = [BoardCardDB.tenant_id == tenant, BoardCardDB.status == "claimed", match]
    if agent:
        where.append(BoardCardDB.holder_agent == agent)
    async with session() as s:
        rows = (await s.execute(
            update(BoardCardDB).where(*where)
            .values(status="open", holder_agent=None, holder_key=None, holder_label=None, claim_expires_at=None,
                    event_seq=BoardCardDB.event_seq + 1, updated_at=now)
            .returning(BoardCardDB.id, BoardCardDB.board_id, BoardCardDB.event_seq)
            .execution_options(synchronize_session=False))).all()
        for card_id, board_id, seq in rows:
            s.add(BoardCardEventDB(tenant_id=tenant, card_id=card_id, seq=int(seq), board_id=board_id,
                                   kind="released", by={"source": "engine"}, comment=why, created_at=now))
    return len(rows)


async def add_comment(card_id: str, text: str, *, by: Actor, tainted: bool = False) -> CardEvent:
    """Append a comment. Comments are never edited or removed by an agent; a person can take one back (undo)."""
    from core.store.models import BoardCardDB

    (text,) = await _held_masked(text)
    words = _text(text, "comment", COMMENT_MAX, required=True)
    now = _now()
    values: dict[str, Any] = {"updated_at": now}
    if tainted:
        values["tainted"] = True
    wrote = await _write(card_id, [BoardCardDB.status != "archived"], values,
                         {"kind": "comment", "by": by.to_json(), "comment": words, "tainted": bool(tainted),
                          "created_at": now})
    if wrote is None:
        raise await _explain(card_id, None, None)
    return CardEvent(card_id=card_id, seq=wrote[0], board_id=wrote[1], kind="comment", by=by.to_json(),
                     before=None, after=None, comment=words, tainted=bool(tainted), created_at=now)


async def undo(card_id: str, seq: int, *, by: Actor) -> Card:
    """A person takes back one change from a card's history: an edit, a close or a reopen is restored if the
    card still reads as that change left it; an add archives the card; a comment is taken back. Within
    `HISTORY_FOR`."""
    from core.store import session
    from core.store.models import BoardCardDB, BoardCardEventDB

    async with session() as s:
        row = (await s.execute(select(BoardCardEventDB).where(
            BoardCardEventDB.tenant_id == get_tenant(), BoardCardEventDB.card_id == card_id,
            BoardCardEventDB.seq == int(seq)))).scalars().first()
    if row is None:
        raise NothingToUndo(f"Card {card_id} has no change {seq}.")
    event = _event(row)
    now = _now()
    if event.created_at is not None and now - event.created_at > HISTORY_FOR:
        raise NothingToUndo(f"That change is older than {HISTORY_FOR.days} days, too old to undo.")
    card = await get_card(card_id)
    if card is None:
        raise NoSuchCard(f"There is no card {card_id}.")
    record = {"kind": "undo", "by": by.to_json(), "created_at": now}
    if event.kind == "comment":
        if any(e.kind == "undo" and int((e.after or {}).get("undoes") or 0) == event.seq
               for e in await history(card_id, limit=500)):
            raise NothingToUndo("That comment was already taken back.")
        seq_new = await _write(card_id, [], {"updated_at": now}, {**record, "after": {"undoes": event.seq}})
    elif event.kind == "created":
        if card.status == "archived":
            raise NothingToUndo(f"Card {card_id} is already archived.")
        seq_new = await _write(card_id, [BoardCardDB.version == card.version],
                               {"status": "archived", "holder_agent": None, "holder_key": None,
                                "holder_label": None, "claim_expires_at": None, "version": card.version + 1,
                                "updated_at": now, "closed_at": card.closed_at or now},
                               {**record, "before": {"status": card.status}, "after": {"undoes": event.seq,
                                                                                         "status": "archived"}})
    elif event.kind in ("edited", "closed", "reopened"):
        after, before = dict(event.after or {}), dict(event.before or {})
        for name, value in after.items():
            now_value = _iso(card.closed_at) if name == "closed_at" else getattr(card, name)
            if name == "status" and now_value == "claimed":
                now_value = "open"
            if now_value != value:
                raise StaleCard(card)
        values = {k: (_parse(v) if k == "closed_at" else v) for k, v in before.items()}
        values.update(version=card.version + 1, updated_at=now)
        seq_new = await _write(card_id, [BoardCardDB.version == card.version], values,
                               {**record, "before": after, "after": {**before, "undoes": event.seq}})
    else:
        raise NothingToUndo(f"A {event.kind} cannot be undone.")
    if seq_new is None:
        raise await _explain(card_id, None, card.version)
    fresh = await get_card(card_id)
    assert fresh is not None
    return fresh


async def sweep(*, now: datetime | None = None) -> tuple[int, int]:
    """Archive cards closed more than `ARCHIVE_AFTER` ago, and drop change rows older than `HISTORY_FOR`
    (creations, comments and undos stay: they are what the card says). How many of each."""
    from core.store import session
    from core.store.models import BoardCardDB, BoardCardEventDB

    now = now or _now()
    tenant = get_tenant()
    async with session() as s:
        archived = await s.execute(
            update(BoardCardDB).where(BoardCardDB.tenant_id == tenant, BoardCardDB.status == "done",
                                      BoardCardDB.closed_at < now - ARCHIVE_AFTER)
            .values(status="archived").execution_options(synchronize_session=False))
        dropped = await s.execute(delete(BoardCardEventDB).where(
            BoardCardEventDB.tenant_id == tenant, BoardCardEventDB.created_at < now - HISTORY_FOR,
            BoardCardEventDB.kind.notin_(("created", "comment", "undo"))))
    return int(archived.rowcount or 0), int(dropped.rowcount or 0)


#: A team's run board left by a turn that never finished (a crash, a restart) goes after this long idle (TEAM1a).
RUN_BOARD_IDLE = timedelta(hours=24)


async def sweep_run_boards(*, now: datetime | None = None) -> int:
    """Delete `run` boards (a team's task list, TEAM1a) whose last change is older than `RUN_BOARD_IDLE`.

    A team deletes its own board when its turn ends; a parked turn keeps it. This is for the ones nothing came
    back to. Run by a team when it makes its board, never while Boards are off and no team works. How many.
    """
    from core.store import session
    from core.store.models import BoardCardDB, BoardDB

    now = now or _now()
    cutoff = now - RUN_BOARD_IDLE
    tenant = get_tenant()
    async with session() as s:
        boards = (await s.execute(select(BoardDB.id).where(
            BoardDB.tenant_id == tenant, BoardDB.scope == "run", BoardDB.updated_at < cutoff))).scalars().all()
        stale = []
        for board_id in boards:
            latest = (await s.execute(select(func.max(BoardCardDB.updated_at)).where(
                BoardCardDB.tenant_id == tenant, BoardCardDB.board_id == board_id))).scalar()
            if latest is None or _aware(latest) < cutoff:
                stale.append(board_id)
    for board_id in stale:
        await delete_board(board_id)
    return len(stale)

