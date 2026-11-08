"""Blue/green rebuilds (B2b, D167): a change to how a knowledge base's index is built makes a new snapshot
beside the serving one, which keeps answering until the new one holds every document; then they switch.
The real pipeline, SQL index and search; a fake embedder that answers in the model's own name and width.
SQLite always; Postgres with `SYNAPSE_TEST_POSTGRES_URL`."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from _fakes.knowledge_env import enable_knowledge, use_saved_settings, write_vault
from core.knowledge import bases, budgets, dispatch, documents, events, search, seams, snapshots
from core.knowledge.api_models import KnowledgeBaseCreate, KnowledgeBaseUpdate, SearchRequest
from core.knowledge.errors import EmbedderUnavailable, EmbeddingError, KnowledgeError
from core.knowledge.jobs import run_ingest_job
from core.knowledge.kb_settings import Chunking, Embedding, Retrieval
from core.knowledge.ledger import CostRow, record_costs
from core.knowledge.testing.fakes import HashingEmbedder, RecordingDispatcher
from core.knowledge.trace import Origin
from core.knowledge.types import Attribution, EmbedResult, Usage
from core.store import session
from core.store.models import KnowledgeChunkDB, KnowledgeDocumentDB, KnowledgeSnapshotDB

pytestmark = pytest.mark.usefixtures("knowledge_store")

PRICED = "text-embedding-3-small"


class ByModel:
    """Embeds with the width its model's name gives (`fake-16`, `fake-24`; a priced name is 16 wide) and
    answers in that model's name, so a snapshot records which model built it. `fail_on`: a text containing
    one of these words fails (retryably); `unavailable`: these models have no key."""

    max_batch = 8

    def __init__(self, *, fail_on=(), unavailable=()):
        self.fail_on = set(fail_on)
        self.unavailable = set(unavailable)
        self.calls: list[tuple[str, str, int]] = []

    async def embed(self, texts, *, kind, spec, settings, attribution):
        model = spec.model or "fake-16"
        if model in self.unavailable:
            raise EmbedderUnavailable(f"{model}: no key saved")
        if any(word in text for text in texts for word in self.fail_on):
            raise EmbeddingError(f"{model}: the provider refused")
        dims = 16 if model == PRICED else int(model.rsplit("-", 1)[1])
        hasher = HashingEmbedder(dims=dims)
        self.calls.append((model, kind, len(texts)))
        usage = Usage(stage="embed", cost_source="byok", model=model, provider="fake", input_tokens=10 * len(texts),
                      est_usd=0.0)
        return EmbedResult(vectors=[hasher.vector(t) for t in texts], model=model, dims=dims, usage=usage)


@pytest.fixture(autouse=True)
async def _on(monkeypatch):
    seams.reset_all()
    dispatch.reset()
    use_saved_settings(monkeypatch)
    await enable_knowledge()
    monkeypatch.setattr(snapshots, "BACKOFF_S", (0.0, 0.0))
    yield
    seams.reset_all()
    dispatch.reset()


@pytest.fixture
def jobs():
    recorder = RecordingDispatcher()
    seams.set_ingest_dispatcher(recorder)
    return recorder


@pytest.fixture
def embedder():
    fake = ByModel()
    seams.set_knowledge_embedder(fake)
    return fake


@pytest.fixture
def heard():
    got: list = []
    events.knowledge_observers.append(got.append)
    yield got
    events.knowledge_observers.remove(got.append)


DOCS = {
    "kb/refunds.md": "# Refunds\n\nA refund is paid within thirty days of the request.",
    "kb/hosting.md": "# Hosting\n\nThe hosting fee is billed monthly in advance.",
    "kb/support.md": "# Support\n\nSupport answers within one business day.",
}


async def _write(jobs, kb, path: str, text: str) -> str:
    write_vault(path, text)
    row, queued = await documents.upsert_for_path(kb, path, source="vault", sha256=None, size=None)
    if queued:
        await dispatch.enqueue(kb.id, "upsert", "vault_write", path=row.path, doc_id=row.id)
    return row.id


async def _indexed_base(jobs, **kw):
    kb = await bases.create_base(KnowledgeBaseCreate(name="Policies", folder="kb", **kw))
    for path, text in DOCS.items():
        await _write(jobs, kb, path, text)
    await jobs.drain(run_ingest_job)
    assert {r.status for r in await documents.list_for(kb.id)} == {"indexed"}
    return kb


async def _edit(kb_id: str, **fields):
    current = await bases.require_base(kb_id)
    before, after = await bases.update_base(kb_id, KnowledgeBaseUpdate(revision=current.revision, **fields))
    return after, await snapshots.after_save(before, after)


async def _snaps(kb_id: str) -> dict[str, KnowledgeSnapshotDB]:
    async with session() as s:
        rows = (await s.execute(select(KnowledgeSnapshotDB).where(KnowledgeSnapshotDB.kb_id == kb_id)
                                .order_by(KnowledgeSnapshotDB.created_at))).scalars().all()
    return {r.status: r for r in rows}


async def _find(kb_id: str, query: str = "refund paid", **kw):
    return await search.search(SearchRequest(query=query, kb_ids=[kb_id], **kw), origin=Origin(surface="ui"))


async def _step(jobs, n: int = 1) -> None:
    """Run the next `n` queued jobs."""
    for _ in range(n):
        await run_ingest_job(jobs.jobs.pop(0))


# ── the rebuild ────────────────────────────────────────────────────────────────

class TestBlueGreen:
    async def test_a_new_model_builds_beside_the_serving_snapshot_then_switches(self, jobs, embedder, heard):
        kb = await _indexed_base(jobs)
        old = (await _snaps(kb.id))["active"]
        assert old.embed_model == "fake-16"

        kb, started = await _edit(kb.id, embedding=Embedding(model="fake-24"))
        assert started
        snaps = await _snaps(kb.id)
        building = snaps["building"]
        assert snaps["active"].id == old.id and building.docs_total == 3
        assert [(j.op, j.reason, j.snapshot_id) for j in jobs.jobs] == [("reindex", "snapshot_build", building.id)]

        found = await _find(kb.id)
        assert {h.citation.snapshot_id for h in found.hits} == {old.id}, "the old snapshot serves meanwhile"

        await jobs.drain(run_ingest_job)
        snaps = await _snaps(kb.id)
        assert snaps["active"].id == building.id and snaps["active"].embed_model == "fake-24"
        assert snaps["retired"].id == old.id and snaps["retired"].retired_at is not None
        assert {r.snapshot_id for r in await documents.list_for(kb.id)} == {building.id}
        found = await _find(kb.id)
        assert found.hits and {h.citation.snapshot_id for h in found.hits} == {building.id}
        assert ("fake-24", "query", 1) in embedder.calls
        assert [e.snapshot_id for e in heard if e.type == "knowledge.base.reindexed"] == [building.id]
        state = (await bases.states([kb]))[kb.id]
        assert state.active_snapshot == building.id and state.build is None

    async def test_a_query_time_change_starts_nothing(self, jobs, embedder):
        kb = await _indexed_base(jobs)
        kb, started = await _edit(kb.id, retrieval=Retrieval(top_k=3))
        assert not started and "building" not in await _snaps(kb.id) and jobs.jobs == []

    async def test_a_base_never_indexed_starts_nothing(self, jobs, embedder):
        kb = await bases.create_base(KnowledgeBaseCreate(name="Empty", folder="kb"))
        kb, started = await _edit(kb.id, embedding=Embedding(model="fake-24"))
        assert not started and await _snaps(kb.id) == {}

    async def test_a_base_with_nothing_served_switches_at_once(self, jobs, embedder):
        kb = await _indexed_base(jobs)
        async with session() as s:
            await s.execute(update(KnowledgeDocumentDB).values(status="failed", status_reason="internal"))
        kb, started = await _edit(kb.id, embedding=Embedding(model="fake-24"))
        await jobs.drain(run_ingest_job)
        assert (await _snaps(kb.id))["active"].docs_total == 0

    async def test_documents_are_rebuilt_in_steps(self, jobs, embedder, monkeypatch):
        monkeypatch.setattr(snapshots, "STEP_DOCS", 1)
        kb = await _indexed_base(jobs)
        kb, _ = await _edit(kb.id, embedding=Embedding(model="fake-24"))
        await _step(jobs)
        building = (await _snaps(kb.id))["building"]
        assert (building.docs_done, building.docs_total) == (1, 3)
        state = (await bases.states([kb]))[kb.id]
        assert state.build.status == "building" and (state.build.docs_done, state.build.docs_total) == (1, 3)
        assert len(jobs.jobs) == 1, "the next step is queued"
        await jobs.drain(run_ingest_job)
        assert (await _snaps(kb.id))["active"].id == building.id


# ── what happens while it builds ───────────────────────────────────────────────

class TestDuringABuild:
    async def test_a_document_written_meanwhile_joins_the_serving_snapshot_as_it_was_built(self, jobs, embedder,
                                                                                         monkeypatch):
        """While the rebuild for "english" runs, a new version still joins the "simple" snapshot as "simple":
        the serving snapshot never mixes two configurations."""
        monkeypatch.setattr(snapshots, "STEP_DOCS", 1)
        kb = await _indexed_base(jobs)
        old = (await _snaps(kb.id))["active"]
        kb, _ = await _edit(kb.id, language="english")
        await _step(jobs)  # one document built into the new snapshot
        doc_id = await _write(jobs, kb, "kb/refunds.md", "# Refunds\n\nRefunds are paid within ten days now.")
        ingest = next(j for j in jobs.jobs if j.op == "upsert")
        jobs.jobs.remove(ingest)
        await run_ingest_job(ingest)
        row = await documents.get(doc_id)
        assert (row.status, row.version, row.snapshot_id) == ("indexed", 2, old.id)
        async with session() as s:
            langs = set((await s.execute(select(KnowledgeChunkDB.lang).where(
                KnowledgeChunkDB.doc_id == doc_id, KnowledgeChunkDB.snapshot_id == old.id))).scalars())
        assert langs == {"simple"}

        await jobs.drain(run_ingest_job)
        new = (await _snaps(kb.id))["active"]
        assert new.id != old.id
        row = await documents.get(doc_id)
        assert row.snapshot_id == new.id
        async with session() as s:
            versions = set((await s.execute(select(KnowledgeChunkDB.doc_version, KnowledgeChunkDB.lang).where(
                KnowledgeChunkDB.doc_id == doc_id, KnowledgeChunkDB.snapshot_id == new.id))).all())
        assert versions == {(2, "english")}, "the new snapshot caught up on the new version, built its own way"

    async def test_a_document_deleted_meanwhile_leaves_nothing_behind(self, jobs, embedder, monkeypatch):
        from core.knowledge import ingest

        monkeypatch.setattr(snapshots, "STEP_DOCS", 1)
        kb = await _indexed_base(jobs)
        kb, _ = await _edit(kb.id, embedding=Embedding(model="fake-24"))
        await _step(jobs)
        first = sorted(r.id for r in await documents.list_for(kb.id))[0]
        await ingest.delete_document(await documents.get(first))  # the file, then the document
        await jobs.drain(run_ingest_job)
        async with session() as s:
            left = (await s.execute(select(KnowledgeChunkDB).where(KnowledgeChunkDB.doc_id == first))).all()
        assert left == [] and (await _snaps(kb.id))["active"].docs_total == 2

    async def test_reverting_the_change_cancels_the_build(self, jobs, embedder, monkeypatch):
        monkeypatch.setattr(snapshots, "STEP_DOCS", 1)
        kb = await _indexed_base(jobs)
        old = (await _snaps(kb.id))["active"]
        kb, _ = await _edit(kb.id, embedding=Embedding(model="fake-24"))
        await _step(jobs)
        building = (await _snaps(kb.id))["building"]
        kb, started = await _edit(kb.id, embedding=Embedding())
        assert not started and set(await _snaps(kb.id)) == {"active"}
        await jobs.drain(run_ingest_job)  # the queued step finds nothing to do
        assert (await _snaps(kb.id))["active"].id == old.id
        async with session() as s:
            assert (await s.execute(select(KnowledgeChunkDB).where(
                KnowledgeChunkDB.snapshot_id == building.id))).all() == []

    async def test_a_second_change_supersedes_the_first(self, jobs, embedder, monkeypatch):
        monkeypatch.setattr(snapshots, "STEP_DOCS", 1)
        kb = await _indexed_base(jobs)
        kb, _ = await _edit(kb.id, embedding=Embedding(model="fake-24"))
        await _step(jobs)
        first = (await _snaps(kb.id))["building"]
        kb, started = await _edit(kb.id, embedding=Embedding(model="fake-32"))
        assert started and (await _snaps(kb.id))["building"].id != first.id
        await jobs.drain(run_ingest_job)
        assert (await _snaps(kb.id))["active"].embed_model == "fake-32"
        async with session() as s:
            assert (await s.execute(select(KnowledgeChunkDB).where(
                KnowledgeChunkDB.snapshot_id == first.id))).all() == []

    async def test_two_steps_never_build_at_once(self, jobs, embedder):
        kb = await _indexed_base(jobs)
        kb, _ = await _edit(kb.id, embedding=Embedding(model="fake-24"))
        building = (await _snaps(kb.id))["building"]
        held = await snapshots._claim(building.id)
        assert held is not None
        assert await snapshots.build_step(kb, building.id) == "left", "the lease is held"
        assert await snapshots._release(held)
        assert await snapshots.build_step(kb, building.id) == "switched"

    async def test_a_build_whose_worker_died_is_taken_up_again(self, jobs, embedder):
        kb = await _indexed_base(jobs)
        kb, _ = await _edit(kb.id, embedding=Embedding(model="fake-24"))
        building = (await _snaps(kb.id))["building"]
        assert await snapshots._claim(building.id) is not None  # a step that will never finish
        jobs.jobs.clear()
        await snapshots.tend(kb)
        assert jobs.jobs == [], "a live lease is left alone"
        later = datetime.now(timezone.utc) + snapshots.LEASE + timedelta(minutes=1)
        await snapshots.tend(kb, now=later)
        assert [j.snapshot_id for j in jobs.jobs] == [building.id]

    async def test_a_restart_releases_the_dead_processs_lease(self, jobs, embedder):
        kb = await _indexed_base(jobs)
        kb, _ = await _edit(kb.id, embedding=Embedding(model="fake-24"))
        building = (await _snaps(kb.id))["building"]
        assert await snapshots._claim(building.id) is not None
        jobs.jobs.clear()
        await snapshots.tend(kb, startup=True)
        await jobs.drain(run_ingest_job)
        assert (await _snaps(kb.id))["active"].id == building.id


# ── failing and pausing ────────────────────────────────────────────────────────

class TestFailingAndPausing:
    async def test_a_document_that_cannot_be_built_fails_the_build_and_the_old_one_serves(self, jobs, embedder, heard):
        kb = await _indexed_base(jobs)
        old = (await _snaps(kb.id))["active"]
        embedder.fail_on = {"hosting"}
        kb, _ = await _edit(kb.id, embedding=Embedding(model="fake-24"))
        building = (await _snaps(kb.id))["building"]
        await jobs.drain(run_ingest_job)

        snaps = await _snaps(kb.id)
        assert snaps["active"].id == old.id and snaps["failed"].id == building.id
        assert "kb/hosting.md" in snaps["failed"].stats["error"] and "after 3 tries" in snaps["failed"].stats["error"]
        assert snaps["failed"].stats["failed_docs"][0]["path"] == "kb/hosting.md"
        async with session() as s:
            assert (await s.execute(select(KnowledgeChunkDB).where(
                KnowledgeChunkDB.snapshot_id == building.id))).all() == []
        assert {h.citation.snapshot_id for h in (await _find(kb.id)).hits} == {old.id}
        state = (await bases.states([kb]))[kb.id]
        assert state.build.status == "failed" and "kb/hosting.md" in state.build.error and state.build.docs_failed == 1
        assert [e.snapshot_id for e in heard if e.type == "knowledge.snapshot.failed"] == [building.id]

        embedder.fail_on = set()
        kb, started = await _edit(kb.id)  # an edit that changes nothing: the retry
        assert started
        await jobs.drain(run_ingest_job)
        assert (await _snaps(kb.id))["active"].embed_model == "fake-24"
        assert (await bases.states([kb]))[kb.id].build is None

    async def test_a_missing_key_pauses_the_build_and_never_switches_to_keyword_only(self, jobs, embedder):
        kb = await _indexed_base(jobs)
        embedder.unavailable = {"fake-24"}
        kb, _ = await _edit(kb.id, embedding=Embedding(model="fake-24"))
        building = (await _snaps(kb.id))["building"]
        await jobs.drain(run_ingest_job)
        building = (await _snaps(kb.id))["building"]
        assert building.stats["paused_reason"] == "embedder_unavailable"
        async with session() as s:
            assert (await s.execute(select(KnowledgeChunkDB).where(
                KnowledgeChunkDB.snapshot_id == building.id))).all() == [], "no keyword-only rows kept"
        state = (await bases.states([kb]))[kb.id]
        assert state.build.paused_reason == "embedder_unavailable" and state.paused_reason == "embedder_unavailable"

        embedder.unavailable = set()
        await snapshots.tend(kb)
        assert jobs.jobs == [], "not retried before KEY_RETRY"
        await snapshots.tend(kb, now=datetime.now(timezone.utc) + snapshots.KEY_RETRY + timedelta(seconds=1))
        await jobs.drain(run_ingest_job)
        assert (await _snaps(kb.id))["active"].id == building.id

    async def test_a_spent_budget_pauses_the_build_and_a_raised_cap_resumes_it(self, jobs, embedder, heard):
        kb = await _indexed_base(jobs, budget_monthly_usd=1.0)
        await record_costs([CostRow(Usage(stage="embed", cost_source="byok", model="m", provider="p", est_usd=1.0),
                                    Attribution(kb_id=kb.id))])
        kb, _ = await _edit(kb.id, embedding=Embedding(model=PRICED))
        await jobs.drain(run_ingest_job)
        building = (await _snaps(kb.id))["building"]
        assert building.stats["paused_reason"] == "budget_cap"
        assert [e.status_reason for e in heard if e.type == "knowledge.base.paused"] == ["budget_cap"]

        before = kb
        kb, _ = await _edit(kb.id, budget_monthly_usd=5.0)
        assert budgets.cap_raised(before, kb)
        await budgets.resume(kb, reason="budget_raised")
        await jobs.drain(run_ingest_job)
        assert (await _snaps(kb.id))["active"].id == building.id

    async def test_a_new_month_resumes_a_build_the_budget_paused(self, jobs, embedder):
        kb = await _indexed_base(jobs, budget_monthly_usd=1.0)
        await record_costs([CostRow(Usage(stage="embed", cost_source="byok", model="m", provider="p", est_usd=1.0),
                                    Attribution(kb_id=kb.id))])
        kb, _ = await _edit(kb.id, embedding=Embedding(model=PRICED))
        await jobs.drain(run_ingest_job)
        assert (await _snaps(kb.id))["building"].stats["paused_reason"] == "budget_cap"
        await budgets.tend(kb, now=datetime.now(timezone.utc) + timedelta(days=40))
        assert [j.reason for j in jobs.jobs] == ["snapshot_build"]


# ── after the switch ───────────────────────────────────────────────────────────

class TestAfterTheSwitch:
    async def test_the_retired_snapshot_answers_a_pinned_search_until_its_grace_ends(self, jobs, embedder):
        kb = await _indexed_base(jobs)
        old = (await _snaps(kb.id))["active"]
        kb, _ = await _edit(kb.id, embedding=Embedding(model="fake-24"))
        await jobs.drain(run_ingest_job)
        pinned = await _find(kb.id, snapshot_ids={kb.id: old.id})
        assert pinned.hits and "snapshot_retired" in pinned.degraded

        await snapshots.tend(kb, now=datetime.now(timezone.utc) + timedelta(days=6))
        assert "retired" in await _snaps(kb.id), "within its grace"
        await snapshots.tend(kb, now=datetime.now(timezone.utc) + timedelta(days=8))
        assert "retired" not in await _snaps(kb.id)
        async with session() as s:
            assert (await s.execute(select(KnowledgeChunkDB).where(
                KnowledgeChunkDB.snapshot_id == old.id))).all() == []
        with pytest.raises(KnowledgeError) as caught:
            await _find(kb.id, snapshot_ids={kb.id: old.id})
        assert caught.value.code == "snapshot_not_found"

    async def test_a_document_left_on_the_old_snapshot_is_queued_again(self, jobs, embedder):
        kb = await _indexed_base(jobs)
        old = (await _snaps(kb.id))["active"]
        kb, _ = await _edit(kb.id, embedding=Embedding(model="fake-24"))
        await jobs.drain(run_ingest_job)
        straggler = sorted(r.id for r in await documents.list_for(kb.id))[0]
        async with session() as s:  # indexed into the old snapshot by a job that raced the switch
            await s.execute(update(KnowledgeDocumentDB).where(KnowledgeDocumentDB.id == straggler)
                            .values(snapshot_id=old.id))
        await snapshots.tend(kb)
        assert (await documents.get(straggler)).status == "queued"
        await jobs.drain(run_ingest_job)
        assert (await documents.get(straggler)).snapshot_id == (await _snaps(kb.id))["active"].id

    async def test_chunks_of_a_snapshot_nobody_records_are_dropped(self, jobs, embedder):
        kb = await _indexed_base(jobs)
        active = (await _snaps(kb.id))["active"]
        async with session() as s:
            await s.execute(update(KnowledgeChunkDB).where(KnowledgeChunkDB.doc_id == sorted(
                r.id for r in await documents.list_for(kb.id))[0]).values(snapshot_id="ksnap_" + "9" * 32))
        await snapshots.tend(kb)
        async with session() as s:
            snaps = set((await s.execute(select(KnowledgeChunkDB.snapshot_id))).scalars())
        assert snaps == {active.id}

    async def test_a_chunking_change_is_an_index_change(self, jobs, embedder):
        kb = await _indexed_base(jobs)
        kb, started = await _edit(kb.id, chunking=Chunking(target_tokens=200, max_tokens=400))
        assert started
        await jobs.drain(run_ingest_job)
        active = (await _snaps(kb.id))["active"]
        assert active.stats["settings"]["chunking"]["target_tokens"] == 200
        assert active.embed_model == "fake-16", "the same model, rebuilt"


async def test_a_rebuild_counts_each_document_as_it_goes(jobs, embedder):
    """Found in the browser (B2b): the counts were written once per 25-document step, so a small base's
    rebuild read "0 of 4" until it switched. Each built document now counts at once."""
    kb = await _indexed_base(jobs)
    kb, _ = await _edit(kb.id, embedding=Embedding(model="fake-24"))
    seen: list[int] = []
    real = embedder.embed

    async def watching(texts, **kw):
        if kw["spec"].model == "fake-24" and kw["kind"] == "document":
            seen.append((await _snaps(kb.id))["building"].docs_done)
        return await real(texts, **kw)

    embedder.embed = watching
    await jobs.drain(run_ingest_job)
    assert seen == [0, 1, 2], "the second and third documents see the ones before them counted"
