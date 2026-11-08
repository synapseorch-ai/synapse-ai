"""F1202 (BLD1; CAP3's F-h): the Vault takes a .csv, and its tab-separated twin, as it takes .txt.

"Last week's sales.csv" is the plainest data a person has, and Knowledge reads it (its `csv` format); cloud's vault
takes it. Here `POST /api/vault/file` answered "Only .json, .md, .txt files are allowed", and a .csv already in the
Vault (a run wrote it) opened but could not be saved ("Cannot edit this file type"). Now both are created, read and
saved, on a local Vault and an object store's; every other type is refused as before.
"""
from __future__ import annotations

import pytest

from _fakes.blob_stores import MemoryBlobStore
from core.storage import get_blob_store, set_blob_store

SALES = "region,total\nNorth,41200\nSouth,28950\n"


@pytest.fixture(params=["local", "object"])
def store(request):
    if request.param == "object":
        set_blob_store(MemoryBlobStore())
    return request.param


@pytest.mark.parametrize("name", ["sales.csv", "sales.tsv"])
async def test_a_table_is_created_and_read(client, store, name):
    made = await client.post("/api/vault/file", json={"path": "", "name": name, "content": SALES})
    assert made.status_code == 200, made.text
    assert made.json()["ext"] == name[-4:]
    read = await client.get("/api/vault/file", params={"path": name})
    assert read.status_code == 200 and read.json()["content"] == SALES


async def test_an_empty_table_starts_empty(client, store):
    made = await client.post("/api/vault/file", json={"path": "", "name": "blank.csv"})
    assert made.status_code == 200 and made.json()["content"] == ""


async def test_one_already_there_is_saved(client, store):
    get_blob_store().put("vault/reports/sales.csv", SALES)
    if store == "local":  # the local Vault edits a file that exists
        from core.vault import _vault_root
        target = _vault_root() / "reports" / "sales.csv"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(SALES, encoding="utf-8")
    edited = SALES + "East,21400\n"
    saved = await client.put("/api/vault/file", json={"path": "reports/sales.csv", "content": edited})
    assert saved.status_code == 200, saved.text
    assert (await client.get("/api/vault/file", params={"path": "reports/sales.csv"})).json()["content"] == edited


@pytest.mark.parametrize("name", ["page.html", "run.sh", "data.xlsx", "noext"])
async def test_other_types_are_still_refused(client, store, name):
    made = await client.post("/api/vault/file", json={"path": "", "name": name, "content": "x"})
    assert made.status_code == 400 and "files are allowed" in made.json()["detail"]
