"""B1b's test kit: knowledge bases holding real indexed documents.

Documents are indexed through B1a's own `index_document` from the committed
`ParsedDocument` fixtures (`tests/fixtures/knowledge/parsed/`), so a search,
an agent's tool call or a route answers from the same rows production writes.
Use with the `knowledge_store` fixture (`_fakes/knowledge_db.py`).
"""
from __future__ import annotations

from pathlib import Path

from core.knowledge.kb_settings import COLLECTION, KnowledgeBase
from core.knowledge.parsed import ParsedDocument

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "knowledge" / "parsed"

CONTRACTS = KnowledgeBase(id="kb_00000000000a", name="Contracts", folder="contracts",
                          description="Customer contracts and their renewals.")
REPORTS = KnowledgeBase(id="kb_00000000000b", name="Reports", folder="reports")


def parsed(name: str) -> ParsedDocument:
    return ParsedDocument.model_validate_json((FIXTURES / f"{name}.json").read_text("utf-8"))


async def save_bases(*kbs: KnowledgeBase) -> None:
    """Store `kbs` as the tenant's knowledge bases (replacing any there)."""
    from core.store import collections

    await collections.save(COLLECTION, [kb.model_dump(mode="json") for kb in kbs])


async def index_fixture(kb: KnowledgeBase, fixture: str, n: int, path: str, metadata: dict | None = None,
                        *, source_url: str | None = None) -> str:
    """Index the parsed fixture `fixture` into `kb` at `path` as document number `n`; returns its id."""
    from core.knowledge.pipeline import index_document
    from core.knowledge.pipeline_contract import IndexContext
    from core.knowledge.snapshots import serving_snapshot
    from core.knowledge.types import Attribution
    from core.store import session
    from core.store.models import KnowledgeDocumentDB
    from core.tenancy import get_tenant

    doc_id = "kdoc_" + f"{n:032x}"
    async with session() as s:
        s.add(KnowledgeDocumentDB(id=doc_id, tenant_id=get_tenant(), kb_id=kb.id, path=path, source="upload",
                                  status="indexing", version=1, source_url=source_url))
    doc = parsed(fixture)
    await index_document(IndexContext(
        kb=kb, doc_id=doc_id, doc_version=1, sha256=doc.source.sha256, path=path, source="upload",
        parsed=doc, snapshot_id=await serving_snapshot(kb),
        attribution=Attribution(kb_id=kb.id, doc_id=doc_id), metadata=metadata or {}))
    return doc_id


async def corpus() -> None:
    """Contracts (a contract PDF and an email with an attachment) and Reports (a workbook)."""
    await save_bases(CONTRACTS, REPORTS)
    await index_fixture(CONTRACTS, "pdf_contract", 1, "contracts/acme.pdf", {"doc_type": "contract", "region": "EU"})
    await index_fixture(CONTRACTS, "eml_with_attachment", 2, "contracts/mail/renewal.eml", {"doc_type": "email"})
    await index_fixture(REPORTS, "xlsx_two_sheets", 3, "reports/q3.xlsx", {"doc_type": "report"})
