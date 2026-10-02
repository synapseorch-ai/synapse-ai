"""Knowledge's outward shapes: API models and routes, agent tools, events, telemetry, ids, vectors, the Vault hook."""
from __future__ import annotations

import re

import pytest
from pydantic import BaseModel, ValidationError

from core import vault_events
from core.knowledge import agent_contract as A
from core.knowledge import api_models as M
from core.knowledge import events as E
from core.knowledge import ids, telemetry, vectors


class TestApiModels:
    def _request_models(self):
        return [v for v in vars(M).values() if isinstance(v, type) and issubclass(v, M._Request) and v is not M._Request]

    def test_every_request_refuses_unknown_fields(self):
        for model in self._request_models():
            assert model.model_config.get("extra") == "forbid", model.__name__

    def test_no_model_exposes_an_embedding(self):
        """No response carries a vector (embeddings can be inverted to text, ZSinvert).

        `KnowledgeBase.embedding` is the *model choice*, an object — allowed. An
        array-valued field with an embedding's name is not."""
        from core.knowledge.testing.snapshot import api_schema

        names = {"embedding", "embeddings", "embedding_vec", "query_embedding", "query_vector", "vector", "vectors"}
        found = []

        def walk(node, where):
            if isinstance(node, dict):
                for key, prop in (node.get("properties") or {}).items():
                    if key in names and isinstance(prop, dict) and (
                            prop.get("type") == "array" or prop.get("format") in ("binary", "base64")):
                        found.append(f"{where}.{key}")
                for key, value in node.items():
                    walk(value, f"{where}.{key}")
            elif isinstance(node, list):
                for i, value in enumerate(node):
                    walk(value, f"{where}[{i}]")

        walk(api_schema(), "api")
        assert found == []

    def test_routes_are_unique_and_sane(self):
        keys = [(r.method, r.path) for r in M.ROUTES]
        assert len(keys) == len(set(keys))
        for r in M.ROUTES:
            assert r.path.startswith("/knowledge/"), r.path
            assert r.method in ("GET", "POST", "PATCH", "PUT", "DELETE")
            assert r.surfaces <= {"internal", "v1"} and r.surfaces
            assert r.bucket in (None, "search", "starts")
            if r.method == "GET":
                assert r.scope == "read" and r.bucket != "starts"
            if r.bucket == "starts":
                assert r.scope == "run" and r.status in (201, 202)

    def test_search_is_synchronous_and_ingest_is_accepted(self):
        routes = {(r.method, r.path): r for r in M.ROUTES}
        assert routes[("POST", "/knowledge/search")].status == 200
        assert routes[("POST", "/knowledge/search")].bucket == "search"
        for path in ("/knowledge/bases/{kb_id}/records", "/knowledge/bases/{kb_id}/urls",
                     "/knowledge/uploads/{upload_id}/complete"):
            assert routes[("POST", path)].status == 202

    def test_a_record_is_text_or_json_never_both(self):
        M.RecordIn(text="hello")
        M.RecordIn.model_validate({"json": {"a": 1}})
        with pytest.raises(ValidationError):
            M.RecordIn(text="a", json={"b": 1})
        with pytest.raises(ValidationError):
            M.RecordIn()
        with pytest.raises(ValidationError):
            M.RecordIn(text="x", id="../escape")
        with pytest.raises(ValidationError):
            M.RecordIn(text="x" * (M.MAX_RECORD_BYTES + 1))

    def test_a_bad_filter_is_a_typed_422(self):
        with pytest.raises(ValidationError) as caught:
            M.SearchRequest(query="q", kb_ids=["kb_1"], filters={"year": {"$gt": True}})
        error = caught.value.errors()[0]
        assert error["type"] == "filter.type_mismatch" and error["loc"] == ("filters",)
        assert "/year/$gt" in error["msg"]

    def test_bad_metadata_is_refused_at_the_door(self):
        with pytest.raises(ValidationError) as caught:
            M.UploadInit(filename="a.pdf", size_bytes=1, mime="application/pdf", metadata={"path": "x"})
        assert caught.value.errors()[0]["type"] == "filter.bad_field"

    def test_urls_are_http(self):
        with pytest.raises(ValidationError):
            M.UrlIngestRequest(urls=["file:///etc/passwd"])
        M.UrlIngestRequest(urls=["https://example.com/"])

    def test_a_bases_crawls_are_internal_and_read_only(self):
        """CCR-4 (S1): the screen's view of each web source; the public API decides later (P1)."""
        routes = {(r.method, r.path): r for r in M.ROUTES}
        crawls = routes[("GET", "/knowledge/bases/{kb_id}/crawls")]
        assert crawls.surfaces == frozenset({"internal"}) and crawls.scope == "read" and crawls.status == 200
        state = M.WebCrawlState(source_id="kws_000000000000", url="https://example.com/", kind="site")
        assert state.status == "idle" and state.counts.written == 0

    def test_evaluation_is_internal_and_follows_the_route_rules(self):
        """CCR-6 (V1): a knowledge base's eval. Internal until P1 decides v1; reads are GETs, a run is a start."""
        evals = [r for r in M.ROUTES if "/eval/" in r.path]
        assert len(evals) == 14
        assert all(r.surfaces == frozenset({"internal"}) and r.path.startswith("/knowledge/bases/{kb_id}/eval/")
                   for r in evals)
        routes = {(r.method, r.path): r for r in evals}
        start = routes[("POST", "/knowledge/bases/{kb_id}/eval/runs")]
        assert (start.status, start.bucket, start.scope, start.request) == (202, "starts", "run", M.EvalRunRequest)
        drafts = routes[("POST", "/knowledge/bases/{kb_id}/eval/drafts")]
        assert (drafts.status, drafts.bucket, drafts.scope) == (200, None, "run")  # paid, and saves nothing
        gone = routes[("DELETE", "/knowledge/bases/{kb_id}/eval/sets/{set_id}")]
        assert (gone.status, gone.response) == (204, None)
        assert routes[("POST", "/knowledge/bases/{kb_id}/eval/runs/{run_id}/cancel")].request is None

    def test_an_eval_request_says_what_it_prices_and_drafts(self):
        with pytest.raises(ValidationError):
            M.EvalDraftRequest(direct=0, paraphrase=0, multi_hop=0)
        with pytest.raises(ValidationError):
            M.EvalDraftRequest(direct=6, paraphrase=6, multi_hop=0)
        with pytest.raises(ValidationError):
            M.EvalEstimateRequest()
        with pytest.raises(ValidationError):
            M.EvalEstimateRequest(drafts=M.EvalDraftRequest(), run=M.EvalRunRequest(set_id="kset_1"))
        with pytest.raises(ValidationError):
            M.EvalCompareRequest(weights={"lexical": 0, "vector": 0})
        with pytest.raises(ValidationError):
            M.EvalCompareRequest(weights={"keyword": 1})
        with pytest.raises(ValidationError):
            M.EvalCase(kind="direct", question="Why?", evidence=[])
        assert M.EvalDrafts(model="m").saved is False
        assert M.EvalRunRequest(set_id="kset_1").rerank is None  # as the knowledge base serves

    def test_the_retrieve_step_validates_its_filter_too(self):
        with pytest.raises(ValidationError):
            M.RetrieveStepConfig(kb_ids=["kb_1"], query="{{q}}", filters={"$nope": 1})
        assert M.STEP_TYPES == ("retrieve", "ingest")

    def test_a_citation_snippet_is_short(self):
        with pytest.raises(ValidationError):
            M.Citation(label="[K1]", kb_id="k", doc_id="d", chunk_id="c", path="p", source="upload", char_start=0,
                       char_end=1, snapshot_id="s", snippet="x" * 201)


class TestAgentContract:
    @pytest.mark.parametrize("schema", [A.SEARCH_SCHEMA, A.READ_SCHEMA])
    def test_schemas_are_flat(self, schema):
        """No unions, no nested objects: the shape that broke Gemini on 2026-09-01."""
        assert schema["type"] == "object"
        for prop in schema["properties"].values():
            assert isinstance(prop["type"], str) and prop["type"] in ("string", "integer", "boolean")
            assert not {"anyOf", "oneOf", "allOf", "properties", "items"} & set(prop)

    def test_labels(self):
        assert A.format_label(3) == "[K3]"
        assert [A.parse_label(x) for x in ("[K3]", "K3", "k12", "K", "[X1]")] == [3, 3, 12, None, None]
        assert A.labels_in("Per [K2] and [K1], see [K2] again.") == [2, 1]
        with pytest.raises(ValueError):
            A.format_label(0)

    def test_the_tools_are_not_visitor_engine_tools(self):
        """Knowledge reaches data, so a visitor gets it only when the KB and the widget both allow it."""
        from core.visitor import ENGINE_TOOLS
        assert not (A.KNOWLEDGE_TOOL_NAMES & ENGINE_TOOLS)


class _Captured:
    """An OrchestrationLogger that keeps its lines instead of writing a file."""

    def __new__(cls):
        from core.orchestration.logger import OrchestrationLogger

        class Capture(OrchestrationLogger):
            def __init__(self):
                self.lines = []
                super().__init__(run_id="run_kb0", orchestration_id="o", orchestration_name="o", user_input="x")
                self.lines.clear()

            def _write(self, text):
                self.lines.append(text)

        return Capture()


_EXAMPLES = {
    E.KNOWLEDGE_SEARCH: {"type": E.KNOWLEDGE_SEARCH, "turn": 1, "query_id": "kq_1", "kb_ids": ["kb_1"],
                         "query": "termination", "hit_count": 2, "labels": ["[K1]", "[K2]"], "cost_usd": 0.002},
    E.KNOWLEDGE_READ: {"type": E.KNOWLEDGE_READ, "turn": 2, "label": "[K1]", "chunk_id": "kch_1", "doc_id": "kdoc_1",
                       "path": "contracts/a.pdf", "window": 1},
    E.KNOWLEDGE_CITATIONS: {"type": E.KNOWLEDGE_CITATIONS, "citations": [{"label": "[K1]", "path": "contracts/a.pdf"}],
                            "unresolved": []},
    E.KNOWLEDGE_RETRIEVE: {"type": E.KNOWLEDGE_RETRIEVE, "orch_step_id": "s1", "step_name": "Retrieve",
                           "query_id": "kq_1", "hit_count": 3, "cost_usd": 0.001},
    E.KNOWLEDGE_INGEST: {"type": E.KNOWLEDGE_INGEST, "orch_step_id": "s2", "step_name": "Ingest", "kb_id": "kb_1",
                         "documents": [{"doc_id": "kdoc_1", "status": "queued"}]},
}


#: The lanes whose run-log branches have landed; each removes its types' mark in the same commit.
_LANDED = {"B1", "R1"}


class TestEvents:
    @pytest.mark.parametrize("event_type", [
        t if E.RUN_EVENT_OWNERS[t] in _LANDED else pytest.param(t, marks=pytest.mark.xfail(
            strict=True, reason=f"{E.RUN_EVENT_OWNERS[t]} adds the run-log branch and lands its lane in _LANDED"))
        for t in E.RUN_EVENT_TYPES])
    def test_the_run_log_writes_each_knowledge_event(self, event_type):
        logger = _Captured()
        logger.log_event(_EXAMPLES[event_type])
        assert logger.lines, f"{event_type} ({E.RUN_EVENT_OWNERS[event_type]}) is dropped by the run log"

    def test_every_run_event_has_an_owner_and_an_example(self):
        assert set(E.RUN_EVENT_OWNERS) == set(E.RUN_EVENT_TYPES) == set(_EXAMPLES)

    def test_webhooks_name_lifecycle_events(self):
        assert set(E.WEBHOOK_EVENTS.values()) <= set(E.LIFECYCLE_TYPES)

    def test_a_webhook_is_named_by_its_lifecycle_type(self):
        """CCR-9 (P1a, D176): cloud's webhook endpoints are generic, so Knowledge's events carry
        their family — a customer subscribes to `knowledge.document.indexed`, never a bare
        `document.indexed` another family could also mean."""
        assert E.WEBHOOK_EVENTS == {
            "knowledge.document.indexed": "knowledge.document.indexed",
            "knowledge.document.partial": "knowledge.document.partial",
            "knowledge.document.failed": "knowledge.document.failed",
            "knowledge.base.reindexed": "knowledge.base.reindexed",
        }

    async def test_emit_reaches_observers_and_never_raises(self):
        seen = []

        async def good(ev):
            seen.append(ev.type)

        def bad(ev):
            raise RuntimeError("observer broke")

        E.knowledge_observers.extend([bad, good])
        event = E.KnowledgeLifecycleEvent(id="kev_1", type="knowledge.document.indexed", created_at="now",
                                          kb_id="kb_1", doc_id="kdoc_1", status="indexed")
        await E.emit(event)
        await E.emit(event.model_copy(update={"type": "knowledge.document.exploded"}))
        assert seen == ["knowledge.document.indexed"]


class TestTelemetry:
    def test_every_knowledge_attribute_is_allowed_for_export(self):
        from core.telemetry.attributes import ALLOWED_ATTRIBUTES
        names = [v for k, v in vars(telemetry).items() if k.startswith("KNOWLEDGE_") and isinstance(v, str)]
        assert len(names) == 21
        assert set(names) <= ALLOWED_ATTRIBUTES

    def test_span_names(self):
        assert all(s.startswith("synapse.knowledge.") for s in telemetry.SPANS)
        assert len(set(telemetry.SPANS)) == len(telemetry.SPANS)


class TestIdsAndVectors:
    def test_formats(self):
        assert ids.is_id(ids.new_kb_id(), ids.KB_PREFIX)
        for prefix in (ids.DOCUMENT_PREFIX, ids.SNAPSHOT_PREFIX, ids.QUERY_PREFIX, ids.UPLOAD_PREFIX):
            value = ids.new_id(prefix)
            assert ids.is_id(value, prefix) and len(value) == len(prefix) + 32
        with pytest.raises(ValueError):
            ids.new_id(ids.CHUNK_PREFIX)

    def test_a_chunk_id_is_deterministic_and_separated(self):
        a = ids.chunk_id("kdoc_a", "ksnap_1", 2, 0)
        assert a == ids.chunk_id("kdoc_a", "ksnap_1", 2, 0)
        assert a != ids.chunk_id("kdoc_a", "ksnap_1", 3, 0)
        assert ids.chunk_id("ab", "c", 1, 0) != ids.chunk_id("a", "bc", 1, 0)
        assert re.fullmatch(r"kch_[0-9a-f]{32}", a)

    @pytest.mark.parametrize("value,ok", [("ticket-4411", True), ("a.b_c", True), ("../x", False), ("", False),
                                          ("-lead", False), ("x" * 129, False)])
    def test_record_ids(self, value, ok):
        assert ids.is_record_id(value) is ok

    def test_vectors_are_float32_little_endian(self):
        blob = vectors.pack([1.0, -2.5, 0.125])
        assert blob == b"\x00\x00\x80?\x00\x00 \xc0\x00\x00\x00>"
        assert vectors.unpack(blob) == [1.0, -2.5, 0.125] and vectors.dims_of(blob) == 3
        with pytest.raises(ValueError):
            vectors.unpack(b"\x00\x00\x00")


class TestVaultHook:
    async def test_no_observer_is_fine(self):
        await vault_events.notify_vault_change("a.md", "write")

    async def test_the_observer_hears_every_change_and_never_breaks_a_write(self):
        seen = []

        async def observer(change):
            seen.append(change)
            if change.op == "delete":
                raise RuntimeError("indexer down")

        vault_events.set_vault_observer(observer)
        await vault_events.notify_vault_change("kb/a.md", "write", sha256="x", size=3, source="upload")
        await vault_events.notify_vault_change("kb/a.md", "delete")
        await vault_events.notify_vault_change("kb/b.md", "move", old_path="kb/a.md")
        assert [(c.op, c.path, c.old_path) for c in seen] == [
            ("write", "kb/a.md", None), ("delete", "kb/a.md", None), ("move", "kb/b.md", "kb/a.md")]
        assert seen[0].source == "upload" and seen[0].sha256 == "x"
        vault_events.set_vault_observer(None)
        assert vault_events.get_vault_observer() is None


def test_the_api_models_are_pydantic():
    assert issubclass(M.SearchResponse, BaseModel)
