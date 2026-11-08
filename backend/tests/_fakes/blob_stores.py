"""
Blob stores with no filesystem, for tests that must hold on an object store too.

* `MemoryBlobStore` — the whole `BlobStore` protocol, text and bytes, in a dict
  keyed by `tenant_key(key)` (the `_Pathless` pattern of `test_vault_backend.py`,
  with bytes). `path_for` is None, so `get_vault()` treats it as an object store.
* `FakeBoto3S3` — enough of a boto3 S3 client for `SynapseS3`, so
  `S3BlobStore` runs over real `SynapseS3` code with no network.
"""
from __future__ import annotations

import hashlib
import io
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path

from core.storage.base import CHUNK_SIZE, BlobStat, tenant_key


class MemoryBlobStore:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.times: dict[str, datetime] = {}

    def _stamp(self, key: str, data: bytes) -> None:
        self.blobs[key] = data
        self.times[key] = datetime.now(timezone.utc)

    # text
    def put(self, key: str, content: str) -> None:
        self._stamp(tenant_key(key), content.encode("utf-8"))

    def get(self, key: str) -> str | None:
        data = self.blobs.get(tenant_key(key))
        return None if data is None else data.decode("utf-8")  # raises on bytes that are not UTF-8

    def exists(self, key: str) -> bool:
        return tenant_key(key) in self.blobs

    def delete(self, key: str) -> None:
        full = tenant_key(key)
        for k in [k for k in self.blobs if k == full or k.startswith(full.rstrip("/") + "/")]:
            self.blobs.pop(k, None)
            self.times.pop(k, None)

    def list(self, prefix: str = "") -> list[str]:
        return [b.key for b in self.list_stats(prefix)]

    # bytes
    def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        self._stamp(tenant_key(key), bytes(data))

    def get_bytes(self, key: str) -> bytes | None:
        return self.blobs.get(tenant_key(key))

    def iter_bytes(self, key: str, chunk_size: int = CHUNK_SIZE) -> Iterator[bytes] | None:
        data = self.blobs.get(tenant_key(key))
        if data is None:
            return None
        return iter([data[i:i + chunk_size] for i in range(0, len(data), chunk_size)])

    def put_stream(self, key: str, chunks: Iterable[bytes], *,
                   content_type: str | None = None) -> tuple[int, str]:
        buf = b"".join(chunks)  # an iterator that raises writes nothing
        self._stamp(tenant_key(key), buf)
        return len(buf), hashlib.sha256(buf).hexdigest()

    def stat(self, key: str) -> BlobStat | None:
        full = tenant_key(key)
        if full not in self.blobs:
            return None
        return BlobStat(key=key.lstrip("/"), size=len(self.blobs[full]), modified_at=self.times[full])

    def list_stats(self, prefix: str = "") -> list[BlobStat]:
        full, cut = tenant_key(prefix), tenant_key("")
        return sorted((BlobStat(key=k[len(cut):], size=len(v), modified_at=self.times[k])
                       for k, v in self.blobs.items() if k.startswith(full)), key=lambda b: b.key)

    def path_for(self, key: str) -> Path | None:
        return None


class _NoSuchKey(Exception):
    def __init__(self) -> None:
        super().__init__("NoSuchKey")
        self.response = {"Error": {"Code": "NoSuchKey"}}


class _Body:
    def __init__(self, data: bytes) -> None:
        self._io = io.BytesIO(data)

    def read(self) -> bytes:
        return self._io.read()

    def iter_chunks(self, chunk_size: int) -> Iterator[bytes]:
        while piece := self._io.read(chunk_size):
            yield piece


class _Paginator:
    def __init__(self, s3: FakeBoto3S3) -> None:
        self.s3 = s3

    def paginate(self, Bucket: str, Prefix: str):  # noqa: N803 — boto3's names
        keys = sorted(k for k in self.s3.objects if k.startswith(Prefix))
        for start in range(0, len(keys), 2):  # small pages, so paging is exercised
            yield {"Contents": [{"Key": k, "Size": len(self.s3.objects[k]), "ETag": f'"{self.s3.etags[k]}"',
                                 "LastModified": self.s3.times[k]} for k in keys[start:start + 2]]}


class FakeBoto3S3:
    """The boto3 calls `SynapseS3` makes, on a dict. Multipart parts are kept until complete."""

    class exceptions:  # noqa: N801 — boto3's shape
        NoSuchKey = _NoSuchKey

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.etags: dict[str, str] = {}
        self.times: dict[str, datetime] = {}
        self.uploads: dict[str, dict[int, bytes]] = {}
        self.calls: list[str] = []

    def _store(self, key: str, data: bytes) -> None:
        self.objects[key] = data
        self.etags[key] = hashlib.md5(data).hexdigest()  # noqa: S324 — S3's ETag is an MD5
        self.times[key] = datetime.now(timezone.utc)

    def put_object(self, Bucket, Key, Body, ContentType=None, Metadata=None):  # noqa: N803
        self.calls.append("put_object")
        self._store(Key, Body if isinstance(Body, bytes) else bytes(Body))
        return {"ETag": f'"{self.etags[Key]}"'}

    def get_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            raise _NoSuchKey()
        return {"Body": _Body(self.objects[Key])}

    def head_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            exc = _NoSuchKey()
            exc.response = {"Error": {"Code": "404"}}
            raise exc
        return {"ContentLength": len(self.objects[Key]), "ETag": f'"{self.etags[Key]}"',
                "LastModified": self.times[Key], "Metadata": {}}

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.objects.pop(Key, None)

    def get_paginator(self, name: str) -> _Paginator:
        assert name == "list_objects_v2"
        return _Paginator(self)

    def create_multipart_upload(self, Bucket, Key, ContentType=None):  # noqa: N803
        self.calls.append("create_multipart_upload")
        upload_id = f"up{len(self.uploads) + 1}"
        self.uploads[upload_id] = {}
        return {"UploadId": upload_id}

    def upload_part(self, Bucket, Key, UploadId, PartNumber, Body):  # noqa: N803
        self.calls.append("upload_part")
        self.uploads[UploadId][PartNumber] = bytes(Body)
        return {"ETag": f'"{hashlib.md5(Body).hexdigest()}"'}  # noqa: S324

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):  # noqa: N803
        self.calls.append("complete_multipart_upload")
        parts = self.uploads.pop(UploadId)
        self._store(Key, b"".join(parts[p["PartNumber"]] for p in MultipartUpload["Parts"]))

    def abort_multipart_upload(self, Bucket, Key, UploadId):  # noqa: N803
        self.calls.append("abort_multipart_upload")
        self.uploads.pop(UploadId, None)


def fake_s3_blob_store(prefix: str = "synapse"):
    """An `S3BlobStore` over real `SynapseS3` code and a `FakeBoto3S3`."""
    from core.s3_storage import SynapseS3
    from core.storage.s3 import S3BlobStore

    client = SynapseS3.__new__(SynapseS3)
    client.bucket, client.prefix, client._s3 = "bucket", prefix, FakeBoto3S3()
    return S3BlobStore(client=client)
