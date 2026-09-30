"""The ingestion job's entry point — what every dispatcher runs.

`INGEST_ENTRYPOINT` (`core.knowledge.seams`) names `run_ingest_job`: OSS's
in-process dispatcher awaits it, OSS scale mode and cloud enqueue a job that
calls it in a worker. **A1 builds it** (sync → parse → chunk → embed → index,
with the status machine and the report); until then it refuses honestly.

Owned by A1 after KB0: only the signature below is part of the contract.
"""
from __future__ import annotations

from core.knowledge.errors import KnowledgeUnavailable
from core.knowledge.status import IngestionReport
from core.knowledge.types import IngestJob


async def run_ingest_job(job: IngestJob) -> IngestionReport | None:
    """Do `job`. Returns the document's report, or None for work that is not one document
    (a reindex, a KB delete)."""
    raise KnowledgeUnavailable("Knowledge ingestion is not built yet (lane A1).")
