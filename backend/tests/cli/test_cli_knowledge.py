"""Knowledge from the CLI: bases, uploads, notes and records, documents, search (CLI1, D294).

All of it is v1's own Knowledge routes, so these drive the CLI against the live app with Knowledge on; ingest
jobs are recorded rather than run (a document stays `queued`), as v1's own tests record them.
"""
from __future__ import annotations

import json

import pytest

from _fakes.knowledge_corpus import CONTRACTS, corpus
from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from synapse.client import errors

pytestmark = pytest.mark.usefixtures("knowledge_store")


@pytest.fixture(autouse=True)
def _fresh():
    from core.knowledge import dispatch
    dispatch.reset()
    yield
    dispatch.reset()


@pytest.fixture
async def on(client):
    assert (await client.post("/api/settings", json={"knowledge_enabled": True})).status_code == 200


@pytest.fixture
def jobs():
    from core.knowledge import seams
    from core.knowledge.testing.fakes import RecordingDispatcher
    recorder = RecordingDispatcher()
    seams.set_ingest_dispatcher(recorder)
    return recorder


class TestOff:
    async def test_every_command_says_knowledge_is_off(self, synapse):
        for argv in (("knowledge", "bases", "list"), ("knowledge", "search", "fees"),
                     ("knowledge", "docs", "show", "kdoc_x")):
            code, out, err = await synapse(*argv)
            assert code == errors.NOT_FOUND and out == "", argv
            assert "Knowledge is switched off" in err, argv


class TestBases:
    async def test_made_listed_changed_and_deleted(self, synapse, on, jobs):
        code, out, err = await synapse("knowledge", "bases", "create", "Handbook", "--folder", "handbook",
                                       "--description", "How we work")
        assert code == 0, err
        kb_id = out.strip()
        assert kb_id.startswith("kb_")

        code, out, _ = await synapse("knowledge", "bases", "list")
        assert code == 0 and "Handbook" in out and "handbook" in out

        code, out, err = await synapse("knowledge", "bases", "update", "handbook", "--description", "v2", "--json")
        assert code == 0, err
        assert json.loads(out)["description"] == "v2"

        code, out, _ = await synapse("knowledge", "bases", "show", kb_id)
        assert code == 0 and "v2" in out

        code, _, err = await synapse("knowledge", "bases", "delete", kb_id)
        assert code == errors.USAGE and "--yes" in err
        code, out, _ = await synapse("knowledge", "bases", "delete", kb_id, "--yes")
        assert code == 0 and "Vault folder and files stay" in out

    async def test_a_base_needs_its_folder(self, synapse, on):
        code, _, err = await synapse("knowledge", "bases", "create", "Nameless")
        assert code == errors.USAGE and "--folder" in err


class TestAdding:
    async def test_a_file_is_uploaded_in_parts_and_queued(self, synapse, on, jobs, tmp_path, monkeypatch):
        from core.knowledge import ingest
        from core.storage import get_blob_store

        monkeypatch.setattr(ingest, "PART_SIZE", 10)
        _, out, _ = await synapse("knowledge", "bases", "create", "Notes", "--folder", "kb")
        data = "# Notes\n\nA file long enough for several parts."
        path = tmp_path / "notes.md"
        path.write_text(data)

        code, out, err = await synapse("knowledge", "upload", str(path), "--base", "notes")
        assert code == 0, err
        doc_id = out.strip()
        assert doc_id.startswith("kdoc")
        assert get_blob_store().get_bytes("vault/kb/notes.md") == data.encode(), "every part landed, in order"

        code, out, _ = await synapse("knowledge", "docs", "list", "--base", "Notes", "--json")
        (doc,) = json.loads(out)
        assert (doc["id"], doc["status"]) == (doc_id, "queued")
        code, out, _ = await synapse("knowledge", "docs", "show", doc_id)
        assert code == 0 and "queued" in out and "kb/notes.md" in out

    async def test_wait_reads_the_document_until_it_has_settled(self, synapse, on, jobs, tmp_path, monkeypatch):
        import synapse.client.knowledge as kn

        _, out, _ = await synapse("knowledge", "bases", "create", "Notes", "--folder", "kb")
        reads = []
        real = kn.ApiClient.get

        def indexed_on_the_second_read(self, path, **params):
            body = real(self, path, **params)
            if path.startswith("/knowledge/documents/"):
                reads.append(path)
                if len(reads) >= 2:
                    body = {**body, "status": "indexed"}
            return body
        monkeypatch.setattr(kn.ApiClient, "get", indexed_on_the_second_read)
        monkeypatch.setattr(kn.time, "sleep", lambda s: None)
        code, out, err = await synapse("knowledge", "add-note", "Ship on Fridays.", "--base", "notes",
                                       "--title", "Rule", "--wait")
        assert code == 0, err
        assert len(reads) == 2 and "indexed" in err

    async def test_records_from_a_file(self, synapse, on, jobs, tmp_path):
        await synapse("knowledge", "bases", "create", "CRM", "--folder", "crm")
        path = tmp_path / "records.json"
        path.write_text(json.dumps([{"id": "acct-1", "text": "Acme renews in May.", "metadata": {"tier": "gold"}},
                                    {"id": "acct-2", "json": {"name": "Globex", "renews": "June"}}]))
        code, out, err = await synapse("knowledge", "records", "add", "--base", "crm", "-f", str(path))
        assert code == 0, err
        assert len(out.split()) == 2
        code, out, _ = await synapse("knowledge", "records", "delete", "acct-1", "--base", "crm")
        assert code == 0 and out.strip() == "Deleting."

    async def test_an_empty_file_is_refused_before_anything_is_sent(self, synapse, on, tmp_path):
        await synapse("knowledge", "bases", "create", "Notes", "--folder", "kb")
        empty = tmp_path / "empty.txt"
        empty.write_text("")
        code, _, err = await synapse("knowledge", "upload", str(empty), "--base", "notes")
        assert code == errors.USAGE and "empty" in err


class TestSearch:
    async def test_hits_name_their_source_and_the_trace_is_one_command_away(self, synapse, on):
        await corpus()
        code, out, err = await synapse("knowledge", "search", "hosting monthly fee", "--base", CONTRACTS.id)
        assert code == 0, err
        assert "[K1]" in out and "acme" in out.lower()
        query_id = err.split("synapse knowledge trace ")[1].split()[0]

        code, out, _ = await synapse("knowledge", "trace", query_id, "--json")
        assert code == 0 and json.loads(out)["query_id"] == query_id

    async def test_every_base_is_searched_by_default(self, synapse, on):
        await corpus()
        code, out, _ = await synapse("knowledge", "search", "monthly fee", "--json")
        assert code == 0 and json.loads(out)["hits"]

    async def test_a_filter_it_cannot_read(self, synapse, on):
        await corpus()
        code, _, err = await synapse("knowledge", "search", "fees", "--filter", "{not json")
        assert code == errors.USAGE and "--filter" in err


class TestDocumentsByName:
    async def test_a_note_is_listed_by_its_title(self, synapse, on, jobs):
        await synapse("knowledge", "bases", "create", "Handbook", "--folder", "handbook")
        await synapse("knowledge", "add-note", "Releases ship on Fridays.", "--base", "handbook", "--title",
                      "Release day")
        code, out, _ = await synapse("knowledge", "docs", "list", "--base", "handbook")
        assert code == 0 and "Release day" in out and "_records/" not in out
