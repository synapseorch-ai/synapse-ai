"""Synapse AI's own board: one card per build or change (SAI2-P1, F776; DESIGN.md decision 1).

The conversation's truth across turns and days. Each build or change Synapse AI works on is a card on the
workspace's one `assistant`-scope board, keyed by its id: its stage is where it stands (drafting → waiting for its
person → needs a connection → built, changed or set aside), its body the plan in words, its `data` the typed Plan
or Change, and `owner_id` the person whose it is.

Read and written by code only — the definition's workflow tool steps (E1) call these — never by a model following
a board protocol (BOARD2's lesson). So none of it asks `boards_enabled`: like a team's run board, it works where
Boards is off. It is shown nowhere else yet: the person-facing lists and reads leave this scope out
(`core.boards.service`) and no agent's turn is given it (`core.boards.resolve`); P3 shows it in the panel, and on
the Boards screen once Boards is on, each person their own cards and an admin all.
"""
from __future__ import annotations

from typing import Any

from core.store import boards as store

SCOPE = "assistant"
#: Who writes: Synapse AI's own steps, never a person or another agent.
ACTOR = store.Actor(agent="agent_native_builder", agent_name="Synapse AI", source="synapse_ai")
_OUTCOME = {"built": None, "changed": None, "set_aside": "Set aside"}


async def board() -> store.Board:
    """The workspace's Synapse AI board, made at its first use."""
    return await store.get_or_create_scoped_board(SCOPE, "", by=ACTOR)


async def card(key: str) -> store.Card | None:
    """The card of build or change `key`, whatever its stage."""
    found = await store.find_scoped_board(SCOPE, "")
    if found is None:
        return None
    return await store._card_by_key(found.id, key)


async def put(key: str, *, title: str, stage: str, owner_id: str | None, body: str = "",
              data: dict[str, Any] | None = None, run_id: str = "") -> store.Card:
    """Make the card of build or change `key`, or bring it to `stage` with this title, body and data.

    A closing stage closes it: built and changed as done, set aside as done-failed with its reason. A closed card
    whose build goes on (a set-aside plan taken up again) opens again.
    """
    if stage not in store.STAGES:
        raise store.BadValue(f"A card's stage is one of {', '.join(store.STAGES)}.")
    actor = store.Actor(**{**ACTOR.to_json(), "run": run_id}) if run_id else ACTOR
    home = await board()
    current = await store._card_by_key(home.id, key)
    if current is None:
        current, _ = await store.add_card(home.id, title=title, body=body, key=key, by=actor, stage=stage,
                                          data=data, owner_id=owner_id)
        if stage not in store.CLOSED_STAGES:
            return current
        return await _close(current, stage, actor)

    closing = stage in store.CLOSED_STAGES
    reopen = not closing and current.status == "done"
    fields: dict[str, Any] = {}
    if title != current.title:
        fields["title"] = title
    if body != current.body:
        fields["body"] = body
    if stage != current.stage:
        fields["stage"] = stage
    if data is not None and data != current.data:
        fields["data"] = data
    if reopen:
        fields["status"] = "open"
    if fields:
        current = await store.update_card(current.id, version=current.version, by=actor, holder=None, **fields)
    if closing and current.status != "done":
        current = await _close(current, stage, actor)
    return current


async def _close(current: store.Card, stage: str, actor: store.Actor) -> store.Card:
    if stage == "set_aside":
        return await store.update_card(current.id, version=current.version, by=actor, holder=None, status="failed",
                                       result=current.result or "Set aside")
    return await store.update_card(current.id, version=current.version, by=actor, holder=None, status="done")


async def cards_for(viewer_id: str | None, *, admin: bool, closed: bool = False) -> list[store.Card]:
    """The cards a person sees: their own, or every card for an admin. The open ones, or with `closed` the closed
    ones (built, changed, set aside)."""
    found = await store.find_scoped_board(SCOPE, "")
    if found is None:
        return []
    cards = await store.list_cards([found.id], statuses=("done",) if closed else ("open", "claimed"), limit=200)
    if admin:
        return cards
    return [c for c in cards if viewer_id and c.owner_id == viewer_id]
