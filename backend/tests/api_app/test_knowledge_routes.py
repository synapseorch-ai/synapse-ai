"""Knowledge's internal routes (D157): off means 404, the mounted routes are the frozen table's,
and bases, uploads, records and documents behave as the contract says.
SQLite always; Postgres with `SYNAPSE_TEST_POSTGRES_URL`."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from _fakes.knowledge_env import FakeIndexer, install_fake_indexing
from core.knowledge import dispatch, documents, seams
from core.knowledge.api_models import ROUTES
from core.knowledge.testing.fakes import RecordingDispatcher
from core.storage import get_blob_store

pytestmark = pytest.mark.usefixtures("knowledge_store")

#: Internal routes other lanes mount; each lane removes its own as it lands.
NOT_A1S = {
    ("POST", "/knowledge/bases/{kb_id}/cost-preview"): "B2",
    ("GET", "/knowledge/bases/{kb_id}/costs"): "B2",
    ("POST", "/knowledge/queries/{query_id}/replay"): "V1/P1",
}


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


async def _kb(client, folder="kb", **kw):
    resp = await client.post("/api/knowledge/bases", json={"name": "Contracts", "folder": folder, **kw})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _upload(client, kb_id, name, data: bytes, *, mime="text/markdown", **kw):
    init = await client.post(f"/api/knowledge/bases/{kb_id}/uploads",
                             json={"filename": name, "size_bytes": len(data), "mime": mime, **kw})
    assert init.status_code == 201, init.text
    session = init.json()
    parts = []
    size = session["part_size"]
    for part in session["parts"]:
        n = part["part_number"]
        put = await client.put(part["url"], content=data[(n - 1) * size:n * size])
        assert put.status_code == 200, put.text
        parts.append({"part_number": n, "etag": put.headers["ETag"]})
    return session, parts


def _knowledge_routes(test_app):
    """The routes `core/routes/knowledge.py` declares — every one mounted on the app, which the schema
    shows (on FastAPI ≥ 0.139 `app.routes` holds routers, not routes: F166)."""
    from fastapi.routing import APIRoute

    from core.routes.knowledge import router

    routes = [r for r in router.routes if isinstance(r, APIRoute)]
    paths = test_app.openapi()["paths"]
    for route in routes:
        assert {m.lower() for m in route.methods} <= set(paths.get(route.path, {})), route.path
    assert routes and all(r.path.startswith("/api/knowledge/") for r in routes)
    return routes


class TestTheSurface:
    def test_the_mounted_routes_are_the_frozen_tables(self, test_app):
        specs = {(r.method, r.path): r for r in ROUTES if "internal" in r.surfaces}
        mounted = {}
        for route in _knowledge_routes(test_app):
            for method in route.methods:
                mounted[(method, route.path[len("/api"):])] = route
        assert set(mounted) <= set(specs)
        for key, route in mounted.items():
            spec = specs[key]
            assert (route.status_code or 200) == spec.status, key  # unset means FastAPI's 200
            if spec.response is not None:
                assert str(route.response_model) == str(spec.response) or route.response_model is spec.response, key
            bodies = [p.field_info.annotation for p in route.dependant.body_params]
            assert bodies == ([spec.request] if spec.request else []), key
        assert set(specs) - set(mounted) == set(NOT_A1S)

    async def test_off_every_route_is_a_404(self, client, test_app):
        for route in _knowledge_routes(test_app):
            path = (route.path.replace("{kb_id}", "kb_000000000000").replace("{doc_id}", "kdoc_x")
                    .replace("{upload_id}", "kup_x").replace("{part_number}", "1").replace("{record_id}", "r1"))
            for method in route.methods:
                resp = await client.request(method, path, json={})
                assert resp.status_code == 404, (method, path, resp.text)


class TestBases:
    async def test_create_list_read_edit_delete(self, on, jobs):
        get_blob_store().put("vault/Legal/old.md", "already here")
        kb = await _kb(on, folder="/Legal/")
        assert kb["folder"] == "Legal" and kb["revision"] == 1 and kb["state"]["counts_by_status"] == {}
        assert [(j.op, j.reason) for j in jobs.jobs] == [("reindex", "reconcile")]  # what is there is queued
        assert [b["id"] for b in (await on.get("/api/knowledge/bases")).json()] == [kb["id"]]
        assert (await on.get(f"/api/knowledge/bases/{kb['id']}")).json()["name"] == "Contracts"
        edited = await on.patch(f"/api/knowledge/bases/{kb['id']}", json={"revision": 1, "name": "Legal"})
        assert edited.status_code == 200 and edited.json()["revision"] == 2 and edited.json()["reindex_started"] is False
        stale = await on.patch(f"/api/knowledge/bases/{kb['id']}", json={"revision": 1, "name": "Again"})
        assert stale.status_code == 409
        assert (await on.delete(f"/api/knowledge/bases/{kb['id']}")).status_code == 202
        assert (await on.get(f"/api/knowledge/bases/{kb['id']}")).status_code == 404
        assert get_blob_store().get("vault/Legal/old.md") == "already here"  # a base's files outlive it

    async def test_a_new_folder_is_created(self, on, jobs):
        kb = await _kb(on, folder="brand/new")
        store = get_blob_store()
        path = store.path_for("vault/brand/new")
        assert (path is not None and path.is_dir()) or store.get("vault/brand/new/.keep") == "\n"
        assert kb["folder"] == "brand/new"

    @pytest.mark.parametrize("folder", ["", "tool_outputs", "a/../../x"])
    async def test_a_folder_that_is_not_allowed_is_a_422(self, on, folder):
        resp = await on.post("/api/knowledge/bases", json={"name": "X", "folder": folder})
        assert resp.status_code == 422

    async def test_overlapping_folders_are_a_422(self, on, jobs):
        await _kb(on, folder="docs")
        resp = await on.post("/api/knowledge/bases", json={"name": "X", "folder": "docs/inner"})
        assert resp.status_code == 422 and "overlaps" in resp.json()["detail"]

    async def test_unknown_fields_are_refused(self, on):
        resp = await on.post("/api/knowledge/bases", json={"name": "X", "folder": "x", "colour": "blue"})
        assert resp.status_code == 422

    async def test_a_move_takes_the_documents(self, on, jobs):
        kb = await _kb(on)
        await on.post(f"/api/knowledge/bases/{kb['id']}/records", json={"records": [{"id": "r1", "text": "x"}]})
        jobs.jobs.clear()
        moved = await on.patch(f"/api/knowledge/bases/{kb['id']}", json={"revision": 1, "folder": "moved"})
        assert moved.status_code == 200
        assert sorted((j.op, j.path) for j in jobs.jobs) == [("delete", "kb/_records/r1.json"), ("reindex", None)]

    async def test_an_unknown_base_is_a_404(self, on):
        for method, path in (("GET", "/api/knowledge/bases/kb_000000000000"),
                             ("GET", "/api/knowledge/bases/kb_000000000000/documents"),
                             ("DELETE", "/api/knowledge/bases/kb_000000000000")):
            assert (await on.request(method, path)).status_code == 404


class TestUploads:
    async def test_a_file_uploaded_in_parts_lands_whole_in_the_vault(self, on, jobs, monkeypatch):
        from core.knowledge import ingest

        monkeypatch.setattr(ingest, "PART_SIZE", 10)
        kb = await _kb(on)
        data = "# Notes\n\nA file long enough for several parts.".encode()
        session, parts = await _upload(on, kb["id"], "notes.md", data, path="team", metadata={"year": 2026})
        assert len(session["parts"]) == 5 and session["parts"][0]["method"] == "PUT"
        assert session["parts"][0]["url"] == f"/api/knowledge/uploads/{session['upload_id']}/parts/1"
        done = await on.post(f"/api/knowledge/uploads/{session['upload_id']}/complete",
                             json={"parts": list(reversed(parts))})
        assert done.status_code == 202, done.text
        (doc,) = done.json()["documents"]
        assert (doc["path"], doc["source"], doc["status"], doc["metadata"]) == (
            "kb/team/notes.md", "upload", "queued", {"year": 2026})
        assert doc["sha256"] == hashlib.sha256(data).hexdigest() and doc["size_bytes"] == len(data)
        assert get_blob_store().get_bytes("vault/kb/team/notes.md") == data
        assert get_blob_store().list(f"knowledge/uploads/{session['upload_id']}/") == []
        assert [(j.op, j.reason, j.path) for j in jobs.jobs][-1] == ("upsert", "upload", "kb/team/notes.md")
        again = await on.post(f"/api/knowledge/uploads/{session['upload_id']}/complete", json={"parts": parts})
        assert again.status_code == 404  # finished: the session is gone

    async def test_a_binary_upload_is_kept_byte_for_byte(self, on, jobs):
        kb = await _kb(on)
        data = bytes(range(256)) * 10
        session, parts = await _upload(on, kb["id"], "scan.pdf", data, mime="application/pdf")
        await on.post(f"/api/knowledge/uploads/{session['upload_id']}/complete", json={"parts": parts})
        assert get_blob_store().get_bytes("vault/kb/scan.pdf") == data

    async def test_a_part_that_does_not_match_its_etag_writes_nothing(self, on, jobs):
        kb = await _kb(on)
        session, parts = await _upload(on, kb["id"], "a.md", b"hello")
        parts[0]["etag"] = '"' + "0" * 32 + '"'
        resp = await on.post(f"/api/knowledge/uploads/{session['upload_id']}/complete", json={"parts": parts})
        assert resp.status_code == 422 and "ETag" in resp.json()["detail"]
        assert get_blob_store().get_bytes("vault/kb/a.md") is None

    async def test_missing_parts_and_wrong_sizes_are_refused(self, on, jobs, monkeypatch):
        from core.knowledge import ingest

        monkeypatch.setattr(ingest, "PART_SIZE", 4)
        kb = await _kb(on)
        init = (await on.post(f"/api/knowledge/bases/{kb['id']}/uploads",
                              json={"filename": "a.md", "size_bytes": 8, "mime": "text/markdown"})).json()
        url = init["parts"][0]["url"]
        etag = (await on.put(url, content=b"abcd")).json()["etag"]
        complete = f"/api/knowledge/uploads/{init['upload_id']}/complete"
        assert (await on.post(complete, json={"parts": [{"part_number": 1, "etag": etag}]})).status_code == 422
        other = await on.put(init["parts"][1]["url"], content=b"ef")
        both = [{"part_number": 1, "etag": etag}, {"part_number": 2, "etag": other.json()["etag"]}]
        resp = await on.post(complete, json={"parts": both})
        assert resp.status_code == 422 and "add up to 6 bytes" in resp.json()["detail"]
        assert (await on.put(url, content=b"12345")).status_code == 413  # a part over the part size
        assert (await on.put(url.replace("/parts/1", "/parts/3"), content=b"x")).status_code == 404

    @pytest.mark.parametrize("body,status", [
        ({"filename": "big.md", "size_bytes": 101 * 1024 * 1024, "mime": "text/markdown"}, 413),
        ({"filename": "run.exe", "size_bytes": 10, "mime": "application/octet-stream"}, 415),
        ({"filename": "../escape.md", "size_bytes": 10, "mime": "text/markdown"}, 422),
        ({"filename": "a.md", "size_bytes": 10, "mime": "text/markdown", "path": "../../etc"}, 422),
        ({"filename": "a.json", "size_bytes": 10, "mime": "application/json", "path": "_records"}, 422),
        ({"filename": "a.md", "size_bytes": 10, "mime": "text/markdown", "metadata": {"path": "x"}}, 422),
    ])
    async def test_what_an_upload_refuses_up_front(self, on, jobs, body, status):
        kb = await _kb(on)
        resp = await on.post(f"/api/knowledge/bases/{kb['id']}/uploads", json=body)
        assert resp.status_code == status, resp.text

    async def test_an_unknown_or_expired_upload_is_a_404(self, on, jobs):
        from core.knowledge import ingest

        assert (await on.put("/api/knowledge/uploads/kup_nope/parts/1", content=b"x")).status_code == 404
        kb = await _kb(on)
        init = (await on.post(f"/api/knowledge/bases/{kb['id']}/uploads",
                              json={"filename": "a.md", "size_bytes": 1, "mime": "text/markdown"})).json()
        key = f"knowledge/uploads/{init['upload_id']}/manifest.json"
        manifest = json.loads(get_blob_store().get(key))
        manifest["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        get_blob_store().put(key, json.dumps(manifest))
        assert (await on.put(init["parts"][0]["url"], content=b"x")).status_code == 404
        assert get_blob_store().get(key) is None  # an expired session is removed when it is found
        assert await ingest.sweep_expired_uploads() == 0

    async def test_the_timer_sweeps_expired_sessions(self, on, jobs):
        from core.knowledge import ingest

        kb = await _kb(on)
        init = (await on.post(f"/api/knowledge/bases/{kb['id']}/uploads",
                              json={"filename": "a.md", "size_bytes": 1, "mime": "text/markdown"})).json()
        await on.put(init["parts"][0]["url"], content=b"x")
        key = f"knowledge/uploads/{init['upload_id']}/manifest.json"
        manifest = json.loads(get_blob_store().get(key))
        manifest["expires_at"] = "2000-01-01T00:00:00+00:00"
        get_blob_store().put(key, json.dumps(manifest))
        assert await ingest.sweep_expired_uploads() == 1
        assert get_blob_store().list(f"knowledge/uploads/{init['upload_id']}/") == []


class TestRecords:
    async def test_records_are_files_and_the_same_one_twice_is_one_job(self, on, jobs):
        kb = await _kb(on)
        jobs.jobs.clear()
        body = {"records": [{"id": "t-1", "text": "Reset link expired.", "title": "Ticket", "doc_date": "2025-01-20",
                             "metadata": {"priority": 1}},
                            {"json": {"customer": "Acme", "tier": 2}}]}
        resp = await on.post(f"/api/knowledge/bases/{kb['id']}/records", json=body)
        assert resp.status_code == 202, resp.text
        first, second = resp.json()["documents"]
        assert (first["path"], first["source"], first["title"], first["doc_date"], first["metadata"]) == (
            "kb/_records/t-1.json", "record", "Ticket", "2025-01-20", {"priority": 1})
        assert second["path"].startswith("kb/_records/") and second["source"] == "record"
        stored = json.loads(get_blob_store().get("vault/kb/_records/t-1.json"))
        assert stored == {"id": "t-1", "title": "Ticket", "doc_date": "2025-01-20", "metadata": {"priority": 1},
                          "text": "Reset link expired."}
        assert len(jobs.jobs) == 2
        row = await documents.get(first["id"])
        await documents.transition(row.id, "failed", reason="internal")  # as if a job ran and rested it
        again = await on.post(f"/api/knowledge/bases/{kb['id']}/records", json={"records": [body["records"][0]]})
        assert again.json()["documents"][0]["status"] == "failed"  # unchanged: nothing queued
        changed = {**body["records"][0], "text": "Resent."}
        resent = await on.post(f"/api/knowledge/bases/{kb['id']}/records", json={"records": [changed]})
        assert resent.json()["documents"][0]["status"] == "queued"
        assert resent.json()["documents"][0]["sha256"] != first["sha256"]

    async def test_a_record_is_deleted_with_its_file(self, on, jobs):
        kb = await _kb(on)
        await on.post(f"/api/knowledge/bases/{kb['id']}/records", json={"records": [{"id": "t-1", "text": "x"}]})
        assert (await on.delete(f"/api/knowledge/bases/{kb['id']}/records/t-1")).status_code == 202
        assert get_blob_store().get("vault/kb/_records/t-1.json") is None
        row = await documents.get_by_path(kb["id"], "kb/_records/t-1.json")
        assert row.status == "deleting"
        assert (await on.delete(f"/api/knowledge/bases/{kb['id']}/records/t-1")).status_code == 404

    async def test_a_bad_record_is_a_422(self, on, jobs):
        kb = await _kb(on)
        for record in ({"id": "../x", "text": "a"}, {"text": "a", "json": {}}, {"text": "a", "metadata": {"a b": 1}}):
            resp = await on.post(f"/api/knowledge/bases/{kb['id']}/records", json={"records": [record]})
            assert resp.status_code == 422, record


class TestDocuments:
    async def test_list_detail_retry_delete(self, on, jobs, monkeypatch):
        from core.knowledge.jobs import run_ingest_job

        install_fake_indexing(monkeypatch, FakeIndexer())
        kb = await _kb(on)
        records = [{"id": f"r{n}", "text": f"record {n}"} for n in range(3)]
        await on.post(f"/api/knowledge/bases/{kb['id']}/records", json={"records": records})
        listed = (await on.get(f"/api/knowledge/bases/{kb['id']}/documents", params={"limit": 2})).json()
        rest = (await on.get(f"/api/knowledge/bases/{kb['id']}/documents",
                             params={"after": listed[-1]["id"]})).json()
        assert len(listed) == 2 and len(rest) == 1
        doc_id = listed[0]["id"]
        busy = await on.post(f"/api/knowledge/documents/{doc_id}/retry")
        assert busy.status_code == 409  # queued: someone is on it
        for job in [j for j in jobs.jobs if j.doc_id == doc_id]:
            await run_ingest_job(job)
        detail = (await on.get(f"/api/knowledge/documents/{doc_id}")).json()
        assert detail["status"] == "indexed" and detail["report"]["outcome"] == "indexed"
        retried = await on.post(f"/api/knowledge/documents/{doc_id}/retry")
        assert retried.status_code == 202 and retried.json()["status"] == "queued"
        assert jobs.jobs[-1].op == "retry"
        path = detail["path"]
        assert (await on.delete(f"/api/knowledge/documents/{doc_id}")).status_code == 202
        assert get_blob_store().get(f"vault/{path}") is None
        assert (await on.get(f"/api/knowledge/documents/{doc_id}")).json()["status"] == "deleting"
        assert (await on.get("/api/knowledge/documents/kdoc_nope")).status_code == 404


async def test_an_upload_is_indexed_end_to_end(on):
    """No fakes: the in-process dispatcher runs A1's job, which hands the file to B1's pipeline."""
    kb = await _kb(on)
    session, parts = await _upload(on, kb["id"], "notes.md", b"# Escalation\n\nSeverity one pages on-call.")
    doc = (await on.post(f"/api/knowledge/uploads/{session['upload_id']}/complete",
                         json={"parts": parts})).json()["documents"][0]
    for _ in range(300):
        detail = (await on.get(f"/api/knowledge/documents/{doc['id']}")).json()
        if detail["status"] not in ("queued", "parsing", "indexing"):
            break
        await asyncio.sleep(0.01)
    assert detail["status"] == "indexed", detail
    assert [s["stage"] for s in detail["report"]["stages"]][:2] == ["parse", "sanitize"]
    listed = (await on.get(f"/api/knowledge/bases/{kb['id']}")).json()
    assert listed["state"]["counts_by_status"] == {"indexed": 1} and listed["state"]["active_snapshot"]
