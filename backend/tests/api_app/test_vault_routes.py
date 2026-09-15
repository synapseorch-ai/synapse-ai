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
