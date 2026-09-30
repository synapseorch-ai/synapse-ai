"""
S3-backed blob storage.

Wraps the existing `SynapseS3` client rather than replacing it, so the bucket,
prefix, credential and endpoint handling (including the MinIO / R2
`endpoint_url` path) stay in one place.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

from core.storage.base import CHUNK_SIZE, BlobStat, tenant_key


class S3BlobStore:
    """Blobs in an S3 bucket, one key space per tenant."""

    def __init__(self, client=None):
        self._client = client

    @property
    def client(self):
        if self._client is None:
            from core.s3_storage import get_s3
            self._client = get_s3()
            if self._client is None:
                raise RuntimeError(
                    "S3BlobStore requires an S3 bucket to be configured "
                    "(see s3_bucket in scale config)."
                )
        return self._client

    def put(self, key: str, content: str) -> None:
        self.client.upload_text(tenant_key(key), content)

    def get(self, key: str) -> str | None:
        return self.client.download_text(tenant_key(key))

    def exists(self, key: str) -> bool:
        return self.client.get_metadata(tenant_key(key)) is not None

    def delete(self, key: str) -> None:
        self.client.delete(tenant_key(key))

    def list(self, prefix: str = "") -> list[str]:
        scoped = tenant_key(prefix)
        keys = self.client.list_keys(scoped)
        # Return keys relative to the tenant, so callers see the same shape
        # they would from LocalBlobStore and cannot accidentally reconstruct
        # another tenant's key from one of their own.
        cut = f"{tenant_key('')}"
        return [k[len(cut):] if k.startswith(cut) else k for k in keys]

    def _relative(self, key: str) -> str:
        cut = tenant_key("")
        return key[len(cut):] if key.startswith(cut) else key

    def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        self.client.upload_bytes(tenant_key(key), data, content_type)

    def get_bytes(self, key: str) -> bytes | None:
        return self.client.download_bytes(tenant_key(key))

    def iter_bytes(self, key: str, chunk_size: int = CHUNK_SIZE) -> Iterator[bytes] | None:
        return self.client.stream(tenant_key(key), chunk_size)

    def put_stream(self, key: str, chunks: Iterable[bytes], *,
                   content_type: str | None = None) -> tuple[int, str]:
        return self.client.upload_stream(tenant_key(key), chunks, content_type)

    def stat(self, key: str) -> BlobStat | None:
        head = self.client.head(tenant_key(key))
        if head is None:
            return None
        return BlobStat(key=key.lstrip("/"), size=int(head["size"] or 0), modified_at=head["modified_at"],
                        etag=head["etag"])

    def list_stats(self, prefix: str = "") -> list[BlobStat]:
        return [BlobStat(key=self._relative(o["key"]), size=int(o["size"] or 0), modified_at=o["modified_at"],
                         etag=o["etag"])
                for o in self.client.list_objects(tenant_key(prefix))]

    def path_for(self, key: str) -> Path | None:
        """No filesystem path exists for an object in S3.

        Deliberately None rather than a temp file: a caller that wants a path
        usually wants to hand a *directory* to a subprocess, and silently
        materialising one blob would give it a directory missing every other.
        """
        return None
