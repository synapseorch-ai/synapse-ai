"""Knowledge's search, as a product tool (MCP2, D292): a read, listed while Knowledge is on (D176). And what a
person does to a knowledge base (ASSIST1, D291, F684): add public web pages to it, add a note, retry or delete a
document, a builder's acts as on the Knowledge screen. And, since F1200 (BLD1), make one as that screen makes one
(`bases.make_base`: on a Vault folder, its index the default's until a person changes it there), and add a file the
person already has in the Vault, by an upload's rules (`ingest.add_vault_file`). A host whose Knowledge routes do more
(cloud's storage and quota) makes and fills a base its own way through `set_knowledge_host`, on every door (a card,
MCP, the builder): a path that skipped its Vault reserve would pass its quota.

Its code is `core.mcp_server.knowledge`, with the `set_knowledge_searcher` seam cloud installs; the caller's
credential is handed on, as before, so a search's trace names the key that asked.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from core.product_tools.errors import ToolRefusal
from core.product_tools.registry import ProductTool
from core.product_tools.roles import Role


@dataclass(frozen=True)
class KnowledgeHost:
    """What a host does its own way when a knowledge base is made or filled (F1200's cloud twin, 8a 10-29). Each
    raises `ToolRefusal` in its own words (a quota, a role), or a Knowledge error the tools answer as the routes do."""

    #: `(caller, KnowledgeBaseCreate) -> KnowledgeBase`, already checked (`_new_base`): the base made and queued.
    create_base: Callable[[Any, Any], Awaitable[Any]]
    #: `(caller, KnowledgeBase, vault path) -> [document rows]`: the file copied into the base and queued.
    add_vault_file: Callable[[Any, Any, str], Awaitable[list]]
    #: `(caller, kb_id, KnowledgeBaseUpdate) -> (before, after, rebuild started)` (F1218).
    update_base: Callable[[Any, str, Any], Awaitable[tuple]] | None = None
    #: `(caller, kb_id) -> None`: the base forgotten, its Vault folder kept (F1218).
    delete_base: Callable[[Any, str], Awaitable[None]] | None = None


async def _oss_create_base(caller: Any, body: Any) -> Any:
    from core.knowledge import bases
    return await bases.make_base(body)


async def _oss_add_vault_file(caller: Any, kb: Any, source: str) -> list:
    from core.knowledge import ingest
    return await ingest.add_vault_file(kb, source)


async def _oss_update_base(caller: Any, kb_id: str, body: Any) -> tuple:
    from core.knowledge import bases
    return await bases.apply_update(kb_id, body)


async def _oss_delete_base(caller: Any, kb_id: str) -> None:
    from core.knowledge import bases
    await bases.forget(kb_id)


_HOST = KnowledgeHost(create_base=_oss_create_base, add_vault_file=_oss_add_vault_file,
                      update_base=_oss_update_base, delete_base=_oss_delete_base)
_host = _HOST


def set_knowledge_host(host: KnowledgeHost | None) -> None:
    """Install the host's way of making and filling a base. ``None`` puts this engine's back."""
    global _host
    _host = host or _HOST


async def _search(caller: Any, args: dict[str, Any]) -> str:
    from core.mcp_server import knowledge
    return await knowledge.search_tool(args, caller.principal)


def _OFF() -> str:  # noqa: N802 — read like the constant it was
    """What is off and where it is turned on (F1203)."""
    from core.features import off_sentence
    return off_sentence("knowledge")


def _on() -> bool:
    from core.knowledge.config import knowledge_enabled
    return knowledge_enabled()


async def _base(wanted: str):
    """An active knowledge base by its id or its name (exact first, then a part of it; two that match are asked
    about)."""
    from core.knowledge import bases

    if not _on():
        raise ToolRefusal(_OFF())
    listed = await bases.list_bases()
    needle = (wanted or "").strip().lower()
    exact = [b for b in listed if b.id == wanted or b.name.lower() == needle]
    near = exact or ([b for b in listed if needle in b.name.lower()] if needle else [])
    if len(near) != 1:
        if not near:
            raise ToolRefusal(f"No knowledge base is called {wanted!r}. Make one on the Knowledge screen first.")
        raise ToolRefusal("More than one knowledge base matches: " + ", ".join(f'"{b.name}"' for b in near[:8]))
    if near[0].status != "active":
        raise ToolRefusal(f'"{near[0].name}" is switched off; turn it on on the Knowledge screen first.')
    return near[0]


async def _document(doc_id: str):
    from core.knowledge import documents

    if not _on():
        raise ToolRefusal(_OFF())
    row = await documents.get(str(doc_id or ""))
    if row is None:
        raise ToolRefusal("There is no such document.")
    return row


async def _add_pages(caller: Any, args: dict[str, Any]) -> dict:
    from pydantic import ValidationError

    from core.knowledge import web
    from core.knowledge.api_models import UrlIngestRequest

    kb = await _base(str(args.get("knowledge_base") or ""))
    try:
        body = UrlIngestRequest(urls=list(args.get("urls") or []), kind=args.get("kind") or "page")
    except ValidationError as exc:
        raise ToolRefusal(f"Those pages can't be added: {exc.errors()[0].get('msg')}") from None
    accepted = await web.add_urls(kb, body)
    return {"knowledge_base": kb.name, **accepted.model_dump(mode="json")}


async def _add_note(caller: Any, args: dict[str, Any]) -> dict:
    from core.knowledge import documents, ingest
    from core.knowledge.api_models import RecordIn, RecordIngestRequest

    kb = await _base(str(args.get("knowledge_base") or ""))
    text = str(args.get("text") or "").strip()
    if not text:
        raise ToolRefusal("A note needs its text.")
    rows = await ingest.put_records(kb, RecordIngestRequest(records=[RecordIn(text=text, title=args.get("title"))]))
    return {"knowledge_base": kb.name, "documents": [documents.to_out(r).model_dump(mode="json") for r in rows]}


def _folder_for(name: str) -> str:
    """A new base's Vault folder when none is named: `knowledge/<its name>`, in the Vault's own spelling."""
    slug = "-".join("".join(c if c.isalnum() else " " for c in name.lower()).split())[:60] or "base"
    return f"knowledge/{slug}"


async def _new_base(args: dict[str, Any]):
    """The base `create_knowledge_base` would make, checked as the Knowledge screen's save checks it, before anything
    is saved: its name free, its folder allowed and no other base's (`validate_folder`)."""
    from pydantic import ValidationError

    from core.knowledge import bases
    from core.knowledge.api_models import KnowledgeBaseCreate
    from core.knowledge.errors import KnowledgePathError
    from core.knowledge.kb_settings import validate_folder

    if not _on():
        raise ToolRefusal(_OFF())
    name = str(args.get("name") or "").strip()
    try:
        body = KnowledgeBaseCreate(name=name, description=str(args.get("description") or ""),
                                   folder=str(args.get("folder") or "").strip() or _folder_for(name))
    except ValidationError as exc:
        raise ToolRefusal(f"That knowledge base can't be made: {exc.errors()[0].get('msg')}") from None
    existing = await bases.list_bases()
    if any(b.name.lower() == name.lower() for b in existing):
        raise ToolRefusal(f'A knowledge base is already called "{name}": add to it, or say another name.')
    try:
        return body.model_copy(update={"folder": validate_folder(body.folder, existing)})
    except KnowledgePathError as exc:
        raise ToolRefusal(str(exc)) from None


async def _preview_create_base(caller: Any, args: dict[str, Any]) -> dict:
    body = await _new_base(args)
    return {"will_create": {"type": "knowledge_base", "name": body.name, "folder": body.folder},
            "note": "Files already in its folder are indexed with its embedding model."}


async def _create_base(caller: Any, args: dict[str, Any]) -> dict:
    from core.knowledge.errors import KnowledgePathError

    body = await _new_base(args)
    try:
        kb = await _host.create_base(caller, body)
    except (KnowledgePathError, ValueError) as exc:  # the route's 422s, in its words
        raise ToolRefusal(str(exc)) from None
    return {"knowledge_base": kb.name, "id": kb.id, "folder": kb.folder, "status": kb.status,
            "note": "Files already in its folder are being indexed; add files, pages or notes to fill it."}


async def _preview_add_file(caller: Any, args: dict[str, Any]) -> dict:
    """Where the file would land, or the upload's refusal (415, 413, already there), before anything is written."""
    from core.knowledge import ingest

    kb = await _base(str(args.get("knowledge_base") or ""))
    try:
        rel, path, size = await ingest.vault_file_lands(kb, str(args.get("path") or "").strip().lstrip("/"))
    except (ingest.UploadError, ingest.NotFound) as exc:
        raise ToolRefusal(str(exc)) from None
    return {"will_add": {"file": rel, "lands_at": path, "size_bytes": size, "knowledge_base": kb.name},
            "note": "Indexing it uses the knowledge base's embedding model."}


async def _add_file(caller: Any, args: dict[str, Any]) -> dict:
    from core.knowledge import documents, ingest

    kb = await _base(str(args.get("knowledge_base") or ""))
    try:
        rows = await _host.add_vault_file(caller, kb, str(args.get("path") or "").strip().lstrip("/"))
    except (ingest.UploadError, ingest.NotFound) as exc:  # the upload's 413/415, a file that is not there
        raise ToolRefusal(str(exc)) from None
    return {"knowledge_base": kb.name, "documents": [documents.to_out(r).model_dump(mode="json") for r in rows]}


async def _list_documents(caller: Any, args: dict[str, Any]) -> dict:
    """A base's documents, as its screen lists them: each file, where it stands, why it failed."""
    from core.knowledge import documents, progress

    kb = await _base(str(args.get("knowledge_base") or ""))
    status = str(args.get("status") or "").strip() or None
    limit = max(1, min(int(args.get("limit") or 50), 200))
    rows = await documents.list_for(kb.id, limit=limit, status=status)
    listed = await progress.attach([documents.to_out(r) for r in rows])
    return {"knowledge_base": kb.name, "documents": [d.model_dump(mode="json") for d in listed]}


async def _changed(kb: Any, args: dict[str, Any]) -> Any:
    """The edit `update_knowledge_base` would save: a new name (none another base has) and what it holds."""
    from pydantic import ValidationError

    from core.knowledge import bases
    from core.knowledge.api_models import KnowledgeBaseUpdate

    fields = {k: str(args[k]).strip() for k in ("name", "description") if args.get(k) is not None}
    if not fields:
        raise ToolRefusal("Say what changes: its name, or what it holds (description).")
    if (name := fields.get("name")) and any(b.id != kb.id and b.name.lower() == name.lower()
                                            for b in await bases.list_bases()):
        raise ToolRefusal(f'A knowledge base is already called "{name}": say another name.')
    try:
        return KnowledgeBaseUpdate(revision=kb.revision, **fields)
    except ValidationError as exc:
        raise ToolRefusal(f"That change can't be saved: {exc.errors()[0].get('msg')}") from None


async def _update_base(caller: Any, args: dict[str, Any]) -> dict:
    from core.knowledge.errors import StaleRevision

    kb = await _base(str(args.get("knowledge_base") or ""))
    try:
        _, after, _ = await _host_update(caller, kb.id, await _changed(kb, args))
    except StaleRevision:
        raise ToolRefusal("Someone changed this knowledge base just now; ask again to change it.") from None
    return {"knowledge_base": after.name, "id": after.id, "description": after.description}


async def _retry_rebuild(caller: Any, args: dict[str, Any]) -> dict:
    """The screen's retry of a rebuild: an edit that changes nothing re-checks the index and starts it again."""
    from core.knowledge.api_models import KnowledgeBaseUpdate

    kb = await _base(str(args.get("knowledge_base") or ""))
    _, after, started = await _host_update(caller, kb.id, KnowledgeBaseUpdate(revision=kb.revision))
    return {"knowledge_base": after.name, "rebuild_started": bool(started),
            "note": "A rebuild started beside the serving index." if started
                    else "Its index is current: nothing to rebuild."}


async def _host_update(caller: Any, kb_id: str, body: Any) -> tuple:
    return await (_host.update_base or _oss_update_base)(caller, kb_id, body)


async def _preview_delete_base(caller: Any, args: dict[str, Any]) -> dict:
    """What deleting a base forgets, and what searches or fills it (as `delete_recording`'s preview names them)."""
    from core.knowledge import bases

    kb = await _base(str(args.get("knowledge_base") or ""))
    uses = await bases.used_by(kb.id)
    note = "Its index and documents go; its Vault folder and files stay."
    if uses["workflows"] or uses["agents"]:
        note += " What uses it stops finding anything in it."
    return {"will_delete": {"type": "knowledge_base", "id": kb.id, "name": kb.name, "folder": kb.folder},
            "used_by": uses, "note": note}


async def _delete_base(caller: Any, args: dict[str, Any]) -> dict:
    kb = await _base(str(args.get("knowledge_base") or ""))
    await (_host.delete_base or _oss_delete_base)(caller, kb.id)
    return {"deleted": kb.id, "name": kb.name, "folder_kept": kb.folder}


async def _retry(caller: Any, args: dict[str, Any]) -> dict:
    from core.knowledge import documents, ingest

    again = await ingest.retry_document(await _document(str(args.get("doc_id") or "")))
    if again is None:
        raise ToolRefusal("This document is being worked on; retry once it rests.")
    return documents.to_out(again).model_dump(mode="json")


async def _delete_document(caller: Any, args: dict[str, Any]) -> dict:
    from core.knowledge import ingest

    row = await _document(str(args.get("doc_id") or ""))
    await ingest.delete_document(row)
    return {"deleted": getattr(row, "path", None) or str(args.get("doc_id"))}


async def _preview_delete_document(caller: Any, args: dict[str, Any]) -> dict:
    """What deleting the document removes, before the confirmation (MCP2's token)."""
    row = await _document(str(args.get("doc_id") or ""))
    return {"deletes": f'the document "{getattr(row, "path", None) or row.id}"',
            "with_it": "its Vault file, so it is not indexed again"}


_KB = {"knowledge_base": {"type": "string", "description": "The knowledge base's name (or its id)."}}
_DOC = {"doc_id": {"type": "string", "description": "The document's id."}}


def entries() -> list[ProductTool]:
    from core.mcp_server import knowledge

    act = {"kind": "act", "area": "knowledge", "min_role": Role.BUILDER, "scope": "build", "offered": _on}
    return [
        ProductTool(name=knowledge.NAME, description=knowledge.DESCRIPTION,
                    input_schema=dict(knowledge.SEARCH_SCHEMA), handler=_search, kind="read",
                    area="knowledge", offered=_on),
        ProductTool(name="create_knowledge_base",
                    description="Make a knowledge base: a Vault folder whose files agents search (retrieve steps, "
                                "an agent's knowledge). Its folder is knowledge/<its name> unless one is named; files "
                                "already there are indexed. Its index uses the defaults, which a person changes on the "
                                "Knowledge screen. Fill it with add_knowledge_file, add_knowledge_pages or "
                                "add_knowledge_note.",
                    input_schema={"type": "object", "properties": {
                        "name": {"type": "string", "description": "What it holds, e.g. 'Price list'."},
                        "description": {"type": "string", "description": "What agents find in it."},
                        "folder": {"type": "string", "description": "A Vault folder; omit for knowledge/<name>."}},
                        "required": ["name"]},
                    handler=_create_base, preview=_preview_create_base, audit_action="knowledge.base.created",
                    target=lambda a, r: ("knowledge_base", str((r or {}).get("id") or "")), **act),
        ProductTool(name="add_knowledge_file",
                    description="Add a file the person already has in the Vault to a knowledge base: copied into its "
                                "folder and indexed with its embedding model. Takes what an upload takes (documents, "
                                "spreadsheets, CSV, text, code, PDFs), up to the upload's size per file.",
                    input_schema={"type": "object", "properties": {
                        **_KB, "path": {"type": "string", "description": "The file's Vault path, e.g. 'reports/prices.csv'."}},
                        "required": ["knowledge_base", "path"]},
                    handler=_add_file, preview=_preview_add_file, audit_action="knowledge.file.added",
                    target=lambda a, r: ("knowledge_base", str((r or {}).get("knowledge_base") or "")), **act),
        ProductTool(name="add_knowledge_pages",
                    description="Add public web pages to a knowledge base: a page, a sitemap's pages, or a site "
                                "crawled from a URL. Crawling and indexing them uses its embedding model.",
                    input_schema={"type": "object", "properties": {
                        **_KB, "urls": {"type": "array", "items": {"type": "string"}},
                        "kind": {"type": "string", "enum": ["page", "sitemap", "site"]}},
                        "required": ["knowledge_base", "urls"]},
                    handler=_add_pages, open_world=True, audit_action="knowledge.pages.added",
                    target=lambda a, r: ("knowledge_base", str((r or {}).get("knowledge_base") or "")), **act),
        ProductTool(name="add_knowledge_note", description="Add a note (text) to a knowledge base.",
                    input_schema={"type": "object", "properties": {
                        **_KB, "title": {"type": "string"}, "text": {"type": "string"}},
                        "required": ["knowledge_base", "text"]},
                    handler=_add_note, audit_action="knowledge.records.written",
                    target=lambda a, r: ("knowledge_base", str((r or {}).get("knowledge_base") or "")), **act),
        ProductTool(name="list_knowledge_documents", kind="read", area="knowledge", offered=_on,
                    description="A knowledge base's documents: each file, where it stands (queued, indexed, failed) "
                                "and why one failed.",
                    input_schema={"type": "object", "properties": {
                        **_KB, "status": {"type": "string", "description": "Only those: queued, indexed, failed…"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, "required": ["knowledge_base"]},
                    handler=_list_documents),
        ProductTool(name="update_knowledge_base",
                    description="Rename a knowledge base, or say what it holds (its description). Its folder and "
                                "index settings are changed on the Knowledge screen.",
                    input_schema={"type": "object", "properties": {
                        **_KB, "name": {"type": "string"}, "description": {"type": "string"}},
                        "required": ["knowledge_base"]},
                    handler=_update_base, audit_action="knowledge.base.updated",
                    target=lambda a, r: ("knowledge_base", str((r or {}).get("id") or "")), **act),
        ProductTool(name="retry_knowledge_rebuild",
                    description="Start a knowledge base's rebuild again after it failed or was paused, as its "
                                "screen's Retry does; nothing happens when its index is current.",
                    input_schema={"type": "object", "properties": dict(_KB), "required": ["knowledge_base"]},
                    handler=_retry_rebuild, idempotent=True, audit_action="knowledge.base.rebuilt",
                    target=lambda a, r: ("knowledge_base", str((r or {}).get("knowledge_base") or "")), **act),
        ProductTool(name="delete_knowledge_base",
                    description="Delete a knowledge base: its index and documents go, its Vault folder and files stay. "
                                "Cannot be undone: the first call previews it, with the workflows and agents that "
                                "use it, and gives a confirm_token.",
                    input_schema={"type": "object", "properties": dict(_KB), "required": ["knowledge_base"]},
                    handler=_delete_base, irreversible=True, preview=_preview_delete_base,
                    audit_action="knowledge.base.deleted",
                    target=lambda a, r: ("knowledge_base", str((r or {}).get("deleted") or "")), **act),
        ProductTool(name="retry_knowledge_document", description="Index a document again after it failed.",
                    input_schema={"type": "object", "properties": dict(_DOC), "required": ["doc_id"]},
                    handler=_retry, idempotent=True, audit_action="knowledge.document.retried",
                    target=lambda a, r: ("knowledge_document", str(a.get("doc_id"))), **act),
        ProductTool(name="delete_knowledge_document",
                    description="Delete a document from its knowledge base, with its Vault file.",
                    input_schema={"type": "object", "properties": dict(_DOC), "required": ["doc_id"]},
                    handler=_delete_document, irreversible=True, preview=_preview_delete_document,
                    audit_action="knowledge.document.deleted",
                    target=lambda a, r: ("knowledge_document", str(a.get("doc_id"))), **act),
    ]
