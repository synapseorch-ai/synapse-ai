"""Which kept sessions a browser starts with (BRW1, D328, F1140; DESIGN §v5.1).

A person picks sessions at four levels: the agent (`browser.sessions`), the chat, the run's start and the step. A
browser gets them all, one per site, the nearer level winning on a site: step, then run, then chat, then agent. Code
matches each to its site (`core.sites`); there is no tool or argument through which the model names one, so a page
can't talk it into another site's session. Two sessions for one site in one pick are refused where they are picked
(`OnePerSite`, the 422 `{detail, site, ids}`), and the union only ever keeps the nearer one.

A chat's picks live in the tenant's blob store (`browser_picks/chats/<chat id>.json`), where cloud's workers read
them too; a run's ride its shared state (`_browser_sessions`); a step's are its own field.
"""
from __future__ import annotations

import asyncio
import json
import re

#: Nearer first: on one site, the first level that picked one wins.
LEVELS = ("step", "run", "chat", "agent")
RUN_KEY = "_browser_sessions"
_CHAT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")


class PickRefused(ValueError):
    """A pick that cannot stand: answered as a flat 422 `{detail, site, ids}` wherever sessions are picked."""

    def __init__(self, detail: str, site: str, ids: list[str]):
        super().__init__(detail)
        self.site, self.ids = site, ids

    def body(self) -> dict:
        return {"detail": str(self), "site": self.site, "ids": self.ids}


class OnePerSite(PickRefused):
    """Two picked sessions are for one site."""

    def __init__(self, site: str, ids: list[str]):
        super().__init__(f"Pick one session for {site}.", site, ids)


class NotUsable(PickRefused):
    """Someone else's session, not shared with the person picking it (F1167): shown in the library, never picked."""

    def __init__(self, site: str, ids: list[str], owner: str):
        super().__init__(f"That session for {site} is {owner or 'someone else'}'s and isn't shared with you.", site, ids)


def _usable(row: dict, who: str) -> bool:
    shared = row.get("shared_with") or []
    return row.get("owner") == who or shared == "workspace" or (isinstance(shared, list) and who in shared)


async def usable_new(ids: list[str], *, before: list[str] | None = None, for_user: str | None = None) -> None:
    """Refuse, with `NotUsable`, an id new to this save (not in `before`) that isn't the person's or shared with them.
    Ids already saved stay: what loads is decided by the job's person at the browser's start (`state_for`)."""
    from core.site_sessions import install_person
    store, who, kept = _store(), for_user or install_person(), set(before or [])
    for sid in dict.fromkeys(i for i in ids or [] if i not in kept):
        try:
            row = await store.get(sid) if hasattr(store, "get") else None
        except Exception:
            row = None
        if row and not _usable(row, who):
            raise NotUsable(row.get("site") or "", [sid], row.get("owner_name") or row.get("owner") or "")


def session_view(row: dict, who: str | None = None) -> dict:
    """A kept session as it is shown outside the app (MCP's tools, cloud's public API; BRW-API, F1188): its site, whose
    it is and when it ends; an allowlist, so never what it holds, nor whom it is shared with. `mine` on the row (a
    lister's) decides "you"; else its owner against `who`."""
    mine = row["mine"] if "mine" in row else row.get("owner") == who
    return {"id": row.get("id"), "site": row.get("site"), "owner": "you" if mine else "shared",
            "owner_name": row.get("owner_name") or row.get("owner"), "expires_at": row.get("expires_at"),
            "last_used_at": row.get("last_used_at"), "status": "expired" if row.get("expired") else "active"}


def _store():
    from core.browser.client import _logins
    return _logins()


async def _sites(ids: list[str]) -> dict[str, str | None]:
    """Each session's site, or None when the store doesn't know it (removed) or can't say."""
    store, out = _store(), {}
    for sid in dict.fromkeys(ids):
        try:
            row = await store.get(sid) if hasattr(store, "get") else None
        except Exception:
            row = None
        out[sid] = (row or {}).get("site") if row else None
    return out


async def one_per_site(ids: list[str]) -> list[str]:
    """`ids` once each, refused with `OnePerSite` when two are for one site."""
    ids = list(dict.fromkeys(i for i in ids or [] if isinstance(i, str) and i.strip()))
    seen: dict[str, str] = {}
    for sid, site in (await _sites(ids)).items():
        if not site:
            continue
        if site in seen:
            raise OnePerSite(site, [seen[site], sid])
        seen[site] = sid
    return ids


def _chat_key(chat_id: str) -> str | None:
    return f"browser_picks/chats/{chat_id}.json" if _CHAT_ID.match(chat_id or "") and ".." not in chat_id else None


async def chat_picks(chat_id: str | None) -> list[str]:
    key = _chat_key(chat_id or "")
    if key is None:
        return []
    from core.storage import get_blob_store
    try:
        return list(json.loads(await asyncio.to_thread(get_blob_store().get, key) or "[]"))
    except (ValueError, TypeError):
        return []


async def set_chat_picks(chat_id: str, ids: list[str]) -> list[str]:
    key = _chat_key(chat_id)
    if key is None:
        raise ValueError("That is not a chat.")
    ids = await one_per_site(ids)
    await usable_new(ids, before=await chat_picks(chat_id))
    from core.storage import get_blob_store
    store = get_blob_store()
    if ids:
        await asyncio.to_thread(store.put, key, json.dumps(ids))
    else:
        await asyncio.to_thread(store.delete, key)
    return ids


async def settings_for(browser: dict | None, *, chat_id: str | None = None, run: list[str] | None = None,
                       step: list[str] | None = None) -> dict:
    """The agent's browser settings with every level's picks merged: `sessions` (one per site, the nearer level's)
    and `levels` (`{id: level}`), for the browser's start and its `browser_session` event."""
    browser = dict(browser or {})
    agent = list(browser.get("sessions") or ([browser["login_ref"]] if browser.get("login_ref") else []))
    by_level = {"step": list(step or []), "run": list(run or []), "chat": await chat_picks(chat_id) if chat_id else [],
                "agent": agent}
    sites = await _sites([sid for level in LEVELS for sid in by_level[level]])
    taken, picked, level_of = set(), [], {}
    for level in LEVELS:
        for sid in by_level[level]:
            site = sites.get(sid)
            if sid in level_of or (site and site in taken):
                continue
            if site:
                taken.add(site)
            picked.append(sid)
            level_of[sid] = level
    return {**{k: v for k, v in browser.items() if k != "login_ref"}, "sessions": picked, "levels": level_of}


async def uses(session_id: str) -> dict:
    """Where a session is picked, for Remove's warning: agents, chats and workflow steps."""
    return (await uses_of([session_id]))[session_id]


async def uses_of(ids: list[str]) -> dict[str, dict]:
    from core.storage import get_blob_store
    from core.store.resources import load_agents, load_orchestrations
    counts = {i: {"agents": 0, "chats": 0, "workflows": 0} for i in ids}
    for agent in await load_agents():
        browser = agent.get("browser") or {}
        for sid in {*(browser.get("sessions") or []), *([browser["login_ref"]] if browser.get("login_ref") else [])}:
            if sid in counts:
                counts[sid]["agents"] += 1
    store = get_blob_store()
    for key in await asyncio.to_thread(store.list, "browser_picks/chats/"):
        try:
            picked = set(json.loads(await asyncio.to_thread(store.get, key) or "[]"))
        except (ValueError, TypeError):
            continue
        for sid in picked & counts.keys():
            counts[sid]["chats"] += 1
    for orch in await load_orchestrations():
        named = {sid for step in orch.get("steps") or [] for sid in (step.get("browser_sessions") or [])}
        for sid in named & counts.keys():
            counts[sid]["workflows"] += 1
    return counts
