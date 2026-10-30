"""Recordings and kept sessions, as product tools (BRW-API, F1188; BRW1, D328).

What an MCP client may read of the browser's library, and the one act it may do there: delete a recording. Synapse AI,
in the app, also uses or drops a kept fix and renames a recording (F1270, REACH1; D340), as the Recordings screen does:
those three are the app's only (`app_only`), so BRW-API's surface stays as Naveen set it. Each answer
is the shape cloud's public API answers with too (BRW3): `core.browser.recordings.listed_view` / `text_view` and
`core.browser.picks.session_view`, each an allowlist. A recording's steps come as text, never their pictures (Naveen
2026-10-29: those are its creator's and the Builders', on screen, and an MCP client is neither); a session comes as its
site, whose it is and when it ends, never what it holds, and only one the caller may use.

Not here, by design, and each tool's description says so: keeping a session (a person signs in, in the app), recording
(a person drives), the pictures, and who a session is shared with. The builder makes a Browser step or agent from what
these list (`build.py`'s step and agent tools, F1189).

A delete is the app's: anyone who can build, the store's own delete (its pictures with it), not refused while a step
or agent uses it; its preview names those. A host whose people are ids (cloud) says their names with `set_names`.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from core.product_tools import page as paging
from core.product_tools.errors import ToolRefusal
from core.product_tools.registry import ProductTool
from core.product_tools.roles import Role

GONE = "That recording is gone."
_IN_THE_APP = ("Recording a task and keeping a site's session are a person's, in the app (they drive and sign in), "
               "and a step's pictures stay there too: none of that is a tool.")

#: async `fn(ids) -> {id: name}` for the people who made and changed recordings. None: their ids are names (OSS).
_names: Callable[[set[str]], Awaitable[dict]] | None = None


def set_names(fn: Callable[[set[str]], Awaitable[dict]] | None) -> None:
    global _names
    _names = fn


async def _names_of(docs: list[dict]) -> dict | None:
    if _names is None:
        return None
    return await _names({str(w) for d in docs for w in (d.get("created_by"), d.get("updated_by")) if w})


async def _list_recordings(caller: Any, args: dict[str, Any]) -> dict:
    from core.browser import recordings
    docs = await recordings.all_recordings()
    names = await _names_of(docs)
    return paging.page([recordings.listed_view(d, names) for d in docs], args)


async def _recording(args: dict[str, Any]) -> dict:
    from core.browser import recordings
    try:
        return await recordings.get(str(args.get("recording_id") or ""))
    except recordings.NotFound:
        raise ToolRefusal(GONE) from None


async def _get_recording(caller: Any, args: dict[str, Any]) -> dict:
    from core.browser import recordings
    doc = await _recording(args)
    return recordings.text_view(doc, await _names_of([doc]))


async def _list_sessions(caller: Any, args: dict[str, Any]) -> dict:
    from core import site_sessions
    from core.browser.picks import session_view
    rows = [r for r in await site_sessions.listed(caller) if r.get("usable")]
    return paging.page([session_view(r) for r in rows], args)


async def _preview_delete(caller: Any, args: dict[str, Any]) -> dict:
    from core.browser import recordings
    doc = await _recording(args)
    uses = await recordings.used_by(doc["id"])
    note = "This cannot be undone: its steps and pictures go."
    if uses["workflows"] or uses["agents"]:
        note += (" The Browser steps named here stop at it until they pick another recording, and the agents named "
                 "here lose it.")
    # Keyword arguments, not a literal: `"type": "…"` in code reads as an event type to F854's event census.
    will = dict(type="recording", id=doc["id"], name=doc.get("name"), steps=len(doc.get("steps") or []))
    return {"will_delete": will, "used_by": uses, "note": note}


async def _delete(caller: Any, args: dict[str, Any]) -> dict:
    from core.browser import recordings
    doc = await _recording(args)
    if not await recordings.delete(doc["id"]):
        raise ToolRefusal("That recording is gone already.")
    return {"deleted": doc["id"], "name": doc.get("name")}


def _by(caller: Any) -> str:
    """Who made a change, as the routes say it: the person (cloud's id), else this install's one person."""
    from core.site_sessions import install_person
    return str(getattr(caller, "user_id", None) or install_person())


async def _edit(args: dict[str, Any]) -> tuple[dict, dict]:
    """The recording and the kept fix `edit_id` names, or why not: a fix approved or dropped since is gone."""
    doc = await _recording(args)
    edit = next((e for e in doc.get("pending_edits") or [] if e.get("edit_id") == str(args.get("edit_id") or "")),
                None)
    if edit is None:
        raise ToolRefusal("That fix is gone: it was used or dropped already.")
    return doc, edit


def edit_words(doc: dict, edit: dict) -> dict:
    """A kept fix as a person reads it: the step it replaces (and, for a re-record, every step after it) and what
    comes in its place, each step as the editor's sentence."""
    from core.browser.recording import line_of

    def said(step: dict) -> str:
        return str(step.get("title") or step.get("line") or line_of(step))

    steps = [s for s in doc.get("steps") or [] if isinstance(s, dict)]
    at = next((i for i, s in enumerate(steps) if s.get("id") == edit.get("step_id")), None)
    if at is None:
        raise ToolRefusal("The step that fix replaces is gone from the recording: drop the fix instead.")
    before = steps[at:] if edit.get("rest") else steps[at:at + 1]
    return {"recording": doc.get("name") or doc["id"], "from_step": steps[at].get("n") or at + 1,
            "rest": bool(edit.get("rest")), "before": [said(s) for s in before],
            "after": [said(s) for s in edit.get("steps") or [] if isinstance(s, dict)],
            "by_run": edit.get("run_id")}


async def _preview_approve(caller: Any, args: dict[str, Any]) -> dict:
    doc, edit = await _edit(args)
    return {**edit_words(doc, edit), "note": "The recording's next version uses it; the one before is kept in its "
                                             "history."}


async def _approve(caller: Any, args: dict[str, Any]) -> dict:
    from core.browser import recordings
    doc, edit = await _edit(args)
    try:
        after = await recordings.approve_edit(doc["id"], edit["edit_id"], by=_by(caller))
    except recordings.NotFound:
        raise ToolRefusal("That fix is gone: it was used or dropped already.") from None
    return {"approved": edit["edit_id"], "recording": after["id"], "version": after["version"]}


async def _preview_discard(caller: Any, args: dict[str, Any]) -> dict:
    doc, edit = await _edit(args)
    return {**edit_words(doc, edit), "note": "The recording stays as it is."}


async def _discard(caller: Any, args: dict[str, Any]) -> dict:
    from core.browser import recordings
    doc, edit = await _edit(args)
    try:
        await recordings.discard_edit(doc["id"], edit["edit_id"])
    except recordings.NotFound:
        raise ToolRefusal("That fix is gone: it was used or dropped already.") from None
    return {"discarded": edit["edit_id"], "recording": doc["id"]}


def _new_name(doc: dict, args: dict[str, Any]) -> str:
    name = " ".join(str(args.get("name") or "").split())[:120]
    if not name:
        raise ToolRefusal("Say the recording's new name.")
    if name == doc.get("name"):
        raise ToolRefusal(f'The recording is already called "{name}".')
    return name


async def _preview_rename(caller: Any, args: dict[str, Any]) -> dict:
    doc = await _recording(args)
    return {"recording": doc.get("name") or doc["id"], "to": _new_name(doc, args)}


async def _rename(caller: Any, args: dict[str, Any]) -> dict:
    from core.browser import recordings
    doc = await _recording(args)
    name = _new_name(doc, args)
    try:
        after = await recordings.patch(doc["id"], base_version=doc["version"], by=_by(caller), name=name)
    except recordings.Stale as stale:
        raise ToolRefusal(str(stale)) from None
    except recordings.NotFound:
        raise ToolRefusal(GONE) from None
    return {"renamed": after["id"], "name": after["name"], "version": after["version"]}


_ID = {"recording_id": {"type": "string", "description": "The recording's id, from list_recordings."}}
_EDIT = {**_ID, "edit_id": {"type": "string", "description": "The kept fix's id."}}


def entries() -> list[ProductTool]:
    read = {"kind": "read", "area": "browser"}
    return [
        ProductTool(name="list_recordings", paginated=True, handler=_list_recordings, input_schema={
                        "type": "object", "properties": {}},
                    description="The workspace's recordings: tasks a person did once in the browser, kept to replay "
                                "by code. Each with its sites, how many steps, its inputs by name and whether a "
                                f"workflow made it. A Browser step or agent names one by its id. {_IN_THE_APP}",
                    **read),
        ProductTool(name="get_recording", handler=_get_recording, input_schema={
                        "type": "object", "properties": dict(_ID), "required": ["recording_id"]},
                    description="One recording, its steps as text (what each does, in order, with its id), its "
                                "inputs and outputs by name. A Browser step fills the inputs (recording_inputs) and "
                                f"may let named steps send on their own (may_send_steps). {_IN_THE_APP}",
                    **read),
        ProductTool(name="list_browser_sessions", paginated=True, handler=_list_sessions, input_schema={
                        "type": "object", "properties": {}},
                    description="The kept sessions you may use: sites a person stayed signed in to, so a Browser "
                                "agent or step starts signed in. Each with its site, whether it is yours or shared "
                                "with you, its owner's name, when it ends and whether it has expired. Never what a "
                                "session holds, nor whom it is shared with (that is its owner's, in the app). "
                                f"{_IN_THE_APP}",
                    **read),
        ProductTool(name="delete_recording", handler=_delete, kind="act", area="browser", min_role=Role.BUILDER,
                    scope="build", irreversible=True, preview=_preview_delete, audit_action="recording.deleted",
                    target=lambda a, r: ("recording", str((r or {}).get("deleted") or a.get("recording_id"))),
                    input_schema={"type": "object", "properties": dict(_ID), "required": ["recording_id"]},
                    description="Delete a recording, with its pictures. Cannot be undone: the first call previews it "
                                "and names the workflows' Browser steps and the agents that use it (they stop at it, "
                                "or lose it), and gives a confirm_token; call again with the same arguments and that "
                                f"token to delete. {_IN_THE_APP}"),
        # In the app only (F1270): Synapse AI's cards, as the Recordings screen's buttons.
        ProductTool(name="approve_recording_edit", handler=_approve, kind="act", area="browser", app_only=True,
                    min_role=Role.BUILDER, scope="build", confirmed=True, preview=_preview_approve,
                    audit_action="recording.edit_approved",
                    target=lambda a, r: ("recording", str(a.get("recording_id"))),
                    input_schema={"type": "object", "properties": dict(_EDIT),
                                  "required": ["recording_id", "edit_id"]},
                    description="Use a kept fix in its recording: the step it replaces (from it on, for a "
                                "re-record) becomes what the fix did, as a new version."),
        ProductTool(name="discard_recording_edit", handler=_discard, kind="act", area="browser", app_only=True,
                    min_role=Role.BUILDER, scope="build", confirmed=True, preview=_preview_discard,
                    audit_action="recording.edit_discarded",
                    target=lambda a, r: ("recording", str(a.get("recording_id"))),
                    input_schema={"type": "object", "properties": dict(_EDIT),
                                  "required": ["recording_id", "edit_id"]},
                    description="Drop a kept fix: the recording stays as it is."),
        ProductTool(name="rename_recording", handler=_rename, kind="act", area="browser", app_only=True,
                    min_role=Role.BUILDER, scope="build", confirmed=True, preview=_preview_rename,
                    audit_action="recording.renamed", target=lambda a, r: ("recording", str(a.get("recording_id"))),
                    input_schema={"type": "object", "properties": {**_ID, "name": {"type": "string"}},
                                  "required": ["recording_id", "name"]},
                    description="Rename a recording, as a new version."),
    ]
