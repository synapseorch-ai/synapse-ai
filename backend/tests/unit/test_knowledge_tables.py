"""The six Knowledge tables: portable, tenant-scoped, and saying no to what they must refuse (D153, CCR-6).

SQLite always; Postgres too when `SYNAPSE_TEST_POSTGRES_URL` is set.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable

from core.knowledge import api_models, status, trace, types
from core.knowledge.chunks import CHUNK_KINDS, CHUNK_ROLES
from core.knowledge.parsed import SourceKind
from core.knowledge.vectors import pack, unpack
from core.store.models import (
    Base, KnowledgeChunkDB, KnowledgeCostDB, KnowledgeDocumentDB, KnowledgeEvalRunDB, KnowledgeQueryDB,
    KnowledgeSnapshotDB,
)

TABLES = ("knowledge_documents", "knowledge_chunks", "knowledge_snapshots", "knowledge_queries", "knowledge_costs",
          "knowledge_eval_runs")


def _check_values(model, name: str) -> set[str]:
    """The values a CHECK constraint named `name` allows, read from the model."""
    for c in model.__table__.constraints:
        if c.name == name:
            text = str(c.sqltext)
            return {v.strip().strip("'") for v in text[text.index("(") + 1: text.rindex(")")].split(",")}
    raise AssertionError(f"no constraint {name}")


def test_the_check_values_match_the_contract():
    assert _check_values(KnowledgeDocumentDB, "ck_knowledge_documents_status") == set(status.STATUSES)
    assert _check_values(KnowledgeDocumentDB, "ck_knowledge_documents_source") == set(SourceKind.__args__)
    assert _check_values(KnowledgeChunkDB, "ck_knowledge_chunks_kind") == set(CHUNK_KINDS)
    assert _check_values(KnowledgeChunkDB, "ck_knowledge_chunks_role") == set(CHUNK_ROLES)
    assert _check_values(KnowledgeQueryDB, "ck_knowledge_queries_surface") == set(trace.SURFACES)
    assert _check_values(KnowledgeCostDB, "ck_knowledge_costs_stage") == set(types.COST_STAGES)
    assert _check_values(KnowledgeCostDB, "ck_knowledge_costs_cost_source") == set(types.COST_SOURCES)
    assert _check_values(KnowledgeCostDB, "ck_knowledge_costs_unit") == set(types.UNITS)
    assert _check_values(KnowledgeEvalRunDB, "ck_knowledge_eval_runs_status") == set(api_models.EVAL_RUN_STATUSES)
    assert _check_values(KnowledgeEvalRunDB, "ck_knowledge_eval_runs_started_by") == set(api_models.EVAL_STARTED_BY)


def test_an_active_eval_run_is_one_the_index_counts():
    """The partial index's statuses are the contract's active ones (CCR-6)."""
    index = next(i for i in KnowledgeEvalRunDB.__table__.indexes if i.name == "ux_knowledge_eval_runs_tenant_set_active")
    for where in (index.dialect_options["sqlite"]["where"], index.dialect_options["postgresql"]["where"]):
        text = str(where)
        assert {v.strip().strip("'") for v in text[text.index("(") + 1: text.rindex(")")].split(",")} == set(
            api_models.EVAL_ACTIVE_STATUSES)


@pytest.mark.parametrize("name", TABLES)
def test_portable_ddl(name):
    table = Base.metadata.tables[name]
    lite = str(CreateTable(table).compile(dialect=sqlite.dialect())).upper()
    pg = str(CreateTable(table).compile(dialect=postgresql.dialect())).upper()
    assert "JSONB" not in lite and "BYTEA" not in lite
    for pg_only in (" VECTOR", "TSVECTOR", "REGCONFIG"):
        # Cloud adds its vector and tsvector columns in its own DDL; never here.
        assert pg_only not in lite and pg_only not in pg
    for ddl in (lite, pg):
        line = next(ln for ln in ddl.splitlines() if ln.strip().startswith("TENANT_ID "))
        assert "VARCHAR(255)" in line and "NOT NULL" in line
    for column in table.columns:
        assert column.name != "metadata", "Declarative reserves `metadata`; the column is `meta`"


def test_the_chunk_row_key_is_sqlites_rowid():
    lite = str(CreateTable(KnowledgeChunkDB.__table__).compile(dialect=sqlite.dialect()))
    pg = str(CreateTable(KnowledgeChunkDB.__table__).compile(dialect=postgresql.dialect()))
    assert "\tid INTEGER NOT NULL" in lite and "PRIMARY KEY (id)" in lite
    assert "id BIGSERIAL NOT NULL" in pg
    assert "embedding BLOB" in lite and "embedding BYTEA" in pg


def _doc(tenant: str = "default", *, doc_id: str = "kdoc_" + "1" * 32, path: str = "contracts/a.pdf", **kw):
    values = dict(id=doc_id, tenant_id=tenant, kb_id="kb_00000000000a", path=path, source="upload")
    values.update(kw)
    return KnowledgeDocumentDB(**values)


def _chunk(tenant: str = "default", *, chunk_id: str = "kch_" + "1" * 32, doc_id: str = "kdoc_" + "1" * 32, **kw):
    values = dict(chunk_id=chunk_id, tenant_id=tenant, kb_id="kb_00000000000a", doc_id=doc_id,
                  snapshot_id="ksnap_" + "1" * 32, doc_version=1, ordinal=0, content="hello", char_start=0,
                  char_end=5)
    values.update(kw)
    return KnowledgeChunkDB(**values)


async def _factory(url: str):
    from core.store import build_engine, build_session_factory, init_db
    from core.store.engine import normalise_db_url

    engine = build_engine(normalise_db_url(url))
    if url.startswith("sqlite"):
        await init_db(engine)
    else:
        tables = [Base.metadata.tables[n] for n in TABLES]
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.drop_all(c, tables=list(reversed(tables))))
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    return engine, build_session_factory(engine)


_PG = os.environ.get("SYNAPSE_TEST_POSTGRES_URL", "")
URLS = ["sqlite+aiosqlite:///:memory:"] + ([_PG] if _PG else [])


@pytest.fixture(params=URLS, ids=lambda u: "sqlite" if u.startswith("sqlite") else "postgres")
async def db(request):
    engine, factory = await _factory(request.param)
    yield factory
    if not request.param.startswith("sqlite"):
        tables = [Base.metadata.tables[n] for n in TABLES]
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.drop_all(c, tables=list(reversed(tables))))
    await engine.dispose()


async def test_deleting_a_document_deletes_its_chunks(db):
    async with db() as s:
        s.add(_doc())
        await s.flush()
        s.add(_chunk())
        await s.commit()
    async with db() as s:
        await s.delete(await s.get(KnowledgeDocumentDB, "kdoc_" + "1" * 32))
        await s.commit()
    async with db() as s:
        assert (await s.execute(select(KnowledgeChunkDB))).all() == []


async def test_a_chunk_needs_its_document(db):
    async with db() as s:
        s.add(_chunk(doc_id="kdoc_" + "9" * 32))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_one_path_per_kb_per_tenant(db):
    async with db() as s:
        s.add(_doc())
        s.add(_doc("other-tenant", doc_id="kdoc_" + "2" * 32))  # another tenant may use the same path
        await s.commit()
    async with db() as s:
        s.add(_doc(doc_id="kdoc_" + "3" * 32))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_a_chunk_id_is_unique_per_tenant_only(db):
    async with db() as s:
        s.add(_doc())
        s.add(_doc("t2", doc_id="kdoc_" + "2" * 32))
        await s.flush()
        s.add(_chunk())
        s.add(_chunk("t2", doc_id="kdoc_" + "2" * 32))
        await s.commit()
    async with db() as s:
        s.add(_chunk(ordinal=1))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_one_active_snapshot_per_kb(db):
    def snap(sid: str, state: str) -> KnowledgeSnapshotDB:
        return KnowledgeSnapshotDB(id=sid, tenant_id="default", kb_id="kb_00000000000a", status=state)

    async with db() as s:
        s.add_all([snap("ksnap_a", "active"), snap("ksnap_b", "retired"), snap("ksnap_c", "building")])
        await s.commit()
    async with db() as s:
        s.add(snap("ksnap_d", "active"))
        with pytest.raises(IntegrityError):
            await s.commit()


@pytest.mark.parametrize("model,field,bad", [
    (KnowledgeDocumentDB, "status", "done"),
    (KnowledgeDocumentDB, "source", "email"),
])
async def test_a_value_outside_the_contract_is_refused(db, model, field, bad):
    async with db() as s:
        s.add(_doc(**{field: bad}))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_an_embedding_round_trips_as_float32_bytes(db):
    vector = [0.25, -1.5, 3.0, 0.0]
    async with db() as s:
        s.add(_doc())
        await s.flush()
        s.add(_chunk(embedding=pack(vector), meta={"tags": ["a"], "year": 2024}))
        await s.commit()
    async with db() as s:
        row = (await s.execute(select(KnowledgeChunkDB))).scalar_one()
        assert unpack(row.embedding) == vector
        assert row.meta == {"tags": ["a"], "year": 2024}
        assert isinstance(row.id, int)


async def test_a_cost_row_takes_the_contracts_values(db):
    async with db() as s:
        s.add(KnowledgeCostDB(tenant_id="default", kb_id="kb_00000000000a", stage="embed", cost_source="byok",
                              unit="tokens", est_usd=0.001, at=datetime.now(timezone.utc)))
        await s.commit()
    async with db() as s:
        s.add(KnowledgeCostDB(tenant_id="default", kb_id="kb_00000000000a", stage="translate",
                              cost_source="byok", unit="tokens"))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_one_active_eval_run_per_set(db):
    def run(rid: str, state: str, set_id: str = "kset_a", tenant: str = "default") -> KnowledgeEvalRunDB:
        return KnowledgeEvalRunDB(id=rid, tenant_id=tenant, kb_id="kb_00000000000a", set_id=set_id, status=state)

    async with db() as s:
        s.add_all([run("krun_a", "running"), run("krun_b", "completed"), run("krun_c", "skipped"),
                   run("krun_d", "queued", set_id="kset_b"), run("krun_e", "running", tenant="t2")])
        await s.commit()
    for state in ("queued", "cancelling"):
        async with db() as s:
            s.add(run(f"krun_{state}", state))
            with pytest.raises(IntegrityError):
                await s.commit()
    async with db() as s:
        s.add(run("krun_f", "started"))  # not a status
        with pytest.raises(IntegrityError):
            await s.commit()
    async with db() as s:
        s.add(KnowledgeEvalRunDB(id="krun_g", tenant_id="default", kb_id="kb_00000000000a", set_id="kset_c",
                                 started_by="cron"))
        with pytest.raises(IntegrityError):
            await s.commit()
