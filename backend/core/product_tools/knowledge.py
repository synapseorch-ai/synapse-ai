"""Knowledge's search, as a product tool (MCP2, D292): a read, listed while Knowledge is on (D176). And what a
person does to a knowledge base that exists (ASSIST1, D291, F684): add public web pages to it, add a note, retry or
delete a document, a builder's acts as on the Knowledge screen. A new knowledge base is made on that screen, where its
Vault folder and its index are chosen. A host whose Knowledge routes do more (cloud's storage and quota) registers its
own handlers for these names.

Its code is `core.mcp_server.knowledge`, with the `set_knowledge_searcher` seam cloud installs; the caller's
credential is handed on, as before, so a search's trace names the key that asked.
"""
from __future__ import annotations

from typing import Any

from core.product_tools.errors import ToolRefusal
from core.product_tools.registry import ProductTool
from core.product_tools.roles import Role


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
