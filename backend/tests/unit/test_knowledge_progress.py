"""Progress you can see (PG1, F357; CCR-12): a working document says its stage, how far it has got and the time
left; a provider's rate limit on embedding is waited out, not failed, and said; what a stopped embed stage paid for is
kept for the next attempt (F345); a rebuild counts its passages (and the switch gives each document its new count,
F359). The real pipeline and SQL index; the embedder is the default one over a fake provider call.
SQLite always; Postgres with `SYNAPSE_TEST_POSTGRES_URL`."""
from __future__ import annotations

import asyncio
import hashlib
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

import core.llm_providers as llm
from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from _fakes.knowledge_env import enable_knowledge, use_saved_settings, write_vault
from core.knowledge import (
    bases, dispatch, documents, embed_cache, embedders, pacing, profiles, progress, seams, snapshots,
)
from core.knowledge.api_models import KnowledgeBaseCreate, KnowledgeBaseUpdate
from core.knowledge.embedders import ProviderEmbedder
from core.knowledge.errors import EmbeddingError
from core.knowledge.jobs import run_ingest_job
from core.knowledge.kb_settings import Chunking
from core.knowledge.parse import limits
from core.knowledge.testing.fakes import HashingEmbedder, RecordingDispatcher
from core.knowledge.types import Attribution, EmbeddingSpec, EmbedResult, Usage
from core.store import session
from core.store.models import CollectionDB, KnowledgeChunkDB, KnowledgeCostDB

pytestmark = pytest.mark.usefixtures("knowledge_store")

MODEL = "text-embedding-3-small"  # OpenAI's: the wait names "OpenAI"
GATEWAY = "synapse.titan-embed-v2"  # our gateway's: what it settled is the cost
FAKE_CHILD = [sys.executable, str(Path(__file__).resolve().parents[1] / "_fakes" / "knowledge_misbehaving_child.py")]

#: Eight sections, each its own passage under the structure chunker.
TEXT = "\n\n".join(f"# Part {i}\n\n" + f"Clause {i} sets the terms for item {i} in full. " * 60 for i in range(8))


class Provider:
    """`llm.embed_batch_ex`: each text answered with a 4-wide vector, after `delay`. `script[n]` is what the n-th
    call does instead: an exception to raise, or a callable that returns one (or None to answer)."""

    def __init__(self, *, script=None, delay: float = 0.0, charge: int | None = None):
        self.calls: list[list[str]] = []
        self.script = dict(script or {})
        self.delay = delay
        self.charge = charge
        self.during = None

    async def __call__(self, texts, model, settings, *, input_type=None, dims=None):
        self.calls.append(list(texts))
        n = len(self.calls)
        if self.during is not None:
            await self.during(n, texts)
        if self.delay:
            await asyncio.sleep(self.delay)
        failure = self.script.get(n)
        if callable(failure):
            failure = failure()
        if failure is not None:
            raise failure
        vectors = [[1.0, float(len(t) % 7) + 0.5, 0.5, float(i) + 0.25] for i, t in enumerate(texts)]
        return llm.EmbeddingResponse(vectors, model, llm._providers.provider_for(model).id,
                                     input_tokens=10 * len(texts),
                                     charged_microusd=self.charge * len(texts) if self.charge else None)

    @property
    def sent(self) -> list[str]:
        return [t for call in self.calls for t in call]


def _limit(charged: int | None = None, provider: str = "openai") -> llm.EmbeddingFailed:
    return llm.EmbeddingFailed("OpenAI embedding refused: HTTP 429: Rate limit reached for requests per min.",
                               provider=provider, status=429, charged_microusd=charged)


def _wallet() -> llm.EmbeddingFailed:
    return llm.EmbeddingFailed("model gateway embedding refused: HTTP 402: the wallet is empty",
                               provider="model_gateway", status=402, needed_microusd=10, available_microusd=0)


@pytest.fixture(autouse=True)
async def _on(monkeypatch):
    from core import usage_tracker

    seams.reset_all()
    dispatch.reset()
    profiles.forget_probes()
    use_saved_settings(monkeypatch)
    await enable_knowledge(knowledge_embedding_model=MODEL, openai_key="sk-test",
                           model_gateway_base_url="http://gateway.test", model_gateway_key="gw")

    async def no_rates():
        return {}

    monkeypatch.setattr(usage_tracker, "_ensure_pricing", no_rates)
    real = profiles.resolve

    async def small_slices(model, settings):  # four wide, two texts a call: a document is several calls
        found = await real(model, settings) if model == MODEL else profiles.UNKNOWN
        return replace(found, name=found.name or "fake", dims=4, max_dims=None, widths=(), max_batch=2,
                       usd_per_mtok=1.0, kind="none")

    monkeypatch.setattr(profiles, "resolve", small_slices)
    monkeypatch.setattr(progress, "FLUSH_EVERY_S", 0.01)
    monkeypatch.setattr(pacing, "RATE_WAIT_S", 0.0)
    monkeypatch.setattr(pacing, "RATE_WAIT_MAX_S", 0.0)
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
def provider(monkeypatch):
    fake = Provider()
    monkeypatch.setattr(llm, "embed_batch_ex", fake)
    return fake


async def _base(**kw):
    return await bases.create_base(KnowledgeBaseCreate(name="Policies", folder="kb", **kw))


async def _queue(kb, name: str = "doc.md", text: str = TEXT):
    write_vault(f"kb/{name}", text)
    row, _ = await documents.upsert_for_path(kb, f"kb/{name}", source="vault", sha256=None, size=None)
    await dispatch.enqueue(kb.id, "upsert", "vault_write", path=row.path, doc_id=row.id)
    return row


async def _ingest(jobs, kb, name: str = "doc.md", text: str = TEXT):
    row = await _queue(kb, name, text)
    await jobs.drain(run_ingest_job)
    return await documents.get(row.id)


async def _rows() -> list[CollectionDB]:
    async with session() as s:
        return (await s.execute(select(CollectionDB).where(CollectionDB.collection == progress.COLLECTION))
                ).scalars().all()


async def _costs():
    async with session() as s:
        return (await s.execute(select(KnowledgeCostDB).where(KnowledgeCostDB.stage == "embed"))).scalars().all()


async def _passages(kb_id: str) -> int:
    async with session() as s:
        return len((await s.execute(select(KnowledgeChunkDB.id).where(KnowledgeChunkDB.kb_id == kb_id))).all())


async def _until(check, *, timeout: float = 2.0):
    """Poll `check()` (async) until it answers something true."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        got = await check()
        if got or asyncio.get_running_loop().time() > deadline:
            return got
        await asyncio.sleep(0.01)


# ── the tracker ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("seconds,shown", [(0, 0), (1, 10), (14, 20), (59, 60), (61, 60), (100, 90), (299, 300),
                                           (601, 600), (1000, 1020), (3725, 3720)])
def test_an_estimate_is_rounded_as_a_person_reads_it(seconds, shown):
    assert progress.round_eta(seconds) == shown


def test_an_estimate_waits_for_a_pace_and_counts_from_where_the_stage_began():
    clock = [100.0]
    tracker = progress.Tracker(clock=lambda: clock[0])
    tracker.stage("enrich", total=100, unit="passages", done=40)  # 40 kept from an earlier attempt
    clock[0] += 30
    tracker.advance(done=42)
    assert tracker.eta_s() is None, "two passages are not a pace"
    tracker.advance(done=50)
    assert tracker.eta_s() == 150, "10 passages in 30 s, 50 left: 150 s — the 40 kept are not pace"
    tracker.stage("index")
    assert tracker.eta_s() is None and tracker.value()["total"] is None, "a stage that cannot count"


def test_time_spent_waiting_out_a_rate_limit_is_not_pace():
    """Measured in the browser: 32 passages, then two waits (15 s, 30 s), read as "about 1 h 19 min left" for a
    document that took two minutes more. While it waits the screen says so instead; the estimate is the work's pace."""
    clock = [0.0]
    tracker = progress.Tracker(clock=lambda: clock[0])
    tracker.stage("embed", total=3251, unit="passages")
    clock[0] += 3
    tracker.advance(done=32)
    first = tracker.waiting(provider_id="openai", provider="OpenAI", seconds=15)
    second = tracker.waiting(provider_id="openai", provider="OpenAI", seconds=30)  # overlapping: counted once
    clock[0] += 15
    tracker.resumed(first)
    clock[0] += 30
    tracker.resumed(second)
    clock[0] += 3
    tracker.advance(done=96)
    assert tracker.eta_s() == progress.round_eta((3251 - 96) * 6 / 96), "6 s of work for 96 passages, not 51 s"
    tracker.waiting(provider_id="openai", provider="OpenAI", seconds=60)
    clock[0] += 60  # still waiting: that time is not pace either
    assert tracker.eta_s() == progress.round_eta((3251 - 96) * 6 / 96)


def test_counts_never_go_back_or_past_the_end_and_a_closed_tracker_is_inert():
    tracker = progress.Tracker()
    tracker.stage("embed", total=10, unit="passages")
    tracker.advance(by=4)
    tracker.advance(done=3)
    assert tracker.done == 4
    tracker.advance(by=20)
    assert tracker.done == 10 and tracker.passages == 10
    tracker.close()
    tracker.stage("index")
    tracker.waiting(provider_id="openai", provider="OpenAI", seconds=5)
    assert tracker.stage_name == "embed" and tracker.value()["waiting"] is None


def test_two_waits_at_once_are_each_their_own():
    tracker = progress.Tracker()
    first = tracker.waiting(provider_id="openai", provider="OpenAI", seconds=15)
    second = tracker.waiting(provider_id="openai", provider="OpenAI", seconds=30)
    tracker.resumed(first)
    assert tracker.value()["waiting"]["provider"] == "OpenAI", "the second call still waits"
    tracker.resumed(second)
    assert tracker.value()["waiting"] is None


# ── the row ──────────────────────────────────────────────────────────────────

async def test_the_row_is_written_at_once_kept_up_to_date_and_removed_however_the_work_ends():
    async with progress.tracking("kb_1", "kdoc_1", doc_id="kdoc_1") as tracker:
        (row,) = await _rows()
        assert row.key == "kb_1:kdoc_1" and row.value["stage"] is None, "written when the work starts"
        tracker.stage("embed", total=8, unit="passages")
        tracker.advance(by=3)
        assert await _until(lambda: _value_is(stage="embed", done=3))
    assert await _rows() == []
    with pytest.raises(RuntimeError):
        async with progress.tracking("kb_1", "kdoc_2"):
            raise RuntimeError("the parser fell over")
    assert await _rows() == [], "an error ends the work too"


async def _value_is(**want):
    rows = await _rows()
    return bool(rows) and all(rows[0].value.get(k) == v for k, v in want.items())


async def test_a_write_that_fails_never_fails_the_work(monkeypatch):
    async def broken(*a, **kw):
        raise OSError("the database went away")

    monkeypatch.setattr(progress, "_write", broken)
    async with progress.tracking("kb_1", "kdoc_1") as tracker:
        tracker.stage("parse")
        await asyncio.sleep(0.05)
    assert progress.current() is None


async def test_a_job_started_from_inside_tracked_work_reports_nothing_into_it(jobs, monkeypatch):
    """The in-process dispatcher copies its caller's context into the job it spawns."""
    seen = []
    real = bases.get_base

    async def watching(kb_id):
        seen.append(progress.current())
        return await real(kb_id)

    monkeypatch.setattr(bases, "get_base", watching)
    kb = await _base()
    async with progress.tracking(kb.id, "kdoc_parent"):
        await asyncio.create_task(run_ingest_job(dispatch.job_for(kb.id, "reindex", "reconcile")))
    assert seen == [None]


async def test_a_screen_sees_working_documents_only_and_no_estimate_from_a_stale_row(jobs, provider):
    kb = await _base()
    row = await _queue(kb)
    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    stale = {"doc_id": row.id, "stage": "enrich", "done": 3, "total": 9, "unit": "passages", "eta_s": 60,
             "waiting": {"reason": "rate_limited", "provider": "OpenAI", "provider_id": "openai", "until": old},
             "passages": 3, "at": old}
    await progress._write(progress.key(kb.id, row.id), stale)
    (queued,) = await progress.attach([documents.to_out(await documents.get(row.id))])
    assert queued.progress is None, "a resting or queued document shows none, whatever a dead worker left"
    await documents.transition(row.id, "parsing", previous="queued")
    (working,) = await progress.attach([documents.to_out(await documents.get(row.id))])
    assert working.progress.stage == "enrich" and (working.progress.done, working.progress.total) == (3, 9)
    assert working.progress.eta_s is None and working.progress.waiting is None, "nobody has written it for 5 min"


# ── a document at work ───────────────────────────────────────────────────────

async def test_a_working_document_says_its_stage_and_how_far_it_has_got(jobs, provider):
    kb = await _base()
    row = await _queue(kb)
    seen = []

    async def look(n, texts):
        async def read():
            (out,) = await progress.attach([documents.to_out(await documents.get(row.id))])
            return out.progress

        async def caught_up():  # the row is written by its own task: wait for the slices answered so far
            p = await read()
            return p if p and p.stage == "embed" and p.done == 2 * (n - 1) else None

        seen.append(await _until(caught_up) or await read())

    provider.during = look
    await jobs.drain(run_ingest_job)
    done = await documents.get(row.id)
    assert done.status == "indexed" and done.chunks >= 4
    assert {p.stage for p in seen} == {"embed"} and {p.unit for p in seen} == {"passages"}
    assert {p.total for p in seen} == {done.chunks}
    assert [p.done for p in seen] == sorted(p.done for p in seen) and seen[0].done == 0 and seen[-1].done > 0
    assert seen[-1].done == 2 * (len(seen) - 1), "each answered slice is counted, once"
    assert await _rows() == [], "a document at rest has no row"
    (rest,) = await progress.attach([documents.to_out(done)])
    assert rest.progress is None


async def test_a_long_index_call_keeps_the_document_from_looking_stuck(jobs, provider, monkeypatch):
    beats = []
    real = documents.heartbeat

    async def counting(doc_id, **kw):
        beats.append(kw.get("status"))
        return await real(doc_id, **kw)

    monkeypatch.setattr(limits, "HEARTBEAT_S", 0.01)
    monkeypatch.setattr(documents, "heartbeat", counting)
    provider.delay = 0.03
    kb = await _base()
    row = await _ingest(jobs, kb)
    assert row.status == "indexed"
    assert beats.count("indexing") >= 3, "the embedding, not only Contextual Retrieval, beats"


# ── embedding paced ──────────────────────────────────────────────────────────

async def test_a_rate_limited_slice_waits_is_halved_and_the_document_finishes(jobs, provider):
    provider.script = {2: _limit()}
    kb = await _base()
    row = await _ingest(jobs, kb)
    assert row.status == "indexed"
    sizes = [len(c) for c in provider.calls]
    assert sizes[:4] == [2, 2, 1, 2], "the limited slice is sent again halved, then whole slices again"
    assert len(provider.sent) == row.chunks + 2 and len(set(provider.sent)) == row.chunks, "two texts sent twice"


async def test_the_wait_names_the_provider_and_until_when(monkeypatch):
    monkeypatch.setattr(pacing, "RATE_WAIT_S", 0.3)
    async with progress.tracking("kb_1", "kdoc_1") as tracker:
        waiting = asyncio.create_task(pacing.wait_out(0.3, model=MODEL))
        await asyncio.sleep(0.05)
        wait = tracker.value()["waiting"]
        assert (wait["reason"], wait["provider"], wait["provider_id"]) == ("rate_limited", "OpenAI", "openai")
        assert datetime.fromisoformat(wait["until"]) > datetime.now(timezone.utc)
        assert await _until(lambda: _value_is(waiting=wait))
        await waiting
        assert tracker.value()["waiting"] is None


async def test_a_limit_that_never_lifts_fails_the_document_after_the_no_progress_time(jobs, provider, monkeypatch):
    monkeypatch.setattr(pacing, "NO_PROGRESS_S", 0.05)
    monkeypatch.setattr(pacing, "RATE_WAIT_S", 0.01)
    monkeypatch.setattr(pacing, "RATE_WAIT_MAX_S", 0.01)
    provider.script = {n: _limit for n in range(2, 500)}
    kb = await _base()
    row = await _ingest(jobs, kb)
    assert (row.status, row.status_reason) == ("failed", "embed_failed")
    assert len(provider.calls) > 3, "it waited and asked again before giving up"
    assert [len(c) for c in provider.calls[2:]] == [1] * (len(provider.calls) - 2), "halved to one text"


async def test_a_paced_wait_never_records_a_charge_twice(jobs, provider, monkeypatch):
    """Our gateway settles what it embedded before its limit: that is recorded once, alone; the slices answered
    are the call's own cost."""
    await enable_knowledge(knowledge_embedding_model=GATEWAY)
    provider.charge = 5
    provider.script = {2: _limit(charged=3, provider="model_gateway")}
    kb = await _base()
    row = await _ingest(jobs, kb)
    assert row.status == "indexed"
    charged = [c.charged_microusd for c in await _costs()]
    assert sorted(charged) == [3, 5 * row.chunks]


async def test_giving_up_on_a_limit_records_each_charge_once(jobs, provider, monkeypatch):
    """The last limited call's charge is recorded when it is limited; the document's failure adds only what the
    slices answered before it cost."""
    monkeypatch.setattr(pacing, "NO_PROGRESS_S", 0.05)
    monkeypatch.setattr(pacing, "RATE_WAIT_S", 0.01)
    monkeypatch.setattr(pacing, "RATE_WAIT_MAX_S", 0.01)
    await enable_knowledge(knowledge_embedding_model=GATEWAY)
    provider.charge = 5
    provider.script = {n: (lambda: _limit(charged=3, provider="model_gateway")) for n in range(2, 500)}
    kb = await _base()
    row = await _ingest(jobs, kb)
    assert (row.status, row.status_reason) == ("failed", "embed_failed")
    limited = len(provider.calls) - 1
    assert sum(c.charged_microusd for c in await _costs()) == 5 * 2 + 3 * limited


async def test_a_search_and_the_backfill_are_never_kept_waiting(provider):
    provider.script = {1: _limit, 2: _limit}
    attribution = Attribution(kb_id="kb_1")
    with pacing.paced(), pytest.raises(EmbeddingError):  # a question, even inside indexing's block
        await ProviderEmbedder().embed(["q"], kind="query", spec=EmbeddingSpec(model=MODEL),
                                       settings={"openai_key": "k"}, attribution=attribution)
    with pytest.raises(EmbeddingError):  # a document outside it: the backfill stops and tries on its next pass
        await ProviderEmbedder().embed(["d"], kind="document", spec=EmbeddingSpec(model=MODEL),
                                       settings={"openai_key": "k"}, attribution=attribution)
    assert len(provider.calls) == 2


async def test_a_key_out_of_credit_is_never_paced(jobs, provider):
    out = llm.EmbeddingFailed("OpenAI embedding refused: HTTP 429: You exceeded your current quota "
                              "(insufficient_quota)", provider="openai", status=429)
    assert out.code == llm.QUOTA_EXHAUSTED
    provider.script = {1: out}
    kb = await _base()
    row = await _ingest(jobs, kb)
    assert (row.status, row.status_reason) == ("failed", "embed_failed") and len(provider.calls) == 1


# ── what a stopped embed stage paid for is kept (F345) ───────────────────────

async def test_a_document_resumed_after_a_402_buys_only_what_was_not_answered(jobs, provider):
    await enable_knowledge(knowledge_embedding_model=GATEWAY)
    provider.charge = 5
    provider.script = {3: _wallet()}  # two slices answered, then the wallet is empty
    kb = await _base()
    row = await _ingest(jobs, kb)
    assert (row.status, row.status_reason) == ("paused", "wallet_402")
    first = list(provider.sent)
    assert len(first) == 6  # four answered, two refused
    again = await documents.transition(row.id, "queued", previous="paused")
    await dispatch.enqueue(kb.id, "upsert", "budget_resume", path=again.path, doc_id=again.id)
    await jobs.drain(run_ingest_job)
    row = await documents.get(row.id)
    assert row.status == "indexed"
    resumed = provider.sent[len(first):]
    assert not set(resumed) & set(first[:4]), "the four answered before the 402 are not bought again"
    assert len(resumed) == row.chunks - 4
    assert sum(c.charged_microusd for c in await _costs()) == 5 * row.chunks, "every passage paid for once"
    embed = next(s for s in row.report["stages"] if s["stage"] == "embed")
    assert embed["detail"]["cached"] == 4 and embed["detail"]["vectors"] == row.chunks
    sha = hashlib.sha256(TEXT.encode()).hexdigest()
    sig = embed_cache.signature(GATEWAY, None, embedders.profile_version(GATEWAY))
    assert await embed_cache.load(sha, sig) == {}, "the rows hold them now"


async def test_a_cancelled_embed_keeps_what_was_answered(jobs, provider):
    """A job cancelled or timed out mid-embedding (arq's timeout) keeps its paid passages too."""
    provider.script = {3: asyncio.CancelledError}
    kb = await _base()
    row = await _queue(kb)
    with pytest.raises(asyncio.CancelledError):
        await jobs.drain(run_ingest_job)
    sha = hashlib.sha256(TEXT.encode()).hexdigest()
    kept = await embed_cache.load(sha, embed_cache.signature(MODEL, None, embedders.profile_version(MODEL)))
    assert len(kept) == 4
    async with session() as s:
        costs = (await s.execute(select(KnowledgeCostDB))).scalars().all()
    assert sum(c.input_tokens for c in costs) == 40, "and what they cost is recorded"
    assert (await documents.get(row.id)).status == "indexing"  # reconcile takes it up again


async def test_the_budget_check_prices_only_what_is_left_to_embed(jobs, provider, monkeypatch):
    from core.knowledge import pipeline

    provider.script = {3: _wallet()}
    await enable_knowledge(knowledge_embedding_model=GATEWAY)
    kb = await _base()
    row = await _ingest(jobs, kb)
    asked = []
    real = pipeline.check_budget

    async def recording(kb, usd):
        asked.append(usd)
        return await real(kb, usd)

    monkeypatch.setattr(pipeline, "check_budget", recording)
    from core.knowledge.budgets import embed_estimate_usd

    texts = await _chunk_texts(kb, row)
    again = await documents.transition(row.id, "queued", previous="paused")
    await dispatch.enqueue(kb.id, "upsert", "budget_resume", path=again.path, doc_id=again.id)
    await jobs.drain(run_ingest_job)
    assert asked == [await embed_estimate_usd(GATEWAY, texts[4:])]


async def _chunk_texts(kb, row) -> list[str]:
    from core.knowledge.chunker import chunk_document
    from core.knowledge.parse import parse_bytes

    parsed = parse_bytes(TEXT.encode(), path=row.path, source="vault", sha256="x", mime="text/markdown")
    chunks = chunk_document(parsed, doc_id=row.id, kb_id=kb.id, snapshot_id="s", doc_version=1,
                            settings=kb.chunking, lang=kb.language).chunks
    from core.knowledge.pipeline import compose_context, enrich_prefix

    for c in chunks:
        c.context = compose_context(enrich_prefix(parsed.title or "doc.md", None, c.heading_path), None, c.context)
    return [c.search_text() for c in chunks]


# ── reading ──────────────────────────────────────────────────────────────────

async def test_a_long_parse_tells_the_pages_it_has_read():
    from core.knowledge.parse.runner import run_parse

    told = []
    async with progress.tracking("kb_1", "kdoc_1") as tracker:
        real = tracker.advance

        def watching(**kw):
            real(**kw)
            told.append((tracker.stage_name, tracker.done, tracker.total))

        tracker.advance = watching
        tracker.stage("parse")
        job = {"path": "kb/a.pdf", "source": "vault", "sha256": "0" * 64, "mime": "x", "fake": "count"}
        async with run_parse(b"x", job, timeout_s=30, memory_mb=1024, command=FAKE_CHILD) as out:
            assert out.parsed.blocks[0].text == "Four pages were read."
        assert (tracker.stage_name, tracker.done, tracker.total, tracker.unit) == ("parse", 3, 4, "pages")
    assert told == [("parse", 3, 4)], "1 of 4 began the count; the torn file was never read as one"


def test_a_page_count_that_cannot_be_read_is_none(tmp_path):
    from core.knowledge.parse.runner import read_pages

    file = tmp_path / "progress.json"
    assert read_pages(file) is None
    for text in ('{"done": 1, "tot', "[]", '{"done": 5, "total": 4}', '{"done": 1, "total": 0}', "null"):
        file.write_text(text)
        assert read_pages(file) is None, text
    file.write_text('{"done": 2, "total": 9}')
    assert read_pages(file) == (2, 9)


def test_the_basic_pdf_reader_tells_each_page():
    from _fakes.knowledge_formats import pdf, text_page
    from core.knowledge.parse import parse_bytes

    data = pdf([text_page("One."), text_page("Two."), text_page("Three.")])
    told = []
    parse_bytes(data, path="kb/a.pdf", source="vault", sha256="0" * 64, mime="application/pdf",
                report=lambda done, total: told.append((done, total)))
    assert told == [(1, 3), (2, 3), (3, 3)]


def test_a_count_that_fails_never_stops_a_parse():
    from _fakes.knowledge_formats import pdf, text_page
    from core.knowledge.parse import parse_bytes

    def broken(done, total):
        raise OSError("the parent holds the file")

    parsed = parse_bytes(pdf([text_page("One.")]), path="kb/a.pdf", source="vault", sha256="0" * 64,
                         mime="application/pdf", report=broken)
    assert parsed.blocks


# ── rebuilds ─────────────────────────────────────────────────────────────────

class Watching:
    """A seam embedder that answers like the real ones and, mid-call, reads what a screen shows of the rebuild."""

    max_batch = 2

    def __init__(self, kb_id_getter):
        self.kb_id = kb_id_getter
        self.builds = []

    async def embed(self, texts, *, kind, spec, settings, attribution):
        if attribution.snapshot_id and self.kb_id() is not None:
            kb = await bases.require_base(self.kb_id())
            if (await snapshots.build_states([kb])).get(kb.id) is not None:
                async def shown():  # the row is written by its own task: wait for this stage to reach it
                    build = (await snapshots.build_states([kb]))[kb.id]
                    return build if build.stage == "embed" else None

                self.builds.append(await _until(shown) or (await snapshots.build_states([kb]))[kb.id])
        model = spec.model or MODEL
        usage = Usage(stage="embed", cost_source="byok", model=model, provider="fake", input_tokens=len(texts))
        return EmbedResult(vectors=[HashingEmbedder(dims=8).vector(t) for t in texts], model=model, dims=8,
                           usage=usage)


async def _edit(kb_id: str, **fields):
    current = await bases.require_base(kb_id)
    before, after = await bases.update_base(kb_id, KnowledgeBaseUpdate(revision=current.revision, **fields))
    return after, await snapshots.after_save(before, after)


async def test_a_rebuild_counts_its_passages_and_says_where_it_is(jobs, monkeypatch):
    monkeypatch.setattr(progress, "ETA_MIN_S", 0.0)  # a test's rebuild takes well under the five seconds a pace needs
    kb_id = [None]
    watcher = Watching(lambda: kb_id[0])
    seams.set_knowledge_embedder(watcher)
    kb = await _base()
    for i in range(3):
        await _queue(kb, f"doc{i}.md")
    await jobs.drain(run_ingest_job)
    held = await _passages(kb.id)
    kb_id[0] = kb.id
    _, started = await _edit(kb.id, chunking=Chunking(target_tokens=200, max_tokens=300))
    assert started
    build = (await snapshots.build_states([kb]))[kb.id]
    assert (build.passages_done, build.passages_total) == (0, held), "seeded with what the base holds now"
    await jobs.drain(run_ingest_job)
    assert watcher.builds, "watched mid-build"
    assert {b.stage for b in watcher.builds} == {"embed"}
    done = [b.passages_done for b in watcher.builds]
    assert done == sorted(done) and done[-1] > done[0], "monotone, and it moves within a document"
    assert all(b.passages_done <= b.passages_total for b in watcher.builds)
    assert all(b.eta_s is None for b in watcher.builds if b.passages_done < 3), "no estimate before a pace"
    assert any(b.eta_s is not None for b in watcher.builds), "then one"
    assert (await snapshots.build_states([await bases.require_base(kb.id)])).get(kb.id) is None, "switched"
    assert await _rows() == []


async def test_after_a_rebuild_each_document_reports_the_passages_the_new_index_holds(jobs):
    """F359: the switch gives each document its count in the snapshot that now serves, not the old one's."""
    seams.set_knowledge_embedder(Watching(lambda: None))
    kb = await _base()
    row = await _ingest(jobs, kb)
    before = row.chunks
    await _edit(kb.id, chunking=Chunking(target_tokens=128, max_tokens=160))
    await jobs.drain(run_ingest_job)
    row = await documents.get(row.id)
    async with session() as s:
        held = (await s.execute(select(KnowledgeChunkDB.tokens).where(
            KnowledgeChunkDB.doc_id == row.id, KnowledgeChunkDB.snapshot_id == row.snapshot_id))).scalars().all()
    assert len(held) != before, "the new chunking cuts it differently"
    assert (row.chunks, row.tokens) == (len(held), sum(held))


def test_the_pace_starts_again_after_a_gap():
    now = datetime.now(timezone.utc)
    fresh = snapshots._pace({"passages_done": 40}, now)
    assert fresh["passages"] == 40 and fresh["since"] == fresh["last"] == now.isoformat()
    going = {"since": (now - timedelta(minutes=9)).isoformat(), "passages": 10,
             "last": (now - timedelta(seconds=20)).isoformat()}
    assert snapshots._pace({"passages_done": 40, "pace": going}, now) == going, "steps that follow keep it"
    paused = {**going, "last": (now - snapshots.PACE_GAP - timedelta(seconds=1)).isoformat()}
    assert snapshots._pace({"passages_done": 40, "pace": paused}, now)["passages"] == 40, "a pause starts it again"


def test_a_paused_rebuild_shows_no_estimate_and_no_document():
    now = datetime.now(timezone.utc)
    stats = {"passages_done": 30, "passages_total": 90, "cursor": "kdoc_a", "paused_reason": "wallet_402",
             "pace": {"since": (now - timedelta(minutes=2)).isoformat(), "passages": 0, "last": now.isoformat()}}
    row = {"doc_id": "kdoc_b", "stage": "embed", "passages": 4, "at": now.isoformat(), "waiting": None}
    shown = snapshots._build_progress(stats, row, now)
    assert (shown["passages_done"], shown["passages_total"], shown["stage"]) == (30, 90, None)
    assert "eta_s" not in shown
    going = snapshots._build_progress({**stats, "paused_reason": None}, row, now)
    assert (going["passages_done"], going["stage"]) == (34, "embed")
    assert going["eta_s"] == 210, "34 passages in 2 min, 56 left: 198 s, read as 3½ min"
    counted = snapshots._build_progress({**stats, "paused_reason": None, "cursor": "kdoc_b"}, row, now)
    assert counted["passages_done"] == 30, "once its beat has counted the document, its row does not again"
