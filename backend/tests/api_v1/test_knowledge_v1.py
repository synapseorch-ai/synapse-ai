"""Knowledge on the public API (P1a, D176): `/api/v1/knowledge/*` is the frozen route table's v1 rows.

Behind an API key and Knowledge's own switch (off answers 404, as if the routes did not exist), with the
internal routes' handlers, bodies and status codes; a search is traced as the API's, naming the key; an
upload's parts are PUT to their URLs with their own headers and no key (CCR-8), as cloud's presigned
parts are. SQLite always; Postgres with `SYNAPSE_TEST_POSTGRES_URL`.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from _fakes.knowledge_corpus import CONTRACTS, corpus
from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from core.knowledge import dispatch, seams
from core.knowledge.api_models import ROUTES
from core.knowledge.testing.fakes import RecordingDispatcher
from core.storage import get_blob_store
from core.store import session
from core.store.models import KnowledgeQueryDB

pytestmark = pytest.mark.usefixtures("knowledge_store")

V1 = "/api/v1"
PART_PUT = ("PUT", "/knowledge/uploads/{upload_id}/parts/{part_number}")


@pytest.fixture(autouse=True)
def _fresh():
    dispatch.reset()
    yield
    dispatch.reset()


@pytest.fixture
async def on(client):
    assert (await client.post("/api/settings", json={"knowledge_enabled": True})).status_code == 200
    return client


@pytest.fixture
def jobs():
    recorder = RecordingDispatcher()
    seams.set_ingest_dispatcher(recorder)
    return recorder


def _v1_routes(test_app):
    """The routes `core/routes/api_v1_knowledge.py` declares, each mounted on the app (the schema shows
    it: on FastAPI ≥ 0.139 `app.routes` holds routers, not routes — F166)."""
    from fastapi.routing import APIRoute

    from core.routes.api_v1_knowledge import router

    routes = [r for r in router.routes if isinstance(r, APIRoute)]
    paths = test_app.openapi()["paths"]
    for route in routes:
        assert {m.lower() for m in route.methods} <= set(paths.get(route.path, {})), route.path
    assert routes and all(r.path.startswith(V1 + "/knowledge/") for r in routes)
    return routes


def _filled(path: str) -> str:
    return (path.replace("{kb_id}", "kb_000000000000").replace("{doc_id}", "kdoc_x").replace("{query_id}", "kq_x")
            .replace("{upload_id}", "kup_x").replace("{part_number}", "1").replace("{record_id}", "r1"))


async def _kb(client, headers, folder="kb", **kw):
    resp = await client.post(f"{V1}/knowledge/bases", json={"name": "Contracts", "folder": folder, **kw},
                             headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestTheSurface:
    def test_the_mounted_routes_are_the_tables_v1_rows(self, test_app):
        specs = {(r.method, r.path): r for r in ROUTES if "v1" in r.surfaces}
        mounted = {}
        for route in _v1_routes(test_app):
            for method in route.methods:
                mounted[(method, route.path[len(V1):])] = route
        assert set(mounted) == set(specs)
        for key, route in mounted.items():
            spec = specs[key]
            assert (route.status_code or 200) == spec.status, key
            if spec.response is not None:
                assert str(route.response_model) == str(spec.response) or route.response_model is spec.response, key
            bodies = [p.field_info.annotation for p in route.dependant.body_params]
            assert bodies == ([spec.request] if spec.request else []), key

    def test_what_is_internal_stays_off_v1(self, test_app):
        paths = test_app.openapi()["paths"]
        internal = [r for r in ROUTES if "v1" not in r.surfaces]
        assert {r.path for r in internal} >= {"/knowledge/bases/{kb_id}/crawls", "/knowledge/queries/{query_id}/replay"}
        for r in internal:
            assert r.method.lower() not in paths.get(V1 + r.path, {}), (r.method, r.path)

    def test_every_route_but_the_part_put_takes_a_key(self, test_app):
        from core.api_key_middleware import require_api_key

        for route in _v1_routes(test_app):
            calls = {d.call for d in route.dependant.dependencies}
            wanted = (route.methods != {"PUT"})
            assert (require_api_key in calls) == wanted, route.path

    async def test_off_every_route_is_a_404_even_with_a_key(self, client, test_app, api_key):
        for route in _v1_routes(test_app):
            for method in route.methods:
                resp = await client.request(method, _filled(route.path), json={}, headers=api_key["headers"])
                assert resp.status_code == 404, (method, route.path, resp.text)

    async def test_on_a_route_without_a_key_is_refused(self, on, test_app):
        for route in _v1_routes(test_app):
            for method in route.methods:
                resp = await on.request(method, _filled(route.path), json={})
                expected = 404 if (method, route.path[len(V1):]) == PART_PUT else 401
                assert resp.status_code == expected, (method, route.path, resp.text)

    async def test_a_revoked_key_is_refused(self, on):
        resp = await on.get(f"{V1}/knowledge/bases", headers={"Authorization": "Bearer sk-syn-not-a-real-key"})
        assert resp.status_code == 401


class TestBasesRecordsDocuments:
    async def test_a_base_its_records_and_its_documents(self, on, jobs, api_key):
        h = api_key["headers"]
        kb = await _kb(on, h, description="Signed agreements")
        listed = (await on.get(f"{V1}/knowledge/bases", headers=h)).json()
        assert [b["id"] for b in listed] == [kb["id"]] and "build" in listed[0]["state"]
        assert (await on.get(f"{V1}/knowledge/bases/{kb['id']}", headers=h)).json()["description"] == "Signed agreements"
        patched = await on.patch(f"{V1}/knowledge/bases/{kb['id']}", headers=h,
                                 json={"revision": kb["revision"], "description": "Agreements"})
        assert patched.status_code == 200 and patched.json()["description"] == "Agreements"
        stale = await on.patch(f"{V1}/knowledge/bases/{kb['id']}", headers=h,
                               json={"revision": kb["revision"], "description": "Again"})
        assert stale.status_code == 409

        sent = await on.post(f"{V1}/knowledge/bases/{kb['id']}/records", headers=h, json={"records": [
            {"id": "t-1", "text": "The refund window is 30 days.", "metadata": {"team": "support"}}]})
        assert sent.status_code == 202, sent.text
        (doc,) = sent.json()["documents"]
        assert (doc["path"], doc["status"], doc["metadata"]) == ("kb/_records/t-1.json", "queued", {"team": "support"})

        docs = await on.get(f"{V1}/knowledge/bases/{kb['id']}/documents", params={"status": "queued", "limit": 5},
                            headers=h)
        assert [d["id"] for d in docs.json()] == [doc["id"]]
        detail = await on.get(f"{V1}/knowledge/documents/{doc['id']}", headers=h)
        assert detail.status_code == 200 and detail.json()["id"] == doc["id"]

        preview = await on.post(f"{V1}/knowledge/bases/{kb['id']}/cost-preview", json={}, headers=h)
        assert preview.status_code == 200, preview.text
        costs = await on.get(f"{V1}/knowledge/bases/{kb['id']}/costs", params={"group_by": "stage"}, headers=h)
        assert costs.status_code == 200 and isinstance(costs.json(), list)
        bad = await on.get(f"{V1}/knowledge/bases/{kb['id']}/costs", params={"since": "yesterday"}, headers=h)
        assert bad.status_code == 422

        gone = await on.delete(f"{V1}/knowledge/bases/{kb['id']}/records/t-1", headers=h)
        assert gone.status_code == 202 and gone.json() == {"status": "deleting"}
        assert (await on.delete(f"{V1}/knowledge/bases/{kb['id']}", headers=h)).status_code == 202

    async def test_an_unknown_base_or_document_is_a_404(self, on, api_key):
        h = api_key["headers"]
        assert (await on.get(f"{V1}/knowledge/bases/kb_000000000000", headers=h)).status_code == 404
        assert (await on.get(f"{V1}/knowledge/documents/kdoc_x", headers=h)).status_code == 404
        assert (await on.post(f"{V1}/knowledge/documents/kdoc_x/retry", headers=h)).status_code == 404


class TestSearch:
    async def test_a_search_is_traced_as_the_api_naming_the_key(self, on, api_key):
        await corpus()
        h = api_key["headers"]
        resp = await on.post(f"{V1}/knowledge/search", headers=h, json={
            "query": "hosting monthly fee", "kb_ids": [CONTRACTS.id], "include_trace": True})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["hits"][0]["label"] == "[K1]" and body["hits"][0]["citation"]["path"] == "contracts/acme.pdf"
        assert body["trace"]["origin"] == {**body["trace"]["origin"], "surface": "api",
                                           "api_key_id": api_key["record"]["id"]}
        async with session() as s:
            row = (await s.execute(select(KnowledgeQueryDB).where(KnowledgeQueryDB.id == body["query_id"]))).scalar_one()
        assert (row.surface, row.api_key_id) == ("api", api_key["record"]["id"])
        trace = await on.get(f"{V1}/knowledge/queries/{body['query_id']}", headers=h)
        assert trace.status_code == 200 and trace.json()["query_id"] == body["query_id"]

    async def test_a_bad_filter_or_an_unknown_base(self, on, api_key):
        await corpus()
        h = api_key["headers"]
        bad = await on.post(f"{V1}/knowledge/search", headers=h,
                            json={"query": "fees", "kb_ids": [CONTRACTS.id], "filters": {"$where": "1"}})
        assert bad.status_code == 422 and bad.json()["detail"][0]["type"].startswith("filter.")
        unknown = await on.post(f"{V1}/knowledge/search", headers=h,
                                json={"query": "fees", "kb_ids": ["kb_ffffffffffff"]})
        assert unknown.status_code == 404 and "kb_ffffffffffff" in unknown.json()["detail"]


async def _put_parts(client, session, data: bytes) -> list[dict]:
    """PUT each part exactly as a client of either product does: its URL, its headers, nothing else."""
    parts, size = [], session["part_size"]
    for part in session["parts"]:
        n = part["part_number"]
        assert part["method"] == "PUT"
        put = await client.put(part["url"], content=data[(n - 1) * size:n * size], headers=part["headers"])
        assert put.status_code == 200, put.text
        parts.append({"part_number": n, "etag": put.headers["ETag"]})
    return parts


class TestUploads:
    async def test_a_file_uploaded_over_v1_lands_whole(self, on, jobs, api_key, monkeypatch):
        from core.knowledge import ingest

        monkeypatch.setattr(ingest, "PART_SIZE", 10)
        h = api_key["headers"]
        kb = await _kb(on, h)
        data = "# Notes\n\nA file long enough for several parts.".encode()
        init = await on.post(f"{V1}/knowledge/bases/{kb['id']}/uploads", headers=h, json={
            "filename": "notes.md", "size_bytes": len(data), "mime": "text/markdown", "metadata": {"year": 2026}})
        assert init.status_code == 201, init.text
        session = init.json()
        assert len(session["parts"]) == 5
        first = session["parts"][0]
        assert first["url"] == f"{V1}/knowledge/uploads/{session['upload_id']}/parts/1"
        assert set(first["headers"]) == {"X-Synapse-Upload-Token"} and "Authorization" not in first["headers"]
        assert len({tuple(p["headers"].items()) for p in session["parts"]}) == 1  # one token per session

        parts = await _put_parts(on, session, data)
        done = await on.post(f"{V1}/knowledge/uploads/{session['upload_id']}/complete", headers=h,
                             json={"parts": parts})
        assert done.status_code == 202, done.text
        (doc,) = done.json()["documents"]
        assert (doc["path"], doc["source"], doc["status"]) == ("kb/notes.md", "upload", "queued")
        assert doc["sha256"] == hashlib.sha256(data).hexdigest()
        assert get_blob_store().get_bytes("vault/kb/notes.md") == data

    async def test_a_part_without_its_token_is_a_404(self, on, jobs, api_key):
        h = api_key["headers"]
        kb = await _kb(on, h)
        session = (await on.post(f"{V1}/knowledge/bases/{kb['id']}/uploads", headers=h, json={
            "filename": "a.md", "size_bytes": 2, "mime": "text/markdown"})).json()
        url = session["parts"][0]["url"]
        (name, token), = session["parts"][0]["headers"].items()
        for headers in ({}, {name: ""}, {name: token[:-1] + ("A" if token[-1] != "A" else "B")},
                        {**h}, {name: "é".encode("latin-1") * 8}):  # a non-ASCII token, as raw bytes
            resp = await on.put(url, content=b"hi", headers=headers)
            assert resp.status_code == 404, (headers, resp.text)
        assert get_blob_store().list(f"knowledge/uploads/{session['upload_id']}/") == [
            f"knowledge/uploads/{session['upload_id']}/manifest.json"]
        assert (await on.put(url, content=b"hi", headers={name: token})).status_code == 200

    async def test_a_session_opened_in_the_app_is_not_v1s(self, on, jobs, api_key):
        """The app's own sessions carry no token, so their parts are never taken on v1."""
        kb = await _kb(on, api_key["headers"])
        session = (await on.post(f"/api/knowledge/bases/{kb['id']}/uploads", json={
            "filename": "a.md", "size_bytes": 2, "mime": "text/markdown"})).json()
        assert session["parts"][0]["headers"] == {}
        assert session["parts"][0]["url"] == f"/api/knowledge/uploads/{session['upload_id']}/parts/1"
        resp = await on.put(f"{V1}/knowledge/uploads/{session['upload_id']}/parts/1", content=b"hi",
                            headers={"X-Synapse-Upload-Token": "anything"})
        assert resp.status_code == 404

    async def test_an_expired_session_is_a_404(self, on, jobs, api_key):
        h = api_key["headers"]
        kb = await _kb(on, h)
        session = (await on.post(f"{V1}/knowledge/bases/{kb['id']}/uploads", headers=h, json={
            "filename": "a.md", "size_bytes": 2, "mime": "text/markdown"})).json()
        key = f"knowledge/uploads/{session['upload_id']}/manifest.json"
        manifest = json.loads(get_blob_store().get(key))
        manifest["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        get_blob_store().put(key, json.dumps(manifest))
        part = session["parts"][0]
        assert (await on.put(part["url"], content=b"hi", headers=part["headers"])).status_code == 404

    async def test_a_disabled_base_takes_no_upload(self, on, jobs, api_key):
        h = api_key["headers"]
        kb = await _kb(on, h)
        await on.patch(f"{V1}/knowledge/bases/{kb['id']}", headers=h, json={"revision": kb["revision"],
                                                                             "status": "disabled"})
        resp = await on.post(f"{V1}/knowledge/bases/{kb['id']}/uploads", headers=h, json={
            "filename": "a.md", "size_bytes": 2, "mime": "text/markdown"})
        assert resp.status_code == 409
