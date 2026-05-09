"""
API Key Management Endpoints
-----------------------------
CRUD endpoints for managing API keys from the frontend Settings page
and the CLI.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from core.api_keys import generate_api_key, list_api_keys, delete_api_key

router = APIRouter()


class CreateKeyRequest(BaseModel):
    name: str = "Unnamed Key"


@router.get("/api/settings/api-keys")
async def list_keys():
    """List all API keys (metadata only — no raw keys or hashes)."""
    return list_api_keys()


@router.post("/api/settings/api-keys")
async def create_key(body: CreateKeyRequest):
    """Generate a new API key.

    The raw key is returned in this response ONLY — it is never stored
    and cannot be retrieved again.
    """
    raw_key, record = generate_api_key(body.name)
    return {
        "key": raw_key,  # shown once
        "id": record["id"],
        "name": record["name"],
        "key_prefix": record["key_prefix"],
        "created_at": record["created_at"],
    }


@router.delete("/api/settings/api-keys/{key_id}")
async def remove_key(key_id: str):
    """Delete an API key permanently."""
    if delete_api_key(key_id):
        return {"status": "deleted", "id": key_id}
    raise HTTPException(status_code=404, detail="API key not found")
