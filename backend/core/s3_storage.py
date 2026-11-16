"""
S3 storage helper for scale mode.
Provides a thin boto3 wrapper for vault files and run logs when an S3 bucket is configured.
Returns None from get_s3() in standalone mode so callers can guard with a simple None check.
"""
import hashlib
import threading
from typing import Iterable, Iterator, Optional

#: S3 needs every part but the last to be at least 5 MiB; 8 MiB keeps the
#: part count of a 100 MB upload low and each buffered part small.
MULTIPART_PART_SIZE = 8 * 1024 * 1024


class SynapseS3:
    def __init__(
        self,
        bucket: str,
        region: str = "us-east-1",
        prefix: str = "synapse",
        access_key_id: str = "",
        secret_access_key: str = "",
        endpoint_url: str = "",
    ):
        import boto3
        self.bucket = bucket
        self.prefix = prefix.rstrip("/")

        kwargs: dict = {"region_name": region or "us-east-1"}
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        if access_key_id and secret_access_key:
            kwargs["aws_access_key_id"] = access_key_id
            kwargs["aws_secret_access_key"] = secret_access_key

        self._s3 = boto3.client("s3", **kwargs)

    # ── Key helpers ────────────────────────────────────────────────────────────

    def full_key(self, rel: str) -> str:
        """Prepend the configured prefix to a relative key path."""
        rel = rel.lstrip("/")
        return f"{self.prefix}/{rel}" if self.prefix else rel

    # ── Core operations ────────────────────────────────────────────────────────

    def upload_text(self, rel_key: str, content: str, metadata: Optional[dict] = None) -> None:
        """Upload a UTF-8 string to S3. metadata values must be strings."""
        extra = {}
        if metadata:
            # S3 user metadata values must be strings and <= 2048 bytes per key
            extra["Metadata"] = {k: str(v)[:256] for k, v in metadata.items()}
        self._s3.put_object(
            Bucket=self.bucket,
            Key=self.full_key(rel_key),
            Body=content.encode("utf-8"),
            ContentType="text/plain; charset=utf-8",
            **extra,
        )

    def download_text(self, rel_key: str) -> Optional[str]:
        """Download a UTF-8 string from S3. Returns None if the key doesn't exist."""
        try:
            resp = self._s3.get_object(Bucket=self.bucket, Key=self.full_key(rel_key))
            return resp["Body"].read().decode("utf-8")
        except self._s3.exceptions.NoSuchKey:
            return None
        except Exception as exc:
            # ClientError with 404 NoSuchKey
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404"):
                return None
            raise

    def upload_bytes(self, rel_key: str, data: bytes, content_type: Optional[str] = None, *,
                     if_match: Optional[str] = None, if_none_match: bool = False) -> None:
        """Upload bytes as one object. With `if_match`, only over that ETag; with `if_none_match`, only where there
        is no object; `core.storage.PreconditionFailed` otherwise, nothing written (F1626: S3's conditional PUT)."""
        conditions = {**({"IfMatch": if_match} if if_match is not None else {}),
                      **({"IfNoneMatch": "*"} if if_none_match else {})}
        try:
            self._s3.put_object(
                Bucket=self.bucket,
                Key=self.full_key(rel_key),
                Body=data,
                ContentType=content_type or "application/octet-stream",
                **conditions,
            )
        except Exception as exc:
            if conditions and _precondition_failed(exc):
                from core.storage.base import PreconditionFailed

                raise PreconditionFailed(f"{rel_key} changed after it was read") from None
            raise

    def etag(self, rel_key: str) -> Optional[str]:
        """The object's ETag exactly as S3 answers it, quotes and all, or None when there is none: what a conditional
        PUT's If-Match compares (F1626). `head()` strips the quotes for its other readers; this never does."""
        try:
            resp = self._s3.head_object(Bucket=self.bucket, Key=self.full_key(rel_key))
        except Exception as exc:
            if _not_found(exc):
                return None
            raise
        return resp.get("ETag") or None

    def download_bytes(self, rel_key: str) -> Optional[bytes]:
        """Download an object's bytes. None if the key doesn't exist."""
        try:
            resp = self._s3.get_object(Bucket=self.bucket, Key=self.full_key(rel_key))
            return resp["Body"].read()
        except Exception as exc:
            if _not_found(exc):
                return None
            raise

    def stream(self, rel_key: str, chunk_size: int) -> Optional[Iterator[bytes]]:
        """An object's bytes in pieces, or None if the key doesn't exist."""
        try:
            resp = self._s3.get_object(Bucket=self.bucket, Key=self.full_key(rel_key))
        except Exception as exc:
            if _not_found(exc):
                return None
            raise
        return resp["Body"].iter_chunks(chunk_size)

    def upload_stream(self, rel_key: str, chunks: Iterable[bytes], content_type: Optional[str] = None,
                      part_size: Optional[int] = None) -> tuple[int, str]:
        """Upload `chunks` as one object; returns (size, sha256 hex).

        Under one part it is a single `put_object`. Otherwise a multipart
        upload, aborted if anything fails, so no half-object is ever visible.
        """
        key = self.full_key(rel_key)
        ctype = content_type or "application/octet-stream"
        part_size = part_size or MULTIPART_PART_SIZE
        digest = hashlib.sha256()
        size = 0
        buffer = bytearray()
        upload_id: Optional[str] = None
        parts: list[dict] = []

        def send(body: bytes) -> None:
            nonlocal upload_id
            if upload_id is None:
                upload_id = self._s3.create_multipart_upload(
                    Bucket=self.bucket, Key=key, ContentType=ctype)["UploadId"]
            number = len(parts) + 1
            resp = self._s3.upload_part(Bucket=self.bucket, Key=key, UploadId=upload_id,
                                        PartNumber=number, Body=body)
            parts.append({"ETag": resp["ETag"], "PartNumber": number})

        try:
            for piece in chunks:
                digest.update(piece)
                size += len(piece)
                buffer.extend(piece)
                while len(buffer) >= part_size:
                    send(bytes(buffer[:part_size]))
                    del buffer[:part_size]
            if upload_id is None:
                self._s3.put_object(Bucket=self.bucket, Key=key, Body=bytes(buffer), ContentType=ctype)
            else:
                if buffer:
                    send(bytes(buffer))
                self._s3.complete_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id,
                                                   MultipartUpload={"Parts": parts})
        except BaseException:
            if upload_id is not None:
                try:
                    self._s3.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)
                except Exception:
                    pass
            raise
        return size, digest.hexdigest()

    def head(self, rel_key: str) -> Optional[dict]:
        """Size, modification time and ETag of a key. None if it doesn't exist."""
        try:
            resp = self._s3.head_object(Bucket=self.bucket, Key=self.full_key(rel_key))
        except Exception as exc:
            if _not_found(exc):
                return None
            raise
        return {"size": resp.get("ContentLength", 0), "modified_at": resp.get("LastModified"),
                "etag": (resp.get("ETag") or "").strip('"') or None}

    def list_objects(self, rel_prefix: str) -> list[dict]:
        """`list_keys` with each object's size, modification time and ETag."""
        full_prefix = self.full_key(rel_prefix)
        out: list[dict] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
            for obj in page.get("Contents", []):
                rel = obj["Key"]
                if self.prefix and rel.startswith(self.prefix + "/"):
                    rel = rel[len(self.prefix) + 1:]
                out.append({"key": rel, "size": obj.get("Size", 0), "modified_at": obj.get("LastModified"),
                            "etag": (obj.get("ETag") or "").strip('"') or None})
        return out

    def get_metadata(self, rel_key: str) -> Optional[dict]:
        """Return S3 user metadata for a key. None if key not found."""
        try:
            resp = self._s3.head_object(Bucket=self.bucket, Key=self.full_key(rel_key))
            return resp.get("Metadata", {})
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404", "403"):
                return None
            raise

    def list_keys(self, rel_prefix: str) -> list[str]:
        """List all keys under rel_prefix (relative to the bucket prefix). Returns relative keys."""
        full_prefix = self.full_key(rel_prefix)
        keys: list[str] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
            for obj in page.get("Contents", []):
                # Strip the top-level prefix back off so callers get clean relative paths
                rel = obj["Key"]
                if self.prefix and rel.startswith(self.prefix + "/"):
                    rel = rel[len(self.prefix) + 1:]
                keys.append(rel)
        return keys

    def delete(self, rel_key: str) -> None:
        self._s3.delete_object(Bucket=self.bucket, Key=self.full_key(rel_key))

    def test_connection(self) -> dict:
        """Attempt a low-cost S3 operation to validate credentials and bucket access."""
        try:
            self._s3.list_objects_v2(Bucket=self.bucket, MaxKeys=1)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def _precondition_failed(exc: Exception) -> bool:
    """A conditional PUT S3 refused: its version was not the one given (412), or another conditional write on the
    same key was in flight (409)."""
    response = getattr(exc, "response", None) or {}
    code = str((response.get("Error") or {}).get("Code") or "")
    status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return code in ("PreconditionFailed", "ConditionalRequestConflict") or status in (409, 412)


def _not_found(exc: Exception) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    return code in ("NoSuchKey", "404", "NotFound")


# ── Module-level singleton ─────────────────────────────────────────────────────

_lock = threading.Lock()
_instance: Optional[SynapseS3] = None
_last_bucket: str = ""


def get_s3() -> Optional[SynapseS3]:
    """
    Return the shared SynapseS3 instance, or None when S3 is not configured.
    Re-initialises automatically if the bucket setting changes.
    """
    global _instance, _last_bucket
    try:
        from core.scale.config import get_scale_config
        cfg = get_scale_config()
    except Exception:
        return None

    if not cfg.s3_bucket:
        return None

    with _lock:
        if _instance is None or cfg.s3_bucket != _last_bucket:
            _instance = SynapseS3(
                bucket=cfg.s3_bucket,
                region=cfg.s3_region,
                prefix=cfg.s3_prefix,
                access_key_id=cfg.s3_access_key_id,
                secret_access_key=cfg.s3_secret_access_key,
                endpoint_url=cfg.s3_endpoint_url,
            )
            _last_bucket = cfg.s3_bucket

    return _instance


def invalidate_s3_singleton() -> None:
    """Force the singleton to be rebuilt on next get_s3() call (e.g. after config save)."""
    global _instance, _last_bucket
    with _lock:
        _instance = None
        _last_bucket = ""
