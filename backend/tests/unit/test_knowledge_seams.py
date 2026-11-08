"""Knowledge's five seams: setters, defaults, and the embedder guard no implementation can skip (D153)."""
from __future__ import annotations

import asyncio
import math

import pytest

from core.knowledge import seams
from core.knowledge.errors import EmbedderUnavailable, EmbeddingInvalid, KnowledgeUnavailable
from core.knowledge.testing.fakes import FakeOcr, FakeReranker, HashingEmbedder, RecordingDispatcher
from core.knowledge.testing.reference_index import InMemoryKnowledgeIndex
from core.knowledge.types import (
    Attribution, EmbeddingSpec, EmbedResult, IngestJob, PageImage, RerankSpec, Usage,
)

ATTR = Attribution(kb_id="kb_00000000000a")
SPEC = EmbeddingSpec(model="hash-64", dims=64)


class _Answers:
    """An embedder that answers whatever it is told to."""

    max_batch = 8

    def __init__(self, vectors, dims):
        self.vectors, self.dims = vectors, dims

    async def embed(self, texts, *, kind, spec, settings, attribution):
        usage = Usage(stage="embed", cost_source="local", model="x", provider="x")
        return EmbedResult(vectors=self.vectors, model="x", dims=self.dims, usage=usage)


class TestDefaults:
    def test_the_default_index_is_the_sql_one(self):
        from core.knowledge.index_sql import SqlKnowledgeIndex
        index = seams.get_knowledge_index()
        assert isinstance(index, SqlKnowledgeIndex) and isinstance(index, seams.KnowledgeIndex)
        assert seams.get_knowledge_index() is index, "resolved once, then reused"

    def test_an_unbuilt_index_is_an_honest_refusal(self, monkeypatch):
        monkeypatch.setattr(seams, "_load", lambda dotted: None)
        with pytest.raises(KnowledgeUnavailable):
            seams.get_knowledge_index()

    def test_an_unbuilt_embedder_is_an_honest_refusal(self, monkeypatch):
        monkeypatch.setattr(seams, "_load", lambda dotted: None)
        with pytest.raises(EmbedderUnavailable):
            seams.get_knowledge_embedder()

    def test_the_default_embedder_is_the_provider_one_behind_the_guard(self):
        from core.knowledge.embedders import ProviderEmbedder
        embedder = seams.get_knowledge_embedder()
        assert isinstance(embedder, seams._Validated) and isinstance(embedder.inner, ProviderEmbedder)
        assert isinstance(embedder.inner, seams.KnowledgeEmbedder)

    def test_no_reranker_and_no_ocr_by_default(self):
        assert seams.get_reranker({}) is None
        assert seams.get_ocr_provider() is None

    def test_the_default_dispatcher_runs_in_process(self):
        assert isinstance(seams.get_ingest_dispatcher(), seams.InProcessDispatcher)

    def test_a_missing_module_is_none_but_a_broken_one_raises(self, tmp_path, monkeypatch):
        assert seams._load("core.knowledge.no_such_module:X") is None
        (tmp_path / "kb0_broken_mod.py").write_text("import kb0_does_not_exist\n")
        monkeypatch.syspath_prepend(str(tmp_path))
        with pytest.raises(ModuleNotFoundError):
            seams._load("kb0_broken_mod:X")


class TestSetters:
    def test_set_and_restore(self):
        index, embedder, reranker = InMemoryKnowledgeIndex(), HashingEmbedder(), FakeReranker()
        dispatcher, ocr = RecordingDispatcher(), FakeOcr({1: "x"})
        seams.set_knowledge_index(index)
        seams.set_knowledge_embedder(embedder)
        seams.set_reranker(reranker)
        seams.set_ingest_dispatcher(dispatcher)
        seams.set_ocr_provider(ocr)
        assert seams.get_knowledge_index() is index
        assert seams.get_knowledge_embedder().inner is embedder
        assert seams.get_reranker({}) is reranker
        assert seams.get_ingest_dispatcher() is dispatcher
        assert seams.get_ocr_provider() is ocr
        seams.reset_all()
        from core.knowledge.index_sql import SqlKnowledgeIndex
        assert isinstance(seams.get_knowledge_index(), SqlKnowledgeIndex), "None restores the SQL default"
        assert seams.get_reranker({}) is None
        assert seams.get_ocr_provider() is None

    def test_the_fakes_satisfy_the_protocols(self):
        assert isinstance(InMemoryKnowledgeIndex(), seams.KnowledgeIndex)
        assert isinstance(HashingEmbedder(), seams.KnowledgeEmbedder)
        assert isinstance(FakeReranker(), seams.KnowledgeReranker)
        assert isinstance(RecordingDispatcher(), seams.IngestDispatcher)
        assert isinstance(FakeOcr({}), seams.OcrProvider)


class TestTheEmbedderGuard:
    async def _embed(self, inner, texts=("a", "b"), dims=None):
        seams.set_knowledge_embedder(inner)
        spec = EmbeddingSpec(model="x", dims=dims)
        return await seams.get_knowledge_embedder().embed(list(texts), kind="document", spec=spec,
                                                          settings={}, attribution=ATTR)

    async def test_good_vectors_pass(self):
        result = await self._embed(HashingEmbedder(dims=16), dims=16)
        assert len(result.vectors) == 2 and all(len(v) == 16 for v in result.vectors)
        assert result.usage.stage == "embed"

    async def test_a_zero_vector_is_refused(self):
        with pytest.raises(EmbeddingInvalid, match="zero"):
            await self._embed(HashingEmbedder(dims=16, zero_on=["b"]))

    async def test_a_wrong_count_is_refused(self):
        with pytest.raises(EmbeddingInvalid, match="asked for 2"):
            await self._embed(_Answers([[1.0, 0.0]], 2))

    async def test_mixed_widths_are_refused(self):
        with pytest.raises(EmbeddingInvalid, match="dimensions"):
            await self._embed(_Answers([[1.0, 0.0], [1.0, 0.0, 0.0]], 2))

    async def test_a_width_other_than_asked_is_refused(self):
        with pytest.raises(EmbeddingInvalid, match="asked for 3 dimensions"):
            await self._embed(_Answers([[1.0, 0.0], [0.0, 1.0]], 2), dims=3)

    @pytest.mark.parametrize("bad", [math.nan, math.inf])
    async def test_a_non_finite_value_is_refused(self, bad):
        with pytest.raises(EmbeddingInvalid, match="non-finite"):
            await self._embed(_Answers([[1.0, 0.0], [bad, 1.0]], 2))

    async def test_a_failing_embedder_raises_its_error(self):
        from core.knowledge.errors import EmbeddingError
        with pytest.raises(EmbeddingError):
            await self._embed(HashingEmbedder(fail_on=["a"]))

    async def test_no_text_and_a_bad_kind_are_refused(self):
        seams.set_knowledge_embedder(HashingEmbedder())
        guard = seams.get_knowledge_embedder()
        with pytest.raises(ValueError):
            await guard.embed([], kind="document", spec=SPEC, settings={}, attribution=ATTR)
        with pytest.raises(ValueError):
            await guard.embed(["a"], kind="passage", spec=SPEC, settings={}, attribution=ATTR)  # type: ignore[arg-type]

    async def test_the_query_kind_reaches_the_embedder(self):
        fake = HashingEmbedder()
        seams.set_knowledge_embedder(fake)
        await seams.get_knowledge_embedder().embed(["q"], kind="query", spec=SPEC, settings={}, attribution=ATTR)
        assert fake.calls[-1]["kind"] == "query"


class TestInProcessDispatcher:
    async def test_it_runs_the_entrypoint_in_the_callers_tenant_and_never_raises(self, monkeypatch):
        seen = []

        async def runner(job):
            from core.tenancy import get_tenant
            seen.append((job.kb_id, get_tenant()))

        monkeypatch.setattr(seams, "_load", lambda dotted: runner if dotted == seams.INGEST_ENTRYPOINT else None)
        dispatcher = seams.InProcessDispatcher()
        receipt = await dispatcher.dispatch(IngestJob(tenant="default", kb_id="kb_1", op="upsert", reason="upload"))
        assert receipt.mode == "background"
        for _ in range(20):
            if seen:
                break
            await asyncio.sleep(0.01)
        assert seen == [("kb_1", "default")]

    async def test_one_document_is_worked_on_by_one_job_at_a_time(self, monkeypatch):
        """F380: a second job for a document waits for the first to end (another document's does not);
        and while it waits it holds no slot."""
        running: list[str] = []
        overlap: list[tuple[str, ...]] = []
        release = asyncio.Event()

        async def runner(job):
            running.append(job.doc_id)
            overlap.append(tuple(running))
            await release.wait()
            running.remove(job.doc_id)

        monkeypatch.setattr(seams, "_load", lambda dotted: runner if dotted == seams.INGEST_ENTRYPOINT else None)
        dispatcher = seams.InProcessDispatcher()

        def job(doc):
            return IngestJob(tenant="default", kb_id="kb_1", op="upsert", reason="vault_write", doc_id=doc)

        for doc in ("doc_a", "doc_a", "doc_b"):
            await dispatcher.dispatch(job(doc))
        for _ in range(50):
            if len(overlap) >= 2:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        assert sorted(running) == ["doc_a", "doc_b"]  # the second doc_a waits; doc_b took the other slot
        release.set()
        for _ in range(50):
            if len(overlap) == 3 and not running:
                break
            await asyncio.sleep(0.01)
        assert len(overlap) == 3 and not any(o.count("doc_a") > 1 for o in overlap)

    async def test_a_missing_ingestion_drops_the_job_quietly(self, monkeypatch):
        monkeypatch.setattr(seams, "_load", lambda dotted: None)
        receipt = await seams.InProcessDispatcher().dispatch(
            IngestJob(tenant="default", kb_id="kb_1", op="upsert", reason="upload"))
        assert receipt.mode == "background"
        await asyncio.sleep(0.02)

    async def test_the_shipped_entrypoint_is_a1s_job_and_refuses_nothing_quietly(self, monkeypatch):
        """A1 built it (D157): a job for a knowledge base that does not exist does nothing, and says
        nothing is wrong — the document it named is gone with its base. The job's behaviour is in
        test_knowledge_ingest_job.py."""
        from _fakes.knowledge_env import enable_knowledge, use_saved_settings
        from core.knowledge.jobs import run_ingest_job

        assert seams._load(seams.INGEST_ENTRYPOINT) is run_ingest_job
        use_saved_settings(monkeypatch)
        await enable_knowledge()
        assert await run_ingest_job(IngestJob(tenant="default", kb_id="kb_000000000000", op="upsert",
                                              reason="upload", path="kb/a.md")) is None


class TestFakes:
    async def test_recording_dispatcher_keeps_one_job_per_key(self):
        d = RecordingDispatcher()
        job = IngestJob(tenant="t", kb_id="kb_1", op="upsert", reason="upload", path="a.pdf")
        await d.dispatch(job)
        await d.dispatch(job)
        assert len(d.jobs) == 1
        assert await d.drain(lambda j: asyncio.sleep(0, result=j.path)) == ["a.pdf"]

    async def test_reranker_orders_by_overlap_then_index(self):
        result = await FakeReranker().rerank("renewal invoice", ["nothing", "invoice", "renewal invoice"],
                                             spec=RerankSpec(model="x", top_n=3), settings={}, attribution=ATTR)
        assert [i for i, _ in result.ranking] == [2, 1, 0]
        assert result.usage.unit == "search_units"

    async def test_ocr_reports_pages(self):
        page = PageImage(page=1, png=b"", width=1, height=1, dpi=72)
        result = await FakeOcr({1: "hello"}).ocr([page], lang_hints=["en"], settings={}, attribution=ATTR)
        assert result.pages[0].text == "hello" and result.usage.unit == "pages"
