"""Bytes in the blob store (D157): what uploads and Knowledge's ingestion read and write.

Every case runs on the three stores a deployment can have: a local directory, a
store with no filesystem at all (the object-store branch of the Vault), and
`S3BlobStore` over the real `SynapseS3` code with a fake boto3 client. The text
half of the protocol is unchanged, and a case here holds that too.
"""
from __future__ import annotations

import hashlib

import pytest

from _fakes.blob_stores import MemoryBlobStore, fake_s3_blob_store
from core.scale.context import set_resource_provider
from core.storage import LocalBlobStore
from core.tenancy import tenant_scope

BINARY = bytes(range(256)) * 3 + b"\xff\xfe not utf-8 \x80"


@pytest.fixture(params=["local", "memory", "s3"])
def store(request, tmp_path):
    if request.param == "local":
        return LocalBlobStore(tmp_path / "blobs")
    if request.param == "memory":
        return MemoryBlobStore()
    return fake_s3_blob_store()


class _Provider:
    async def resolve_agent(self, agent_id):
        return None

    async def resolve_orchestration(self, orch_id):
        return None

    async def resolve_custom_tools(self):
        return []

    async def resolve_mcp_servers(self):
        return []


@pytest.fixture
def multi_tenant():
    set_resource_provider(_Provider())
    yield
    set_resource_provider(None)


class TestBytes:
    def test_bytes_round_trip(self, store):
        store.put_bytes("vault/kb/a.pdf", BINARY, content_type="application/pdf")
        assert store.get_bytes("vault/kb/a.pdf") == BINARY
        assert store.get_bytes("vault/kb/missing.pdf") is None

    def test_text_get_still_refuses_bytes_that_are_not_text(self, store):
        """The Vault routes and the mention reader read this ValueError as "a binary file"."""
        store.put_bytes("vault/kb/a.pdf", BINARY)
        with pytest.raises(ValueError):
            store.get("vault/kb/a.pdf")

    def test_text_and_bytes_are_one_blob(self, store):
        store.put("vault/notes.md", "héllo")
        assert store.get_bytes("vault/notes.md") == "héllo".encode("utf-8")
        store.put_bytes("vault/notes.md", "bye".encode("utf-8"))
        assert store.get("vault/notes.md") == "bye"

    def test_iter_bytes_streams_the_whole_blob(self, store):
        store.put_bytes("big.bin", BINARY)
        pieces = list(store.iter_bytes("big.bin", chunk_size=100))
        assert b"".join(pieces) == BINARY
        assert len(pieces) == -(-len(BINARY) // 100) and all(len(p) <= 100 for p in pieces)
        assert store.iter_bytes("nothing.bin") is None

    def test_put_stream_reports_size_and_sha256(self, store):
        size, sha = store.put_stream("vault/kb/up.bin", iter([BINARY[:10], BINARY[10:500], BINARY[500:]]))
        assert (size, sha) == (len(BINARY), hashlib.sha256(BINARY).hexdigest())
        assert store.get_bytes("vault/kb/up.bin") == BINARY

    def test_put_stream_is_all_or_nothing(self, store):
        store.put_bytes("vault/kb/up.bin", b"before")

        def broken():
            yield b"half of the new"
            raise OSError("the upload stream broke")

        with pytest.raises(OSError):
            store.put_stream("vault/kb/up.bin", broken())
        assert store.get_bytes("vault/kb/up.bin") == b"before"
        assert [b.key for b in store.list_stats("vault/kb")] == ["vault/kb/up.bin"]  # no temp file left

    def test_stat_and_list_stats(self, store):
        store.put_bytes("vault/kb/a.pdf", BINARY)
        store.put("vault/kb/sub/b.md", "four")
        store.put("vault/other/c.md", "x")
        a = store.stat("vault/kb/a.pdf")
        assert a.key == "vault/kb/a.pdf" and a.size == len(BINARY) and a.modified_at is not None
        assert store.stat("vault/kb/none") is None
        listed = store.list_stats("vault/kb")
        assert [(b.key, b.size) for b in listed] == [("vault/kb/a.pdf", len(BINARY)), ("vault/kb/sub/b.md", 4)]
        assert [b.key for b in listed] == store.list("vault/kb")

    def test_tenants_never_share_bytes(self, store, multi_tenant):
        with tenant_scope("acme"):
            store.put_bytes("vault/kb/a.pdf", b"acme")
        with tenant_scope("globex"):
            assert store.get_bytes("vault/kb/a.pdf") is None
            assert store.stat("vault/kb/a.pdf") is None
            assert store.list_stats("vault") == []
            store.put_stream("vault/kb/a.pdf", iter([b"globex"]))
        with tenant_scope("acme"):
            assert store.get_bytes("vault/kb/a.pdf") == b"acme"
            assert [b.key for b in store.list_stats("vault")] == ["vault/kb/a.pdf"]


class TestLocal:
    @pytest.mark.parametrize("key", ["../escape.bin", "vault/../../x.bin"])
    def test_bytes_keys_cannot_leave_the_tenant(self, tmp_path, key):
        store = LocalBlobStore(tmp_path)
        for call in (lambda: store.put_bytes(key, b"x"), lambda: store.get_bytes(key),
                     lambda: store.put_stream(key, [b"x"]), lambda: store.stat(key), lambda: store.iter_bytes(key)):
            with pytest.raises(ValueError):
                call()

    def test_a_directory_is_not_a_blob(self, tmp_path):
        store = LocalBlobStore(tmp_path)
        store.put("vault/kb/a.md", "a")
        assert store.stat("vault/kb") is None and store.get_bytes("vault/kb") is None


class TestS3Multipart:
    @pytest.fixture
    def s3(self, monkeypatch):
        import core.s3_storage as s3mod
        monkeypatch.setattr(s3mod, "MULTIPART_PART_SIZE", 64)
        return fake_s3_blob_store()

    def test_a_small_blob_is_one_put(self, s3):
        s3.put_stream("vault/a.bin", [b"x" * 10, b"y" * 10])
        assert s3.client._s3.calls == ["put_object"]

    def test_a_big_blob_is_a_multipart_upload(self, s3):
        size, sha = s3.put_stream("vault/a.bin", (BINARY[i:i + 50] for i in range(0, len(BINARY), 50)))
        calls = s3.client._s3.calls
        assert calls[0] == "create_multipart_upload" and calls[-1] == "complete_multipart_upload"
        assert calls.count("upload_part") == -(-len(BINARY) // 64)
        assert s3.get_bytes("vault/a.bin") == BINARY and size == len(BINARY)
        assert sha == hashlib.sha256(BINARY).hexdigest()

    def test_a_broken_stream_aborts_the_upload(self, s3):
        def broken():
            yield BINARY[:200]
            raise OSError("gone")

        with pytest.raises(OSError):
            s3.put_stream("vault/a.bin", broken())
        assert s3.client._s3.calls[-1] == "abort_multipart_upload"
        assert s3.get_bytes("vault/a.bin") is None and s3.client._s3.uploads == {}

    def test_the_calls_match_boto3s_own_shapes(self):
        """The real boto3 client, stubbed: botocore checks every parameter against the S3 model."""
        import boto3
        from botocore.stub import Stubber

        from core.s3_storage import SynapseS3

        real = boto3.client("s3", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y")
        client = SynapseS3.__new__(SynapseS3)
        client.bucket, client.prefix, client._s3 = "bucket", "synapse", real
        key = "synapse/t/vault/a.bin"
        with Stubber(real) as stub:
            stub.add_response("create_multipart_upload", {"UploadId": "u1"},
                              {"Bucket": "bucket", "Key": key, "ContentType": "application/pdf"})
            for n, body in ((1, b"a" * 6), (2, b"b" * 2)):
                stub.add_response("upload_part", {"ETag": f'"e{n}"'},
                                  {"Bucket": "bucket", "Key": key, "UploadId": "u1", "PartNumber": n, "Body": body})
            stub.add_response("complete_multipart_upload", {},
                              {"Bucket": "bucket", "Key": key, "UploadId": "u1",
                               "MultipartUpload": {"Parts": [{"ETag": '"e1"', "PartNumber": 1},
                                                             {"ETag": '"e2"', "PartNumber": 2}]}})
            stub.add_response("head_object", {"ContentLength": 8, "ETag": '"abc"'},
                              {"Bucket": "bucket", "Key": key})
            size, _ = client.upload_stream("t/vault/a.bin", [b"a" * 4, b"a" * 2 + b"b" * 2], "application/pdf",
                                           part_size=6)
            assert size == 8
            assert client.head("t/vault/a.bin")["etag"] == "abc"
            stub.assert_no_pending_responses()
