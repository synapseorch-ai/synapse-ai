"""The frozen contract, as data — so a change to it is a visible diff, never an accident.

`contract_snapshot()` records every protocol signature, dataclass field, table
column (as each dialect compiles it), CHECK value, status transition, event
and span name, setting default, filter rule, tool schema and route.
`api_schema()` records the JSON schema of every API model. Both are committed
under `tests/fixtures/knowledge/`; `test_knowledge_contract_frozen` fails on any
difference. Updating them is how a contract-change request (16 §8) lands:
`python backend/tests/fixtures/knowledge/make_fixtures.py --snapshots`.
"""
from __future__ import annotations

import dataclasses
import inspect
from typing import Any

from pydantic import BaseModel, TypeAdapter


def _params(fn) -> list[list[str]]:
    out = []
    for p in inspect.signature(fn).parameters.values():
        if p.name == "self":
            continue
        annotation = p.annotation if isinstance(p.annotation, str) else getattr(p.annotation, "__name__", repr(p.annotation))
        default = "" if p.default is inspect.Parameter.empty else repr(p.default)
        out.append([p.name, p.kind.name, annotation if p.annotation is not inspect.Parameter.empty else "", default])
    return out


def _protocol(proto) -> dict[str, Any]:
    methods = {}
    attributes = sorted(getattr(proto, "__annotations__", {}))
    for name, member in sorted(vars(proto).items()):
        if name.startswith("_") or not callable(member):
            continue
        methods[name] = {"async": inspect.iscoroutinefunction(member), "params": _params(member)}
    return {"attributes": attributes, "methods": methods}


def _dataclass(cls) -> list[list[str]]:
    return [[f.name, str(f.type)] for f in dataclasses.fields(cls)]


def _table(name: str) -> dict[str, Any]:
    from sqlalchemy.dialects import postgresql, sqlite
    from sqlalchemy.schema import CheckConstraint, UniqueConstraint

    from core.store.models import Base

    table = Base.metadata.tables[name]
    columns = [[c.name, str(c.type.compile(dialect=sqlite.dialect())), str(c.type.compile(dialect=postgresql.dialect())),
                "null" if c.nullable else "not null", "pk" if c.primary_key else ""] for c in table.columns]
    checks = sorted(str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint))
    uniques = sorted([c.name, [col.name for col in c.columns]] for c in table.constraints
                     if isinstance(c, UniqueConstraint))
    indexes = sorted([i.name, [c.name for c in i.columns], bool(i.unique)] for i in table.indexes)
    foreign = sorted([fk.parent.name, fk.target_fullname, fk.ondelete or ""] for fk in table.foreign_keys)
    return {"columns": columns, "checks": checks, "uniques": uniques, "indexes": indexes, "foreign_keys": foreign}


def contract_snapshot() -> dict[str, Any]:
    from core import vault_events
    from core.knowledge import (
        agent_contract, api_models, config, events, filters, ids, kb_settings, parsed, pipeline_contract, seams,
        status, telemetry, textsearch, trace, types,
    )

    protocols = {name: _protocol(getattr(seams, name)) for name in (
        "KnowledgeIndex", "KnowledgeEmbedder", "KnowledgeReranker", "IngestDispatcher", "OcrProvider")}
    dataclass_names = sorted(n for n, v in vars(types).items() if dataclasses.is_dataclass(v) and isinstance(v, type))
    return {
        "protocols": protocols,
        "seam_functions": {n: _params(getattr(seams, n)) for n in (
            "set_knowledge_index", "set_knowledge_embedder", "set_reranker", "set_ingest_dispatcher",
            "set_ocr_provider", "get_reranker")},
        "seam_defaults": [seams.DEFAULT_INDEX, seams.DEFAULT_EMBEDDER, seams.DEFAULT_RERANKER, seams.INGEST_ENTRYPOINT],
        "types": {n: _dataclass(getattr(types, n)) for n in dataclass_names},
        "cost": {"stages": list(types.COST_STAGES), "sources": list(types.COST_SOURCES), "units": list(types.UNITS)},
        "tables": {n: _table(n) for n in ("knowledge_documents", "knowledge_chunks", "knowledge_snapshots",
                                          "knowledge_queries", "knowledge_costs")},
        "statuses": list(status.STATUSES),
        "transitions": {k: sorted(v) for k, v in sorted(status.TRANSITIONS.items())},
        "reasons": {k: list(v) for k, v in sorted(status.REASONS.items())},
        "warning_codes": list(parsed.WARNING_CODES),
        "parsed_schema_version": parsed.PARSED_SCHEMA_VERSION,
        "filters": {"ops": list(filters.FIELD_OPS) + list(filters.LOGIC_OPS), "builtins": filters.BUILTINS,
                    "sources": list(filters.SOURCE_VALUES), "reserved": sorted(filters.RESERVED_METADATA),
                    "limits": dataclasses.asdict(filters.LIMITS), "error_codes": list(filters.FILTER_ERROR_CODES),
                    "field_re": filters.FIELD_RE.pattern},
        "textsearch": {"languages": list(textsearch.TS_LANGUAGES), "tsv": textsearch.tsv_expression_sql()},
        "trace": {"version": trace.TRACE_VERSION, "snippet_max": trace.SNIPPET_MAX, "max_bytes": trace.TRACE_MAX_BYTES,
                  "forbidden_keys": sorted(trace.FORBIDDEN_KEYS), "degraded": list(trace.DEGRADED_CODES),
                  "surfaces": list(trace.SURFACES)},
        "events": {"run": list(events.RUN_EVENT_TYPES), "owners": events.RUN_EVENT_OWNERS,
                   "lifecycle": list(events.LIFECYCLE_TYPES), "webhooks": events.WEBHOOK_EVENTS},
        "telemetry": {"spans": list(telemetry.SPANS), "formats": list(telemetry.FORMATS)},
        "settings": config.DEFAULTS,
        "kb": {"collection": kb_settings.COLLECTION, "refused_roots": sorted(kb_settings.REFUSED_ROOTS),
               "index_affecting": list(kb_settings.INDEX_AFFECTING),
               "schema": kb_settings.KnowledgeBase.model_json_schema()},
        "ids": {"prefixes": [ids.KB_PREFIX, ids.DOCUMENT_PREFIX, ids.SNAPSHOT_PREFIX, ids.QUERY_PREFIX,
                             ids.CHUNK_PREFIX, ids.UPLOAD_PREFIX, ids.CRAWL_PREFIX, ids.EVENT_PREFIX,
                             ids.WEB_SOURCE_PREFIX],
                "dirs": [ids.WEB_DIR, ids.RECORDS_DIR], "record_id": ids.RECORD_ID_RE.pattern},
        "agent": {"tools": [agent_contract.SEARCH_TOOL, agent_contract.READ_TOOL],
                  "search_schema": agent_contract.SEARCH_SCHEMA, "read_schema": agent_contract.READ_SCHEMA,
                  "label_re": agent_contract.LABEL_RE.pattern},
        "vault_events": {"change": _dataclass(vault_events.VaultChange),
                         "notify": _params(vault_events.notify_vault_change)},
        "pipeline": {"entrypoint": pipeline_contract.INDEX_ENTRYPOINT,
                     "context": _dataclass(pipeline_contract.IndexContext),
                     "outcome": _dataclass(pipeline_contract.IndexOutcome)},
        "step_types": list(api_models.STEP_TYPES),
        "routes": [[r.method, r.path, getattr(r.request, "__name__", None), _type_name(r.response), r.status,
                    sorted(r.surfaces), r.bucket, r.scope] for r in api_models.ROUTES],
    }


def _type_name(t: Any) -> str | None:
    if t is None:
        return None
    return getattr(t, "__name__", None) or str(t)


def api_schema() -> dict[str, Any]:
    """The JSON schema of every model the API and the steps use."""
    from core.knowledge import api_models

    models = {name: value for name, value in vars(api_models).items()
              if isinstance(value, type) and issubclass(value, BaseModel) and value.__module__ in (
                  api_models.__name__, "core.knowledge.trace", "core.knowledge.status", "core.knowledge.kb_settings")
              and not name.startswith("_")}
    return {name: TypeAdapter(model).json_schema(mode="serialization") for name, model in sorted(models.items())}
