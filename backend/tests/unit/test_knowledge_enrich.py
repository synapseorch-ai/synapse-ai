"""Contextual Retrieval (CR1, D180; F328): a model writes each passage's context before it is indexed.

The model is faked at `enrich._ask` (one call), so these run the real job, pipeline, snapshot, ledger and index.
`test_the_chat_path_logs_each_call_itself` runs one call through the real `generate_response_ex` instead.
"""
from __future__ import annotations

import asyncio
import re

import pytest
from sqlalchemy import select, update

from _fakes.fake_http import FakeResponse, install_httpx
from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from _fakes.knowledge_env import enable_knowledge, use_saved_settings, write_vault
from core.knowledge import bases, costs, dispatch, documents, enrich, enrich_cache, seams
from core.knowledge.api_models import KnowledgeBaseCreate
from core.knowledge.errors import BudgetExceeded, EnrichmentError, WalletExhausted
from core.knowledge.index_sql import SqlKnowledgeIndex
from core.knowledge.jobs import failure_status, run_ingest_job
from core.knowledge.kb_settings import Enrichment
from core.knowledge.snapshots import serving_snapshot
from core.knowledge.testing.fakes import RecordingDispatcher
from core.knowledge.types import LexicalQuery, Scope
from core.llm_providers import EmptyAnswer, LLMError, LLMResult
from core.store import session
from core.store.models import KnowledgeChunkDB, KnowledgeCostDB, KnowledgeDocumentDB, KnowledgeSnapshotDB, UsageLogDB

pytestmark = pytest.mark.usefixtures("knowledge_store")

MODEL = "gpt-4o-mini"  # priced below; OpenAI caches a repeated prefix by itself
RATES = {"gpt-4o-mini": {"provider": "openai", "input_per_1m": 0.15, "output_per_1m": 0.60},
         "gpt-4.1-nano": {"provider": "openai", "input_per_1m": 0.10, "output_per_1m": 0.40},
         "claude-haiku-4-5": {"provider": "anthropic", "input_per_1m": 1.0, "output_per_1m": 5.0}}
KEYS = {"openai_key": "sk-test", "anthropic_key": "sk-ant-test",
        "model_gateway_base_url": "http://gateway.test", "model_gateway_key": "gw-test"}

TEXT = ("# Refunds\n\n" + "A refund is paid within thirty days of the request. " * 40 + "\n\n"
        "# Hosting\n\n" + "The hosting fee is billed monthly in advance. " * 40)


def _long_text(sections: int = 22) -> str:
    """About 4,000 characters a section, in paragraphs of ~800: 22 make ~22k tokens, cut into two windows."""
    return "\n\n".join(f"# Section {i}\n\n" + "\n\n".join(f"Clause {i}.{p} sets the terms for item {i}. " * 20
                                                             for p in range(5))
                       for i in range(sections))


class Writer:
    """`enrich._ask`: answers each passage with a context naming its first words, after `delay` seconds."""

    def __init__(self, *, delay: float = 0.0, answer=None, charged: int | None = None, usage=(120, 20, 0, 0)):
        self.calls: list[dict] = []
        self.events: list[tuple[str, str]] = []
        self.delay = delay
        self.answer = answer
        self.charged = charged
        self.usage = usage
        self.running = 0
        self.most = 0
        self.peaks: list[int] = []

    @staticmethod
    def passage(prompt: str) -> str:
        return re.search(r"<passage>\n(.*?)\n</passage>", prompt, re.S).group(1)

    async def __call__(self, model, system, prompt, settings, attribution):
        passage = self.passage(prompt)
        self.calls.append({"model": model, "system": system, "prompt": prompt, "passage": passage,
                           "attribution": attribution})
        n = len(self.calls)  # this call's own number, before any other call is appended
        self.running += 1
        self.most = max(self.most, self.running)
        self.peaks.append(self.running)
        self.events.append(("start", passage[:40]))
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.answer is not None:
                out = self.answer(passage, n)
                if isinstance(out, BaseException):
                    raise out
                if isinstance(out, LLMResult):
                    return out
                text = out
            else:
                text = f"Zebracontext about {' '.join(passage.split()[:3])}"
            i, o, r, w = self.usage
            return LLMResult(text, i, o, r, w, "end_turn", (), "", self.charged)
        finally:
            self.running -= 1
            self.events.append(("end", passage[:40]))


@pytest.fixture(autouse=True)
async def _on(monkeypatch):
    from core import usage_tracker

    seams.reset_all()
    dispatch.reset()
    use_saved_settings(monkeypatch)
    await enable_knowledge(**KEYS)
    monkeypatch.setattr(usage_tracker, "_pricing_snapshot", dict(RATES))
    yield
    seams.reset_all()
    dispatch.reset()


@pytest.fixture
def jobs():
    recorder = RecordingDispatcher()
    seams.set_ingest_dispatcher(recorder)
    return recorder


@pytest.fixture
def writer(monkeypatch):
    fake = Writer()
    monkeypatch.setattr(enrich, "_ask", fake)
    return fake


async def _base(**kw):
    kw.setdefault("enrichment", Enrichment(mode="contextual", contextual_model=MODEL))
    return await bases.create_base(KnowledgeBaseCreate(name="Policies", folder="kb", **kw))


async def _ingest(jobs, kb, name: str = "doc0.md", text: str = TEXT):
    write_vault(f"kb/{name}", text)
    row, _ = await documents.upsert_for_path(kb, f"kb/{name}", source="vault", sha256=None, size=None)
    await dispatch.enqueue(kb.id, "upsert", "vault_write", path=row.path, doc_id=row.id)
    await jobs.drain(run_ingest_job)
    return await documents.get(row.id)


async def _chunks(kb_id: str):
    async with session() as s:
        return (await s.execute(select(KnowledgeChunkDB).where(KnowledgeChunkDB.kb_id == kb_id)
                                .order_by(KnowledgeChunkDB.doc_id, KnowledgeChunkDB.ordinal))).scalars().all()


async def _costs(stage: str = "enrich"):
    async with session() as s:
        return (await s.execute(select(KnowledgeCostDB).where(KnowledgeCostDB.stage == stage))).scalars().all()


def _enrich_stage(row):
    return next(s for s in row.report["stages"] if s["stage"] == "enrich")


# ── a base that opted in ───────────────────────────────────────────────────────

async def test_every_passage_is_searched_with_the_context_the_model_wrote(jobs, writer):
    kb = await _base()
    row = await _ingest(jobs, kb)
    assert row.status == "indexed"
    chunks = await _chunks(kb.id)
    assert chunks and len(writer.calls) == len(chunks)
    for chunk in chunks:
        prefix, sentence = chunk.context.split(" · Zebracontext", 1)
        assert prefix.startswith("Document: ") and " · Section: " in prefix, "the free prefix comes first"
        assert "Zebracontext" + sentence == enrich.clean("Zebracontext about " + " ".join(chunk.content.split()[:3]))
    snapshot = await serving_snapshot(kb)
    hits = await SqlKnowledgeIndex().search_lexical(
        LexicalQuery((Scope(kb.id, snapshot),), "zebracontext", "simple", None, 10))
    assert len(hits.candidates) == len(chunks), "the keyword leg reads the written context"
    stage = _enrich_stage(row)
    assert stage["status"] == "ok"
    assert stage["detail"]["mode"] == "contextual"
    assert stage["detail"]["model"] == MODEL and stage["detail"]["version"] == enrich.CONTEXTUAL_VERSION
    assert stage["detail"]["calls"] == len(chunks) and stage["detail"]["written"] == len(chunks)
    assert stage["detail"]["fallback"] == 0 and stage["detail"]["windows"] == 1 and stage["detail"]["whole"] is True


async def test_the_model_sees_the_document_and_is_asked_about_one_passage(jobs, writer):
    kb = await _base()
    await _ingest(jobs, kb)
    call = writer.calls[0]
    assert call["model"] == MODEL
    assert "<document>\n" in call["system"] and "Refunds" in call["system"] and "titled 'Refunds'" in call["system"]
    assert call["passage"] in TEXT and "Answer only with the succinct context" in call["prompt"]
    assert call["attribution"].session_id == f"knowledge:{kb.id}" and call["attribution"].snapshot_id
    assert len({c["system"] for c in writer.calls}) == 1, "one document: every call shares the cached prefix"


async def test_the_call_turns_reasoning_off_and_caps_the_answer(jobs, monkeypatch):
    seen = []

    async def fake_ex(**kwargs):
        seen.append(kwargs)
        return LLMResult("Zebracontext.", 10, 3)

    import core.llm_providers as llm
    monkeypatch.setattr(llm, "generate_response_ex", fake_ex)
    kb = await _base()
    await _ingest(jobs, kb)
    assert seen and all(k["reasoning_effort"] == "off" and k["max_output_tokens"] == enrich.MAX_OUTPUT_TOKENS
                        for k in seen)
    assert {k["agent_id"] for k in seen} == {"knowledge_enrich"} and {k["source"] for k in seen} == {"knowledge"}
    assert {k["tool_name"] for k in seen} == {"knowledge:enrich"}


async def test_a_prefix_base_makes_no_call_and_is_unchanged(jobs, writer):
    kb = await _base(enrichment=Enrichment(mode="prefix"))
    row = await _ingest(jobs, kb)
    assert writer.calls == []
    assert _enrich_stage(row)["detail"] == {"mode": "prefix", "version": "prefix-1"}
    assert all("Zebracontext" not in c.context for c in await _chunks(kb.id))
    assert await _costs() == []


async def test_a_snapshot_made_before_contextual_was_built_keeps_the_prefix(jobs, writer):
    """Restates `test_contextual_enrichment_is_reported_as_not_built`: an index the prefix made stays the prefix's
    until it is rebuilt, and says so."""
    kb = await _base()
    snapshot = await serving_snapshot(kb)
    async with session() as s:  # as every snapshot was recorded before CR1
        await s.execute(update(KnowledgeSnapshotDB).where(KnowledgeSnapshotDB.id == snapshot).values(
            enrichment_mode="prefix", enrichment_version="prefix-1"))
    row = await _ingest(jobs, kb)
    assert writer.calls == []
    detail = _enrich_stage(row)["detail"]
    assert detail["mode"] == "prefix" and detail["requested"] == "contextual"
    assert "rebuild" in detail["note"]


async def test_extract_metadata_is_said_to_be_not_built(jobs, writer):
    """A base stored with it (CCR-13 refuses turning it on, not a base that has it) is told it is not built."""
    from _fakes.knowledge_corpus import save_bases

    kb = await _base()
    kb = kb.model_copy(update={"enrichment": kb.enrichment.model_copy(update={"extract_metadata": True})})
    await save_bases(kb)
    row = await _ingest(jobs, kb)
    assert _enrich_stage(row)["detail"]["extract_metadata"] == "not built yet"


# ── the model ──────────────────────────────────────────────────────────────────

def test_the_model_is_the_bases_then_the_deployments_then_the_workspaces():
    assert enrich.resolve_model(Enrichment(mode="contextual", contextual_model="a"),
                                {"knowledge_contextual_model": "b", "model": "c"}) == "a"
    assert enrich.resolve_model(Enrichment(mode="contextual"), {"knowledge_contextual_model": "b", "model": "c"}) == "b"
    assert enrich.resolve_model(Enrichment(mode="contextual"), {"model": "c"}) == "c"
    assert enrich.resolve_model(Enrichment(mode="contextual"), {}) == ""


async def test_a_snapshot_keeps_the_model_it_was_made_with(jobs, writer):
    await enable_knowledge(**KEYS, knowledge_contextual_model=MODEL)
    kb = await _base(enrichment=Enrichment(mode="contextual"))
    await _ingest(jobs, kb, "doc0.md")
    async with session() as s:
        snap = (await s.execute(select(KnowledgeSnapshotDB).where(KnowledgeSnapshotDB.kb_id == kb.id))).scalar_one()
    assert snap.enrichment_mode == "contextual" and snap.enrichment_version == enrich.CONTEXTUAL_VERSION
    assert snap.stats["enrichment"]["model"] == MODEL

    await enable_knowledge(**KEYS, knowledge_contextual_model="gpt-4.1-nano")  # the default moves on
    writer.calls.clear()
    await _ingest(jobs, kb, "doc1.md", TEXT.replace("Refunds", "Returns"))
    assert writer.calls and {c["model"] for c in writer.calls} == {MODEL}, "never two models' contexts in one index"


# ── windows ────────────────────────────────────────────────────────────────────

def test_a_short_text_is_one_window_and_a_long_one_is_cut_at_line_breaks():
    assert enrich.spans("short") == [(0, 5)]
    text = _long_text()
    cut = enrich.spans(text)
    assert len(cut) == 2 and cut[0][0] == 0 and cut[-1][1] == len(text)
    assert all(a[1] == b[0] for a, b in zip(cut, cut[1:])), "the windows tile the text"
    assert text[cut[0][1] - 1] == "\n", "an edge is moved to a line break"


async def test_a_long_document_is_asked_in_windows_with_its_opening(jobs, writer):
    kb = await _base()
    text = _long_text()
    row = await _ingest(jobs, kb, text=text)
    systems = sorted({c["system"] for c in writer.calls}, key=len)
    assert len(systems) == 2, "one cached prefix per window"
    later = next(s for s in systems if "[…]" in s)
    assert "part of a long document" in later and "# Section 0" in later, "a later window carries the opening"
    detail = _enrich_stage(row)["detail"]
    assert detail["windows"] == 2 and detail["whole"] is False
    assert all(enrich.estimate_tokens(s) < enrich.HEAD_TOKENS + enrich.WINDOW_TOKENS + 1500 for s in systems)


async def test_a_windows_first_call_finishes_before_the_rest_start_and_at_most_four_run(jobs, monkeypatch):
    writer = Writer(delay=0.02)
    monkeypatch.setattr(enrich, "_ask", writer)
    kb = await _base()
    await _ingest(jobs, kb, text=_long_text(10))  # ~10k tokens: one window, many passages
    assert writer.events[0][0] == "start" and writer.events[1][0] == "end", "the first call runs alone"
    assert 1 < writer.most <= enrich.CONCURRENCY


async def test_an_attachments_passages_are_asked_against_the_attachment(jobs, writer, monkeypatch):
    from core.knowledge.parsed import Attachment, ParsedDocument, ParserInfo, SourceInfo
    from core.knowledge.pipeline import index_document
    from core.knowledge.pipeline_contract import IndexContext
    from core.knowledge.types import Attribution

    kb = await _base()
    inner = ParsedDocument(source=SourceInfo(path="note.txt", sha256="b" * 64, mime="text/plain", size_bytes=10, source="upload"),
                           parser=ParserInfo(name="text", version="1"), text="The attachment says the fee is ten.")
    outer = ParsedDocument(source=SourceInfo(path="kb/mail.eml", sha256="a" * 64, mime="message/rfc822", size_bytes=10,
                                             source="upload"),
                           parser=ParserInfo(name="eml", version="1"), text="Please see the attached note.",
                           attachments=[Attachment(name="note.txt", sha256="b" * 64, mime="text/plain",
                                                   size_bytes=10, parsed=inner)])
    async with session() as s:
        s.add(KnowledgeDocumentDB(id="kdoc_" + "e" * 32, tenant_id="default", kb_id=kb.id, path="kb/mail.eml",
                                  source="upload", status="indexing", version=1))
    snapshot = await serving_snapshot(kb)
    await index_document(IndexContext(kb=kb, doc_id="kdoc_" + "e" * 32, doc_version=1, sha256="a" * 64,
                                      path="kb/mail.eml", source="upload", parsed=outer, snapshot_id=snapshot,
                                      attribution=Attribution(kb_id=kb.id, doc_id="kdoc_" + "e" * 32,
                                                              session_id=f"knowledge:{kb.id}"),
                                      settings=dict(KEYS)))
    by_passage = {c["passage"]: c["system"] for c in writer.calls}
    assert "The attachment says the fee is ten." in by_passage["The attachment says the fee is ten."]
    assert "attached note" not in by_passage["The attachment says the fee is ten."]


# ── the context cache ──────────────────────────────────────────────────────────

async def test_indexing_the_same_content_again_asks_nothing(jobs, writer):
    kb = await _base()
    await _ingest(jobs, kb, "doc0.md")
    asked = len(writer.calls)
    await _ingest(jobs, kb, "doc1.md")  # the same bytes under another name: the same file sha256
    assert len(writer.calls) == asked
    assert all(" · Zebracontext about " in c.context for c in await _chunks(kb.id))


async def test_the_cache_lives_beside_the_parse_cache_and_goes_with_the_content(jobs, writer):
    from core.knowledge.parse import cache as parse_cache
    from core.storage import get_blob_store

    kb = await _base()
    row = await _ingest(jobs, kb)
    sig = enrich_cache.signature(MODEL, prompt_version=enrich.PROMPT_VERSION, windows=enrich.WINDOWS)
    key = enrich_cache.key(row.sha256, sig)
    assert key.startswith(parse_cache.PREFIX + "/") and get_blob_store().get_bytes(key)
    assert len(await enrich_cache.load(row.sha256, sig)) == len(writer.calls)
    async with session() as s:
        await s.execute(update(KnowledgeDocumentDB).values(sha256="f" * 64))  # no document has that content now
    await parse_cache.forget_unreferenced(row.sha256)
    assert get_blob_store().get_bytes(key) is None


async def test_a_cache_entry_is_never_read_as_a_parse(jobs, writer):
    from core.knowledge.parse import cache as parse_cache

    kb = await _base()
    row = await _ingest(jobs, kb)
    entries = [k for k in await asyncio.to_thread(_keys, parse_cache.PREFIX) if row.sha256 in k]
    assert entries and all(".ctx-" in k for k in entries), "a .md is not parse-cached; only the context entry is there"


def _keys(prefix: str) -> list[str]:
    from core.storage import get_blob_store

    return [s.key for s in get_blob_store().list_stats(prefix + "/")]


# ── failures, by cause ─────────────────────────────────────────────────────────

async def test_an_unusable_answer_is_asked_once_more(jobs, monkeypatch):
    def answer(passage, n):
        return "" if n == 1 else f"Zebracontext {n}"

    writer = Writer(answer=answer)
    monkeypatch.setattr(enrich, "_ask", writer)
    kb = await _base()
    row = await _ingest(jobs, kb)
    chunks = await _chunks(kb.id)
    assert len(writer.calls) == len(chunks) + 1
    assert all("Zebracontext" in c.context for c in chunks)
    assert _enrich_stage(row)["detail"]["fallback"] == 0


async def test_a_passage_with_two_unusable_answers_keeps_the_prefix_and_is_warned(jobs, monkeypatch):
    blocked = EmptyAnswer("blocked by a safety filter")
    blocked.usage = (90, 0, 0, 0)

    def answer(passage, n):
        return blocked if passage.startswith("# Hosting") or "hosting" in passage[:60].lower() else "Zebracontext."

    writer = Writer(answer=answer)
    monkeypatch.setattr(enrich, "_ask", writer)
    kb = await _base()
    row = await _ingest(jobs, kb)
    assert row.status == "indexed", "a passage's fallback is the passage's, never the document's"
    stage = _enrich_stage(row)
    assert stage["status"] == "partial" and stage["detail"]["fallback"] >= 1
    assert any(w["code"] == "enrich_fallback" for w in row.report["warnings"])
    hosting = [c for c in await _chunks(kb.id) if "hosting" in c.content.lower()]
    assert hosting and all("Zebracontext" not in c.context and c.context.startswith("Document: ") for c in hosting)
    cost = (await _costs())[0]
    assert cost.units == len(writer.calls), "every call is counted, the unusable ones too"

    writer.answer = lambda passage, n: "Zebracontext now."  # the next build asks again for those only
    asked = len(writer.calls)
    await _ingest(jobs, kb, "doc1.md")
    assert len(writer.calls) - asked == len(hosting)


async def test_an_answer_cut_at_the_token_limit_twice_falls_back(jobs, monkeypatch):
    def answer(passage, n):
        return LLMResult("Zebracontext that never ends", 10, 150, 0, 0, "max_tokens")

    monkeypatch.setattr(enrich, "_ask", Writer(answer=answer))
    kb = await _base()
    row = await _ingest(jobs, kb)
    assert _enrich_stage(row)["detail"]["fallback"] == len(await _chunks(kb.id))


async def test_a_provider_still_failing_fails_the_document_retryably_and_keeps_what_was_written(jobs, monkeypatch):
    def answer(passage, n):
        return LLMError("HTTP 503: overloaded") if n > 2 else f"Zebracontext {n}"

    writer = Writer(answer=answer)
    monkeypatch.setattr(enrich, "_ask", writer)
    kb = await _base()
    row = await _ingest(jobs, kb, text=_long_text(6))  # one window, many passages
    assert (row.status, row.status_reason) == ("failed", "enrich_failed")
    assert "could not write the passages' context" in row.error
    assert len(writer.calls) < 30, "the calls still waiting were cancelled, not made"
    written = await enrich_cache.load(
        row.sha256, enrich_cache.signature(MODEL, prompt_version=enrich.PROMPT_VERSION, windows=enrich.WINDOWS))
    assert len(written) == 2, "what was written before the failure is kept for the retry"
    assert sum(c.units for c in await _costs()) == 2, "and what the answered calls spent is recorded"

    from core.knowledge.ingest import retry_document

    writer.answer = None  # the retry pays only for the passages it has no context for
    asked = len(writer.calls)
    await retry_document(row)
    await jobs.drain(run_ingest_job)
    again = await documents.get(row.id)
    n = len(await _chunks(kb.id))
    assert again.status == "indexed" and len(writer.calls) - asked == n - 2


@pytest.mark.parametrize("error, retryable", [
    (LLMError("HTTP 503: overloaded"), True),
    (LLMError("HTTP 429: slow down"), True),
    (LLMError("HTTP 401: invalid api key"), False),
    (LLMError("HTTP 429: insufficient_quota"), False),
])
def test_how_a_provider_failure_is_classed(error, retryable):
    mapped = enrich._hard(error, MODEL, platform=False)
    assert isinstance(mapped, EnrichmentError) and mapped.retryable is retryable and mapped.code == "enrich_failed"
    assert failure_status(mapped) == ("failed", "enrich_failed")


def test_an_empty_wallet_on_our_gateway_pauses():
    mapped = enrich._hard(LLMError("Model gateway refused the call: wallet empty", provider="gateway", status=402),
                          "synapse.nova-lite", platform=True)
    assert isinstance(mapped, WalletExhausted) and failure_status(mapped) == ("paused", "wallet_402")
    own = enrich._hard(LLMError("HTTP 402: payment required"), MODEL, platform=False)
    assert isinstance(own, EnrichmentError) and not own.retryable, "an own key's 402 is that account's quota"


async def test_a_gateway_402_pauses_the_document(jobs, monkeypatch):
    def answer(passage, n):
        return LLMError("Model gateway refused the call: empty", provider="gateway", status=402)

    monkeypatch.setattr(enrich, "_ask", Writer(answer=answer))
    kb = await _base(enrichment=Enrichment(mode="contextual", contextual_model="synapse.nova-lite"))
    row = await _ingest(jobs, kb)
    assert (row.status, row.status_reason) == ("paused", "wallet_402")


async def test_with_no_model_named_anywhere_the_document_fails_before_any_call(jobs, writer):
    kb = await _base(enrichment=Enrichment(mode="contextual"))
    snapshot = await serving_snapshot(kb)
    async with session() as s:  # a deployment and a workspace that name no chat model
        snap = (await s.execute(select(KnowledgeSnapshotDB).where(KnowledgeSnapshotDB.id == snapshot))).scalar_one()
        snap.stats = {**snap.stats, "enrichment": {"model": ""}}
    await enable_knowledge(**KEYS, model="", knowledge_contextual_model="")
    row = await _ingest(jobs, kb)
    assert (row.status, row.status_reason) == ("failed", "enrich_failed") and "no model" in row.error
    assert writer.calls == []


async def test_a_key_not_saved_fails_before_any_call(jobs, writer):
    kb = await _base(enrichment=Enrichment(mode="contextual", contextual_model="gemini-2.5-flash-lite"))
    row = await _ingest(jobs, kb)
    assert (row.status, row.status_reason) == ("failed", "enrich_failed") and "key" in row.error
    assert writer.calls == []


# ── money ──────────────────────────────────────────────────────────────────────

async def test_each_windows_calls_are_one_cost_row_logged_by_the_chat_path(jobs, monkeypatch):
    writer = Writer(usage=(100, 20, 300, 0), charged=7)
    monkeypatch.setattr(enrich, "_ask", writer)
    kb = await _base()
    row = await _ingest(jobs, kb)
    rows = await _costs()
    assert len(rows) == 1
    cost = rows[0]
    n = len(writer.calls)
    assert cost.units == n and cost.unit == "requests" and cost.model == MODEL and cost.cost_source == "byok"
    assert cost.usage_logged_by == "chat" and cost.doc_id == row.id and cost.snapshot_id
    assert cost.input_tokens == n * 400 and cost.output_tokens == n * 20
    assert cost.details["cache_read_tokens"] == n * 300 and cost.details["uncached_input_tokens"] == n * 100
    assert cost.charged_microusd == n * 7, "what a gateway reported it charged"
    from core import usage_tracker
    assert cost.est_usd == pytest.approx(usage_tracker.calculate_cost(
        MODEL, input_tokens=n * 100, output_tokens=n * 20, cache_read_tokens=n * 300))
    async with session() as s:
        mirrored = (await s.execute(select(UsageLogDB).where(UsageLogDB.tool_name == "knowledge:enrich"))).all()
    assert mirrored == [], "the ledger never mirrors an enrich row: the chat path logs its own"
    report_cost = row.report["cost"]["by_stage"]
    assert report_cost["enrich"] == pytest.approx(cost.est_usd)


async def test_a_budget_too_small_for_the_contexts_pauses_before_any_call(jobs, writer):
    kb = await _base(budget_monthly_usd=0.0000001)
    row = await _ingest(jobs, kb)
    assert (row.status, row.status_reason) == ("paused", "budget_cap")
    assert writer.calls == []


async def test_the_budget_is_checked_before_every_window(jobs, monkeypatch):
    checked = []
    from core.knowledge import ledger

    async def spy(kb, estimate, **kw):
        checked.append(estimate)

    monkeypatch.setattr(ledger, "check_budget", spy)
    monkeypatch.setattr(enrich, "_ask", Writer())
    kb = await _base()
    await _ingest(jobs, kb, text=_long_text())
    enrich_checks = [c for c in checked if c > 0]
    assert len(enrich_checks) >= 2 and all(c > 0 for c in enrich_checks), "one real estimate per window"


async def test_the_chat_path_logs_each_call_itself(jobs, monkeypatch):
    """One call through the real `generate_response_ex` (an OpenAI-compatible server, faked): `usage_logs` gets the
    chat path's own row, in the base's ingestion session."""
    install_httpx(monkeypatch, FakeResponse({
        "usage": {"prompt_tokens": 50, "completion_tokens": 9},
        "choices": [{"message": {"content": "Zebracontext from the server."}, "finish_reason": "stop"}]}))
    await enable_knowledge(**KEYS, openai_compatible_base_url="http://compat.test", openai_compatible_key="k")
    kb = await _base(enrichment=Enrichment(mode="contextual", contextual_model="oaic.fake-writer"))
    row = await _ingest(jobs, kb, text="# Note\n\nA short note about the hosting fee.")
    assert row.status == "indexed"
    async with session() as s:
        logs = (await s.execute(select(UsageLogDB).where(UsageLogDB.tool_name == "knowledge:enrich"))).scalars().all()
    assert len(logs) == 1 and logs[0].session_id == f"knowledge:{kb.id}" and logs[0].agent_id == "knowledge_enrich"
    assert logs[0].source == "knowledge" and logs[0].input_tokens == 50
    assert (await _costs())[0].usage_logged_by == "chat"


# ── a long enrichment keeps its lease ──────────────────────────────────────────

async def test_a_long_enrichment_keeps_the_document_from_looking_stuck(jobs, monkeypatch):
    from core.knowledge.parse import limits

    beats = []
    real = documents.heartbeat

    async def counting(doc_id, **kw):
        beats.append(kw.get("status"))
        return await real(doc_id, **kw)

    monkeypatch.setattr(limits, "HEARTBEAT_S", 0.01)
    monkeypatch.setattr(documents, "heartbeat", counting)
    monkeypatch.setattr(enrich, "_ask", Writer(delay=0.03))
    kb = await _base()
    await _ingest(jobs, kb)
    assert beats and set(beats) == {"indexing"}


async def test_the_heartbeat_moves_only_a_row_in_the_status_it_names():
    kb = await _base()
    async with session() as s:
        for doc_id, status in (("kdoc_" + "1" * 32, "indexing"), ("kdoc_" + "2" * 32, "deleting")):
            s.add(KnowledgeDocumentDB(id=doc_id, tenant_id="default", kb_id=kb.id, path=f"kb/{status}.md",
                                      source="vault", status=status, version=1))
    assert await documents.heartbeat("kdoc_" + "1" * 32, status="indexing") is True
    assert await documents.heartbeat("kdoc_" + "1" * 32) is False, "a parse's beat never moves an indexing row"
    assert await documents.heartbeat("kdoc_" + "2" * 32, status="indexing") is False, "a tombstone keeps its age"


def test_clean():
    assert enrich.clean('  Context:  "This passage covers refunds."  ') == "This passage covers refunds."
    assert enrich.clean("**Refunds** under\nthe 2024 policy") == "Refunds under the 2024 policy"
    long = enrich.clean("word " * 400)
    assert len(long) <= enrich.MAX_CONTEXT_CHARS + 1 and long.endswith("…")
    assert enrich.clean("   ") == ""


def test_of_several_failures_an_empty_wallet_then_a_lasting_one_is_reported():
    wallet = WalletExhausted("empty")
    lasting = EnrichmentError("bad key", retryable=False)
    passing = EnrichmentError("overloaded", retryable=True)
    assert min([passing, lasting, wallet], key=enrich._telling) is wallet
    assert min([passing, lasting], key=enrich._telling) is lasting


# ── turning it on is a blue/green rebuild ──────────────────────────────────────

async def _edit(kb_id: str, **fields):
    from core.knowledge import snapshots
    from core.knowledge.api_models import KnowledgeBaseUpdate

    current = await bases.require_base(kb_id)
    before, after = await bases.update_base(kb_id, KnowledgeBaseUpdate(revision=current.revision, **fields))
    return after, await snapshots.after_save(before, after)


async def _snaps(kb_id: str) -> dict[str, KnowledgeSnapshotDB]:
    async with session() as s:
        rows = (await s.execute(select(KnowledgeSnapshotDB).where(KnowledgeSnapshotDB.kb_id == kb_id)
                                .order_by(KnowledgeSnapshotDB.created_at))).scalars().all()
    return {r.status: r for r in rows}


async def _zebra_hits(kb_id: str, snapshot_id: str) -> int:
    hits = await SqlKnowledgeIndex().search_lexical(
        LexicalQuery((Scope(kb_id, snapshot_id),), "zebracontext", "simple", None, 20))
    return len(hits.candidates)


async def _prefix_base(jobs):
    kb = await _base(enrichment=Enrichment(mode="prefix"))
    await _ingest(jobs, kb, "doc0.md")
    await _ingest(jobs, kb, "doc1.md", TEXT.replace("Refunds", "Returns"))
    return kb


async def test_turning_it_on_builds_beside_the_serving_index_then_switches(jobs, writer):
    kb = await _prefix_base(jobs)
    old = (await _snaps(kb.id))["active"]
    kb, started = await _edit(kb.id, enrichment=Enrichment(mode="contextual", contextual_model=MODEL))
    assert started
    building = (await _snaps(kb.id))["building"]
    assert building.enrichment_mode == "contextual" and building.stats["enrichment"]["model"] == MODEL
    assert await _zebra_hits(kb.id, old.id) == 0, "the prefix index serves meanwhile"

    await jobs.drain(run_ingest_job)
    snaps = await _snaps(kb.id)
    assert snaps["active"].id == building.id and snaps["retired"].id == old.id
    assert await _zebra_hits(kb.id, building.id) == len(writer.calls) > 0
    assert await _zebra_hits(kb.id, old.id) == 0, "the replaced index stays as it was, to compare against"


async def test_a_provider_outage_fails_the_rebuild_and_the_old_index_serves(jobs, monkeypatch):
    from core.knowledge import snapshots

    monkeypatch.setattr(snapshots, "BACKOFF_S", (0.0, 0.0))
    writer = Writer()
    monkeypatch.setattr(enrich, "_ask", writer)
    kb = await _prefix_base(jobs)
    old = (await _snaps(kb.id))["active"]
    writer.answer = lambda passage, n: LLMError("HTTP 503: overloaded")
    kb, _ = await _edit(kb.id, enrichment=Enrichment(mode="contextual", contextual_model=MODEL))
    await jobs.drain(run_ingest_job)
    snaps = await _snaps(kb.id)
    assert snaps["active"].id == old.id and "failed" in snaps
    assert "after 3 tries" in snaps["failed"].stats["error"]
    assert snaps["failed"].stats["failed_docs"][0]["code"] == "enrich_failed"

    writer.answer = None  # the outage is over: an edit that changes nothing retries the rebuild
    kb, started = await _edit(kb.id)
    assert started
    await jobs.drain(run_ingest_job)
    assert (await _snaps(kb.id))["active"].enrichment_mode == "contextual"


async def test_an_empty_wallet_pauses_the_rebuild(jobs, monkeypatch):
    writer = Writer()
    monkeypatch.setattr(enrich, "_ask", writer)
    kb = await _prefix_base(jobs)
    writer.answer = lambda passage, n: LLMError("Model gateway refused the call: empty", provider="gateway",
                                               status=402)
    kb, _ = await _edit(kb.id, enrichment=Enrichment(mode="contextual", contextual_model="synapse.nova-lite"))
    await jobs.drain(run_ingest_job)
    building = (await _snaps(kb.id))["building"]
    assert building.stats.get("paused_reason") == "wallet_402"


async def test_a_long_rebuild_keeps_its_lease(jobs, monkeypatch):
    from core.knowledge import snapshots
    from core.knowledge.parse import limits

    beats = []
    real = snapshots._beat

    async def counting(snapshot, *a, **kw):
        beats.append(snapshot.id)
        return await real(snapshot, *a, **kw)

    monkeypatch.setattr(enrich, "_ask", Writer())
    kb = await _prefix_base(jobs)
    monkeypatch.setattr(limits, "HEARTBEAT_S", 0.01)
    monkeypatch.setattr(snapshots, "_beat", counting)
    monkeypatch.setattr(enrich, "_ask", Writer(delay=0.03))
    kb, _ = await _edit(kb.id, enrichment=Enrichment(mode="contextual", contextual_model=MODEL))
    await jobs.drain(run_ingest_job)
    building_beats = len(beats)
    assert building_beats > 4, "beaten while the contexts were written, not only after each document"



# ── a provider's rate limit is paced, not failed (F356) ────────────────────────

@pytest.fixture
def quick_pacing(monkeypatch):
    monkeypatch.setattr(enrich, "RATE_WAIT_S", 0.0, raising=False)
    monkeypatch.setattr(enrich, "RATE_WAIT_MAX_S", 0.0, raising=False)


async def test_a_rate_limit_slows_the_document_to_one_call_at_a_time_and_it_finishes(jobs, monkeypatch, quick_pacing):
    """F356, measured on a tier-1 OpenAI key: 4 calls at once of ~13.5k tokens each drew 429s past the provider's own
    retries and failed the rebuild. Now the document goes one call at a time, waits each limit out, and finishes."""
    limited: list[int] = []

    def answer(passage, n):
        if n in (3, 4, 6):  # three calls refused, past the provider's retries
            limited.append(n)
            return LLMError("HTTP 429: Rate limit reached for gpt-4.1-nano on tokens per min (TPM)")
        return f"Zebracontext {n}"

    writer = Writer(answer=answer, delay=0.01)
    monkeypatch.setattr(enrich, "_ask", writer)
    kb = await _base()
    row = await _ingest(jobs, kb, text=_long_text(6))  # one window, many passages
    assert (row.status, row.status_reason) == ("indexed", None), row.error
    chunks = await _chunks(kb.id)
    assert all("Zebracontext" in c.context for c in chunks), "every passage got its context after all"
    detail = _enrich_stage(row)["detail"]
    assert detail["rate_limited"]["waits"] == 3 and detail["fallback"] == 0
    # Calls already in flight when a limit arrives finish as they started; everything after them is one at a time.
    settled = writer.peaks[limited[-1] + enrich.CONCURRENCY:]
    assert settled and max(settled) == 1, writer.peaks
    assert max(writer.peaks[:limited[0]]) > 1, "before any limit, several at once"
    assert sum(c.units for c in await _costs()) == len(chunks), "refused calls cost nothing; each answer once"


async def test_a_rate_limit_that_never_lifts_fails_the_document_retryably(jobs, monkeypatch, quick_pacing):
    monkeypatch.setattr(enrich, "NO_PROGRESS_S", 0.05, raising=False)
    writer = Writer(answer=lambda passage, n: LLMError("HTTP 429: Too Many Requests"), delay=0.005)
    monkeypatch.setattr(enrich, "_ask", writer)
    kb = await _base()
    row = await _ingest(jobs, kb)
    assert (row.status, row.status_reason) == ("failed", "enrich_failed"), "nothing answered for the deadline"
    assert len(writer.calls) > 1, "it waited and asked again before giving up"


async def test_inside_parking_a_long_limit_gives_the_slot_back_and_the_next_run_asks_only_the_rest(jobs, monkeypatch):
    """KL1 (F360): in cloud's knowledge worker a limit that would keep the document waiting past the give-back time
    parks it — its contexts written so far kept — and the next run asks only for the passages still without one."""
    from core.knowledge import pacing

    monkeypatch.setattr(pacing, "GIVE_BACK_AFTER_S", 0.0)
    monkeypatch.setattr(enrich, "RATE_WAIT_S", 30.0, raising=False)
    limit = {"on": True}

    def answer(passage, n):
        if n > 2 and limit["on"]:
            return LLMError("HTTP 429: Rate limit reached for gpt-4.1-nano on tokens per min (TPM)")
        return f"Zebracontext {n}"

    writer = Writer(answer=answer)
    monkeypatch.setattr(enrich, "_ask", writer)
    kb = await _base()
    write_vault("kb/doc0.md", _long_text(6))
    row, _ = await documents.upsert_for_path(kb, "kb/doc0.md", source="vault", sha256=None, size=None)
    await dispatch.enqueue(kb.id, "upsert", "vault_write", path=row.path, doc_id=row.id)
    with pacing.parking(), pytest.raises(pacing.Parked) as parked:
        await jobs.drain(run_ingest_job)
    assert parked.value.reason == "rate_limited"
    row = await documents.get(row.id)
    assert (row.status, row.attempts) == ("queued", 0)
    asked_before = len(writer.calls)
    limit["on"] = False
    await dispatch.enqueue(kb.id, "upsert", "retry", path=row.path, doc_id=row.id)
    with pacing.parking():
        await jobs.drain(run_ingest_job)
    row = await documents.get(row.id)
    assert row.status == "indexed", row.error
    chunks = await _chunks(kb.id)
    assert all("Zebracontext" in c.context for c in chunks)
    assert len(writer.calls) - asked_before == len(chunks) - 2, "the two contexts written before the park are kept"


async def test_bedrock_throttling_is_paced_like_any_rate_limit(jobs, monkeypatch, quick_pacing):
    """Bedrock's throttling reaches the engine with no HTTP status (a `model_error`): its words say what it is."""
    def answer(passage, n):
        if n == 2:
            return LLMError("Bedrock LLM Error: failed after 5 attempts. Last error: An error occurred "
                            "(ThrottlingException) when calling the Converse operation: Too many requests")
        return "Zebracontext."

    monkeypatch.setattr(enrich, "_ask", Writer(answer=answer))
    kb = await _base()
    row = await _ingest(jobs, kb)
    assert row.status == "indexed" and _enrich_stage(row)["detail"]["rate_limited"]["waits"] == 1


def test_an_exhausted_quota_or_a_refused_key_is_never_paced():
    assert enrich._rate_limited(LLMError("HTTP 429: Rate limit reached"))
    assert enrich._rate_limited(LLMError("Bedrock refused: (ThrottlingException) slow down"))
    assert not enrich._rate_limited(LLMError("HTTP 429: You exceeded your current quota: insufficient_quota"))
    assert not enrich._rate_limited(LLMError("HTTP 401: invalid api key"))
    assert not enrich._rate_limited(LLMError("HTTP 503: overloaded"))


# ── progress you can see (PG1, F357) ───────────────────────────────────────────

@pytest.fixture
def told(monkeypatch):
    """What the document's progress was told: each stage begun, each count in the contexts stage, each wait."""
    from core.knowledge import progress

    got = {"stages": [], "counts": [], "waits": []}
    stage, advance, waiting = progress.Tracker.stage, progress.Tracker.advance, progress.Tracker.waiting

    def on_stage(self, name, **kw):
        got["stages"].append((name, kw.get("total"), kw.get("unit"), kw.get("done")))
        stage(self, name, **kw)

    def on_advance(self, **kw):
        advance(self, **kw)
        if self.stage_name == "enrich":
            got["counts"].append(self.done)

    def on_wait(self, **kw):
        got["waits"].append(kw)
        return waiting(self, **kw)

    monkeypatch.setattr(progress.Tracker, "stage", on_stage)
    monkeypatch.setattr(progress.Tracker, "advance", on_advance)
    monkeypatch.setattr(progress.Tracker, "waiting", on_wait)
    return got


async def test_writing_contexts_counts_each_passage_and_those_kept_are_done_already(jobs, writer, told):
    kb = await _base()
    row = await _ingest(jobs, kb)
    (contexts,) = [s for s in told["stages"] if s[0] == "enrich"]
    assert contexts == ("enrich", row.chunks, "passages", 0)
    assert told["counts"] == list(range(1, row.chunks + 1)), "one per passage answered"
    told["stages"].clear(), told["counts"].clear()
    calls = len(writer.calls)
    await _ingest(jobs, kb, name="copy.md")  # the same content: every context is kept
    (contexts,) = [s for s in told["stages"] if s[0] == "enrich"]
    assert contexts == ("enrich", row.chunks, "passages", row.chunks) and len(writer.calls) == calls


async def test_a_rate_limit_waited_out_is_told_with_the_provider_s_own_name(jobs, monkeypatch, quick_pacing, told):
    writer = Writer(answer=lambda passage, n: LLMError("HTTP 429: Rate limit reached") if n == 2 else f"Ctx {n}")
    monkeypatch.setattr(enrich, "_ask", writer)
    kb = await _base()
    row = await _ingest(jobs, kb)
    assert row.status == "indexed"
    assert [(w["provider"], w["provider_id"]) for w in told["waits"]] == [("OpenAI", "openai")]
