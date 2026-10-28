"""Recordings (BRW1, D328, F1142; DESIGN §v5.2): record a task once, keep it, change it, and its versions and pending
fixes. The logic is `core.browser.recordings`; this is OSS's door to it (cloud's API calls the same functions).
Anyone who can build may change a recording (v5-12); here that is the install's person.
"""
from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from core.browser import client as browser_client
from core.browser import recordings

router = APIRouter()


def _person() -> str:
    from core.site_sessions import install_person
    return install_person()


class NewRecording(BaseModel):
    sessions: list[str] = []
    start_url: str = ""
    name: str = ""
    recording_id: str | None = None  # re-recording this recording from `from_step` (F1143)
    from_step: str | None = None


@router.post("/api/recordings/new")
async def new_recording(body: NewRecording):
    """"Record a task": a browser of the person's alone, signed in with the picked sessions, recording. With
    `{recording_id, from_step}`, "re-record from here": the browser is that recording's, its steps before `from_step`
    replay first, and Stop with `from_step` keeps what follows as a pending fix. 409 `{detail, replayed,
    browser_session_id}` when those steps stopped (the browser stays open: after the person's own step, `record/start
    {carry_on: true}` goes on from there)."""
    from core.browser.picks import PickRefused, one_per_site, usable_new
    try:
        sessions = await one_per_site(body.sessions)
        await usable_new(sessions)
    except PickRefused as refused:
        return JSONResponse(status_code=422, content=refused.body())
    start = (body.start_url or "").strip()
    if start and urlparse(start if "://" in start else f"https://{start}").scheme not in ("http", "https"):
        raise HTTPException(status_code=422, detail="Start from a web address.")
    if start and "://" not in start:
        start = f"https://{start}"
    replay_first = None
    if body.from_step:
        if not body.recording_id:
            raise HTTPException(status_code=422, detail="Re-recording from a step needs its recording.")
        replay_first = await _steps_before(body.recording_id, body.from_step)
        rid = body.recording_id
    else:
        rid = recordings.new_id()
    try:
        sid = await browser_client.tools.open_record_session(rid, sessions=sessions, start_url=start,
                                                             replay_first=replay_first)
    except browser_client.RecordingOpen:
        raise HTTPException(status_code=409, detail="This recording is being recorded in another browser.") from None
    except browser_client.ReplayStopped as stopped:
        return JSONResponse(status_code=409, content={"detail": "Replaying the steps before it stopped.",
                                                      "replayed": stopped.replayed, "recording_id": rid,
                                                      "browser_session_id": stopped.session_id})
    return {"recording_id": rid, "browser_session_id": sid}


async def _steps_before(recording_id: str, step_id: str) -> list[dict]:
    try:
        doc = await recordings.get(recording_id)
    except recordings.NotFound:
        raise HTTPException(status_code=404, detail="That recording is gone.") from None
    ids = [s.get("id") for s in doc["steps"]]
    if step_id not in ids:
        raise HTTPException(status_code=422, detail="That step is not in this recording.")
    return doc["steps"][:ids.index(step_id)]


@router.get("/api/recordings")
async def list_recordings():
    return {"recordings": await recordings.list_recordings()}


@router.get("/api/recordings/{recording_id}")
async def get_recording(recording_id: str):
    try:
        doc = await recordings.get(recording_id)
    except recordings.NotFound:
        raise HTTPException(status_code=404, detail="That recording is gone.") from None
    return {k: v for k, v in doc.items() if k != "history"}


class RecordingChange(BaseModel):
    base_version: int
    name: str | None = None
    steps: list[dict] | None = None
    inputs: list[dict] | None = None
    outputs: list[dict] | None = None


@router.patch("/api/recordings/{recording_id}")
async def change_recording(recording_id: str, body: RecordingChange):
    try:
        doc = await recordings.patch(recording_id, base_version=body.base_version, by=_person(), name=body.name,
                                     steps=body.steps, inputs=body.inputs, outputs=body.outputs)
    except recordings.Stale as stale:
        return JSONResponse(status_code=409, content={"detail": str(stale), "version": stale.version})
    except recordings.NotFound:
        raise HTTPException(status_code=404, detail="That recording is gone.") from None
    return {k: v for k, v in doc.items() if k != "history"}


@router.delete("/api/recordings/{recording_id}")
async def delete_recording(recording_id: str):
    if not await recordings.delete(recording_id):
        raise HTTPException(status_code=404, detail="That recording is gone already.")
    return {"deleted": True}


@router.get("/api/recordings/{recording_id}/shots/{ref}")
async def recording_shot(recording_id: str, ref: str):
    import asyncio
    from core.browser import kept
    data = await asyncio.to_thread(kept.read_recording_shot, recording_id, ref)
    if data is None:
        raise HTTPException(status_code=404, detail="That picture is gone.")
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/api/recordings/{recording_id}/versions")
async def recording_versions(recording_id: str):
    try:
        return {"versions": await recordings.versions(recording_id)}
    except recordings.NotFound:
        raise HTTPException(status_code=404, detail="That recording is gone.") from None


@router.post("/api/recordings/{recording_id}/edits/{edit_id}/approve")
async def approve_edit(recording_id: str, edit_id: str):
    try:
        doc = await recordings.approve_edit(recording_id, edit_id, by=_person())
    except recordings.NotFound:
        raise HTTPException(status_code=404, detail="That fix is gone.") from None
    return {k: v for k, v in doc.items() if k != "history"}


@router.post("/api/recordings/{recording_id}/edits/{edit_id}/discard")
async def discard_edit(recording_id: str, edit_id: str):
    try:
        doc = await recordings.discard_edit(recording_id, edit_id)
    except recordings.NotFound:
        raise HTTPException(status_code=404, detail="That fix is gone.") from None
    return {k: v for k, v in doc.items() if k != "history"}


# ── recording in an open browser ─────────────────────────────────────────────

class RecordStart(BaseModel):
    recording_id: str | None = None
    from_step: str | None = None
    carry_on: bool = False  # after the person's own step stopped a re-record's replay: go on from there


@router.post("/api/browser/{session_id}/record/start")
async def record_start(session_id: str, body: RecordStart):
    """Record in this browser. With `from_step`, a re-record: the recording's steps before it replay first, then the
    person carries on from there; Stop with the same `recording_id` and `from_step` keeps it as a pending fix."""
    if await browser_client.tools.owner_of(session_id) is None:
        raise HTTPException(status_code=404, detail="That browser is closed.")
    payload: dict = {}
    if body.carry_on:
        payload["carry_on"] = True
    elif body.from_step:
        if not body.recording_id:
            raise HTTPException(status_code=422, detail="Re-recording from a step needs its recording.")
        payload["replay_first"] = await _steps_before(body.recording_id, body.from_step)
    status, answer = await browser_client.tools.viewer(session_id, "/record/start", payload)
    if status == 409 and answer.get("replayed"):  # the steps before it stopped: say where, for the person
        return JSONResponse(status_code=409, content={"detail": "Replaying the steps before it stopped.",
                                                      "replayed": answer["replayed"]})
    if status >= 400:
        raise HTTPException(status_code=status, detail=answer.get("error") or "Refused.")
    return answer


class RecordStop(BaseModel):
    keep: bool = True
    name: str = ""
    recording_id: str | None = None
    from_step: str | None = None


@router.post("/api/browser/{session_id}/record/stop")
async def record_stop(session_id: str, body: RecordStop):
    """Stop recording: kept as a recording (or, re-recording a step, as a pending fix of one), or dropped. A
    "Record a task" browser closes with it."""
    owner = await browser_client.tools.owner_of(session_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="That browser is closed.")
    status, result = await browser_client.tools.viewer(session_id, "/record/stop", {})
    if status >= 400:
        raise HTTPException(status_code=status, detail=result.get("error") or "Refused.")
    kind, _, ident = owner.partition(":")
    out: dict = {}
    if body.keep:
        out = await recordings.from_stop(result, by=_person(), recording_id=ident if kind == "record" else
                                         body.recording_id, from_step=body.from_step, name=body.name)
    if kind == "record":
        await browser_client.tools.close_owner(owner)
    return out
