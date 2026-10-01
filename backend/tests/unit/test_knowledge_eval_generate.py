"""Drafting test questions from a knowledge base's own passages (V1, D168): a seeded sample, quotes that must be
in their passage, paraphrases kept far from it, multi-hop pairs, nothing saved, and a price first.
The model is a fake that reads the passages it is shown ($0). SQLite always; Postgres with
`SYNAPSE_TEST_POSTGRES_URL`."""
from __future__ import annotations

import random

import pytest
from sqlalchemy import func, select

from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from _fakes.knowledge_env import enable_knowledge, use_saved_settings
from _fakes.knowledge_eval import POLICIES, Writer, first_sentence, policies
from core import usage_tracker
from core.knowledge import dispatch, evals_generate as G, seams
from core.knowledge.api_models import EvalDraftRequest
from core.knowledge.testing.fakes import HashingEmbedder, RecordingDispatcher
from core.store import session
from core.store.models import CollectionDB, KnowledgeChunkDB

pytestmark = pytest.mark.usefixtures("knowledge_store")
SETTINGS = {"model": "writer-1", "judge_model": ""}


@pytest.fixture(autouse=True)
async def _on(monkeypatch):
    seams.reset_all()
    dispatch.reset()
    use_saved_settings(monkeypatch)
    await enable_knowledge()
    seams.set_knowledge_embedder(HashingEmbedder(dims=16))
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
    monkeypatch.setattr("core.llm_providers.generate_response", fake)
    return fake


async def _chunk(chunk_id: str) -> KnowledgeChunkDB:
    async with session() as s:
        return (await s.execute(select(KnowledgeChunkDB).where(KnowledgeChunkDB.chunk_id == chunk_id))).scalar_one()


class TestWords:
    def test_locate_finds_a_quote_exactly_or_with_spacing_and_punctuation_changed(self):
        text = "Refunds are issued by the billing team — in Lisbon — and arrive soon."
        assert G.locate("billing team", text) == (26, 38)
        start, end = G.locate("“issued by the billing team in Lisbon”", text)
        assert text[start:end] == "issued by the billing team — in Lisbon"
        assert G.locate("in Paris and Rome", text) is None
        assert G.locate("Lisbon", "nothing like it") is None

    def test_overlap_counts_content_words(self):
        passage = POLICIES["policies/refunds.md"]
        assert G.overlap("Who issued refunds in Lisbon?", passage) == 1.0
        assert G.overlap("Who issues refunds from Lisbon?", passage) == 0.6667  # "issues" ≠ "issued"
        assert G.overlap("How long until my money comes back?", passage) == 0.0
        assert G.content_words("返金は三十日以内") == {"返金", "金は", "は三", "三十", "十日", "日以", "以内"}

    def test_the_writer_is_the_judge_model_else_the_workspaces(self):
        assert G.default_model({"judge_model": "j", "model": "m"}) == "j"
        assert G.default_model({"judge_model": "default", "model": "m"}) == "m"
        assert G.default_model({}) == ""


class TestSample:
    async def test_a_seed_draws_the_same_passages_documents_first(self, jobs):
        kb = await policies(jobs)
        one = await G.sample(kb, random.Random(7), size=4)
        two = await G.sample(kb, random.Random(7), size=4)
        assert [p.chunk_id for p in one] == [p.chunk_id for p in two]
        assert len({p.doc_id for p in one}) == 4  # one passage from each document before a second from any

    async def test_short_repeated_and_already_asked_passages_are_left_out(self, jobs):
        kb = await policies(jobs)
        drawn = await G.sample(kb, random.Random(1))
        assert all(G._tokens(p.content) >= G.MIN_PASSAGE_TOKENS for p in drawn)
        asked = drawn[0]
        quote = first_sentence(asked.content)
        again = await G.sample(kb, random.Random(1), exclude=[{
            "doc_id": asked.doc_id, "doc_sha256": asked.doc_sha256, "char_start": asked.char_start,
            "char_end": asked.char_end, "quote": quote}])
        assert asked.chunk_id not in {p.chunk_id for p in again} and len(again) == len(drawn) - 1

    async def test_nothing_indexed_is_said(self):
        from core.knowledge import bases
        from core.knowledge.api_models import KnowledgeBaseCreate

        kb = await bases.create_base(KnowledgeBaseCreate(name="Empty", folder="empty"))
        with pytest.raises(ValueError, match="Nothing in this knowledge base is indexed"):
            await G.sample(kb, random.Random(1))


class TestDrafts:
    async def test_direct_drafts_quote_their_passage_and_save_nothing(self, jobs, writer):
        kb = await policies(jobs)
        out = await G.draft(kb, EvalDraftRequest(direct=3, paraphrase=0, multi_hop=0, seed=3), SETTINGS)
        assert (out.model, out.saved, out.dropped) == ("writer-1", False, {})
        assert len(out.drafts) == 3 and {d.kind for d in out.drafts} == {"direct"}
        for d in out.drafts:
            ev = d.evidence[0]
            async with session() as s:
                chunk = (await s.execute(select(KnowledgeChunkDB).where(
                    KnowledgeChunkDB.doc_id == ev.doc_id, KnowledgeChunkDB.char_start <= ev.char_start,
                    KnowledgeChunkDB.char_end >= ev.char_end))).scalars().first()
            assert chunk.content[ev.char_start - chunk.char_start: ev.char_end - chunk.char_start] == ev.quote
            assert ev.doc_sha256 == chunk.doc_sha256 and ev.path.startswith("policies/")
        async with session() as s:
            assert (await s.execute(select(func.count()).select_from(CollectionDB).where(
                CollectionDB.collection == "knowledge_eval_sets"))).scalar_one() == 0
        call = writer.calls[0]
        assert (call["current_model"], call["source"], call["agent_id"], call["session_id"]) == (
            "writer-1", "evals", "knowledge_eval_writer", f"knowledge-eval:{kb.id}")

    async def test_a_paraphrase_too_close_to_its_passage_is_asked_again(self, jobs, monkeypatch):
        kb = await policies(jobs)
        writer = Writer([lambda passages: first_sentence(passages[0]),  # every word the passage's own
                         "How long do I have to change my mind and get my money back?"])
        monkeypatch.setattr("core.llm_providers.generate_response", writer)
        out = await G.draft(kb, EvalDraftRequest(direct=0, paraphrase=1, multi_hop=0, seed=11), SETTINGS)
        assert len(writer.calls) == 2 and "reused these words" in writer.calls[1]["prompt_msg"]
        assert len(out.drafts) == 1 and out.drafts[0].overlap <= G.MAX_OVERLAP

    async def test_a_paraphrase_that_stays_close_is_dropped(self, jobs, monkeypatch):
        kb = await policies(jobs)
        monkeypatch.setattr("core.llm_providers.generate_response",
                            Writer([lambda passages: first_sentence(passages[0])]))
        out = await G.draft(kb, EvalDraftRequest(direct=0, paraphrase=1, multi_hop=0, seed=11), SETTINGS)
        assert out.drafts == [] and out.dropped == {"too_close_to_passage": 1}

    @pytest.mark.parametrize("writer_kw,why", [
        ({"quote": lambda passage: "words the passage never says at all"}, "quote_not_found"),
        ({"reply": "Sure! Here is a question about refunds."}, "unreadable"),
    ])
    async def test_a_draft_that_cannot_be_checked_is_dropped_and_counted(self, jobs, monkeypatch, writer_kw, why):
        kb = await policies(jobs)
        monkeypatch.setattr("core.llm_providers.generate_response", Writer(**writer_kw))
        out = await G.draft(kb, EvalDraftRequest(direct=2, paraphrase=0, multi_hop=0, seed=1), SETTINGS)
        assert out.drafts == [] and out.dropped == {why: 2}

    async def test_a_failed_call_drops_one_draft(self, jobs, monkeypatch):
        kb = await policies(jobs)

        async def broken(*args, **kwargs):
            raise RuntimeError("provider down")

        monkeypatch.setattr("core.llm_providers.generate_response", broken)
        out = await G.draft(kb, EvalDraftRequest(direct=1, paraphrase=0, multi_hop=0, seed=1), SETTINGS)
        assert out.dropped == {"failed": 1}

    async def test_multi_hop_pairs_documents_that_share_a_rare_term(self, jobs, writer):
        kb = await policies(jobs)
        out = await G.draft(kb, EvalDraftRequest(direct=0, paraphrase=0, multi_hop=2, seed=5), SETTINGS)
        assert len(out.drafts) == 2
        pairs = {frozenset(ev.path for ev in d.evidence) for d in out.drafts}
        assert pairs == {frozenset({"policies/refunds.md", "policies/hosting.md"}),
                         frozenset({"policies/support.md", "policies/security.md"})}

    async def test_pairs_are_drawn_before_the_single_passages_use_them_up(self, jobs, writer):
        """Found in the browser walk: multi-hop drafted last got only the passages the others left, and no pair."""
        kb = await policies(jobs)
        out = await G.draft(kb, EvalDraftRequest(direct=3, paraphrase=0, multi_hop=1, seed=5), SETTINGS)
        assert [d.kind for d in out.drafts].count("multi_hop") == 1
        assert out.dropped == {"no_passage": 1}  # four passages: the pair takes two, the directs the rest

    async def test_no_model_is_a_sentence(self, jobs, writer):
        kb = await policies(jobs)
        with pytest.raises(ValueError, match="Choose a model"):
            await G.draft(kb, EvalDraftRequest(), {})


class TestEstimate:
    async def test_drafts_are_priced_by_the_rate_card(self, jobs, monkeypatch):
        kb = await policies(jobs)
        monkeypatch.setattr(usage_tracker, "_pricing_snapshot",
                            {"writer-1": {"input_per_1m": 1.0, "output_per_1m": 5.0}})
        est = await G.estimate_drafts(kb, EvalDraftRequest(direct=4, paraphrase=4, multi_hop=2), SETTINGS)
        line = est.lines[0]
        assert (line.part, line.model, line.counts_toward_budget) == ("drafts", "writer-1", False)
        assert line.input_tokens > 0 and line.output_tokens == round((4 + 4 * 1.3 + 2) * G.REPLY_TOKENS)
        assert est.total_est_usd == pytest.approx(line.input_tokens / 1e6 + line.output_tokens * 5 / 1e6)
        assert est.unpriced == []

    async def test_a_model_nothing_prices_is_said_to_be_unpriced(self, jobs, monkeypatch):
        kb = await policies(jobs)
        monkeypatch.setattr(usage_tracker, "_pricing_snapshot", {"writer-1": {"input_per_1m": 1.0}})
        est = await G.estimate_drafts(kb, EvalDraftRequest(model="mystery-7"), SETTINGS)
        assert est.lines[0].est_usd is None and est.unpriced == ["mystery-7"] and est.total_est_usd == 0.0


class TestRoute:
    async def test_drafts_through_the_api_save_nothing(self, jobs, client, writer):
        kb = await policies(jobs)
        resp = await client.post(f"/api/knowledge/bases/{kb.id}/eval/drafts",
                                 json={"direct": 2, "paraphrase": 0, "multi_hop": 0, "seed": 2, "model": "writer-1"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["saved"] is False and len(body["drafts"]) == 2
        assert (await client.post(f"/api/knowledge/bases/{kb.id}/eval/drafts",
                                  json={"direct": 0, "paraphrase": 0, "multi_hop": 0})).status_code == 422
