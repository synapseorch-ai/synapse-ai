"""Recordings: tasks the person did once, kept to replay (BRW1, D328, F1142; DESIGN §v5.2).

Plain async functions over the engine's store, no request objects: OSS's routes call them, and cloud's API calls
them under its org scope (BRW3). A recording is one item of the `browser_recordings` collection:

    {id, name, sites[], inputs[{name, label, step_id}], outputs[{name, kind, step_id}], steps[], version,
     history[{version, at, by, name, steps, inputs, outputs}], pending_edits[{edit_id, step_id, steps[], by,
     run_id, at}], changed[step_id], created_at, created_by, updated_at, updated_by}

A step is the recorder's (`core.browser.recording`): a stable `id`, `n`, `line` (the server's sentence, rewritten on
every change) and an optional `title` (a rename). Its pictures are `kept`'s, under `recordings/<id>/`, kept until
the recording goes. Anyone who can build in the workspace may change one (v5-12); every change is a version, with
who made it, and a change made from an older version is refused (`Stale`), so an approved fix and an open editor
can't overwrite each other. Within one process changes are serialised; across processes the version check is the
guard (cloud's API may run several).
"""
from __future__ import annotations

import asyncio
import base64
import re
import secrets
import time
from urllib.parse import urlparse

from core.browser.recording import line_of

COLLECTION = "browser_recordings"
#: Versions kept in the document; older ones go.
HISTORY = 20
_lock = asyncio.Lock()


class Stale(ValueError):
    """A change made from an older version than the recording's own."""

    def __init__(self, version: int):
        super().__init__(f"This recording changed since you opened it (it is at version {version}). Reload it.")
        self.version = version


class NotFound(LookupError):
    pass


def new_id() -> str:
    return f"rec_{secrets.token_hex(10)}"


def _now() -> float:
    return time.time()


def _sites(steps: list[dict]) -> list[str]:
    from core.sites import registrable_domain
    out: list[str] = []
    for step in steps:
        host = urlparse(str(step.get("url") or "")).hostname if step.get("action") == "open" else None
        site = registrable_domain(host) if host else ""
        if site and site not in out:
            out.append(site)
    return out


def _lined(steps: list[dict]) -> list[dict]:
    """Each step's sentence rewritten from what it now is (a value made an input, a target changed)."""
    out = []
    for i, step in enumerate(steps, 1):
        step = {**step, "n": i}
        step["line"] = line_of(step)
        out.append(step)
    return out


def _summary(doc: dict) -> dict:
    return {k: doc.get(k) for k in ("id", "name", "sites", "version", "created_at", "updated_at")} | {
        "steps": len(doc.get("steps") or []), "pending_edits": len(doc.get("pending_edits") or [])}


async def _load(recording_id: str) -> dict:
    from core.store.collections import get_item
    doc = await get_item(COLLECTION, recording_id) if recording_id.startswith("rec_") else None
    if not doc:
        raise NotFound(recording_id)
    return doc


async def _save(doc: dict) -> None:
    from core.store.collections import put_item
    await put_item(COLLECTION, doc["id"], doc)


async def _update(recording_id: str, mutate) -> dict:
    """A change in one transaction, the recording locked while it is checked and written (exact across processes)."""
    from core.store.collections import update_item
    async with _lock:
        doc = await update_item(COLLECTION, recording_id, mutate) if recording_id.startswith("rec_") else None
    if doc is None:
        raise NotFound(recording_id)
    return doc


def _keep_shots(recording_id: str, shots: dict[str, str] | None) -> None:
    from core.browser import kept
    for ref, b64 in (shots or {}).items():
        try:
            kept.keep_recording_shot(recording_id, ref, base64.b64decode(b64))
        except (ValueError, TypeError):
            continue


async def create(name: str, steps: list[dict], *, shots: dict[str, str] | None = None, by: str,
                 recording_id: str | None = None) -> dict:
    """A new recording from a stopped one's steps and pictures (base64, as the component hands them over)."""
    rid = recording_id if recording_id and recording_id.startswith("rec_") else new_id()
    steps = _lined([s for s in steps or [] if isinstance(s, dict)])
    now = _now()
    doc = {"id": rid, "name": (name or "").strip()[:120] or "A recorded task", "sites": _sites(steps),
           "inputs": [], "outputs": [], "steps": steps, "version": 1, "history": [], "pending_edits": [],
           "changed": [], "created_at": now, "created_by": by, "updated_at": now, "updated_by": by}
    await asyncio.to_thread(_keep_shots, rid, shots)
    async with _lock:
        await _save(doc)
    return doc


def slug(doc: dict) -> str:
    """How an agent names a recording (F1145): its name's words, lower-case and hyphened; its id with no name."""
    words = re.findall(r"[a-z0-9]+", str(doc.get("name") or "").lower())
    return "-".join(words)[:60] or doc["id"]


def input_names(doc: dict) -> list[str]:
    """The inputs its steps fill in, in step order."""
    return list(dict.fromkeys((s.get("value") or {}).get("input") for s in doc.get("steps") or []
                              if (s.get("value") or {}).get("input")))


async def picked(ids: list[str]) -> list[dict]:
    """The picked recordings that still exist, in pick order."""
    out = []
    for rid in dict.fromkeys(ids or []):
        try:
            out.append(await get(rid))
        except NotFound:
            continue
    return out


async def get(recording_id: str) -> dict:
    return await _load(recording_id)


async def list_recordings() -> list[dict]:
    from core.store.collections import load
    return sorted((_summary(d) for d in await load(COLLECTION)), key=lambda d: d["updated_at"] or 0, reverse=True)


async def patch(recording_id: str, *, base_version: int, by: str, name: str | None = None,
                steps: list[dict] | None = None, inputs: list[dict] | None = None,
                outputs: list[dict] | None = None) -> dict:
    """A person's change from the editor: refused (`Stale`) unless made from the recording's current version."""
    def change(doc: dict) -> dict:
        if int(base_version) != int(doc["version"]):
            raise Stale(doc["version"])
        _push_history(doc)
        if name is not None:
            doc["name"] = name.strip()[:120] or doc["name"]
        if steps is not None:
            doc["steps"] = _lined([s for s in steps if isinstance(s, dict)])
            doc["sites"] = _sites(doc["steps"])
        if inputs is not None:
            doc["inputs"] = [i for i in inputs if isinstance(i, dict)]
        if outputs is not None:
            doc["outputs"] = [o for o in outputs if isinstance(o, dict)]
        _bump(doc, by)
        return doc
    return await _update(recording_id, change)


def _push_history(doc: dict) -> None:
    doc["history"] = ([{"version": doc["version"], "at": doc.get("updated_at"), "by": doc.get("updated_by"),
                        "name": doc.get("name"), "steps": doc.get("steps"), "inputs": doc.get("inputs"),
                        "outputs": doc.get("outputs")}] + list(doc.get("history") or []))[:HISTORY]


def _bump(doc: dict, by: str) -> None:
    doc["version"] = int(doc["version"]) + 1
    doc["updated_at"], doc["updated_by"] = _now(), by


async def delete(recording_id: str) -> bool:
    """The recording and its pictures, at once."""
    from core.browser import kept
    from core.store.collections import delete_item
    async with _lock:
        gone = await delete_item(COLLECTION, recording_id) if recording_id.startswith("rec_") else False
    await asyncio.to_thread(kept.forget_recording, recording_id)
    return gone


async def versions(recording_id: str) -> list[dict]:
    doc = await _load(recording_id)
    current = {"version": doc["version"], "at": doc.get("updated_at"), "by": doc.get("updated_by"),
               "steps": len(doc.get("steps") or []), "current": True}
    return [current] + [{"version": h["version"], "at": h.get("at"), "by": h.get("by"),
                         "steps": len(h.get("steps") or []), "current": False} for h in doc.get("history") or []]


async def add_edit(recording_id: str, *, step_id: str, steps: list[dict], by: str, run_id: str | None = None,
                   shots: dict[str, str] | None = None, rest: bool = False) -> str:
    """A proposed change to one step (an agent's fix, F1144, or with `rest` a re-record from a step that replaces it
    and every step after): kept pending until a person approves or discards it; the recording is unchanged till then."""
    edit_id = f"ed_{secrets.token_hex(6)}"
    await asyncio.to_thread(_keep_shots, recording_id, shots)

    def add(doc: dict) -> dict:
        doc["pending_edits"] = [*(doc.get("pending_edits") or []), {
            "edit_id": edit_id, "step_id": step_id, "steps": _lined(steps), "by": by, "run_id": run_id, "at": _now(),
            **({"rest": True} if rest else {})}]
        return doc
    await _update(recording_id, add)
    return edit_id


async def approve_edit(recording_id: str, edit_id: str, *, by: str) -> dict:
    """The pending edit replaces its step (from it to the end, for a re-record): a new version."""
    def approve(doc: dict) -> dict:
        edit = next((e for e in doc.get("pending_edits") or [] if e.get("edit_id") == edit_id), None)
        if edit is None:
            raise NotFound(edit_id)
        steps = doc.get("steps") or []
        at = next((i for i, s in enumerate(steps) if s.get("id") == edit["step_id"]), None)
        if at is None:
            raise NotFound(edit["step_id"])
        _push_history(doc)
        tail = [] if edit.get("rest") else steps[at + 1:]
        doc["steps"] = _lined([*steps[:at], *edit["steps"], *tail])
        doc["sites"] = _sites(doc["steps"])
        doc["pending_edits"] = [e for e in doc["pending_edits"] if e.get("edit_id") != edit_id]
        doc["changed"] = [c for c in doc.get("changed") or [] if c != edit["step_id"]]
        _bump(doc, by)
        return doc
    return await _update(recording_id, approve)


async def discard_edit(recording_id: str, edit_id: str) -> dict:
    def discard(doc: dict) -> dict:
        kept_edits = [e for e in doc.get("pending_edits") or [] if e.get("edit_id") != edit_id]
        if len(kept_edits) == len(doc.get("pending_edits") or []):
            raise NotFound(edit_id)
        doc["pending_edits"] = kept_edits
        return doc
    return await _update(recording_id, discard)


async def from_stop(result: dict, *, by: str, recording_id: str | None = None, from_step: str | None = None,
                    name: str | None = None) -> dict:
    """What `record/stop` kept: a new recording, or (re-recording from a step) a pending edit of an existing one.
    `{recording_id, steps}` or `{recording_id, pending_edit}`."""
    steps, shots = result.get("steps") or [], result.get("shots") or {}
    if recording_id and from_step:  # a re-record replaces its step and every step after it
        edit_id = await add_edit(recording_id, step_id=from_step, steps=steps, by=by, shots=shots, rest=True)
        return {"recording_id": recording_id, "pending_edit": edit_id}
    doc = await create(name or "", steps, shots=shots, by=by, recording_id=recording_id)
    return {"recording_id": doc["id"], "steps": len(doc["steps"])}
