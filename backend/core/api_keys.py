"""
API Key Management
------------------
Generate, validate, list, revoke, and delete API keys.

Keys use the format: sk-syn-<32 hex chars>
Only the SHA-256 hash is persisted — the raw key is returned exactly once
at generation time and never stored.

Storage: the `api_keys` table in the engine's store, one row per key.

Validation is a lookup by hash rather than a scan. These were a JSON file read
whole on every authenticated request, so the auth path's cost grew with the
number of keys a user had ever created, and a revoked key still cost a full
read to reject.

The lookup is deliberately *not* scoped to the current tenant: a request
arrives carrying nothing but the key, so this is the call that establishes
which tenant it belongs to. The tenant comes back with the record.

An embedder that already has an API key table registers a provider and answers
all five calls itself — see ``set_api_key_provider`` below.
"""
import hashlib
import secrets
import uuid
from datetime import datetime, timezone
from typing import Optional, Protocol

from sqlalchemy import select

from core.tenancy import get_tenant

# Key prefix format
_KEY_PREFIX = "sk-syn-"
_KEY_HEX_LENGTH = 32  # 32 hex chars = 128 bits of entropy


# ---------------------------------------------------------------------------
# Embedder hook
# ---------------------------------------------------------------------------

class ApiKeyProvider(Protocol):
    """How an embedder answers the engine's API key calls.

    Every other provider hook is *given* a tenant and asked about it. This one
    is the opposite: ``validate`` runs before any tenant exists and its answer
    is what establishes one, so the record it returns must carry ``tenant_id``.
    The other four run inside a tenant and read ``core.tenancy.get_tenant()``.

    ``validate`` takes the SHA-256 hash, never the raw key. The engine has
    already checked the prefix and hashed it, and there is no reason to hand a
    live credential across an interface that does not need it.
    """

    async def validate(self, key_hash: str) -> Optional[dict]: ...
    async def generate(self, name: str) -> tuple[str, dict]: ...
    async def list(self) -> list[dict]: ...
    async def revoke(self, key_id: str) -> bool: ...
    async def delete(self, key_id: str) -> bool: ...


_provider: Optional[ApiKeyProvider] = None


def set_api_key_provider(provider: Optional[ApiKeyProvider]) -> None:
    """Install an API key provider. None restores the engine's own table.

    When one is installed it is the **only** source. There is deliberately no
    fallback to the local table if it returns nothing — the same fail-closed
    rule ``core/scale/context.py`` documents, and it matters more here: a
    fallback in the call that decides who you are is how an authorization
    failure turns into another tenant's session.

    Nor is a provider's answer cached. The engine caches its own lookup because
    it owns the row and can invalidate on revoke; an embedder owns both, and
    guessing a staleness window on someone else's revocation is not the
    engine's call to make.

    **This is not a way to obtain multi-tenancy.** A record's ``tenant_id`` is
    only usable inside ``tenant_scope()``, which stays shut until
    ``set_resource_provider()`` opens it.
    """
    global _provider
    _provider = provider


def get_api_key_provider() -> Optional[ApiKeyProvider]:
    return _provider


def _hash_key(raw_key: str) -> str:
    """SHA-256 hash of the raw API key."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value) -> Optional[str]:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def _to_record(row) -> dict:
    """The dict shape callers have always seen."""
    return {
        "id": row.id,
        "name": row.name,
        "key_hash": row.key_hash,
        "key_prefix": row.key_prefix,
        "created_at": _iso(row.created_at),
        "last_used_at": _iso(row.last_used_at),
        "is_active": bool(row.is_active),
        "tenant_id": row.tenant_id,
    }


async def generate_api_key(name: str) -> tuple[str, dict]:
    """Generate a new API key.

    Returns:
        (plaintext_key, key_record) — the plaintext key is shown ONCE.
    """
    if _provider is not None:
        return await _provider.generate(name or "Unnamed Key")

    from core.store import session
    from core.store.models import ApiKeyDB

    raw_key = f"{_KEY_PREFIX}{secrets.token_hex(_KEY_HEX_LENGTH)}"
    row = ApiKeyDB(
        id=str(uuid.uuid4()),
        tenant_id=get_tenant(),
        name=name or "Unnamed Key",
        key_hash=_hash_key(raw_key),
        key_prefix=raw_key[:12],   # "sk-syn-XXXX" for display
        is_active=True,
        created_at=_now(),
    )

    async with session() as s:
        s.add(row)

    return raw_key, _to_record(row)


async def validate_api_key(raw_key: str) -> Optional[dict]:
    """Validate a raw API key.

    Returns the key record if valid and active, None otherwise.
    Also updates last_used_at on success.
    """
    if not raw_key or not raw_key.startswith(_KEY_PREFIX):
        return None

    key_hash = _hash_key(raw_key)

    if _provider is not None:
        # Not cached: see set_api_key_provider().
        return await _provider.validate(key_hash)

    from core.store import cache

    # Cached on a deliberately short TTL — this runs on every authenticated
    # request, but a revoked key has to stop working promptly, and revocation
    # in another replica cannot invalidate this one's copy.
    return await cache.get_or_load(
        "api_keys", f"hash:{key_hash}", lambda: _validate_uncached(key_hash)
    )


async def _validate_uncached(key_hash: str) -> Optional[dict]:
    from core.store import session
    from core.store.models import ApiKeyDB

    async with session() as s:
        row = (
            await s.execute(
                select(ApiKeyDB).where(ApiKeyDB.key_hash == key_hash)
            )
        ).scalar_one_or_none()

        if row is None or not row.is_active:
            return None

        row.last_used_at = _now()
        return _to_record(row)


async def list_api_keys() -> list[dict]:
    """Key metadata for the current tenant. Never includes the hash."""
    if _provider is not None:
        return await _provider.list()

    from core.store import session
    from core.store.models import ApiKeyDB

    async with session() as s:
        rows = (
            await s.execute(
                select(ApiKeyDB)
                .where(ApiKeyDB.tenant_id == get_tenant())
                .order_by(ApiKeyDB.created_at, ApiKeyDB.id)
            )
        ).scalars().all()

    return [
        {
            "id": r.id,
            "name": r.name,
            "key_prefix": r.key_prefix,
            "created_at": _iso(r.created_at),
            "last_used_at": _iso(r.last_used_at),
            "is_active": bool(r.is_active),
        }
        for r in rows
    ]


async def revoke_api_key(key_id: str) -> bool:
    """Soft-revoke a key (set is_active=False). Returns True if found."""
    if _provider is not None:
        return await _provider.revoke(key_id)

    from core.store import session
    from core.store.models import ApiKeyDB

    async with session() as s:
        row = (
            await s.execute(
                select(ApiKeyDB).where(
                    ApiKeyDB.id == key_id,
                    ApiKeyDB.tenant_id == get_tenant(),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        row.is_active = False

    from core.store import cache
    cache.invalidate("api_keys", tenant=None)
    return True


async def delete_api_key(key_id: str) -> bool:
    """Hard-delete a key. Returns True if found and deleted."""
    if _provider is not None:
        return await _provider.delete(key_id)

    from sqlalchemy import delete as _delete

    from core.store import session
    from core.store.models import ApiKeyDB

    async with session() as s:
        result = await s.execute(
            _delete(ApiKeyDB).where(
                ApiKeyDB.id == key_id,
                ApiKeyDB.tenant_id == get_tenant(),
            )
        )

    from core.store import cache
    cache.invalidate("api_keys", tenant=None)
    return bool(result.rowcount)
