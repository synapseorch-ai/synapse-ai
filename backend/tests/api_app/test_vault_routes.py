"""The self-hosted vault routes — the four bugs session A of the cloud vault found.

Each was a code read against `core/routes/vault.py` (D94): a file or folder
name written under the parent unvalidated, so `name="../../x.md"` left the
vault; a DELETE with an empty path resolving to the root and removing the whole
vault; a `startswith` guard that lets a sibling directory whose name begins
with the vault's pass; and an S3 branch that reached the bucket through the raw
client — no tenant — while the engine writes the tenant's keys.
"""

from __future__ import annotations

import pytest

from core.storage import set_blob_store
from core.storage.base import tenant_key
from core.tenancy import get_tenant
from core.vault import _vault_root


async def _create(client, name, content="hello", path=""):
    return await client.post("/api/vault/file", json={"path": path, "name": name, "content": content})


class _Pathless:
    """A store with no filesystem, like S3, applying the tenant inside as S3 does."""

    def __init__(self):
        self.blobs: dict[str, str] = {}

    def put(self, key, content):
        self.blobs[tenant_key(key)] = content

    def get(self, key):
        return self.blobs.get(tenant_key(key))

    def exists(self, key):
        return tenant_key(key) in self.blobs

    def delete(self, key):
        self.blobs.pop(tenant_key(key), None)

    def list(self, prefix=""):
        scoped = tenant_key(prefix)
        cut = tenant_key("")
        return [k[len(cut):] for k in self.blobs if k.startswith(scoped)]

    def path_for(self, key):
        return None


# ── (a) a name is one segment ────────────────────────────────────────────────
class TestANameIsOneSegment:
    BAD = ["../../escape.md", "sub/dir.md", "back\\slash.md", "..", ".hidden.md",
           "notes[1].md", " ", "", "bad" + chr(0) + ".md"]
    IDS = ["dotdot", "slash", "backslash", "only-dots", "leading-dot", "bracket",
           "blank", "empty", "control"]

    @pytest.mark.parametrize("name", BAD, ids=IDS)
    async def test_create_file_refuses(self, client, name):
        resp = await _create(client, name)
        assert resp.status_code == 400, resp.text
        root = _vault_root()
        assert not (root.parent.parent / "escape.md").exists(), "the file left the vault"
        assert not list(root.rglob("*.md")), "something was written"

    @pytest.mark.parametrize("name", BAD, ids=IDS)
    async def test_create_folder_refuses(self, client, name):
        resp = await client.post("/api/vault/folder", json={"path": "", "name": name})
        assert resp.status_code == 400, resp.text
        root = _vault_root()
        assert not (root.parent.parent / "escape.md").exists()
        assert not (root.exists() and [p for p in root.iterdir() if p.is_dir()]), "a folder was made"

    async def test_a_plain_name_still_works(self, client):
        assert (await client.post("/api/vault/folder", json={"path": "", "name": "notes"})).status_code == 200
        resp = await _create(client, "q3 plan (draft).md", path="notes")
        assert resp.status_code == 200, resp.text
        assert resp.json()["path"] == "notes/q3 plan (draft).md"
        assert (_vault_root() / "notes" / "q3 plan (draft).md").read_text() == "hello"


# ── (b) DELETE refuses the root ──────────────────────────────────────────────
class TestDeleteRefusesTheRoot:
    @pytest.mark.parametrize("path", ["", "/", ".", "./", "../", "sub/.."],
                             ids=["empty", "slash", "dot", "dot-slash", "dotdot-slash", "climb-back"])
    async def test_the_vault_survives(self, client, path):
        assert (await _create(client, "keep.md")).status_code == 200
        resp = await client.request("DELETE", "/api/vault/item", json={"path": path})
        assert resp.status_code == 400, resp.text
        assert (_vault_root() / "keep.md").exists(), "the vault was deleted"

    async def test_a_file_and_a_folder_still_delete(self, client):
        await client.post("/api/vault/folder", json={"path": "", "name": "gone"})
        await _create(client, "a.md", path="gone")
        await _create(client, "b.md")
        assert (await client.request("DELETE", "/api/vault/item", json={"path": "b.md"})).status_code == 200
        assert (await client.request("DELETE", "/api/vault/item", json={"path": "gone"})).status_code == 200
        assert not (_vault_root() / "b.md").exists() and not (_vault_root() / "gone").exists()


# ── (c) a sibling whose name starts with the vault's is outside it ──────────
class TestASiblingIsOutsideTheVault:
    async def test_a_climb_into_a_sibling_is_refused(self, client):
        root = _vault_root()
        sibling = root.parent / (root.name + "2")
        sibling.mkdir(parents=True, exist_ok=True)
        (sibling / "secret.md").write_text("not yours", encoding="utf-8")

        resp = await client.get("/api/vault/file", params={"path": f"sub/../../{sibling.name}/secret.md"})
        assert resp.status_code == 403, resp.text
        assert "not yours" not in resp.text


# ── (d) an object store is reached through the tenant's blob store ───────────
class TestAnObjectStoreIsTheTenants:
    @pytest.fixture
    def store(self):
        s = _Pathless()
        set_blob_store(s)
        yield s
        set_blob_store(None)

    async def test_the_tree_lists_this_tenants_vault_only(self, client, store):
        store.put("vault/a.md", "mine")
        store.put("vault/notes/.keep", "\n")
        store.put("vault/notes/b.md", "mine too")
        store.put("logs/run_1.log", "not a vault file")
        store.blobs["other-org/vault/theirs.md"] = "not yours"

        resp = await client.get("/api/vault/tree")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["source"] == "s3"
        names = {item["name"] for item in body["tree"]}
        assert names == {"a.md", "notes"}, body
        assert "theirs.md" not in resp.text and "run_1.log" not in resp.text

    async def test_search_read_write_and_delete_go_through_the_store(self, client, store):
        me = get_tenant()
        assert (await client.post("/api/vault/folder", json={"path": "", "name": "notes"})).status_code == 200
        assert store.blobs[f"{me}/vault/notes/.keep"] == "\n", "a folder marker must never be empty"

        resp = await _create(client, "c.md", content="see", path="notes")
        assert resp.status_code == 200, resp.text
        assert store.blobs[f"{me}/vault/notes/c.md"] == "see"
        assert (await _create(client, "c.md", path="notes")).status_code == 409

        found = (await client.get("/api/vault/search", params={"q": "c.md"})).json()
        assert found["files"] == [{"name": "c.md", "path": "notes/c.md", "ext": ".md"}]

        assert (await client.put("/api/vault/file", json={"path": "notes/c.md", "content": "seen"})).status_code == 200
        got = (await client.get("/api/vault/file", params={"path": "notes/c.md"})).json()
        assert got["content"] == "seen"

        assert (await client.request("DELETE", "/api/vault/item", json={"path": "notes"})).status_code == 200
        assert not [k for k in store.blobs if k.startswith(f"{me}/vault/notes")]

    async def test_the_root_cannot_be_deleted_here_either(self, client, store):
        store.put("vault/a.md", "mine")
        resp = await client.request("DELETE", "/api/vault/item", json={"path": ""})
        assert resp.status_code == 400
        assert store.get("vault/a.md") == "mine"

    async def test_a_name_is_one_segment_here_too(self, client, store):
        assert (await _create(client, "../../x.md")).status_code == 400
        assert not [k for k in store.blobs if "x.md" in k]
