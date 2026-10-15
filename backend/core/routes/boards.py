"""Boards for people: the boards list, a board with its cards, shared boards and their access lists, a person's
cards, comments, history and undo (BOARD1b, D282; MEMORY 20 §5, C7).

Boards are off until a person switches them on in Settings: while `boards_enabled` is false every route here
answers 404, as if it did not exist, and none reads the store. The shapes are `core.boards.service`'s, which
cloud's routes serve too.

A person's write is a person's (`service.person`): no claim holds it off, and it always names the version the
person read, so an agent's change in between is never written over.
"""
from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field


def _require_enabled() -> None:
    from core.boards.config import boards_enabled

    if not boards_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


router = APIRouter(dependencies=[Depends(_require_enabled)])


def _errors(handler: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """The store's refusals as HTTP answers; each one's text is the sentence to show."""
    from core.boards.service import WrongScope
    from core.store import boards as store

    conflicts = (store.StaleCard, store.CardHeld, store.CardClosed, store.BoardFull, store.TooManyBoards,
                 store.NothingToUndo, WrongScope)

    @functools.wraps(handler)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return await handler(*args, **kwargs)
        except (store.NoSuchBoard, store.NoSuchCard) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except conflicts as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except store.BoardError as exc:  # BadValue, SecretInCard, NotClaimable
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return wrapped


def _person(request: Request) -> str:
    """Who is writing, as the card's history names them: the signed-in user, else the workspace owner's name
    from Personal details (read by the caller), else "You"."""
    from core.internal_auth import SESSION_COOKIE, SESSION_HEADER
    from core.user_auth import ACCESS_SUBJECT, session_claims

    token = request.headers.get(SESSION_HEADER) or request.cookies.get(SESSION_COOKIE) or ""
    sub = str((session_claims(token) or {}).get("sub") or "") if token else ""
    return sub if sub and sub != ACCESS_SUBJECT else ""


async def _actor(request: Request):
    from core.boards.service import person

    name = _person(request)
    if not name:
        from core.personal_details import load_personal_details

        details = await load_personal_details() or {}
        name = " ".join(p for p in (str(details.get("first_name") or "").strip(),
                                    str(details.get("last_name") or "").strip()) if p)
    return person(name or "You")


async def _directory():
    from core.boards.service import Directory
    from core.routes.orchestrations import load_orchestrations
    from core.store import schedules
    from core.store.resources import load_agents

    agents = {str(a["id"]): str(a.get("name") or a["id"]) for a in await load_agents() if a.get("id")}
    workflows = {str(o["id"]): str(o.get("name") or o["id"]) for o in await load_orchestrations() if o.get("id")}
    rows = {str(s["id"]): {"name": s.get("name") or s["id"], "target_type": s.get("target_type") or "agent",
                           "target_id": s.get("target_id")} for s in await schedules.load() if s.get("id")}
    return Directory(agents=agents, workflows=workflows, schedules=rows)


async def _current_run(board) -> dict[str, Any] | None:
    """A workflow's board's current run: the workflow's latest."""
    if board is None or board.scope != "workflow":
        return None
    from core.orchestration.state import SharedState

    runs = await SharedState.list_runs(limit=1, orchestration_ids={board.scope_id})
    if not runs:
        return None
    run = runs[0]
    return {"run_id": run["run_id"], "status": run.get("status"), "started_at": run.get("started_at")}


def _caps():
    from core.boards.config import board_settings

    return board_settings()


# ── bodies ─────────────────────────────────────────────────────────────────────

class SharedBoardIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)


class SharedBoardPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)


class AccessEntry(BaseModel):
    kind: Literal["agent", "workflow", "schedule"]
    id: str = Field(min_length=1)
    access: Literal["add", "work"]


class AccessIn(BaseModel):
    entries: list[AccessEntry] = Field(default_factory=list)


class CardIn(BaseModel):
    title: str = Field(min_length=1)
    body: str = ""
    kind: Literal["task", "note"] = "task"
    key: str | None = None


class CardPatch(BaseModel):
    version: int
    title: str | None = None
    body: str | None = None
    status: Literal["open", "done", "failed"] | None = None
    result: str | None = None


class CommentIn(BaseModel):
    text: str = Field(min_length=1)


class UndoIn(BaseModel):
    seq: int


# ── boards ─────────────────────────────────────────────────────────────────────

@router.get("/api/boards")
@_errors
async def list_boards():
    from core.boards import service

    return await service.boards_list(await _directory(), max_shared=_caps().max_shared)


@router.post("/api/boards", status_code=201)
@_errors
async def create_board(body: SharedBoardIn, request: Request):
    from core.boards import service

    return await service.create_shared(body.name, description=body.description, by=await _actor(request),
                                       max_shared=_caps().max_shared)


@router.get("/api/boards/scoped/{kind}/{scope_id}")
@_errors
async def scoped_board(kind: Literal["workflow", "schedule"], scope_id: str, run: Literal["all", "this"] = "all",
                       q: str | None = None):
    from core.boards import service
    from core.store import boards as store

    found = await store.find_scoped_board(kind, scope_id)
    return await service.scoped_view(kind, scope_id, await _directory(), current_run=await _current_run(found),
                                     only_this_run=run == "this", query=q)


@router.post("/api/boards/scoped/{kind}/{scope_id}/cards", status_code=201)
@_errors
async def add_scoped_card(kind: Literal["workflow", "schedule"], scope_id: str, body: CardIn, request: Request):
    from core.boards import service

    return await service.add_scoped_card(kind, scope_id, directory=await _directory(), title=body.title,
                                         body=body.body, kind=body.kind, key=body.key, by=await _actor(request),
                                         max_open=_caps().max_open_cards)


@router.get("/api/boards/for/{kind}/{principal_id}")
@_errors
async def boards_for(kind: Literal["agent", "workflow", "schedule"], principal_id: str):
    from core.boards import service

    return {"boards": await service.boards_for(kind, principal_id, await _directory())}


@router.get("/api/boards/cards/{card_id}")
@_errors
async def get_card(card_id: str):
    from core.boards import service

    return await service.card_view(card_id)


@router.patch("/api/boards/cards/{card_id}")
@_errors
async def update_card(card_id: str, body: CardPatch, request: Request):
    from core.boards import service

    return await service.update_card(card_id, version=body.version, by=await _actor(request), title=body.title,
                                     body=body.body, status=body.status, result=body.result)


@router.post("/api/boards/cards/{card_id}/comments", status_code=201)
@_errors
async def add_comment(card_id: str, body: CommentIn, request: Request):
    from core.boards import service

    return await service.comment(card_id, body.text, by=await _actor(request))


@router.post("/api/boards/cards/{card_id}/undo")
@_errors
async def undo_change(card_id: str, body: UndoIn, request: Request):
    from core.boards import service

    return await service.undo(card_id, body.seq, by=await _actor(request))


@router.get("/api/boards/{board_id}")
@_errors
async def get_board(board_id: str, run: Literal["all", "this"] = "all", q: str | None = None):
    from core.boards import service
    from core.store import boards as store

    return await service.open_board(board_id, await _directory(),
                                    current_run=await _current_run(await store.get_board(board_id)),
                                    only_this_run=run == "this", query=q)


@router.patch("/api/boards/{board_id}")
@_errors
async def update_board(board_id: str, body: SharedBoardPatch):
    from core.boards import service

    return await service.update_shared(board_id, name=body.name, description=body.description,
                                       directory=await _directory())


@router.delete("/api/boards/{board_id}", status_code=204)
@_errors
async def delete_board(board_id: str):
    from core.boards import service

    await service.delete(board_id, await _directory())


@router.put("/api/boards/{board_id}/access")
@_errors
async def set_access(board_id: str, body: AccessIn, request: Request):
    from core.boards import service

    return {"access": await service.set_access(board_id, [e.model_dump() for e in body.entries],
                                               directory=await _directory(), by=await _actor(request))}


@router.post("/api/boards/{board_id}/cards", status_code=201)
@_errors
async def add_card(board_id: str, body: CardIn, request: Request):
    from core.boards import service

    return await service.add_card(board_id, title=body.title, body=body.body, kind=body.kind, key=body.key,
                                  by=await _actor(request), max_open=_caps().max_open_cards)
