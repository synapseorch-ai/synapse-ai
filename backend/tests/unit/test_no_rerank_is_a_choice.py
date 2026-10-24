"""A workspace can choose not to rerank (QA1e's F1020, GUIDE1, D318).

Cloud reads an empty `knowledge_rerank_model` as "the platform's reranker while it answers" (BF9), so once a person
had picked a reranker there was no value meaning "stop", and no way to stop reranking or paying for it. `RERANK_OFF`
is that value: the engine reads it as no default reranker, the same as OSS's empty one. A base that names its own
reranker still uses it.
"""
from __future__ import annotations

from core.knowledge.config import RERANK_OFF, knowledge_settings
from core.knowledge.rerankers import from_settings


def test_off_is_no_default_reranker():
    assert knowledge_settings({"knowledge_rerank_model": RERANK_OFF}).rerank_model == ""
    assert from_settings({"knowledge_rerank_model": RERANK_OFF}) is None


def test_empty_and_a_model_read_as_before():
    assert knowledge_settings({"knowledge_rerank_model": ""}).rerank_model == ""
    assert from_settings({"knowledge_rerank_model": ""}) is None
    assert knowledge_settings({"knowledge_rerank_model": "cohere.rerank-v3.5"}).rerank_model == "cohere.rerank-v3.5"
    assert from_settings({"knowledge_rerank_model": "cohere.rerank-v3.5"}) is not None


def test_off_is_not_a_model_id():
    # A rerank model id always names its provider (`cohere.…`, `synapse.…`): "none" can never collide with one.
    assert "." not in RERANK_OFF
