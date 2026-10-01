"""The Retrieve and Ingest workflow steps (R1, D166).

A workflow searches its knowledge bases with a **Retrieve** step and adds what it
made to one with an **Ingest** step. The executors in `core/orchestration/steps.py`
are thin: they hand the step's settings here and write what comes back into
the run's shared state.

**Settings** are the contract's own bodies (`api_models.RetrieveStepConfig`,
`IngestStepConfig`), stored on the step as `retrieve` / `ingest`. They are held
to the contract here, when the step runs — the editor saves a half-filled step,
as it saves an agent step with no agent yet — and a step that does not validate
fails with the field named.

**Retrieve** asks `search.search` once, with `Origin(surface="step")` and the
run's ids, so its query embedding and rerank are cost rows on the run (`ledger`
→ `usage_logs.run_id`) and its trace is stored in `knowledge_queries`. A step
pinned to a `ksnap_…` snapshot searches that index version and nothing else;
`"active"` searches each base's active one at that moment, and the output says
which (`snapshot_ids`). The output's `context` is the passages as an agent's
`search_knowledge` shows them — between markers, under their `[K#]` labels,
sanitized, with the line saying they are documents and not instructions — so
a later agent step can read it safely. `citations` names where each label's
words are. The labels are the step's own: a later agent's own searches number
theirs from `[K1]` again.

**Ingest** turns a list of objects in shared state into records
(`ingest.put_records`), all or nothing: every record is checked before any is
written. A record with no `id_field` is named by its content, so running the
same records again changes nothing. It answers with each document as it stands
— `queued` for new or changed records — and never waits for indexing, as the
records API answers 202. Indexing's cost is the knowledge base's
(`knowledge:<kb_id>`, D153), not the run's: it happens in the job afterwards.

Both refuse while Knowledge is off.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from core.knowledge.api_models import (
    MAX_BATCH, Citation, IngestStepConfig, IngestStepOutput, RecordIn, RecordIngestRequest, RetrieveStepConfig,
    RetrieveStepOutput, SearchRequest,
)
from core.knowledge.events import KNOWLEDGE_INGEST, KNOWLEDGE_RETRIEVE
from core.knowledge.ids import SNAPSHOT_PREFIX, is_id
from core.knowledge.trace import Origin

#: The longest query a search takes (`SearchRequest.query`); a rendered template past it is cut, with a warning.
MAX_QUERY = 2000
#: The most records one Ingest step writes. More is refused, never cut: a Loop step splits them.
MAX_INGEST_RECORDS = 1000


class StepFailed(RuntimeError):
    """The step cannot do its work; the message says why, in words the workflow's author can act on."""


# ── settings ───────────────────────────────────────────────────────────────────

def _first_error(exc: ValidationError) -> str:
    error = exc.errors()[0]
    where = ".".join(str(p) for p in error.get("loc") or ()) or "settings"
    return f"{where}: {error.get('msg', 'is not valid')}"


def check_enabled(kind: str, step_name: str, settings: Any = None) -> None:
    from core.knowledge.config import knowledge_enabled

    if not knowledge_enabled(settings):
        raise StepFailed(f"Knowledge is switched off, so {kind} step '{step_name}' cannot run.")


def retrieve_config(raw: Any, *, step_name: str) -> RetrieveStepConfig:
    """The step's `retrieve` settings, held to the contract."""
    try:
        config = RetrieveStepConfig.model_validate(raw or {})
    except ValidationError as exc:
        raise StepFailed(f"Retrieve step '{step_name}' is not set up: {_first_error(exc)}") from None
    pinned_snapshot(config, step_name=step_name)
    return config


def pinned_snapshot(config: RetrieveStepConfig, *, step_name: str) -> dict[str, str] | None:
    """`{kb_id: ksnap_…}` for a pinned step; None for `"active"`. A pin names one knowledge base's version, so a
    pinned step searches exactly one."""
    snapshot = (config.snapshot or "active").strip()
    if snapshot == "active":
        return None
    if not is_id(snapshot, SNAPSHOT_PREFIX):
        raise StepFailed(f"Retrieve step '{step_name}': snapshot is \"active\" or a {SNAPSHOT_PREFIX}… id, "
                         f"not {snapshot[:80]!r}.")
    if len(config.kb_ids) != 1:
        raise StepFailed(f"Retrieve step '{step_name}' is pinned to {snapshot}, which is one knowledge base's "
                         f"index version: search exactly one knowledge base, or search \"active\".")
    return {config.kb_ids[0]: snapshot}


def ingest_config(raw: Any, *, step_name: str) -> IngestStepConfig:
    """The step's `ingest` settings, held to the contract."""
    try:
        config = IngestStepConfig.model_validate(raw or {})
    except ValidationError as exc:
        raise StepFailed(f"Ingest step '{step_name}' is not set up: {_first_error(exc)}") from None
    if not config.kb_id.strip():
        raise StepFailed(f"Ingest step '{step_name}' is not set up: kb_id: name a knowledge base.")
    if not config.records_from.strip():
        raise StepFailed(f"Ingest step '{step_name}' is not set up: records_from: name the state key that holds "
                         f"the records.")
    return config


# ── Retrieve ───────────────────────────────────────────────────────────────────

@dataclass
class Retrieved:
    output: RetrieveStepOutput
    event: dict
    warnings: list[str] = field(default_factory=list)


def context_text(query: str, hits: list, names: list[str], budget: int) -> str:
    """The passages as a later step reads them: the agent tool's markers, labels and sanitizing, within `budget`
    tokens; passages past it are named, and their whole text stays in the step's `chunks`."""
    from core.knowledge.chunker import estimate_tokens
    from core.knowledge.citations import sanitize
    from core.knowledge.tools import UNTRUSTED, _cut, _one_line, _where

    where_from = ", ".join(names)
    if not hits:
        return f'No passages matched "{sanitize(_one_line(query, 200))}" in {where_from}.'
    blocks: list[str] = []
    not_shown: list[str] = []
    used = 0
    for hit in hits:
        where = _where(hit.citation, hit.metadata)
        body = sanitize((f"{hit.context}\n" if hit.context else "") + hit.content)
        block = f"{hit.label} {where}\n{body}"
        if not not_shown and used + estimate_tokens(block) <= budget:
            blocks.append(block)
            used += estimate_tokens(block)
        elif not blocks and not not_shown:
            shown = _cut(body, max(1, budget - estimate_tokens(f"{hit.label} {where}")),
                         "[… cut at the context budget — the whole passage is in this step's chunks]")
            blocks.append(f"{hit.label} {where}\n{shown}")
            used += estimate_tokens(blocks[-1])
        else:
            not_shown.append(f"{hit.label} {where}")
    count = len(hits)
    lines = [
        f"{count} passage{'s' if count != 1 else ''} from {where_from}.",
        UNTRUSTED,
        "<<KNOWLEDGE RESULTS>>",
        "\n\n".join(blocks),
        "<<END KNOWLEDGE RESULTS>>",
    ]
    if not_shown:
        lines.append(f"Not shown (over the {budget:,}-token budget): {'; '.join(not_shown)} — their text is in "
                     f"this step's chunks.")
    return "\n".join(lines)


async def retrieve(config: RetrieveStepConfig, query: str, *, origin: Origin, step_id: str, step_name: str,
                   settings: Any = None) -> Retrieved:
    """Search as the step says. `query` is the step's template already rendered against the run's state."""
    from core.knowledge import bases, search
    from core.knowledge.errors import KnowledgeDisabled, KnowledgeError

    check_enabled("Retrieve", step_name, settings)
    warnings: list[str] = []
    query = (query or "").strip()
    if not query:
        raise StepFailed(f"Retrieve step '{step_name}': the query is empty once the run's state is filled in.")
    if len(query) > MAX_QUERY:
        warnings.append(f"The query was {len(query):,} characters once filled in; the first {MAX_QUERY:,} were "
                        f"searched.")
        query = query[:MAX_QUERY]
    pinned = pinned_snapshot(config, step_name=step_name)

    t0 = time.monotonic()
    try:
        response = await search.search(
            SearchRequest(query=query, kb_ids=config.kb_ids, filters=config.filters, top_k=config.top_k,
                          rerank=config.rerank, snapshot_ids=pinned, include_trace=True),
            origin=origin, settings=settings)
    except KnowledgeDisabled:
        raise StepFailed(f"Knowledge is switched off, so Retrieve step '{step_name}' cannot run.") from None
    except KnowledgeError as exc:
        raise StepFailed(f"Retrieve step '{step_name}': {exc}") from None
    latency_ms = int((time.monotonic() - t0) * 1000)

    snapshot_ids = {kb_id: p.snapshot_id for kb_id, p in (response.trace.pipeline if response.trace else {}).items()}
    known = {kb.id: kb for kb in await bases.list_bases()}
    searched = [known[k] for k in config.kb_ids if k in known]
    budget = max((kb.retrieval.max_context_tokens for kb in searched), default=4000)
    names = [kb.name for kb in searched] or list(config.kb_ids)
    citations: list[Citation] = [hit.citation for hit in response.hits]
    output = RetrieveStepOutput(
        context=context_text(query, response.hits, names, budget), chunks=response.hits, citations=citations,
        trace_id=response.query_id, snapshot_ids=snapshot_ids)
    event = {
        "type": KNOWLEDGE_RETRIEVE, "orch_step_id": step_id, "step_name": step_name, "query_id": response.query_id,
        "snapshot_ids": sorted(set(snapshot_ids.values())), "hit_count": len(response.hits),
        "cost_usd": float(response.cost.total_est_usd or 0.0), "latency_ms": latency_ms,
        "degraded": list(response.degraded),
    }
    return Retrieved(output, event, warnings)


# ── Ingest ─────────────────────────────────────────────────────────────────────

@dataclass
class Ingested:
    output: IngestStepOutput
    event: dict


_MISSING = object()


def content_id(body: dict, metadata: dict) -> str:
    """A record's id from what it holds: the same record always gets the same id, so writing it again is a no-op."""
    canonical = json.dumps({"body": body, "metadata": metadata}, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _type_name(value: Any) -> str:
    return {str: "text", int: "number", float: "number", bool: "true/false", list: "list",
            dict: "object", type(None): "nothing"}.get(type(value), type(value).__name__)


def records_for(config: IngestStepConfig, value: Any, *, step_name: str) -> list[RecordIn]:
    """The records `value` holds, every one checked before any is written; the first that is not a record fails the
    step. One object is a list of one. Records with the same id are one record: the last one wins, as writing them
    in order would."""
    key = state_key(config)
    if value is _MISSING:
        raise StepFailed(f"Ingest step '{step_name}': the run's state has no {key!r}, so there is nothing to ingest.")
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        raise StepFailed(f"Ingest step '{step_name}': {key!r} holds {_type_name(value)}, not a list of objects.")
    if len(value) > MAX_INGEST_RECORDS:
        raise StepFailed(f"Ingest step '{step_name}': {key!r} holds {len(value):,} records; one step ingests at most "
                         f"{MAX_INGEST_RECORDS:,}. Split them with a Loop step.")
    records: dict[str, RecordIn] = {}
    for n, obj in enumerate(value):
        label = f"record {n + 1} of {key!r}"
        if not isinstance(obj, dict):
            raise StepFailed(f"Ingest step '{step_name}': {label} is {_type_name(obj)}, not an object.")
        if config.text_field:
            text = obj.get(config.text_field)
            if text is None or text == "":
                raise StepFailed(f"Ingest step '{step_name}': {label} has no {config.text_field!r} to index.")
            body: dict[str, Any] = {"text": _text(text)}
        else:
            body = {"json": obj}
        metadata = {name: obj[name] for name in config.metadata_fields if name in obj}
        if config.id_field:
            given = obj.get(config.id_field)
            if given is None or given == "":
                raise StepFailed(f"Ingest step '{step_name}': {label} has no {config.id_field!r} to name it by.")
            record_id = str(given)
        else:
            record_id = content_id(body, metadata)
        try:
            record = RecordIn.model_validate({"id": record_id, **body, "metadata": metadata})
        except ValidationError as exc:
            error = exc.errors()[0]
            raise StepFailed(f"Ingest step '{step_name}': {label} ({record_id[:80]}): "
                             f"{error.get('msg', 'is not a valid record')}") from None
        records.pop(record_id, None)
        records[record_id] = record
    return list(records.values())


def state_key(config: IngestStepConfig) -> str:
    """The state key `records_from` names; a `state.` in front is allowed."""
    key = config.records_from.strip()
    return key[len("state."):] if key.startswith("state.") else key


async def ingest(config: IngestStepConfig, state: dict, *, step_id: str, step_name: str,
                 settings: Any = None) -> Ingested:
    """Write the records in `state[records_from]` into the step's knowledge base."""
    from core.knowledge import bases, documents
    from core.knowledge import ingest as records_api
    from core.knowledge.errors import KnowledgeError

    check_enabled("Ingest", step_name, settings)
    kb = await bases.get_base(config.kb_id)
    if kb is None:
        raise StepFailed(f"Ingest step '{step_name}': there is no knowledge base {config.kb_id!r}.")
    if kb.status != "active":
        raise StepFailed(f"Ingest step '{step_name}': knowledge base '{kb.name}' is disabled.")
    records = records_for(config, state.get(state_key(config), _MISSING), step_name=step_name)

    rows = []
    try:
        for start in range(0, len(records), MAX_BATCH):
            rows += await records_api.put_records(kb, RecordIngestRequest(records=records[start:start + MAX_BATCH]))
    except KnowledgeError as exc:
        raise StepFailed(f"Ingest step '{step_name}': {exc}") from None

    record_of = {records_api.record_path(kb, r.id): r.id for r in records}
    out = [documents.to_out(row) for row in rows]
    event = {
        "type": KNOWLEDGE_INGEST, "orch_step_id": step_id, "step_name": step_name, "kb_id": kb.id,
        "documents": [{"doc_id": d.id, "record_id": record_of.get(d.path), "status": d.status} for d in out],
    }
    return Ingested(IngestStepOutput(kb_id=kb.id, documents=out), event)
