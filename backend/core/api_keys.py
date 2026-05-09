"""
API Key Management
------------------
Generate, validate, list, revoke, and delete API keys.

Keys use the format: sk-syn-<32 hex chars>
Only the SHA-256 hash is persisted — the raw key is returned exactly once
at generation time and never stored.

Storage: DATA_DIR/api_keys.json
"""
import hashlib
import json
import os
import secrets
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

from core.config import DATA_DIR

API_KEYS_FILE = os.path.join(DATA_DIR, "api_keys.json")
_lock = threading.Lock()

# Key prefix format
_KEY_PREFIX = "sk-syn-"
_KEY_HEX_LENGTH = 32  # 32 hex chars = 128 bits of entropy


def _load_keys() -> list[dict]:
    """Load all key records from disk."""
    if not os.path.exists(API_KEYS_FILE):
        return []
    try:
        with open(API_KEYS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_keys(keys: list[dict]):
    """Persist key records to disk."""
    with open(API_KEYS_FILE, "w", encoding="utf-8") as f:
        json.dump(keys, f, indent=2, ensure_ascii=False)


def _hash_key(raw_key: str) -> str:
    """SHA-256 hash of the raw API key."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_api_key(name: str) -> tuple[str, dict]:
    """Generate a new API key.

    Returns:
        (plaintext_key, key_record) — the plaintext key is shown ONCE.
    """
    hex_part = secrets.token_hex(_KEY_HEX_LENGTH)
    raw_key = f"{_KEY_PREFIX}{hex_part}"

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    record = {
        "id": str(uuid.uuid4()),
        "name": name or "Unnamed Key",
        "key_hash": _hash_key(raw_key),
        "key_prefix": raw_key[:12],  # "sk-syn-XXXX" for display
        "created_at": now,
        "last_used_at": None,
        "is_active": True,
    }

    with _lock:
        keys = _load_keys()
        keys.append(record)
        _save_keys(keys)

    return raw_key, record


def validate_api_key(raw_key: str) -> Optional[dict]:
    """Validate a raw API key.

    Returns the key record if valid and active, None otherwise.
    Also updates last_used_at on success.
    """
    if not raw_key or not raw_key.startswith(_KEY_PREFIX):
        return None

    key_hash = _hash_key(raw_key)

    with _lock:
        keys = _load_keys()
        for key in keys:
            if key["key_hash"] == key_hash and key.get("is_active", True):
                key["last_used_at"] = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                _save_keys(keys)
                return key
    return None


def list_api_keys() -> list[dict]:
    """Return all key records with metadata (no hashes)."""
    with _lock:
        keys = _load_keys()
    # Strip sensitive fields
    return [
        {
            "id": k["id"],
            "name": k["name"],
            "key_prefix": k["key_prefix"],
            "created_at": k["created_at"],
            "last_used_at": k.get("last_used_at"),
            "is_active": k.get("is_active", True),
        }
        for k in keys
    ]


def revoke_api_key(key_id: str) -> bool:
    """Soft-revoke a key (set is_active=False). Returns True if found."""
    with _lock:
        keys = _load_keys()
        for key in keys:
            if key["id"] == key_id:
                key["is_active"] = False
                _save_keys(keys)
                return True
    return False


def delete_api_key(key_id: str) -> bool:
    """Hard-delete a key. Returns True if found and deleted."""
    with _lock:
        keys = _load_keys()
        original_count = len(keys)
        keys = [k for k in keys if k["id"] != key_id]
        if len(keys) < original_count:
            _save_keys(keys)
            return True
    return False
