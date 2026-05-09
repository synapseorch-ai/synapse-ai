"""
API Key Authentication Dependency
----------------------------------
FastAPI dependency that validates Bearer tokens on /api/v1/* routes.

Usage in route handlers:
    from core.api_key_middleware import require_api_key

    @router.post("/chat")
    async def chat(key_record: dict = Depends(require_api_key)):
        # key_record contains id, name, key_prefix, etc.
        ...
"""
from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.api_keys import validate_api_key

_security = HTTPBearer(
    scheme_name="API Key",
    description="API key in Bearer format: `Authorization: Bearer sk-syn-...`",
)


async def require_api_key(
    credentials: HTTPAuthorizationCredentials = Security(_security),
) -> dict:
    """FastAPI dependency: validates Bearer token and returns the key record.

    Raises 401 if the key is missing, invalid, or revoked.
    """
    record = validate_api_key(credentials.credentials)
    if not record:
        raise HTTPException(
            status_code=401,
            detail="Invalid or revoked API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return record
