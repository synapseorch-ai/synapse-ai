"""F323: reading a Vault file that is not text answers 415 on every Vault, not a 500 on a local one.

A local Vault's `GET /api/vault/file` read the file with `read_text` and turned
the decode error into a 500 carrying Python's message; the object-store branch
already answered 415 "Not a text file". Knowledge's uploads put PDFs and Office
files in the Vault, and `run_bash` or a screenshot could already write a binary
there, so the Vault screen hit it by clicking such a file.
"""
from __future__ import annotations

import pytest

from _fakes.blob_stores import MemoryBlobStore
from core.storage import get_blob_store, set_blob_store


@pytest.mark.parametrize("store_kind", ["local", "object"])
async def test_a_file_that_is_not_text_is_a_415(client, store_kind):
    if store_kind == "object":
        set_blob_store(MemoryBlobStore())
    get_blob_store().put_bytes("vault/kb/scan.pdf", b"%PDF-1.7\n\xff\xfe\x00\x81 binary")
    resp = await client.get("/api/vault/file", params={"path": "kb/scan.pdf"})
    assert resp.status_code == 415 and resp.json()["detail"] == "Not a text file"


async def test_a_text_file_still_reads(client):
    get_blob_store().put("vault/kb/a.md", "héllo")
    resp = await client.get("/api/vault/file", params={"path": "kb/a.md"})
    assert resp.status_code == 200 and resp.json()["content"] == "héllo"
