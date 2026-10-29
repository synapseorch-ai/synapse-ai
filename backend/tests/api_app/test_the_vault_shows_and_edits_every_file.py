"""F1207 (BLD1; Naveen 10-29): the Vault shows every file it can and edits any text file, and never runs one.

The screen said "can't be opened here" for every image and PDF, opened code, config and logs in the Markdown editor,
and refused to save them. Now:

- `GET /api/vault/raw` answers a file as it is: a raster image as itself (for an <img>), anything else, and any file
  asked with `download`, as an attachment. Always `nosniff` and a sandboxing CSP: an SVG, an HTML page or a PDF
  never renders as a document on this origin. Paths go through the Vault's one resolver: no `..`, no link out.
- The tree gives each file its size, modified time and whether it is read-only.
- `PUT /api/vault/file` saves any text file: refused in the engine's folders (runs write them), for the
  download-only kinds, over a file that is not text, for text that is not UTF-8 or carries a NUL, and over 5 MB.
  The editor opens at most 5 MB too.
"""
from __future__ import annotations

import os

import pytest

from _fakes.blob_stores import MemoryBlobStore
from core.storage import get_blob_store, set_blob_store

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 20


@pytest.fixture(params=["local", "object"])
def store(request):
    if request.param == "object":
        set_blob_store(MemoryBlobStore())
    return request.param


def _put(rel: str, data: bytes | str) -> None:
    if isinstance(data, str):
        get_blob_store().put(f"vault/{rel}", data)
    else:
        get_blob_store().put_bytes(f"vault/{rel}", data)


def _files(nodes):
    for n in nodes:
        if n["type"] == "folder":
            yield from _files(n["children"])
        else:
            yield n


def _safe(resp):
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in resp.headers["content-security-policy"]
    assert "default-src 'none'" in resp.headers["content-security-policy"]


class TestARawFile:
    async def test_a_raster_image_is_itself_inline(self, client, store):
        _put("pics/chart.png", PNG)
        resp = await client.get("/api/vault/raw", params={"path": "pics/chart.png"})
        assert resp.status_code == 200 and resp.content == PNG
        assert resp.headers["content-type"] == "image/png"
        assert resp.headers["content-disposition"].startswith("inline")
        _safe(resp)

    @pytest.mark.parametrize("name,data", [
        ("logo.svg", "<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"),
        ("page.html", "<script>alert(document.cookie)</script>"),
        ("scan.pdf", b"%PDF-1.7\n/JS (app.alert(1))"),
        ("tool.exe", b"MZ\x90\x00"),
    ])
    async def test_anything_else_is_an_attachment_that_runs_nothing(self, client, store, name, data):
        _put(f"in/{name}", data)
        resp = await client.get("/api/vault/raw", params={"path": f"in/{name}"})
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/octet-stream"
        assert resp.headers["content-disposition"].startswith("attachment")
        _safe(resp)

    async def test_download_makes_an_image_an_attachment(self, client, store):
        _put("pics/chart.png", PNG)
        resp = await client.get("/api/vault/raw", params={"path": "pics/chart.png", "download": "true"})
        assert resp.headers["content-disposition"] == "attachment; filename*=UTF-8''chart.png"
        assert resp.headers["content-type"] == "application/octet-stream"

    @pytest.mark.parametrize("path", ["../outside.md", "a/../../outside.md", "", "missing.png"])
    async def test_a_path_out_of_the_vault_or_to_nothing_is_not_found(self, client, store, path):
        resp = await client.get("/api/vault/raw", params={"path": path})
        assert resp.status_code in (403, 404)

    async def test_a_link_out_of_a_local_vault_is_refused(self, client, tmp_path):
        from core.routes.vault import _vault_root
        secret = tmp_path / "secret.txt"
        secret.write_text("not the vault's")
        link = _vault_root() / "escape.txt"
        os.symlink(secret, link)
        try:
            resp = await client.get("/api/vault/raw", params={"path": "escape.txt"})
            assert resp.status_code == 403 and b"not the vault's" not in resp.content
        finally:
            link.unlink()


class TestTheTree:
    async def test_each_file_has_its_size_time_and_whether_it_is_read_only(self, client, store):
        _put("notes/a.md", "hello")
        _put("tool_outputs/run.json", "{}")
        tree = (await client.get("/api/vault/tree")).json()["tree"]
        by_path = {f["path"]: f for f in _files(tree)}
        assert by_path["notes/a.md"]["size"] == 5 and by_path["notes/a.md"]["modified"]
        assert by_path["notes/a.md"]["read_only"] is False
        assert by_path["tool_outputs/run.json"]["read_only"] is True


class TestSavingAnyTextFile:
    @pytest.mark.parametrize("name", ["job.py", "config.yaml", "run.log", "page.html", "README"])
    async def test_a_text_file_of_any_kind_is_saved(self, client, store, name):
        _put(f"code/{name}", "old\n")
        saved = await client.put("/api/vault/file", json={"path": f"code/{name}", "content": "new\n"})
        assert saved.status_code == 200, saved.text
        assert (await client.get("/api/vault/file", params={"path": f"code/{name}"})).json()["content"] == "new\n"

    async def test_the_engines_folders_are_read_only_on_the_server(self, client, store):
        _put("tool_outputs/run.md", "what a run wrote")
        opened = (await client.get("/api/vault/file", params={"path": "tool_outputs/run.md"})).json()
        assert opened["read_only"] is True
        saved = await client.put("/api/vault/file", json={"path": "tool_outputs/run.md", "content": "changed"})
        assert saved.status_code == 403 and "read-only" in saved.json()["detail"]
        assert (await client.get("/api/vault/file", params={"path": "tool_outputs/run.md"})).json()["content"] == \
            "what a run wrote"

    async def test_a_download_only_kind_is_not_edited(self, client, store):
        _put("pics/chart.png", PNG)
        saved = await client.put("/api/vault/file", json={"path": "pics/chart.png", "content": "text"})
        assert saved.status_code == 400
        assert get_blob_store().get_bytes("vault/pics/chart.png") == PNG

    async def test_a_file_that_is_not_text_is_never_overwritten_with_text(self, client, store):
        _put("data/blob.bin", b"\xff\xfe\x00\x81binary")
        saved = await client.put("/api/vault/file", json={"path": "data/blob.bin", "content": "text"})
        assert saved.status_code == 415
        assert get_blob_store().get_bytes("vault/data/blob.bin") == b"\xff\xfe\x00\x81binary"

    @pytest.mark.parametrize("content,words", [(r"a\u0000b", "NUL"), (r"\ud800", "UTF-8")])
    async def test_text_that_is_not_utf8_text_is_refused(self, client, store, content, words):
        _put("notes/a.txt", "kept")
        # As JSON escapes, the way a page sends them: a lone surrogate can't be encoded by the client itself.
        body = '{"path": "notes/a.txt", "content": "%s"}' % content
        saved = await client.put("/api/vault/file", content=body, headers={"content-type": "application/json"})
        assert saved.status_code == 400 and words in saved.json()["detail"]
        assert (await client.get("/api/vault/file", params={"path": "notes/a.txt"})).json()["content"] == "kept"

    async def test_more_than_5_mb_is_neither_saved_nor_opened(self, client, store):
        from core.routes.vault import MAX_TEXT_BYTES
        _put("logs/small.log", "x")
        saved = await client.put("/api/vault/file", json={"path": "logs/small.log", "content": "x" * (MAX_TEXT_BYTES + 1)})
        assert saved.status_code == 413
        _put("logs/big.log", "y" * (MAX_TEXT_BYTES + 1))
        opened = await client.get("/api/vault/file", params={"path": "logs/big.log"})
        assert opened.status_code == 413 and "download it" in opened.json()["detail"]
