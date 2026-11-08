"""An editor's save that says what it read (F871, F891): one rule for workflows, agents and tools.

The editor sends `If-Match: "<updated_at>"`, the stamp of the copy it opened. A save from a copy someone has since
changed is refused 409 `{"message", "updated_at"}` — the other save's time, which "keep mine" saves from — and one of
a resource since deleted 404 `{"message"}`. A save that sends no header — the AI builder, MCP, the CLI, an import, a
seed, every older caller — is exactly as before. QA1d met the lack of it on cloud with two tabs: the stale save went
in over the newer one, both 200, and the first tab still said "Saved".
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import HTTPException, Request

#: Read, compare and write, one at a time in this process.
SAVE_LOCK = asyncio.Lock()


def if_match(request: Request | None) -> str | None:
    """The stamp an editor read, from `If-Match: "<updated_at>"`; None for every caller that sends none (and for a
    route called as a function, with no request)."""
    raw = ((request.headers.get("if-match") if request is not None else None) or "").strip()
    if not raw:
        return None
    return (raw[2:] if raw.startswith("W/") else raw).strip('"')


def changed_words(noun: str, at: str) -> str:
    return f"This {noun} was changed elsewhere at {at}. Reload it, or keep yours and save over it."


def deleted_words(noun: str) -> str:
    return (f"This {noun} was deleted elsewhere. Your edits are still here: save them as a new {noun}, "
            "or discard them.")


def same_instant(seen: str, current: str) -> bool:
    """Whether two stamps are one moment: `Z` and `+00:00` agree; anything unparsed compares as written."""
    try:
        return datetime.fromisoformat(seen.replace("Z", "+00:00")) == datetime.fromisoformat(
            current.replace("Z", "+00:00"))
    except ValueError:
        return seen == current


def refuse_if_stale(seen: str | None, current: str | None, *, exists: bool, noun: str) -> None:
    """Raise the 404 or 409 an editor's save gets; nothing when it sent no stamp, or when the stamp still holds."""
    if seen is None:
        return
    if not exists:
        raise HTTPException(404, detail={"message": deleted_words(noun)})
    if current and not same_instant(seen, current):
        raise HTTPException(409, detail={"message": changed_words(noun, current), "updated_at": current})
