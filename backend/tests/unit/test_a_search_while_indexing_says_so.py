"""A search that finds nothing while its knowledge base is still indexing says so, never "no passages matched" (F1630).

A workflow made a knowledge base from a Vault file and searched it seconds later, before the file was indexed: the
Retrieve step said "No passages matched … in Policies", and the next step told the person the policy did not say. Now,
when a search finds nothing and a searched base still has documents queued, parsing or indexing, the answer says the
base is still indexing, how many documents, and to run it again shortly; with every document at rest, nothing found is
said as before.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.knowledge import workflow_steps


def _kb(kb_id="kb_policies", name="Policies"):
    return SimpleNamespace(id=kb_id, name=name, retrieval=SimpleNamespace(max_context_tokens=4000))


@pytest.fixture
def searched(monkeypatch):
    """A search that finds nothing, in a base whose documents are in the statuses the test sets."""
    from core.knowledge import bases, documents, search

    statuses: list[str] = []

    async def no_hits(request, **kw):
        return SimpleNamespace(hits=[], query_id="kq_test", trace=None, degraded=[],
                               cost=SimpleNamespace(total_est_usd=0.0))

    async def list_bases(*a, **k):
        return [_kb()]

    async def list_for(kb_id, **k):
        return [SimpleNamespace(id=f"doc_{i}", kb_id=kb_id, status=s) for i, s in enumerate(statuses)]

    monkeypatch.setattr(search, "search", no_hits)
    monkeypatch.setattr(bases, "list_bases", list_bases)
    monkeypatch.setattr(documents, "list_for", list_for)
    monkeypatch.setattr(workflow_steps, "check_enabled", lambda *a, **k: None)
    return statuses


async def _retrieve():
    config = workflow_steps.retrieve_config({"kb_ids": ["kb_policies"], "query": "{{ state.user_input }}"},
                                            step_name="Search Policies")
    return await workflow_steps.retrieve(config, "How many days do customers have to return an item?",
                                         origin="workflow", step_id="search", step_name="Search Policies")


class TestStillIndexingIsSaid:
    async def test_a_document_still_indexing(self, searched):
        searched.append("indexing")
        got = await _retrieve()
        assert "still indexing" in got.output.context and "Policies" in got.output.context, got.output.context
        assert "No passages matched" not in got.output.context
        assert any("indexing" in w for w in got.warnings), got.warnings

    async def test_documents_queued_and_parsing_are_counted(self, searched):
        searched.extend(["queued", "parsing", "indexed"])
        got = await _retrieve()
        assert "2 documents" in got.output.context, got.output.context


class TestAtRestNothingFoundIsSaidAsBefore:
    @pytest.mark.parametrize("status", ["indexed", "failed", "partial"])
    async def test_every_document_at_rest(self, searched, status):
        searched.append(status)
        got = await _retrieve()
        assert "No passages matched" in got.output.context and "still indexing" not in got.output.context


class TestTheAgentsSearchSaysTheSame:
    async def test_the_shared_words(self, searched):
        from core.knowledge.workflow_steps import indexing_said

        searched.append("indexing")
        said = await indexing_said([_kb()])
        assert said and "still indexing" in said and "1 document" in said, said
