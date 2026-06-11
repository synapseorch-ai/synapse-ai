"""
S3 storage helper for scale mode.
Provides a thin boto3 wrapper for vault files and run logs when an S3 bucket is configured.
Returns None from get_s3() in standalone mode so callers can guard with a simple None check.
"""
import threading
from typing import Optional


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
