"""The knowledge base record, its folder rules, and Knowledge's settings (D153 #1, #2, #11, #14)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.knowledge import config as kconfig
from core.knowledge import kb_settings as K
from core.knowledge.errors import KnowledgePathError


def _kb(folder: str = "contracts", **kw) -> K.KnowledgeBase:
    return K.KnowledgeBase(id=kw.pop("id", "kb_00000000000a"), name=kw.pop("name", "Contracts"), folder=folder, **kw)


class TestFolders:
    @pytest.mark.parametrize("raw,clean", [
        ("contracts", "contracts"), ("/contracts/", "contracts"), ("a/./b", "a/b"), ("a\\b", "a/b"),
        ("Zürich", "Zürich"),
    ])
    def test_normalized(self, raw, clean):
        assert K.normalize_folder(raw) == clean

    @pytest.mark.parametrize("bad", ["", "/", ".", "a/../b", "tool_outputs", "x/playwright/y", "compaction_archives"])
    def test_refused(self, bad):
        with pytest.raises(KnowledgePathError):
            K.normalize_folder(bad)

    def test_the_refused_roots_are_the_vaults_engine_folders(self):
        from core.routes.vault import _ENGINE_OWNED
        assert K.REFUSED_ROOTS == frozenset(_ENGINE_OWNED)

    def test_no_two_kbs_overlap(self):
        others = [_kb("contracts/eu", id="kb_00000000000b", name="EU")]
        with pytest.raises(KnowledgePathError, match="overlaps"):
            K.validate_folder("contracts", others)
        with pytest.raises(KnowledgePathError):
            K.validate_folder("contracts/eu/2024", others)
        assert K.validate_folder("contracts2", others) == "contracts2"
        assert K.validate_folder("contracts/eu", others, self_id="kb_00000000000b") == "contracts/eu"

    def test_a_path_belongs_to_the_kb_whose_folder_holds_it(self):
        kbs = [_kb("contracts"), _kb("reports", id="kb_00000000000b"),
               _kb("old", id="kb_00000000000c", status="disabled")]
        assert K.kb_for_path("contracts/acme.pdf", kbs).id == "kb_00000000000a"
        assert K.kb_for_path("contracts/_web/example.com/p.md", kbs).id == "kb_00000000000a"
        assert K.kb_for_path("contracts2/x.pdf", kbs) is None
        assert K.kb_for_path("contracts", kbs) is None  # the folder itself is not a document
        assert K.kb_for_path("old/x.pdf", kbs) is None  # a disabled KB takes no files


class TestRecord:
    def test_defaults_are_the_documented_ones(self):
        kb = _kb()
        assert (kb.chunking.target_tokens, kb.chunking.max_tokens, kb.retrieval.top_k) == (450, 800, 8)
        assert (kb.fusion.rrf_k, kb.fusion.overfetch, kb.rerank.candidates) == (60, 50, 50)
        assert kb.visitor_access is False and kb.enrichment.mode == "prefix" and kb.language == "simple"
        assert kb.rerank.enabled is None  # the deployment decides: cloud on, OSS off

    def test_unknown_fields_and_bad_values_are_refused(self):
        with pytest.raises(ValidationError):
            K.KnowledgeBase(id="kb_1", name="x", folder="a", surprise=1)
        with pytest.raises(ValidationError):
            _kb(language="klingon")
        with pytest.raises(ValidationError):
            _kb(chunking={"target_tokens": 500, "max_tokens": 400})
        with pytest.raises(ValidationError):
            _kb(fusion={"weights": {"lexical": 0, "vector": 0}})

    def test_only_index_shaping_settings_change_the_signature(self):
        kb = _kb()
        base = K.index_signature(kb)
        assert K.index_signature(kb.model_copy(update={"retrieval": K.Retrieval(top_k=20)})) == base
        assert K.index_signature(kb.model_copy(update={"rerank": K.Rerank(enabled=False)})) == base
        assert K.index_signature(kb.model_copy(update={"chunking": K.Chunking(target_tokens=300)})) != base
        assert K.index_signature(kb.model_copy(update={"embedding": K.Embedding(model="oaic.bge-m3")})) != base
        assert K.index_signature(kb.model_copy(update={"language": "german"})) != base


class TestConfig:
    def test_the_engine_defaults_carry_every_key_with_the_same_value(self):
        from core.config import default_settings
        from core.models import Settings
        defaults = default_settings()
        for key, value in kconfig.DEFAULTS.items():
            assert defaults[key] == value, key
            assert Settings.model_fields[key].default == value, key

    def test_off_by_default(self):
        assert kconfig.knowledge_enabled({}) is False
        from core.config import load_settings
        assert kconfig.knowledge_enabled(load_settings()) is False

    def test_the_embedding_model_falls_back_to_the_workspaces(self):
        assert kconfig.knowledge_settings({"embedding_model": "text-embedding-3-small"}).embedding_model == \
            "text-embedding-3-small"
        assert kconfig.knowledge_settings({"embedding_model": "a", "knowledge_embedding_model": "b"}).embedding_model == "b"

